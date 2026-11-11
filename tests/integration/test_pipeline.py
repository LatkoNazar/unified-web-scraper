import json
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from unified_web_parser.core.config import JobConfig, LoadedJob
from unified_web_parser.crawling.errors import FetchError
from unified_web_parser.crawling.pages import make_page
from unified_web_parser.pipeline.runner import PipelineRunner
from unified_web_parser.storage import DatasetStore


def make_job(tmp_path: Path, *, sources=None, outputs=None, max_records=10):
    return LoadedJob(
        config=JobConfig.model_validate(
            {
                "name": "vacancy-test",
                "sources": sources or [{"url": "https://example.org/jobs"}],
                "schema_path": "schema.json",
                "outputs": outputs or ["jsonl", "parquet"],
                "crawl": {
                    "backend": "http",
                    "max_pages": 10,
                    "max_records": max_records,
                    "concurrency": 1,
                    "delay": 0,
                },
                "ai": {"enabled": False},
                "assessment": ["is_relevant"],
            }
        ),
        config_path=tmp_path / "job.yaml",
        schema_path=tmp_path / "schema.json",
        extraction_schema={
            "type": "object",
            "required": ["title", "employer"],
            "additionalProperties": False,
            "properties": {
                "title": {"type": "string"},
                "employer": {"type": "string"},
                "salary": {"type": ["number", "null"]},
            },
        },
    )


class FakeFetcher:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    async def fetch(self, url):
        self.calls.append(url)
        page = self.pages[url]
        if isinstance(page, Exception):
            raise page
        return make_page(url, page, 200)


def vacancy(title, employer, salary):
    return (
        '<script type="application/ld+json">'
        + json.dumps(
            {
                "@type": "JobPosting",
                "title": title,
                "employer": employer,
                "salary": salary,
            }
        )
        + "</script>"
    )


@pytest.mark.asyncio
async def test_full_generic_pipeline_replay_and_idempotent_history(tmp_path):
    fetcher = FakeFetcher(
        {
            "https://example.org/jobs": (
                '<a href="/jobs/python">Python</a><a href="/jobs/data">Data</a>'
            ),
            "https://example.org/jobs/python": vacancy("Python engineer", "Acme", 5000),
            "https://example.org/jobs/data": vacancy("Data engineer", "Other", 4000),
        }
    )
    job = make_job(tmp_path)
    data_dir = tmp_path / "data"
    report = await PipelineRunner(job, data_dir=data_dir, fetcher=fetcher).run()
    assert report.status == "success", report.errors
    assert report.pages_fetched == 3
    assert report.records_written == 2
    parquet = pq.read_table(report.artifacts["parquet"])
    assert parquet.num_rows == 2
    assert parquet.column("salary").to_pylist() == [5000, 4000]
    store = DatasetStore(data_dir)
    before = store.list_records(job_name="vacancy-test")
    assert {r["data"]["employer"] for r in before} == {"Acme", "Other"}
    assert all(Path(r["provenance"]["snapshot_ref"]).is_file() for r in before)
    replay = await PipelineRunner(job, data_dir=data_dir).run(
        replay_dir=Path(report.artifacts["directory"]),
    )
    assert replay.status == "success", replay.errors
    assert replay.pages_fetched == 2  # Discovery homepage is never replayed as a record.
    after = store.list_records(job_name="vacancy-test")
    assert {r["record_id"] for r in before} == {r["record_id"] for r in after}
    assert len(after) == 2
    assert all(len(store.list_history(r["record_id"])) == 1 for r in after)


@pytest.mark.asyncio
async def test_source_failure_is_partial_and_records_retained(tmp_path):
    job = make_job(
        tmp_path,
        sources=[
            {"url": "https://example.org/jobs/python"},
            {"url": "https://blocked.example/job"},
        ],
    )
    fetcher = FakeFetcher(
        {
            "https://example.org/jobs/python": vacancy("Developer", "Acme", 5000),
            "https://blocked.example/job": FetchError(
                "https://blocked.example/job", "HTTP 403", status_code=403
            ),
        }
    )
    report = await PipelineRunner(job, data_dir=tmp_path / "data", fetcher=fetcher).run()
    assert report.status == "partial"
    assert report.records_written == 1
    assert report.errors[0]["status_code"] == 403
    summary = json.loads(Path(report.artifacts["summary"]).read_text(encoding="utf-8"))
    assert summary["status"] == "partial"


@pytest.mark.asyncio
async def test_missing_postgres_configuration_has_terminal_summary(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    job = make_job(tmp_path, outputs=["postgres"])
    report = await PipelineRunner(job, data_dir=tmp_path / "data").run()
    assert report.status == "failed"
    assert "DATABASE_URL" in report.errors[0]["message"]
    assert Path(report.artifacts["summary"]).exists()
    assert DatasetStore(tmp_path / "data").list_runs()[0]["summary"]["status"] == "failed"


@pytest.mark.asyncio
async def test_all_fetches_fail_does_not_claim_empty_success(tmp_path):
    job = make_job(tmp_path)
    fetcher = FakeFetcher({"https://example.org/jobs": RuntimeError("network unavailable")})
    report = await PipelineRunner(job, data_dir=tmp_path / "data", fetcher=fetcher).run()
    assert report.status == "failed"
    assert report.records_written == 0
    assert report.errors[0]["stage"] == "fetch"


@pytest.mark.asyncio
async def test_bad_sitemap_does_not_discard_another_source(tmp_path):
    job = make_job(
        tmp_path,
        sources=[
            {"url": "https://example.org/bad.xml", "kind": "sitemap"},
            {"url": "https://example.org/jobs/python"},
        ],
    )
    fetcher = FakeFetcher(
        {
            "https://example.org/bad.xml": "<malformed",
            "https://example.org/jobs/python": vacancy("Developer", "Acme", 5000),
        }
    )
    report = await PipelineRunner(job, data_dir=tmp_path / "data", fetcher=fetcher).run()
    assert report.status == "partial"
    assert report.records_written == 1
    assert report.errors[0]["stage"] == "discovery"

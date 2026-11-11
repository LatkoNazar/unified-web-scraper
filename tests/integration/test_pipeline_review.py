"""Independent regression coverage for bugs found during orchestration review."""

import asyncio
import json
from pathlib import Path

import pytest

from unified_web_parser.core.config import JobConfig, LoadedJob
from unified_web_parser.core.models import CandidateBlock, Provenance, Record
from unified_web_parser.crawling.pages import make_page
from unified_web_parser.pipeline.runner import PipelineRunner
from unified_web_parser.processing.validation import validate_records
from unified_web_parser.storage import DatasetStore


def job(tmp_path, *, schema=None, **overrides):
    config = {
        "name": "review-job",
        "sources": [{"url": "https://example.org/catalog"}],
        "schema_path": "schema.json",
        "ai": {"enabled": False},
        "crawl": {"concurrency": 1, "delay": 0, "max_pages": 10, "max_records": 1},
        **overrides,
    }
    return LoadedJob(
        config=JobConfig.model_validate(config),
        config_path=tmp_path / "job.yaml",
        schema_path=tmp_path / "schema.json",
        extraction_schema=schema
        or {
            "type": "object",
            "required": ["title"],
            "properties": {"title": {"type": "string"}},
        },
    )


class Fetcher:
    def __init__(self, pages):
        self.pages = pages
        self.urls = []

    async def fetch(self, url):
        self.urls.append(url)
        return make_page(url, self.pages[url], 200)


def dataset(report):
    path = Path(report.artifacts["jsonl"])
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_search_form_homepage_is_discovery_not_a_result(tmp_path):
    source = "https://example.org/catalog"
    search = "https://example.org/search?q=laptop"
    detail = "https://example.org/products/laptop"
    fetcher = Fetcher(
        {
            source: '<h1>Welcome to the shop</h1><form action="/search"><input name="q"></form>',
            search: '<h1>Search results</h1><a href="/products/laptop">Laptop</a>',
            detail: '<script type="application/ld+json">'
            '{"@type":"Product","name":"Actual laptop"}</script>',
        }
    )
    report = asyncio.run(
        PipelineRunner(
            job(tmp_path, search="laptop"), data_dir=tmp_path / "data", fetcher=fetcher
        ).run()
    )
    assert report.status == "success", report.errors
    assert fetcher.urls == [source, search, detail]
    assert dataset(report)[0]["data"]["title"] == "Actual laptop"


def test_replay_skips_discovery_pages_and_keeps_history_idempotent(tmp_path):
    source = "https://example.org/catalog"
    detail = "https://example.org/products/laptop"
    fetcher = Fetcher(
        {
            source: '<script type="application/ld+json">'
            '{"@type":"Product","name":"Category summary"}</script>'
            '<a href="/products/laptop">Actual laptop</a>',
            detail: '<script type="application/ld+json">'
            '{"@type":"Product","name":"Actual laptop"}</script>',
        }
    )
    loaded = job(tmp_path)
    data_dir = tmp_path / "data"
    first = asyncio.run(PipelineRunner(loaded, data_dir=data_dir, fetcher=fetcher).run())
    replay = asyncio.run(
        PipelineRunner(loaded, data_dir=data_dir).run(replay_dir=Path(first.artifacts["directory"]))
    )
    assert first.status == replay.status == "success"
    first_records, replay_records = dataset(first), dataset(replay)
    assert [item["data"] for item in replay_records] == [item["data"] for item in first_records]
    store = DatasetStore(data_dir)
    assert len(store.list_records()) == 1
    assert len(store.list_history(first_records[0]["record_id"])) == 1
    assert replay.pages_fetched == 1


@pytest.mark.parametrize("failure", ["missing_url", "connection_refused"])
def test_postgres_setup_failure_has_terminal_local_run_and_summary(tmp_path, monkeypatch, failure):
    if failure == "missing_url":
        monkeypatch.delenv("DATABASE_URL", raising=False)
    else:
        monkeypatch.setenv("DATABASE_URL", "postgresql://example.invalid/parser")

        def failing_postgres(data_dir, database_url=None):
            if database_url:
                raise RuntimeError("connection refused")
            return DatasetStore(data_dir)

        monkeypatch.setattr("unified_web_parser.pipeline.runner.DatasetStore", failing_postgres)
    report = asyncio.run(
        PipelineRunner(job(tmp_path, outputs=["postgres"]), data_dir=tmp_path / "data").run()
    )
    assert report.status == "failed"
    assert report.errors
    summary = json.loads(Path(report.artifacts["summary"]).read_text(encoding="utf-8"))
    assert summary["status"] == "failed"
    stored = DatasetStore(tmp_path / "data").list_runs()[0]
    assert stored["finished_at"] is not None
    assert stored["summary"]["status"] == "failed"


class TwoCandidates:
    def __init__(self, **_kwargs):
        return None

    def build(self, _page):
        return [
            CandidateBlock(candidate_id="one", kind="dom", content="first evidence"),
            CandidateBlock(candidate_id="two", kind="dom", content="second evidence"),
        ]


def record(page, *, data, record_id="same", candidate_id="one"):
    return Record(
        record_id=record_id,
        job_name="review-job",
        data=data,
        provenance=Provenance(
            page_id=page.page_id,
            source_url=page.url,
            candidate_ids=(candidate_id,),
            extractor="review-fixture",
        ),
    )


def test_merged_candidates_are_validated_before_persistence(tmp_path, monkeypatch):
    class MutuallyExclusiveExtractor:
        def __init__(self, **_kwargs):
            self.llm = None

        async def extract(self, page, candidate, _strategy, _schema):
            data = {"a": 1} if candidate.candidate_id == "one" else {"b": 2}
            return [record(page, data=data, candidate_id=candidate.candidate_id)]

    monkeypatch.setattr("unified_web_parser.pipeline.pages.CandidateBuilder", TwoCandidates)
    monkeypatch.setattr(
        "unified_web_parser.pipeline.pages.SchemaExtractor", MutuallyExclusiveExtractor
    )
    schema = {
        "type": "object",
        "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
        "oneOf": [
            {"required": ["a"], "not": {"required": ["b"]}},
            {"required": ["b"], "not": {"required": ["a"]}},
        ],
    }
    loaded = job(
        tmp_path,
        schema=schema,
        crawl={"max_records": 2, "max_pages": 1, "max_depth": 0, "delay": 0},
    )
    report = asyncio.run(
        PipelineRunner(
            loaded,
            data_dir=tmp_path / "data",
            fetcher=Fetcher(
                {
                    "https://example.org/catalog": (
                        '<script type="application/ld+json">'
                        '{"@type":"Product","name":"Fixture"}</script>'
                    )
                }
            ),
        ).run()
    )
    assert report.status == "success", report.errors
    stored = DatasetStore(tmp_path / "data").list_records()
    assert stored[0]["data"] == {"a": 1}
    valid, errors = validate_records(
        [record(make_page("https://example.org/catalog", "fixture", 200), data=stored[0]["data"])],
        schema,
    )
    assert len(valid) == 1
    assert errors == []
    assert any(warning["stage"] == "merge" for warning in report.warnings)


def test_record_limit_applies_before_assessment_batch(tmp_path, monkeypatch):
    class MultiRecordExtractor:
        def __init__(self, **_kwargs):
            self.llm = None

        async def extract(self, page, candidate, _strategy, _schema):
            return [
                record(page, data={"title": f"Item {index}"}, record_id=f"item-{index}")
                for index in range(3)
            ]

    monkeypatch.setattr("unified_web_parser.pipeline.pages.CandidateBuilder", TwoCandidates)
    monkeypatch.setattr("unified_web_parser.pipeline.pages.SchemaExtractor", MultiRecordExtractor)
    loaded = job(tmp_path, assessment=["is_relevant"])
    report = asyncio.run(
        PipelineRunner(
            loaded,
            data_dir=tmp_path / "data",
            fetcher=Fetcher(
                {
                    "https://example.org/catalog": (
                        '<script type="application/ld+json">'
                        '{"@type":"Product","name":"Fixture"}</script>'
                    )
                }
            ),
        ).run()
    )
    assert report.status == "success", report.errors
    assert report.records_written == 1
    stored = DatasetStore(tmp_path / "data").list_records()
    assert len(stored) == 1
    assert len(stored[0]["assessments"]) == 1

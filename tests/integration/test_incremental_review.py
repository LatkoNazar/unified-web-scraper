"""Independent cross-run checks against a changing, ordinary HTTP source."""

import json
import sqlite3
import threading
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from unified_web_parser.core.config import JobConfig, LoadedJob
from unified_web_parser.pipeline.incremental import load_update_job
from unified_web_parser.pipeline.runner import PipelineRunner
from unified_web_parser.storage import DatasetStore


@pytest.fixture
def changing_source():
    state = {"items": ["a", "b"], "requests": [], "fail": set()}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state["requests"].append(self.path)
            if self.path in state["fail"]:
                self.send_error(503)
                return
            if self.path.startswith("/catalog"):
                second = "page=2" in self.path
                item = state["items"][1 if second else 0]
                body = f'<article><h2><a href="/products/{item}">Entry {item}</a></h2></article>'
                if not second:
                    body += '<a rel="next" href="/catalog?page=2">Next</a>'
            else:
                item = self.path.rsplit("/", 1)[-1]
                entity = {
                    "@type": "Article",
                    "headline": "Entry " + item,
                    "url": state["origin"] + self.path,
                    "description": "Observed summary " + item,
                }
                body = '<script type="application/ld+json">' + json.dumps(entity) + "</script>"
                body += f"<main><h1>Entry {item}</h1><p>Observed summary {item}</p></main>"
            content = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    state["origin"] = f"http://127.0.0.1:{server.server_port}"
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


class EvidenceClient:
    model = "local-review-stub"

    def __init__(self):
        self.urls = []
        self.fail = set()

    async def complete_json(self, _system, payload, _schema, **_kwargs):
        url = payload["source_url"]
        self.urls.append(url)
        if url in self.fail:
            raise RuntimeError("Local extraction fixture failure")
        item = url.rsplit("/", 1)[-1]
        return {
            "records": [
                {"title": "Entry " + item, "url": url, "summary": "Observed summary " + item}
            ]
        }


def review_job(tmp_path, source):
    return LoadedJob(
        config=JobConfig.model_validate(
            {
                "name": "independent-incremental-articles",
                "schema_path": "schema.json",
                "sources": [{"url": source["origin"] + "/catalog"}],
                "incremental": {"enabled": True},
                "navigation": {"mode": "rules"},
                "ai": {"enabled": True, "routing": "rules", "max_calls": 10},
                "crawl": {
                    "max_pages": 10,
                    "max_records": 10,
                    "max_depth": 1,
                    "concurrency": 1,
                    "delay": 0,
                    "retries": 0,
                },
            }
        ),
        config_path=tmp_path / "job.yaml",
        extraction_schema={
            "x-entity-types": ["Article"],
            "type": "object",
            "additionalProperties": False,
            "required": ["title", "url", "summary"],
            "properties": {
                "title": {"type": "string"},
                "url": {"type": "string"},
                "summary": {"type": ["string", "null"]},
            },
        },
    )


async def test_changing_http_catalog_and_pagination_skip_old_fetch_and_extraction(
    tmp_path, changing_source
):
    data = tmp_path / "data"
    client = EvidenceClient()
    job = review_job(tmp_path, changing_source)
    first = await PipelineRunner(job, data_dir=data, llm=client).run()
    assert first.status == "success" and first.records_written == 2, first.errors
    original = (data / "runs" / first.run_id / "records.jsonl").read_bytes()
    assert len(client.urls) == 2

    changing_source.update(items=["b", "c"], requests=[])
    client.urls.clear()
    second = await PipelineRunner(
        load_update_job(data / "runs" / first.run_id), data_dir=data, llm=client
    ).run()

    assert second.status == "success" and second.records_written == 1, second.errors
    assert changing_source["requests"] == ["/catalog", "/catalog?page=2", "/products/c"]
    assert client.urls == [changing_source["origin"] + "/products/c"]
    assert second.incremental["parent_run_id"] == first.run_id
    assert (data / "runs" / first.run_id / "records.jsonl").read_bytes() == original
    rows = DatasetStore(data).list_records(job_name=job.config.name)
    assert len(rows) == 3 and {row["data"]["title"] for row in rows} == {
        "Entry a",
        "Entry b",
        "Entry c",
    }


@pytest.mark.parametrize("failure", ["http", "extraction"])
async def test_failed_detail_remains_retryable_even_if_fallback_saved_a_record(
    tmp_path, changing_source, failure
):
    data = tmp_path / "data"
    client = EvidenceClient()
    job = review_job(tmp_path, changing_source)
    if failure == "http":
        changing_source["fail"].add("/products/b")
    else:
        client.fail.add(changing_source["origin"] + "/products/b")
    first = await PipelineRunner(job, data_dir=data, llm=client).run()
    assert first.errors

    changing_source.update(requests=[], fail=set())
    client.urls.clear()
    client.fail.clear()
    second = await PipelineRunner(
        load_update_job(data / "runs" / first.run_id), data_dir=data, llm=client
    ).run()
    assert second.status == "success" and second.records_written == 1, second.errors
    assert changing_source["requests"] == ["/catalog", "/catalog?page=2", "/products/b"]
    assert client.urls == [changing_source["origin"] + "/products/b"]
    rows = DatasetStore(data).list_records(job_name=job.config.name)
    assert len(rows) == 2 and all(row["data"]["summary"] for row in rows)


@pytest.mark.parametrize("changed", ["query", "schema", "job"])
async def test_other_processing_context_does_not_reuse_prior_completion(
    tmp_path, changing_source, changed
):
    data = tmp_path / "data"
    client = EvidenceClient()
    job = review_job(tmp_path, changing_source)
    first = await PipelineRunner(job, data_dir=data, llm=client).run()
    assert first.records_written == 2
    newer = deepcopy(job)
    if changed == "query":
        newer = newer.model_copy(
            update={"config": newer.config.model_copy(update={"search": "Entry"})}
        )
    elif changed == "job":
        newer = newer.model_copy(
            update={"config": newer.config.model_copy(update={"name": "independent-other-job"})}
        )
    else:
        newer.extraction_schema["properties"]["summary"]["description"] = "New processing contract"
    changing_source["requests"].clear()
    client.urls.clear()
    second = await PipelineRunner(newer, data_dir=data, llm=client).run()
    assert second.status == "success" and second.records_written == 2, second.errors
    assert changing_source["requests"] == [
        "/catalog",
        "/catalog?page=2",
        "/products/a",
        "/products/b",
    ]
    assert len(client.urls) == 2


async def test_routing_failure_fallback_does_not_poison_cross_run_completion(
    tmp_path, changing_source
):
    class FailedRouter:
        async def complete_json(self, *_args, **_kwargs):
            raise RuntimeError("Local routing fixture failure")

    data = tmp_path / "data"
    job = review_job(tmp_path, changing_source)
    job = job.model_copy(
        update={
            "config": job.config.model_copy(
                update={"ai": job.config.ai.model_copy(update={"routing": "ai"})}
            )
        }
    )
    client = EvidenceClient()
    first = await PipelineRunner(
        job, data_dir=data, llm=client, decision_client=FailedRouter()
    ).run()
    assert first.records_written == 2 and any(
        row.get("fallback") == "rules" for row in first.warnings
    )
    changing_source["requests"].clear()
    client.urls.clear()
    second = await PipelineRunner(
        load_update_job(data / "runs" / first.run_id),
        data_dir=data,
        llm=client,
        decision_client=FailedRouter(),
    ).run()
    assert second.records_written == 2
    assert changing_source["requests"] == [
        "/catalog",
        "/catalog?page=2",
        "/products/a",
        "/products/b",
    ]
    assert len(client.urls) == 2


async def test_later_quarantine_invalidates_existing_detail_completion(tmp_path, changing_source):
    data = tmp_path / "data"
    client = EvidenceClient()
    job = review_job(tmp_path, changing_source)
    first = await PipelineRunner(job, data_dir=data, llm=client).run()
    assert first.records_written == 2
    rows = DatasetStore(data).list_records(job_name=job.config.name)
    identifier = next(row["record_id"] for row in rows if row["data"]["title"] == "Entry b")
    with sqlite3.connect(data / "dataset.sqlite") as connection:
        connection.execute(
            "UPDATE parser_records SET excluded_at=? WHERE job_name=? AND record_id=?",
            ("2026-10-24T01:00:00Z", job.config.name, identifier),
        )
    changing_source["requests"].clear()
    client.urls.clear()
    second = await PipelineRunner(
        load_update_job(data / "runs" / first.run_id), data_dir=data, llm=client
    ).run()
    assert second.status == "success" and second.records_written == 1, second.errors
    assert changing_source["requests"] == ["/catalog", "/catalog?page=2", "/products/b"]
    assert client.urls == [changing_source["origin"] + "/products/b"]
    assert len(DatasetStore(data).list_records(job_name=job.config.name)) == 2

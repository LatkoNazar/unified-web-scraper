"""Live snapshots observe durable validated work before the run finishes."""

import asyncio
import json
from datetime import UTC, datetime

import httpx
import pytest

from unified_web_parser.crawling.pages import make_page
from unified_web_parser.interfaces.api import create_app
from unified_web_parser.interfaces.live import (
    LiveRunManager,
    RunCapacityError,
    RunRequest,
    requested_job,
)
from unified_web_parser.pipeline.report import RunReport
from unified_web_parser.pipeline.runner import PipelineRunner
from unified_web_parser.storage import DatasetStore


def profiles(tmp_path):
    root = tmp_path / "jobs"
    root.mkdir()
    (root / "schema.json").write_text(
        json.dumps(
            {
                "type": "object",
                "x-entity-types": ["Article"],
                "required": ["title", "url"],
                "properties": {"title": {"type": "string"}, "url": {"type": "string"}},
            }
        ),
        encoding="utf-8",
    )
    (root / "web.yaml").write_text(
        "name: live-articles\nschema_path: schema.json\nsearch: articles\n"
        "discovery: {enabled: true}\noutputs: [jsonl]\n"
        "crawl: {concurrency: 1, delay: 0, max_pages: 5, max_records: 10}\n"
        "navigation: {mode: rules}\n",
        encoding="utf-8",
    )
    return root


def article(url, title):
    return make_page(
        url,
        '<script type="application/ld+json">'
        + json.dumps(
            {
                "@type": "Article",
                "headline": title,
                "url": url,
                "description": "Source observations and complete details of the article.",
            }
        )
        + "</script>",
        200,
    )


class TwoPages:
    def __init__(self):
        self.second_started = asyncio.Event()
        self.release = asyncio.Event()
        self.cleanup_started = asyncio.Event()
        self.cleanup_release = asyncio.Event()
        self.block_cleanup = False

    async def fetch(self, url):
        if "two" in url:
            self.second_started.set()
            try:
                await self.release.wait()
            finally:
                self.cleanup_started.set()
                if self.block_cleanup:
                    await self.cleanup_release.wait()
        return article(url, "Article two" if "two" in url else "Article one")


def factory(fetcher):
    def make(job, **kwargs):
        return PipelineRunner(job, fetcher=fetcher, **kwargs)

    return make


async def test_live_records_are_durable_before_finish_and_sse_restores_final_snapshot(
    tmp_path,
):
    pages = TwoPages()
    data_dir = tmp_path / "data"
    app = create_app(data_dir, profiles(tmp_path), runner_factory=factory(pages))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        started = await client.post(
            "/runs",
            json={
                "query": "Article",
                "ai": False,
                "sources": ["https://science.example/one", "https://science.example/two"],
            },
        )
        assert started.status_code == 202
        run_id = started.json()["run_id"]
        assert started.headers["location"] == f"/runs/{run_id}"
        await asyncio.wait_for(pages.second_started.wait(), 3)
        live = (await client.get(f"/runs/{run_id}")).json()
        assert live["status"] == "running" and live["report"] is None
        assert live["counters"] == {
            "pages_attempted": 2,
            "pages_fetched": 1,
            "records_found": 1,
            "invalid_records": 0,
        }
        assert live["records"][0]["data"]["title"] == "Article one"
        assert live["persisted"] and live["records_persisted"] == 1
        assert DatasetStore(data_dir).list_records()[0]["data"]["title"] == "Article one"
        assert (
            await client.post(
                "/runs",
                json={
                    "query": "Another",
                    "ai": False,
                    "sources": ["https://science.example/three"],
                },
            )
        ).status_code == 429
        assert (await client.get("/runs")).json()[0]["run_id"] == run_id
        pages.release.set()
        await app.state.run_manager.wait(run_id)
        final = (await client.get(f"/runs/{run_id}")).json()
        assert final["status"] == "success", final["errors"]
        assert final["persisted"] and final["records_persisted"] == 2
        assert final["counters"]["records_found"] == 2
        assert len(DatasetStore(data_dir).list_records()) == 2
        sse = await client.get(
            f"/runs/{run_id}/events", headers={"Last-Event-ID": str(final["revision"])}
        )
        assert sse.headers["content-type"].startswith("text/event-stream")
        assert f"id: {final['revision']}" in sse.text and "event: snapshot" in sse.text
        assert json.loads(sse.text.split("data: ", 1)[1]) == final
    restored = LiveRunManager(data_dir).snapshot(run_id)
    assert restored["records"] == final["records"] and restored["persisted"]


async def test_double_cancel_and_shutdown_wait_for_cleanup_without_recancelling(tmp_path):
    pages = TwoPages()
    pages.block_cleanup = True
    job = requested_job(
        RunRequest(
            query="Article",
            ai=False,
            sources=["https://science.example/one", "https://science.example/two"],
        ),
        profiles(tmp_path),
    )
    manager = LiveRunManager(tmp_path / "data", runner_factory=factory(pages))
    run_id = manager.submit(job, "Article")["run_id"]
    await asyncio.wait_for(pages.second_started.wait(), 3)
    assert manager.cancel(run_id)["status"] == "cancelling"
    await asyncio.wait_for(pages.cleanup_started.wait(), 3)
    assert manager.cancel(run_id)["status"] == "cancelling"
    closing = asyncio.create_task(manager.close())
    await asyncio.sleep(0)
    assert not closing.done()
    assert manager.snapshot(run_id)["status"] == "cancelling"
    pages.cleanup_release.set()
    await asyncio.wait_for(closing, 3)
    final = manager.snapshot(run_id)
    assert final["status"] == "cancelled" and not manager.tasks
    assert final["counters"]["records_found"] == 1
    assert final["report"]["stop_reason"] == "user_cancelled"
    assert final["report"]["errors"] == []
    assert final["records_persisted"] == 1
    assert LiveRunManager(tmp_path / "data").snapshot(run_id)["records"] == final["records"]


async def test_prestart_cancel_and_factory_failure_settle_terminal_state(tmp_path):
    job = requested_job(
        RunRequest(query="Article", ai=False, sources=["https://science.example/one"]),
        profiles(tmp_path),
    )

    def fail(*args, **kwargs):
        raise RuntimeError("Factory failed before report creation")

    manager = LiveRunManager(tmp_path / "data", runner_factory=fail)
    cancelled = manager.submit(job, "Article")["run_id"]
    manager.cancel(cancelled)
    await manager.close()
    assert manager.snapshot(cancelled)["status"] == "cancelled"
    other = LiveRunManager(tmp_path / "other", runner_factory=fail)
    failed = other.submit(job, "Article")["run_id"]
    await other.wait(failed)
    assert other.snapshot(failed)["status"] == "failed"
    assert "before report" in other.snapshot(failed)["errors"][0]["message"]


async def test_sse_disconnect_does_not_cancel_and_heartbeat_has_no_fake_progress(tmp_path):
    pages = TwoPages()
    job = requested_job(
        RunRequest(query="Article", ai=False, sources=["https://science.example/two"]),
        profiles(tmp_path),
    )
    manager = LiveRunManager(tmp_path / "data", runner_factory=factory(pages))
    run_id = manager.submit(job, "Article")["run_id"]
    await pages.second_started.wait()
    events = manager.events(run_id, heartbeat=0.001)
    first = await anext(events)
    assert "event: snapshot" in first and '"pages_attempted": 1' in first
    assert await anext(events) == ": heartbeat\n\n"
    await events.aclose()
    assert not manager.tasks[run_id].cancelled()
    pages.release.set()
    await manager.wait(run_id)
    assert manager.snapshot(run_id)["status"] == "success"


async def test_retention_logs_and_capacity_are_bounded(tmp_path):
    job = requested_job(
        RunRequest(query="Article", ai=False, sources=["https://science.example/one"]),
        profiles(tmp_path),
    )

    class FastRunner:
        def __init__(self, job, *, on_event, **kwargs):
            self.event = on_event

        async def run(self, *, run_id):
            for index in range(30):
                self.event("log", {"message": str(index)})
            return RunReport(
                run_id=run_id,
                job_name=job.config.name,
                started_at=datetime.now(UTC),
                status="success",
            )

    manager = LiveRunManager(
        tmp_path / "data", max_retained=2, max_logs=3, runner_factory=FastRunner
    )
    for _ in range(4):
        run_id = manager.submit(job, "Article")["run_id"]
        with pytest.raises(RunCapacityError):
            manager.submit(job, "Article")
        await manager.wait(run_id)
        assert [item["message"] for item in manager.snapshot(run_id)["logs"]] == ["27", "28", "29"]
    assert len(manager.states) == 2


@pytest.mark.parametrize(
    "extra",
    [
        {"query": ""},
        {"query": "x" * 4001},
        {"ai": "true"},
        {"sources": ["ftp://example.org/file"]},
        {"profile": "../secret"},
        {"max_calls": True},
        {"max_pages": 501},
        {"api_key": "not-accepted"},
    ],
)
async def test_invalid_run_requests_are_rejected_before_execution(tmp_path, extra):
    app = create_app(tmp_path / "data", profiles(tmp_path))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post("/runs", json={"query": "Article", "ai": False, **extra})
    assert response.status_code == 422
    assert not app.state.run_manager.tasks


def test_requested_job_defaults_match_cli_without_implicit_configured_sources(tmp_path):
    root = profiles(tmp_path)
    auto = requested_job(RunRequest(query="Astrophysics"), root)
    assert auto.config.schema_generation.enabled and auto.config.discovery.enabled
    assert auto.config.sources == [] and auto.extraction_schema == {}
    fixed = requested_job(
        RunRequest(query="Astrophysics", ai=False, sources=["https://science.example/"]), root
    )
    assert not fixed.config.schema_generation.enabled and not fixed.config.discovery.enabled
    assert fixed.config.content_profile == "generic"


async def test_observer_failure_cannot_break_real_pipeline_or_mutate_record(tmp_path):
    job = requested_job(
        RunRequest(query="Article", ai=False, sources=["https://science.example/one"]),
        profiles(tmp_path),
    )

    def observer(kind, data):
        if kind == "record":
            data["record"]["data"]["title"] = "Observer mutation"
            raise RuntimeError("Observer unavailable")

    report = await PipelineRunner(
        job, data_dir=tmp_path / "data", fetcher=TwoPages(), on_event=observer
    ).run()
    assert report.status == "success", report.errors
    assert DatasetStore(tmp_path / "data").list_records()[0]["data"]["title"] == "Article one"


async def test_old_run_reload_uses_its_artifact_after_same_job_record_changes(tmp_path):
    job = requested_job(
        RunRequest(query="Article", ai=False, sources=["https://science.example/one"]),
        profiles(tmp_path),
    )
    data_dir = tmp_path / "data"

    class ChangingPages:
        title = "Original article"

        async def fetch(self, url):
            return article(url, self.title)

    pages = ChangingPages()
    manager = LiveRunManager(data_dir, runner_factory=factory(pages))
    first = manager.submit(job, "Article")["run_id"]
    await manager.wait(first)
    pages.title = "Revised article"
    second = manager.submit(job, "Article")["run_id"]
    await manager.wait(second)
    assert DatasetStore(data_dir).list_records()[0]["data"]["title"] == "Revised article"
    (data_dir / "live" / f"{first}.json").unlink()
    historical = LiveRunManager(data_dir).snapshot(first)
    assert historical["records"][0]["data"]["title"] == "Original article"
    assert (
        historical["records"][0]["record_id"] == manager.snapshot(second)["records"][0]["record_id"]
    )


async def test_auxiliary_snapshot_failure_preserves_completed_checkpoint(tmp_path, monkeypatch):
    job = requested_job(
        RunRequest(query="Article", ai=False, sources=["https://science.example/one"]),
        profiles(tmp_path),
    )
    manager = LiveRunManager(tmp_path / "data", runner_factory=factory(TwoPages()))
    persist = manager._persist

    def failing_persist(live):
        if live.state["finished_at"] is not None:
            raise OSError("Disk unavailable")
        persist(live)

    monkeypatch.setattr(manager, "_persist", failing_persist)
    run_id = manager.submit(job, "Article")["run_id"]
    await manager.wait(run_id)
    snapshot = manager.snapshot(run_id)
    assert snapshot["status"] == "success" and not manager.tasks
    assert snapshot["records_persisted"] == 1
    assert manager.states[run_id].state["errors"][-1]["stage"] == "live_storage"
    assert LiveRunManager(tmp_path / "data").snapshot(run_id)["status"] == "success"

"""Recovery admission and disk presentation independently of the crawl worker."""

import asyncio
import json
from datetime import UTC, datetime

import httpx
import pytest

from unified_web_parser.core.config import JobConfig, LoadedJob
from unified_web_parser.core.models import Provenance, Record
from unified_web_parser.crawling.pages import make_page
from unified_web_parser.interfaces.api import create_app
from unified_web_parser.interfaces.live import LiveRunManager
from unified_web_parser.interfaces.live.snapshots import persist_snapshot, restore_snapshot
from unified_web_parser.pipeline.checkpoints import CheckpointStore
from unified_web_parser.pipeline.report import RunReport
from unified_web_parser.pipeline.runner import PipelineRunner
from unified_web_parser.storage.run_lock import RunLease, is_run_active


def interrupted_run(data_dir, run_id="interrupted-1"):
    directory = data_dir / "runs" / run_id
    config = {
        "name": "recovery-test",
        "sources": [{"url": "https://example.org/one"}],
        "schema_path": "missing-original-schema.json",
        "outputs": ["jsonl"],
        "ai": {"enabled": False, "max_calls": 0},
        "crawl": {"max_pages": 3, "max_records": 5},
    }
    schema = {
        "type": "object",
        "required": ["title"],
        "properties": {"title": {"type": "string"}},
    }
    record = Record(
        record_id="first",
        job_name="recovery-test",
        data={"title": "Durable observation"},
        provenance=Provenance(
            page_id="page-one",
            source_url="https://example.org/one",
            candidate_ids=("candidate-one",),
            extractor="dom",
        ),
    )
    report = RunReport(
        run_id=run_id,
        job_name="recovery-test",
        started_at=datetime.now(UTC),
        pages_attempted=1,
        pages_fetched=1,
        records_written=1,
    )
    checkpoint = CheckpointStore(directory)
    checkpoint.update(
        config=config,
        schema=schema,
        effective_config=config,
        report=report.model_dump(mode="json"),
        completed=False,
        phase="crawling",
        revision=7,
    )
    checkpoint.save_record(record)
    return directory, checkpoint, report


async def test_resume_reserves_owner_before_202_and_foreign_reader_keeps_active_state(tmp_path):
    data_dir = tmp_path / "data"
    directory, checkpoint, report = interrupted_run(data_dir)
    release = asyncio.Event()
    entered = asyncio.Event()

    class WaitingRunner:
        def __init__(self, job, **_kwargs):
            assert job.extraction_schema["required"] == ["title"]

        async def run(self, *, resume_dir, run_id, lease):
            assert resume_dir == directory and lease.held
            assert run_id == report.run_id
            entered.set()
            await release.wait()
            result = report.model_copy(
                update={"status": "success", "finished_at": datetime.now(UTC)}
            )
            checkpoint.update(report=result.model_dump(mode="json"), completed=True)
            return result

    app = create_app(data_dir, tmp_path / "jobs", runner_factory=WaitingRunner)
    other = create_app(data_dir, tmp_path / "jobs")
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://one") as client,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=other), base_url="http://two"
        ) as second,
    ):
        before = (await client.get(f"/runs/{report.run_id}")).json()
        assert before["status"] == "interrupted" and before["resume_available"]
        assert before["records_persisted"] == 1 and before["presentation"]["fields"]
        response = await client.post(f"/runs/{report.run_id}/resume")
        assert response.status_code == 202
        assert response.headers["location"] == f"/runs/{report.run_id}"
        assert is_run_active(directory)
        await asyncio.wait_for(entered.wait(), 3)
        competing = await second.post(f"/runs/{report.run_id}/resume")
        assert competing.status_code == 409
        foreign = (await second.get(f"/runs/{report.run_id}")).json()
        assert foreign["run_active"] and foreign["status"] == "running"
        assert not foreign["resume_available"] and len(foreign["records"]) == 1
        release.set()
        await app.state.run_manager.wait(report.run_id)
        assert not is_run_active(directory)
        final = (await client.get(f"/runs/{report.run_id}")).json()
        assert final["status"] == "success" and not final["resume_available"]
        assert (await second.post(f"/runs/{report.run_id}/resume")).status_code == 409


async def test_cancel_before_resume_worker_starts_releases_reserved_lease(tmp_path):
    directory, _checkpoint, report = interrupted_run(tmp_path)

    def must_not_start(*_args, **_kwargs):
        raise AssertionError("Cancelled queued worker must not run")

    manager = LiveRunManager(tmp_path, runner_factory=must_not_start)
    manager.resume(report.run_id)
    assert is_run_active(directory)
    manager.cancel(report.run_id)
    await manager.close()
    assert not is_run_active(directory)
    restored = LiveRunManager(tmp_path).snapshot(report.run_id)
    assert restored["status"] == "cancelled" and restored["resume_available"]
    assert restored["records_persisted"] == 1


def test_completed_summary_wins_over_stale_running_live_file(tmp_path):
    directory = tmp_path / "runs" / "legacy"
    directory.mkdir(parents=True)
    (directory / "job.json").write_text(
        json.dumps(
            {
                "config": {
                    "search": "saved",
                    "crawl": {"max_pages": 3, "max_records": 5},
                    "ai": {"max_calls": 0},
                },
                "schema": {"type": "object", "properties": {"title": {"type": "string"}}},
            }
        ),
        encoding="utf-8",
    )
    report = RunReport(
        run_id="legacy",
        job_name="job",
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
        status="success",
        records_written=1,
    )
    (directory / "summary.json").write_text(report.model_dump_json(), encoding="utf-8")
    (directory / "records.jsonl").write_text(
        json.dumps(
            {
                "record_id": "one",
                "data": {"title": "Committed"},
                "provenance": {"source_url": "https://example.org/one"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    persist_snapshot(
        tmp_path / "live" / "legacy.json",
        {
            "run_id": "legacy",
            "status": "running",
            "records": [],
            "errors": [],
        },
    )
    restored = restore_snapshot(tmp_path, "legacy")
    assert restored["status"] == "success" and not restored["resume_available"]
    assert restored["records"][0]["data"]["title"] == "Committed"


def test_checkpoint_survives_corrupt_live_and_config_files(tmp_path):
    directory, _checkpoint, report = interrupted_run(tmp_path)
    (tmp_path / "live").mkdir()
    (tmp_path / "live" / f"{report.run_id}.json").write_text('{"records":', encoding="utf-8")
    (directory / "job.json").write_text('{"config":', encoding="utf-8")
    restored = restore_snapshot(tmp_path, report.run_id)
    assert restored["status"] == "interrupted" and restored["resume_available"]
    assert restored["records"][0]["data"]["title"] == "Durable observation"
    assert restored["presentation"]["fields"]


def test_cli_checkpoint_appears_interrupted_in_history_without_live_file(tmp_path):
    _directory, _checkpoint, report = interrupted_run(tmp_path)
    recent = LiveRunManager(tmp_path).recent(20)
    assert len(recent) == 1 and recent[0]["run_id"] == report.run_id
    assert recent[0]["status"] == "interrupted" and recent[0]["resume_available"]


def test_live_atomic_replace_failure_keeps_last_complete_snapshot(tmp_path, monkeypatch):
    path = tmp_path / "live.json"
    persist_snapshot(path, {"revision": 1, "records": ["saved"]})

    def fail_replace(*_args, **_kwargs):
        raise OSError("Injected replace failure")

    monkeypatch.setattr(type(path), "replace", fail_replace)
    with pytest.raises(OSError, match="Injected"):
        persist_snapshot(path, {"revision": 2, "records": ["new"]})
    assert json.loads(path.read_text()) == {"revision": 1, "records": ["saved"]}
    assert not list(tmp_path.glob("*.tmp"))


def test_one_broken_historical_live_file_does_not_break_history(tmp_path):
    live_dir = tmp_path / "live"
    live_dir.mkdir()
    (live_dir / "broken.json").write_text("{", encoding="utf-8")
    assert LiveRunManager(tmp_path).recent(20) == []


async def test_corrupt_checkpoint_isolated_without_starting_worker_or_breaking_history(tmp_path):
    _directory, _checkpoint, report = interrupted_run(tmp_path)
    broken = tmp_path / "runs" / "broken"
    broken.mkdir()
    (broken / "checkpoint.sqlite").write_bytes(b"damaged SQLite checkpoint")

    def must_not_start(*_args, **_kwargs):
        raise AssertionError("Reading damaged state must not start a worker")

    app = create_app(tmp_path, tmp_path / "jobs", runner_factory=must_not_start)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://local"
    ) as client:
        for suffix in ("", "/events"):
            response = await client.get(f"/runs/broken{suffix}")
            assert response.status_code == 409
        assert (await client.post("/runs/broken/resume")).status_code == 409
        assert not is_run_active(broken)
        history = await client.get("/runs")
        assert history.status_code == 200
        assert [row["run_id"] for row in history.json()] == [report.run_id]
        assert (await client.get("/health")).json() == {"status": "ok"}
        assert not app.state.run_manager.tasks


async def test_real_record_stream_never_regresses_revision_at_completion_or_restart(tmp_path):
    schema = {
        "type": "object",
        "x-entity-types": ["Article"],
        "properties": {"title": {"type": "string"}, "url": {"type": "string"}},
        "required": ["title", "url"],
        "additionalProperties": False,
    }
    job = LoadedJob(
        config_path=tmp_path / "job.yaml",
        config=JobConfig.model_validate(
            {
                "name": "revision-review",
                "schema_path": "unused.json",
                "sources": [{"url": "https://offline.example/catalog"}],
                "crawl": {"max_records": 30, "max_pages": 4, "delay": 0},
                "ai": {"enabled": False},
                "navigation": {"mode": "rules"},
            }
        ),
        extraction_schema=schema,
    )

    class Pages:
        async def fetch(self, url):
            entities = [
                {
                    "@type": "Article",
                    "headline": f"Article {number}",
                    "url": f"https://offline.example/articles/{number}",
                }
                for number in range(20)
            ]
            return make_page(
                url,
                '<script type="application/ld+json">' + json.dumps(entities) + "</script>",
                200,
            )

    manager = LiveRunManager(
        tmp_path, runner_factory=lambda job, **kw: PipelineRunner(job, fetcher=Pages(), **kw)
    )
    revisions = []
    original_event = manager._event

    def observe(live, kind, data):
        original_event(live, kind, data)
        revisions.append(live.state["revision"])

    manager._event = observe
    initial = manager.submit(job, "Articles")
    run_id = initial["run_id"]
    await manager.wait(run_id)
    await asyncio.sleep(0)  # Let task completion remove the in-memory task owner.
    final = manager.snapshot(run_id)
    restarted = LiveRunManager(tmp_path).snapshot(run_id)
    assert final["status"] == restarted["status"] == "success"
    assert len(final["records"]) == len(restarted["records"]) == 20
    assert final["revision"] >= max(revisions)
    assert restarted["revision"] >= final["revision"]


async def test_foreign_sse_emits_checkpoint_changes_at_same_public_revision_floor(tmp_path):
    directory, checkpoint, report = interrupted_run(tmp_path)
    persist_snapshot(
        tmp_path / "live" / f"{report.run_id}.json",
        {"revision": 100000, "records": [{"must_not_override_checkpoint": True}]},
    )
    manager = LiveRunManager(tmp_path)
    with RunLease(directory):
        stream = manager.events(report.run_id, heartbeat=0.001)
        first = json.loads((await anext(stream)).split("data: ", 1)[1])
        assert first["revision"] == 100000
        assert first["records"][0]["data"]["title"] == "Durable observation"
        record = Record.model_validate(checkpoint.load()["records"][0]).model_copy(
            update={"record_id": "second", "data": {"title": "New committed observation"}}
        )
        checkpoint.save_record(record)
        second = json.loads((await asyncio.wait_for(anext(stream), 2)).split("data: ", 1)[1])
        assert second["revision"] == first["revision"]
        assert len(second["records"]) == 2
        completed = report.model_copy(update={"status": "success"})
        checkpoint.update(report=completed.model_dump(mode="json"), completed=True)
        terminal = json.loads((await asyncio.wait_for(anext(stream), 2)).split("data: ", 1)[1])
        assert terminal["status"] == "success"
        assert terminal["revision"] == first["revision"]
        await stream.aclose()

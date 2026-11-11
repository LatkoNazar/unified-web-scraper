"""Explicit update admission forks work without mutating or restarting the parent."""

import asyncio
import json
from datetime import UTC, datetime

import httpx
import pytest

from unified_web_parser.core.config import JobConfig
from unified_web_parser.core.models import Provenance, Record
from unified_web_parser.interfaces.api import create_app
from unified_web_parser.interfaces.live import LiveRunManager, RunRequest, requested_job
from unified_web_parser.pipeline.checkpoints import CheckpointStore
from unified_web_parser.pipeline.report import RunReport
from unified_web_parser.storage.run_lock import RunLease


def saved_parent(tmp_path):
    directory = tmp_path / "runs" / "parent"
    config = JobConfig.model_validate(
        {
            "name": "incremental-articles",
            "search": "Article",
            "sources": [{"url": "https://example.org/catalog"}],
            "schema_path": "removed-original.json",
            "ai": {"enabled": False, "max_calls": 0},
            "navigation": {"mode": "rules"},
        }
    )
    schema = {
        "type": "object",
        "x-entity-types": ["Article"],
        "required": ["title", "url"],
        "properties": {"title": {"type": "string"}, "url": {"type": "string"}},
    }
    report = RunReport(
        run_id="parent",
        job_name=config.name,
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
        status="success",
        records_written=1,
        ai={"calls": 0, "known_cost_usd": 0, "cost_complete": True},
    )
    checkpoint = CheckpointStore(directory)
    checkpoint.update(
        config=config.model_dump(mode="json"),
        effective_config=config.model_dump(mode="json"),
        schema=schema,
        schema_frozen=True,
        completed=True,
        phase="finished",
        report=report.model_dump(mode="json"),
    )
    checkpoint.save_record(
        Record(
            record_id="known",
            job_name=config.name,
            data={"title": "Article one", "url": "https://example.org/one"},
            provenance=Provenance(
                page_id="page-one",
                source_url="https://example.org/one",
                candidate_ids=("own",),
                extractor="json_ld",
            ),
        )
    )
    (directory / "job.json").write_text(
        json.dumps({"config": config.model_dump(mode="json"), "schema": schema}), encoding="utf-8"
    )
    (directory / "summary.json").write_text(report.model_dump_json(), encoding="utf-8")
    return directory, checkpoint


async def test_update_endpoint_explicitly_forks_delta_and_preserves_parent(tmp_path):
    directory, checkpoint = saved_parent(tmp_path)
    original = checkpoint.load()
    entered, release = asyncio.Event(), asyncio.Event()

    class UpdateWorker:
        def __init__(self, job, **_kwargs):
            assert job.config.incremental.enabled
            assert job.update_from_run == directory.resolve()
            assert job.extraction_schema == original["schema"]
            self.job = job

        async def run(self, *, run_id):
            entered.set()
            await release.wait()
            return RunReport(
                run_id=run_id,
                job_name=self.job.config.name,
                status="success",
                started_at=datetime.now(UTC),
                finished_at=datetime.now(UTC),
                incremental={"enabled": True, "parent_run_id": "parent", "skipped_detail_urls": 1},
            )

    app = create_app(tmp_path, tmp_path / "jobs", runner_factory=UpdateWorker)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://local"
        ) as client,
    ):
        before = (await client.get("/runs/parent")).json()
        assert before["update_available"] and not entered.is_set()
        response = await client.post("/runs/parent/update")
        assert response.status_code == 202
        child = response.json()
        assert child["run_id"] != "parent"
        assert response.headers["location"] == f"/runs/{child['run_id']}"
        assert child["parent_run_id"] == "parent" and child["records"] == []
        await asyncio.wait_for(entered.wait(), 2)
        assert (await client.post("/runs/parent/update")).status_code == 429
        parent = (await client.get("/runs/parent")).json()
        assert parent["records"] == before["records"] and parent["ai"] == before["ai"]
        release.set()
        await app.state.run_manager.wait(child["run_id"])
        await asyncio.sleep(0)
        restored = LiveRunManager(tmp_path).snapshot(child["run_id"])
        assert restored["parent_run_id"] == "parent"
        assert restored["incremental"]["skipped_detail_urls"] == 1
        assert restored["records"] == []
        assert checkpoint.load() == original
        assert (await client.post("/runs/missing/update")).status_code == 404


async def test_update_rejects_active_or_unfinished_parent(tmp_path):
    directory, checkpoint = saved_parent(tmp_path)
    app = create_app(tmp_path, tmp_path / "jobs")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://local"
    ) as client:
        with RunLease(directory):
            assert not (await client.get("/runs/parent")).json()["update_available"]
            assert (await client.post("/runs/parent/update")).status_code == 409
        checkpoint.update(completed=False)
        assert not (await client.get("/runs/parent")).json()["update_available"]
        assert (await client.post("/runs/parent/update")).status_code == 409
        assert not app.state.run_manager.tasks


def test_new_request_incremental_flag_is_strict_and_reaches_job(tmp_path):
    (tmp_path / "schema.json").write_text('{"type":"object","properties":{}}', encoding="utf-8")
    (tmp_path / "web.yaml").write_text(
        "name: request-test\nschema_path: schema.json\n"
        "discovery: {enabled: true}\nsearch: Article\n",
        encoding="utf-8",
    )
    request = RunRequest(
        query="Article", sources=["https://example.org/"], ai=False, incremental=True
    )
    assert requested_job(request, tmp_path).config.incremental.enabled
    assert not requested_job(
        request.model_copy(update={"incremental": False}), tmp_path
    ).config.incremental.enabled
    with pytest.raises(ValueError):
        RunRequest(query="Article", incremental="true")

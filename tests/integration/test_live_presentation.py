"""Presentation follows effective schema events and each run's own saved records."""

import asyncio
import json
from datetime import UTC, datetime

from unified_web_parser.core.config import load_job
from unified_web_parser.interfaces.live import LiveRunManager
from unified_web_parser.pipeline.report import RunReport


def job_at(tmp_path):
    (tmp_path / "schema.json").write_text(
        json.dumps({"type": "object", "properties": {"title": {"type": "string"}}}),
        encoding="utf-8",
    )
    path = tmp_path / "job.yaml"
    path.write_text(
        "name: facets\nschema_path: schema.json\n"
        "sources: [{url: https://example.org/}]\noutputs: [jsonl]\n",
        encoding="utf-8",
    )
    return load_job(path)


def fields(snapshot):
    return {field["key"]: field for field in snapshot["presentation"]["fields"]}


async def test_effective_schema_and_late_record_facets_refresh_without_disk_reads(
    tmp_path, monkeypatch
):
    job = job_at(tmp_path)
    started = asyncio.Event()
    release = asyncio.Event()
    callbacks = []
    effective = {
        "x-display": {"columns": ["title", "altitude"]},
        "properties": {
            "title": {"type": "string"},
            "altitude": {"type": ["integer", "null"], "title": "Altitude", "x-unit": "m"},
        },
    }

    class ObservedRunner:
        def __init__(self, job, *, on_event, **kwargs):
            callbacks.append(on_event)

        async def run(self, *, run_id):
            callbacks[0]("schema", {"schema": effective})
            callbacks[0](
                "record", {"record": {"record_id": "a", "data": {"title": "A", "altitude": 0}}}
            )
            started.set()
            await release.wait()
            return RunReport(
                run_id=run_id,
                job_name="facets",
                status="success",
                started_at=datetime.now(UTC),
                records_written=2,
            )

    data_dir = tmp_path / "data"
    manager = LiveRunManager(data_dir, runner_factory=ObservedRunner)
    run_id = manager.submit(job, "altitude")["run_id"]
    await started.wait()
    first = manager.snapshot(run_id)
    assert first["presentation"]["status"] == "provisional"
    assert first["presentation"]["columns"] == ["title", "altitude"]
    assert fields(first)["altitude"]["unit"] == "m"
    assert fields(first)["altitude"]["stats"]["min"] == 0
    assert job.extraction_schema["properties"] == {"title": {"type": "string"}}
    assert "_presentation_schema" not in first
    first["presentation"]["fields"][0]["label"] = "External mutation"
    assert fields(manager.snapshot(run_id))["title"]["label"] == "Title"
    live = manager.states[run_id]
    cached = live.presentation
    callbacks[0]("stage", {"stage": "crawling"})
    manager.snapshot(run_id)
    assert live.presentation is cached
    callbacks[0](
        "record",
        {
            "record": {
                "record_id": "b",
                "data": {"title": "B", "altitude": None, "new_measure": False},
            }
        },
    )
    original_read = type(tmp_path).read_text

    def forbid_read(*args, **kwargs):
        raise AssertionError("active facets must not read disk")

    monkeypatch.setattr(type(tmp_path), "read_text", forbid_read)
    second = manager.snapshot(run_id)
    assert fields(second)["altitude"]["stats"]["missing"] == 1
    assert fields(second)["new_measure"]["stats"]["values"] == [{"value": False, "count": 1}]
    assert second["presentation"]["sample_size"] == 2
    monkeypatch.setattr(type(tmp_path), "read_text", original_read)
    release.set()
    await manager.wait(run_id)
    final = manager.snapshot(run_id)
    assert final["presentation"]["status"] == "ready"
    restored = LiveRunManager(data_dir).snapshot(run_id)
    assert restored["presentation"] == final["presentation"]
    assert restored["records"] == final["records"]
    assert "_presentation_schema" not in restored


def test_historical_artifact_schema_and_legacy_schemaless_live_snapshot(tmp_path):
    data_dir = tmp_path / "data"
    directory = data_dir / "runs" / "historical"
    directory.mkdir(parents=True)
    report = RunReport(
        run_id="historical",
        job_name="facets",
        status="success",
        started_at=datetime.now(UTC),
        records_written=1,
    )
    (directory / "summary.json").write_text(report.model_dump_json(), encoding="utf-8")
    (directory / "job.json").write_text(
        json.dumps(
            {
                "config": {
                    "search": "sample date",
                    "crawl": {"max_pages": 1, "max_records": 3},
                    "ai": {"max_calls": 0},
                },
                "schema": {"properties": {"old": {"type": "integer"}}},
            }
        ),
        encoding="utf-8",
    )
    (directory / "schema.json").write_text(
        json.dumps(
            {
                "properties": {
                    "observed_on": {"type": "string", "format": "date", "title": "Observed on"}
                }
            }
        ),
        encoding="utf-8",
    )
    records = [{"record_id": "first", "data": {"observed_on": "2026-10-23", "active": False}}]
    (directory / "records.jsonl").write_text(json.dumps(records[0]) + "\n", encoding="utf-8")
    manager = LiveRunManager(data_dir)
    snapshot = manager.snapshot("historical")
    assert snapshot["records"] == records
    assert fields(snapshot)["observed_on"]["filter"] == "range"
    assert fields(snapshot)["observed_on"]["stats"]["min"] == "2026-10-23"
    assert "old" not in fields(snapshot)
    live_dir = data_dir / "live"
    live_dir.mkdir()
    old = {**snapshot, "run_id": "old"}
    old.pop("presentation")
    (live_dir / "old.json").write_text(json.dumps(old), encoding="utf-8")
    restored = manager.snapshot("old")
    assert fields(restored)["active"]["type"] == "boolean"
    assert fields(restored)["active"]["stats"]["values"] == [{"value": False, "count": 1}]
    assert restored["presentation"]["sample_size"] == 1

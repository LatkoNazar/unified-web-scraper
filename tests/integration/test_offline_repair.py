"""A real saved run is corrected without deleting observations or changing billing."""

import importlib.util
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from unified_web_parser.core.config import JobConfig
from unified_web_parser.core.models import Provenance, Record
from unified_web_parser.interfaces.live.snapshots import restore_snapshot
from unified_web_parser.pipeline.report import RunReport
from unified_web_parser.storage import DatasetStore


@pytest.mark.parametrize("confirmed_only", [False, True])
def test_offline_repair_preserves_original_and_billing_but_publishes_only_devices(
    tmp_path, confirmed_only
):
    spec = importlib.util.spec_from_file_location(
        "offline_repair", Path(__file__).resolve().parents[2] / "scripts" / "repair_run.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    run_id = "historical"
    directory = tmp_path / "runs" / run_id
    directory.mkdir(parents=True)
    snapshot = tmp_path / "page.html"
    snapshot.write_text(
        '<link rel="canonical" href="https://example.org/laptop">'
        '<meta property="og:image" content="/images/own-laptop.webp">'
        '<script type="application/ld+json">{"@type":"Product",'
        '"name":"MacBook Air M5 16/512","url":"https://example.org/laptop",'
        '"offers":{"@type":"Offer","price":1200,"priceCurrency":"UAH"}}</script>'
        "<main><h1>MacBook Air M5 16/512</h1></main>",
        encoding="utf-8",
    )
    config = JobConfig.model_validate(
        {
            "name": "repair-test",
            "schema_path": "unused.json",
            "search": "MacBook M5",
            "sources": [{"url": "https://example.org/laptop"}],
            "content_profile": "commerce",
            "assessment_profile": "device",
            "assessment": ["is_relevant"],
            "ai": {"enabled": False},
        }
    )
    schema = {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "url": {"type": "string"},
            "listing_type": {"type": "string"},
        },
        "required": ["title", "url"],
    }
    provenance = Provenance(
        page_id="saved-page",
        source_url="https://example.org/laptop",
        candidate_ids=("own",),
        extractor="dom",
        snapshot_ref=str(snapshot),
    )
    records = [
        Record(
            record_id="laptop",
            job_name=config.name,
            data={
                "title": "MacBook Air M5 16/512",
                "url": "https://example.org/laptop",
                "listing_type": "device",
            },
            provenance=provenance,
        ),
        Record(
            record_id="case",
            job_name=config.name,
            data={
                "title": "Чохол для MacBook Air M5",
                "url": "https://example.org/case",
                "listing_type": "accessory",
            },
            provenance=provenance,
        ),
        Record(
            record_id="unknown-model",
            job_name=config.name,
            data={"title": "MacBook Air", "url": "https://example.org/unknown"},
            provenance=provenance,
        ),
    ]
    original = "".join(record.model_dump_json() + "\n" for record in records)
    (directory / "records.jsonl").write_text(original, encoding="utf-8")
    (directory / "job.json").write_text(
        json.dumps({"config": config.model_dump(mode="json"), "schema": schema}), encoding="utf-8"
    )
    ai = {"calls": 1, "known_cost_usd": 0.005, "cost_complete": False}
    report = RunReport(
        run_id=run_id,
        job_name=config.name,
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
        status="partial",
        records_written=3,
        ai=ai,
    )
    (directory / "summary.json").write_text(report.model_dump_json(), encoding="utf-8")
    store = DatasetStore(tmp_path)
    store.start_run(run_id, config.name, config.model_dump(mode="json"))
    store.save_records(run_id, records)
    before_rows = store.list_records(job_name=config.name)
    expected = 1 if confirmed_only else 2
    preview = module.repair_run(tmp_path, run_id, confirmed_only=confirmed_only)
    assert preview["before"] == 3 and preview["after"] == expected and preview["rejected"] == 1
    assert preview["review"] == 1
    assert (directory / "records.jsonl").read_text(encoding="utf-8") == original
    assert not (directory / "checkpoint.sqlite").exists()
    result = module.repair_run(tmp_path, run_id, apply=True, confirmed_only=confirmed_only)
    restored = restore_snapshot(tmp_path, run_id)
    assert result["network_requests"] == result["ai_calls"] == 0
    assert restored["ai"] == ai
    assert len(restored["records"]) == restored["records_persisted"] == expected
    assert restored["records"][0]["img_url"] == "https://example.org/images/own-laptop.webp"
    archive = Path(result["archive"])
    assert (archive / "records.jsonl").read_text(encoding="utf-8") == original
    rejected = json.loads((directory / "rejected-records.jsonl").read_text(encoding="utf-8"))
    assert rejected["record"]["record_id"] == "case"
    assert (archive / "dataset.sqlite").is_file()
    after_rows = store.list_records(job_name=config.name, include_excluded=True)
    assert {row["record_id"]: row["last_seen_at"] for row in after_rows} == {
        row["record_id"]: row["last_seen_at"] for row in before_rows
    }
    assert len(after_rows) == 3  # Raw historical observations are quarantined, never deleted.
    assert len(store.list_records(job_name=config.name)) == expected
    assert store.list_history("case", config.name)

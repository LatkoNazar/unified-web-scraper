import json
import sqlite3
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from unified_web_parser.core.models import Assessment, Provenance, Record
from unified_web_parser.storage import DatasetStore, export_records


def make_record(price=100, *, job_name="products", record_id="listing-1", page_id="page-1"):
    return Record(
        record_id=record_id,
        job_name=job_name,
        data={"title": "Ноутбук", "price": price, "specs": {"ram": 24}, "tags": ["used"]},
        provenance=Provenance(
            page_id=page_id,
            source_url="https://example.org/item/1",
            candidate_ids=("block-1",),
            extractor="json_ld",
        ),
    )


def test_current_records_idempotent_and_changed_history(tmp_path: Path):
    store = DatasetStore(tmp_path)
    store.start_run("run-1", "products", {"nested": {"schema": "any"}})
    store.save_records("run-1", [make_record()])
    first = store.list_records()[0]
    store.start_run("run-2", "products", {})
    store.save_records("run-2", [make_record(page_id="new-page")])
    unchanged = store.list_records()[0]
    assert len(store.list_records()) == 1
    assert len(store.list_history("listing-1")) == 1
    assert unchanged["provenance"]["page_id"] == "new-page"
    assert unchanged["first_seen_at"] == first["first_seen_at"]
    assert unchanged["last_seen_at"] >= first["last_seen_at"]
    store.save_records("run-2", [make_record(80)])
    # Returning to an earlier price is a new observation, not a global hash dedupe.
    store.save_records("run-2", [make_record(100)])
    assert [row["data"]["price"] for row in store.list_history("listing-1")] == [100, 80, 100]
    store.finish_run("run-2", {"records_written": 1, "status": "completed"})
    latest_run = store.list_runs()[0]
    assert latest_run["summary"]["status"] == "completed"
    assert latest_run["finished_at"]
    assert DatasetStore(tmp_path).list_records()[0]["data"] == make_record().data


def test_job_scoped_identity_and_filters(tmp_path: Path):
    store = DatasetStore(tmp_path)
    for job in ("products", "vacancies"):
        store.start_run(job, job, {})
        store.save_records(job, [make_record(job_name=job)])
    assert len(store.list_records()) == 2
    assert len(store.list_records("vacancies")) == 1
    assert len(store.list_history("listing-1", "products")) == 1
    assert store.list_records(limit=0) == []
    assert store.list_runs(limit=0) == []
    with pytest.raises(ValueError, match="negative"):
        store.list_records(limit=-1)


def test_quarantined_observation_keeps_history_and_fresh_acceptance_restores_current(tmp_path):
    store = DatasetStore(tmp_path)
    store.start_run("old", "products", {})
    store.save_records("old", [make_record()])
    with sqlite3.connect(store.path) as connection:
        connection.execute("UPDATE parser_records SET excluded_at='repair-time'")
    assert store.list_records() == []
    assert store.active_record_ids("products") == set()
    assert store.all_record_ids("products") == {"listing-1"}
    assert store.all_record_ids("different-job") == set()
    assert store.list_records("products") == []
    assert len(store.list_records(include_excluded=True)) == 1
    assert len(store.list_history("listing-1", "products")) == 1
    store.start_run("fresh", "products", {})
    store.save_records("fresh", [make_record()])
    assert store.list_records()[0]["excluded_at"] is None
    assert store.active_record_ids("products") == {"listing-1"}
    assert len(store.list_history("listing-1", "products")) == 1


def test_assessments_and_transaction_rollback(tmp_path: Path):
    store = DatasetStore(tmp_path)
    store.start_run("run", "products", {})
    assessment = Assessment(record_id="listing-1", assessor="rules", results={"relevant": True})
    store.save_records("run", [make_record()], [assessment])
    store.save_records("run", [make_record()], [assessment])
    assert store.list_records()[0]["assessments"] == [assessment.model_dump(mode="json")]
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM parser_assessments").fetchone()[0] == 1
    with pytest.raises(ValueError, match="Assessment"):
        store.save_records("run", [], [assessment])
    # A later unserializable row must roll back an earlier valid row in this batch.
    bad = make_record(record_id="bad").model_copy(update={"data": {"invalid": object()}})
    with pytest.raises(TypeError):
        store.save_records("run", [make_record(record_id="new"), bad])
    assert len(store.list_records()) == 1
    with pytest.raises(ValueError, match="job_name"):
        store.save_records("run", [make_record(job_name="other")])
    with pytest.raises(ValueError, match="Unknown run"):
        store.save_records("missing", [make_record()])
    with pytest.raises(ValueError, match="Unknown run"):
        store.finish_run("missing", {})


def test_parquet_scalar_types_nested_data_and_union_columns(tmp_path: Path):
    other = make_record(record_id="listing-2").model_copy(
        update={"data": {"title": "Other", "price": 80, "active": True}}
    )
    target = export_records([make_record(), other], tmp_path / "dataset.parquet", "parquet")
    table = pq.read_table(target)
    assert pa.types.is_integer(table.schema.field("price").type)
    assert pa.types.is_boolean(table.schema.field("active").type)
    json_columns = json.loads(table.schema.metadata[b"unified_web_parser.json_columns"])
    assert set(json_columns) == {"specs", "tags", "_provenance_json", "_assessments_json"}
    rows = table.to_pylist()
    assert rows[0]["title"] == "Ноутбук"
    assert json.loads(rows[0]["specs"]) == {"ram": 24}
    assert json.loads(rows[0]["tags"]) == ["used"]
    assert rows[1]["active"] is True
    assert rows[1]["specs"] is None
    assert rows[0]["_source_url"] == "https://example.org/item/1"
    assert json.loads(rows[0]["_provenance_json"])["candidate_ids"] == ["block-1"]


def test_jsonl_csv_and_empty_parquet(tmp_path: Path):
    record = make_record()
    target = export_records([record], tmp_path / "nested" / "records.jsonl", "jsonl")
    assert json.loads(target.read_text(encoding="utf-8"))["data"] == record.data
    csv = export_records([record], tmp_path / "records.csv", "csv")
    assert "price" in csv.read_text(encoding="utf-8").splitlines()[0]
    empty = export_records([], tmp_path / "empty.parquet", "parquet")
    table = pq.read_table(empty)
    assert table.num_rows == 0
    assert "_record_id" in table.column_names


def test_export_mixed_types_and_atomic_failure(tmp_path: Path):
    records = [make_record(price=100), make_record(price="negotiable")]
    target = export_records(records, tmp_path / "mixed.parquet", "parquet")
    values = pq.read_table(target).column("price").to_pylist()
    assert [json.loads(value) for value in values] == [100, "negotiable"]
    metadata = pq.read_schema(target).metadata
    assert "price" in json.loads(metadata[b"unified_web_parser.json_columns"])
    target.write_bytes(b"existing artifact")
    collision = make_record().model_copy(update={"data": {"_record_id": "user value"}})
    with pytest.raises(ValueError, match="reserved"):
        export_records([collision], target, "parquet")
    assert target.read_bytes() == b"existing artifact"
    assert not list(tmp_path.glob("*.tmp"))


def test_store_envelopes_export_without_losing_nested_data(tmp_path: Path):
    store = DatasetStore(tmp_path)
    store.start_run("run", "products", {})
    store.save_records("run", [make_record()])
    rows = store.list_records()
    target = export_records(rows, tmp_path / "saved.parquet", "parquet")
    row = pq.read_table(target).to_pylist()[0]
    assert json.loads(row["specs"]) == {"ram": 24}
    assert row["_first_seen_at"] == rows[0]["first_seen_at"]

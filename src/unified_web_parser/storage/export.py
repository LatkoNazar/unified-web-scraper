"""Atomic dataset exports with typed scalar columns in Parquet."""

import csv
import json
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from unified_web_parser.core.models import Record

METADATA_FIELDS = (
    "_record_id",
    "_job_name",
    "_source_url",
    "_img_url",
    "_provenance_json",
    "_assessments_json",
    "_first_seen_at",
    "_last_seen_at",
)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _flat(record: dict[str, Any]) -> dict[str, Any]:
    data = record.get("data", {})
    if not isinstance(data, dict):
        raise ValueError("Export record.data must be an object")
    collision = set(data).intersection(METADATA_FIELDS)
    if collision:
        raise ValueError(f"Data fields conflict with reserved export metadata: {sorted(collision)}")
    provenance = record.get("provenance", {})
    return {
        **data,
        "_record_id": record.get("record_id"),
        "_job_name": record.get("job_name"),
        "_source_url": provenance.get("source_url"),
        "_img_url": record.get("img_url"),
        "_provenance_json": _json(provenance),
        "_assessments_json": _json(record.get("assessments", [])),
        "_first_seen_at": record.get("first_seen_at"),
        "_last_seen_at": record.get("last_seen_at"),
    }


def export_records(
    records: Sequence[Record | dict[str, Any]],
    path: Path,
    format: Literal["jsonl", "parquet", "csv"],
) -> Path:
    """Export envelopes; Parquet/CSV flatten data and prefix metadata with ``_``.

    Nested domain values become JSON strings in tabular exports. Scalar columns
    retain Arrow types when homogeneous; mixed-type columns use JSON strings.
    JSONL preserves full nested record envelopes.
    """
    if format not in {"jsonl", "parquet", "csv"}:
        raise ValueError(f"Unsupported export format: {format}")
    rows = [
        record.model_dump(mode="json")
        if isinstance(record, Record)
        else {**record, "img_url": record.get("img_url")}
        for record in records
    ]
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        if format == "jsonl":
            with temporary.open("w", encoding="utf-8", newline="\n") as stream:
                for row in rows:
                    stream.write(_json(row) + "\n")
        else:
            flattened = [_flat(row) for row in rows]
            fields = list(
                dict.fromkeys([*METADATA_FIELDS, *(key for row in flattened for key in row)])
            )
            if format == "csv":
                with temporary.open("w", encoding="utf-8", newline="") as stream:
                    writer = csv.DictWriter(stream, fieldnames=fields)
                    writer.writeheader()
                    writer.writerows(
                        {
                            key: _json(value) if isinstance(value, dict | list) else value
                            for key, value in row.items()
                        }
                        for row in flattened
                    )
            else:
                import pyarrow as pa
                import pyarrow.parquet as pq

                arrays = {}
                json_columns = {"_provenance_json", "_assessments_json"}
                for field in fields:
                    values = [row.get(field) for row in flattened]
                    nonnull = [value for value in values if value is not None]
                    kinds = {type(value) for value in nonnull}
                    mixed = len(kinds) > 1 and not kinds <= {int, float}
                    if mixed or any(isinstance(value, dict | list) for value in nonnull):
                        values = [_json(value) if value is not None else None for value in values]
                        json_columns.add(field)
                    try:
                        arrays[field] = pa.array(
                            values, type=pa.string() if field == "_img_url" or not values else None
                        )
                    except (pa.ArrowException, OverflowError, TypeError):
                        json_columns.add(field)
                        arrays[field] = pa.array(
                            [_json(value) if value is not None else None for value in values],
                            type=pa.string(),
                        )
                table = pa.table(arrays).replace_schema_metadata(
                    {
                        b"unified_web_parser.nested_encoding": b"json",
                        b"unified_web_parser.json_columns": _json(sorted(json_columns)).encode(),
                        b"unified_web_parser.version": b"1",
                    }
                )
                pq.write_table(table, temporary)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return path

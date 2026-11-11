"""Commit completed records and portable exports, including controlled early stops."""

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from unified_web_parser.core.config import JobConfig
from unified_web_parser.core.models import Record
from unified_web_parser.pipeline.events import EventCallback
from unified_web_parser.pipeline.report import RunReport
from unified_web_parser.storage import export_records


def persist_completed_records(
    config: JobConfig,
    effective_config: JobConfig,
    records_by_id: Mapping[str, Record],
    *,
    run_id: str,
    run_dir: Path,
    store: Any,
    pg_store: Any,
    report: RunReport,
    publish: EventCallback,
) -> list[Record]:
    assessments = []
    if config.assessment:
        publish("stage", {"stage": "assessment"})
        from unified_web_parser.assessment.rules import assess_record

        assessments = [
            assess_record(
                record,
                effective_config.search,
                config.assessment,
                assessment_profile=config.assessment_profile,
                relevance_fields=config.relevance_fields,
            )
            for record in records_by_id.values()
        ]
    records = list(records_by_id.values())[: config.crawl.max_records]
    publish("stage", {"stage": "saving"})
    store.save_records(run_id, records, assessments)
    report.records_written = len(records)
    publish("counters", {"stage": "saving"})
    if pg_store:
        pg_store.save_records(run_id, records, assessments)
    selected_ids = {record.record_id for record in records}
    persisted = {
        item["record_id"]: item
        for item in store.list_records(
            job_name=config.name,
            limit=max(len(records), 1),
        )
        if item["record_id"] in selected_ids
    }
    export_data = [
        persisted.get(
            record.record_id,
            {
                **record.model_dump(mode="json"),
                "assessments": [
                    item.model_dump(mode="json")
                    for item in assessments
                    if item.record_id == record.record_id
                ],
            },
        )
        for record in records
    ]
    # Every run has a portable evidence dataset, even without configured exports.
    export_records(export_data, run_dir / "records.jsonl", "jsonl")
    report.artifacts["jsonl"] = str(run_dir / "records.jsonl")
    for output in config.outputs:
        if output in {"parquet", "csv"}:
            path = export_records(export_data, run_dir / f"records.{output}", output)
            report.artifacts[output] = str(path)
    return records

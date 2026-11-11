"""Preview or apply offline relevance/image repairs to a completed local run.

Original exports, the run view and a consistent database backup are retained.
This tool never opens a crawler or an AI client.
"""

import argparse
import hashlib
import json
import re
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from unified_web_parser.assessment.rules import assess_record
from unified_web_parser.core.config import JobConfig
from unified_web_parser.core.models import Page, Record
from unified_web_parser.extraction.images import image_url, recover_record_image
from unified_web_parser.pipeline.artifacts import atomic_text
from unified_web_parser.pipeline.checkpoints import CheckpointStore, has_checkpoint
from unified_web_parser.pipeline.report import RunReport
from unified_web_parser.processing.admission import admit_record
from unified_web_parser.processing.validation import validate_records
from unified_web_parser.storage import DatasetStore, export_records
from unified_web_parser.storage.run_lock import RunLease


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def repair_run(
    data_dir: Path,
    run_id: str,
    *,
    apply: bool = False,
    image_evidence: Path | None = None,
    confirmed_only: bool = False,
) -> dict:
    """Keep the public run ID; quarantine rejected observations without deleting history."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", run_id):
        raise ValueError("Invalid run ID")
    data_dir = data_dir.resolve()
    extra_images = {}
    if image_evidence is not None:
        evidence_map = json.loads(image_evidence.read_text(encoding="utf-8"))
        if evidence_map.get("complete") is not True:
            raise ValueError("Image evidence collection has not completed")
        extra_images = {row["record_id"]: row for row in evidence_map["records"]}
    directory = data_dir / "runs" / run_id
    if not (directory / "summary.json").is_file():
        raise ValueError("A completed run summary is required")
    with RunLease(directory):
        job = json.loads((directory / "job.json").read_text(encoding="utf-8"))
        config = JobConfig.model_validate(job["config"])
        schema = job["schema"]
        summary = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
        RunReport.model_validate(summary)
        if summary["status"] == "running":
            raise ValueError("Use resume for unfinished runs")
        if has_checkpoint(directory):
            saved = CheckpointStore(directory).load()
            if not saved.get("completed"):
                raise ValueError(
                    "Resume the pending checkpoint before repairing its completed view"
                )
            source_rows = saved["records"]
        else:
            source_rows = [
                json.loads(line)
                for line in (directory / "records.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        accepted, rejected, reviewed, images = [], [], [], []
        missing = []
        for envelope in source_rows:
            record = Record.model_validate({key: envelope.get(key) for key in Record.model_fields})
            decision = admit_record(
                record,
                schema,
                config.search,
                assessment_profile=config.assessment_profile,
                relevance_fields=config.relevance_fields,
            )
            if decision.status == "rejected":
                rejected.append({"record": envelope, "admission": decision.model_dump(mode="json")})
                continue
            if decision.status == "review":
                reviewed.append({"record": envelope, "admission": decision.model_dump(mode="json")})
                if confirmed_only:
                    continue
            original_image = str(record.img_url) if record.img_url else None
            found = image_url(original_image, str(record.provenance.source_url))
            reference = record.provenance.snapshot_ref
            evidence = None
            references = [(reference, str(record.provenance.source_url))] if reference else []
            if extra := extra_images.get(record.record_id):
                if extra.get("status_code") == 200 and extra.get("url") == record.data.get("url"):
                    references.insert(0, (extra["snapshot_ref"], extra["url"]))
            for source_ref, source_url in references:
                if found:
                    break
                snapshot = Path(source_ref)
                if not snapshot.is_absolute():
                    snapshot = data_dir / snapshot
                if snapshot.is_file():
                    html = snapshot.read_text(encoding="utf-8")
                    page = Page(
                        page_id=record.provenance.page_id,
                        url=source_url,
                        fetched_at=datetime.fromisoformat(summary["started_at"]),
                        html=html,
                        status_code=200,
                        snapshot_ref=str(snapshot),
                    )
                    found = recover_record_image(
                        page,
                        record.data,
                        schema=schema,
                        content_profile=config.content_profile,
                        currency_aliases=config.currency_aliases,
                    )
                    evidence = hashlib.sha256(html.encode("utf-8")).hexdigest()
                    if found:
                        reference = str(snapshot)
            if found != original_image:
                record = Record.model_validate({**record.model_dump(mode="json"), "img_url": found})
                images.append(
                    {
                        "record_id": record.record_id,
                        "before": original_image,
                        "after": found,
                        "snapshot_ref": reference,
                        "snapshot_sha256": evidence,
                    }
                )
            if not found:
                missing.append({"record_id": record.record_id, "url": record.data.get("url")})
            assessment = assess_record(
                record,
                config.search,
                config.assessment,
                assessment_profile=config.assessment_profile,
                relevance_fields=config.relevance_fields,
            )
            assessments = [
                item for item in envelope.get("assessments", []) if item.get("assessor") != "rules"
            ]
            assessments.append(assessment.model_dump(mode="json"))
            accepted.append(
                {**envelope, **record.model_dump(mode="json"), "assessments": assessments}
            )
        records = [
            Record.model_validate({key: row.get(key) for key in Record.model_fields})
            for row in accepted
        ]
        valid, invalid = validate_records(records, schema)
        if invalid or len(valid) != len(records):
            raise ValueError("Repaired records failed schema or identity validation")
        audit = {
            "run_id": run_id,
            "at": datetime.now(UTC).isoformat(),
            "before": len(source_rows),
            "after": len(accepted),
            "rejected": len(rejected),
            "review": len(reviewed),
            "review_retained": not confirmed_only,
            "images_before": sum(bool(row.get("img_url")) for row in source_rows),
            "real_images_before": sum(
                bool(image_url(row.get("img_url"), str(row["provenance"]["source_url"])))
                for row in source_rows
            ),
            "images_after": sum(bool(row.get("img_url")) for row in accepted),
            "image_changes": images,
            "missing_images": missing,
            "network_requests": 0,
            "ai_calls": 0,
            "image_evidence_map": str(image_evidence) if image_evidence else None,
            "external_image_evidence_records": len(extra_images),
            "applied": apply,
        }
        if not apply:
            return audit
        archive = (
            directory
            / "repairs"
            / (datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:8])
        )
        archive.mkdir(parents=True)
        for path in directory.iterdir():
            if path.is_file() and path.name != "run.lock":
                shutil.copy2(path, archive / path.name)
        live = data_dir / "live" / f"{run_id}.json"
        if live.is_file():
            shutil.copy2(live, archive / "live.json")
        database = data_dir / "dataset.sqlite"
        if database.is_file():
            with (
                sqlite3.connect(database) as source,
                sqlite3.connect(archive / "dataset.sqlite") as target,
            ):
                source.backup(target)
        audit["archive"] = str(archive)
        stage = archive / "repaired"
        stage.mkdir()
        atomic_text(stage / "audit.json", encoded(audit))
        atomic_text(
            stage / "rejected-records.jsonl", "".join(encoded(row) + "\n" for row in rejected)
        )
        atomic_text(
            stage / "review-records.jsonl", "".join(encoded(row) + "\n" for row in reviewed)
        )
        for format_name in {"jsonl", "csv", "parquet"} & {"jsonl", *config.outputs}:
            export_records(accepted, stage / f"records.{format_name}", format_name)
        summary["records_written"] = len(accepted)
        summary["admission"] = {
            "accepted": len(accepted) - (len(reviewed) if not confirmed_only else 0),
            "review": len(reviewed),
            "rejected": len(rejected),
        }
        summary["artifacts"]["repair"] = str(directory / "repair.json")
        summary["warnings"].append(
            {
                "stage": "repair",
                "message": (
                    f"Offline repair: excluded {len(rejected)} observations; "
                    f"review {len(reviewed)} (retained={not confirmed_only}); "
                    "original evidence archived"
                ),
                "audit": str(directory / "repair.json"),
            }
        )
        checkpoint = CheckpointStore(stage)
        checkpoint.update(
            config=config.model_dump(mode="json"),
            effective_config=config.model_dump(mode="json"),
            schema=schema,
            report=summary,
            completed=True,
            schema_frozen=True,
            phase="finished",
        )
        for record in records:
            checkpoint.save_record(record)
        atomic_text(stage / "summary.json", encoded(summary))
        if database.is_file():
            DatasetStore(data_dir)  # Upgrade local schema after the untouched backup exists.
            # Correct only this run's current rows. A newer observation and all
            # historical snapshots retain their original source evidence/times.
            with sqlite3.connect(database) as connection:
                connection.executemany(
                    "UPDATE parser_records SET img_url=?,assessments=? "
                    "WHERE job_name=? AND record_id=? AND run_id=?",
                    [
                        (
                            row.get("img_url"),
                            encoded(row["assessments"]),
                            config.name,
                            row["record_id"],
                            run_id,
                        )
                        for row in accepted
                    ],
                )
                connection.execute(
                    "UPDATE parser_runs SET summary=? WHERE run_id=? AND job_name=?",
                    (encoded(summary), run_id, config.name),
                )
                connection.executemany(
                    "UPDATE parser_records SET excluded_at=? "
                    "WHERE job_name=? AND record_id=? AND run_id=?",
                    [
                        (audit["at"], config.name, row["record"]["record_id"], run_id)
                        for row in [*rejected, *(reviewed if confirmed_only else [])]
                    ],
                )
        for path in stage.iterdir():
            if path.name in {"checkpoint.sqlite", "audit.json"}:
                continue
            shutil.copy2(path, directory / ("." + path.name + ".repair"))
            (directory / ("." + path.name + ".repair")).replace(directory / path.name)
        atomic_text(directory / "repair.json", encoded(audit))
        shutil.copy2(checkpoint.path, directory / ".checkpoint.repair")
        (directory / ".checkpoint.repair").replace(directory / "checkpoint.sqlite")
        return audit


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_id")
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--image-evidence", type=Path)
    parser.add_argument("--confirmed-only", action="store_true")
    args = parser.parse_args()
    print(
        encoded(
            repair_run(
                args.data_dir,
                args.run_id,
                apply=args.apply,
                image_evidence=args.image_evidence,
                confirmed_only=args.confirmed_only,
            )
        )
    )


if __name__ == "__main__":
    main()

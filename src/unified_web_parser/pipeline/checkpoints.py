"""Transactional per-run recovery authority, independent of current job records."""

import json
import sqlite3
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from unified_web_parser.core.config import JobConfig, LoadedJob
from unified_web_parser.core.exceptions import DurabilityError, PipelineError
from unified_web_parser.core.models import Record
from unified_web_parser.core.schema import validate_schema
from unified_web_parser.storage.run_lock import is_run_active


class CheckpointError(DurabilityError):
    """A mandatory durability boundary failed; do not advance the frontier."""


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


class CheckpointStore:
    """Records and progress survive process death; every write commits with FULL sync."""

    def __init__(self, run_dir: Path) -> None:
        self.run_dir = Path(run_dir).resolve()
        self.path = self.run_dir / "checkpoint.sqlite"
        self.run_dir.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            return
        with self._connection() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS records(
                    record_id TEXT PRIMARY KEY,envelope TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS plans(page_id TEXT PRIMARY KEY,payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS candidates(
                    page_id TEXT NOT NULL,candidate_id TEXT NOT NULL,
                    PRIMARY KEY(page_id,candidate_id));
            """)

    @contextmanager
    def _connection(self):
        connection = None
        try:
            connection = sqlite3.connect(self.path, timeout=30)
            connection.execute("PRAGMA synchronous=FULL")
            with connection:
                yield connection
        except (sqlite3.Error, OSError, ValueError, TypeError) as exc:
            raise CheckpointError("Unable to read or commit the run checkpoint") from exc
        finally:
            if connection is not None:
                connection.close()

    def update(self, **values: Any) -> None:
        with self._connection() as connection:
            previous = connection.execute(
                "SELECT value FROM metadata WHERE key='revision'"
            ).fetchone()
            values.update(
                updated_at=datetime.now(UTC).isoformat(),
                revision=json.loads(previous[0]) + 1 if previous else 1,
            )
            connection.executemany(
                "INSERT INTO metadata VALUES(?,?) ON CONFLICT(key) "
                "DO UPDATE SET value=excluded.value",
                [(key, _json(value)) for key, value in values.items()],
            )

    def save_record(self, record: Record) -> None:
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO records VALUES(?,?) ON CONFLICT(record_id) "
                "DO UPDATE SET envelope=excluded.envelope",
                (record.record_id, _json(record.model_dump(mode="json"))),
            )

    def save_plan(self, page_id: str, payload: dict[str, Any]) -> None:
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO plans VALUES(?,?) ON CONFLICT(page_id) "
                "DO UPDATE SET payload=excluded.payload",
                (page_id, _json(payload)),
            )

    def plan(self, page_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT payload FROM plans WHERE page_id=?", (page_id,)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def plans(self, prefix: str) -> dict[str, dict[str, Any]]:
        with self._connection() as connection:
            return {
                key.removeprefix(prefix): json.loads(payload)
                for key, payload in connection.execute(
                    "SELECT page_id,payload FROM plans WHERE substr(page_id,1,?)=?",
                    (len(prefix), prefix),
                )
            }

    def candidate_done(self, page_id: str, candidate_id: str) -> bool:
        with self._connection() as connection:
            return (
                connection.execute(
                    "SELECT 1 FROM candidates WHERE page_id=? AND candidate_id=?",
                    (page_id, candidate_id),
                ).fetchone()
                is not None
            )

    def complete_candidate(self, page_id: str, candidate_id: str) -> None:
        with self._connection() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO candidates VALUES(?,?)", (page_id, candidate_id)
            )

    def page_done(self, page_id: str) -> bool:
        return self.candidate_done(page_id, "__page_complete__")

    def complete_page(self, page_id: str) -> None:
        self.complete_candidate(page_id, "__page_complete__")

    def load(self) -> dict[str, Any]:
        # URI read-only avoids any DDL or journal mutation in API readers.
        with closing(sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)) as connection:
            result = {
                key: json.loads(value)
                for key, value in connection.execute("SELECT * FROM metadata")
            }
            result["records"] = [
                json.loads(row[0])
                for row in connection.execute("SELECT envelope FROM records ORDER BY rowid")
            ]
        return result


def has_checkpoint(run_dir: Path) -> bool:
    return (Path(run_dir) / "checkpoint.sqlite").is_file()


def load_resume_job(run_dir: Path) -> LoadedJob:
    run_dir = Path(run_dir).resolve()
    if not has_checkpoint(run_dir):
        raise PipelineError("Run has no durable checkpoint; use explicit snapshot reprocessing")
    saved = CheckpointStore(run_dir).load()
    config = JobConfig.model_validate(saved["config"])
    schema = saved.get("schema", {})
    if schema:
        validate_schema(schema)
    return LoadedJob(config=config, config_path=run_dir / "job.json", extraction_schema=schema)


def recovery_snapshot(data_dir: Path, run_id: str) -> dict[str, Any]:
    if not run_id or any(
        character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
        for character in run_id
    ):
        raise KeyError(run_id)
    run_dir = Path(data_dir) / "runs" / run_id
    if not has_checkpoint(run_dir):
        raise KeyError(run_id)
    saved = CheckpointStore(run_dir).load()
    report = saved.get("report", {})
    from unified_web_parser.pipeline.report import RunReport

    RunReport.model_validate(report)
    JobConfig.model_validate(saved.get("config", {}))
    records = saved["records"]
    active = is_run_active(run_dir)
    complete = saved.get("completed", False)
    status = report.get("status", "running")
    if not active and not complete and status in {"running", "queued", "cancelling"}:
        status = "interrupted"
    config = saved.get("config", {})
    ai = report.get("ai", {})
    journal_path = run_dir / "usage.jsonl"
    if journal_path.exists():
        from unified_web_parser.providers.usage_journal import UsageJournal

        ai = UsageJournal(journal_path, read_only=True).summary()
        ai["max_calls"] = config.get("ai", {}).get("max_calls")
        report = {**report, "ai": ai}
    return {
        "_presentation_schema": saved.get("schema", {}),
        "run_id": run_id,
        "job_name": config.get("name"),
        "question": config.get("search"),
        "status": status,
        "stage": saved.get("phase", "preparing")
        if active
        else "finished"
        if complete
        else "interrupted",
        "started_at": report.get("started_at"),
        "finished_at": report.get("finished_at"),
        "records": records,
        "counters": {
            "pages_attempted": report.get("pages_attempted", 0),
            "pages_fetched": report.get("pages_fetched", 0),
            "records_found": len(records),
            "invalid_records": report.get("invalid_records", 0),
        },
        "limits": {
            "max_pages": config.get("crawl", {}).get("max_pages"),
            "max_records": config.get("crawl", {}).get("max_records"),
            "max_calls": config.get("ai", {}).get("max_calls"),
        },
        "logs": [],
        "errors": report.get("errors", []),
        "warnings": report.get("warnings", []),
        "ai": ai,
        "report": report,
        "revision": saved.get("revision", 1),
        "persisted": bool(records),
        "records_persisted": len(records),
        "resume_available": not active and not complete,
        "run_active": active,
        "stop_reason": report.get("stop_reason"),
        "stop_message": report.get("stop_message"),
        "credit_limit_source": report.get("credit_limit_source"),
        "checkpoint_updated_at": saved.get("updated_at"),
    }

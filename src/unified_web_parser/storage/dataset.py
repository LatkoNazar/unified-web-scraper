"""Transactional current records, changed snapshots, and run history."""

import hashlib
import json
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from unified_web_parser.core.models import Assessment, Record


def _json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _decoded(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _now() -> str:
    return datetime.now(UTC).isoformat()


class DatasetStore:
    """Use SQLite locally or PostgreSQL with an explicit PostgreSQL connection URL.

    Record identity is ``(job_name, record_id)``. History tracks changes in extracted
    data or observed image, while provenance and last-seen time update every observation.
    Each ``save_records`` call commits together with its assessments.
    """

    def __init__(self, data_dir: Path, database_url: str | None = None) -> None:
        self.data_dir = Path(data_dir)
        self.database_url = database_url
        if database_url and not database_url.startswith(("postgresql://", "postgres://")):
            raise ValueError("database_url must be a PostgreSQL connection URL")
        self.backend = "postgresql" if database_url else "sqlite"
        self.path = self.data_dir / "dataset.sqlite"
        if not database_url:
            self.data_dir.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connection(self, *, write: bool = False) -> Iterator[Any]:
        if self.database_url:
            import psycopg
            from psycopg.rows import dict_row

            with psycopg.connect(self.database_url, row_factory=dict_row) as connection:
                yield connection
        else:
            connection = sqlite3.connect(self.path, timeout=30)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            try:
                with connection:
                    if write:
                        connection.execute("BEGIN IMMEDIATE")
                    yield connection
            finally:
                connection.close()

    def _execute(self, connection: Any, sql: str, parameters: Sequence[Any] = ()) -> Any:
        return connection.execute(sql.replace("?", "%s") if self.database_url else sql, parameters)

    def _value(self, value: Any) -> Any:
        if self.database_url:
            from psycopg.types.json import Jsonb

            return Jsonb(value, dumps=_json)
        return _json(value)

    def _initialize(self) -> None:
        json_type = "JSONB" if self.database_url else "TEXT"
        serial = (
            "BIGSERIAL PRIMARY KEY" if self.database_url else "INTEGER PRIMARY KEY AUTOINCREMENT"
        )
        statements = [
            f"""CREATE TABLE IF NOT EXISTS parser_runs (
                run_id TEXT PRIMARY KEY, job_name TEXT NOT NULL,
                started_at TEXT NOT NULL, finished_at TEXT,
                config {json_type} NOT NULL, summary {json_type})""",
            f"""CREATE TABLE IF NOT EXISTS parser_records (
                job_name TEXT NOT NULL, record_id TEXT NOT NULL,
                data {json_type} NOT NULL, provenance {json_type} NOT NULL,
                img_url TEXT, excluded_at TEXT,
                assessments {json_type} NOT NULL, data_hash TEXT NOT NULL,
                first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
                run_id TEXT NOT NULL REFERENCES parser_runs(run_id),
                PRIMARY KEY (job_name, record_id))""",
            f"""CREATE TABLE IF NOT EXISTS parser_snapshots (
                snapshot_id {serial}, job_name TEXT NOT NULL, record_id TEXT NOT NULL,
                run_id TEXT NOT NULL REFERENCES parser_runs(run_id), observed_at TEXT NOT NULL,
                data_hash TEXT NOT NULL, data {json_type} NOT NULL,
                provenance {json_type} NOT NULL,
                img_url TEXT,
                FOREIGN KEY (job_name, record_id)
                    REFERENCES parser_records(job_name, record_id))""",
            f"""CREATE TABLE IF NOT EXISTS parser_assessments (
                run_id TEXT NOT NULL REFERENCES parser_runs(run_id),
                job_name TEXT NOT NULL, record_id TEXT NOT NULL, assessor TEXT NOT NULL,
                results {json_type} NOT NULL, assessed_at TEXT NOT NULL,
                PRIMARY KEY (run_id, job_name, record_id, assessor),
                FOREIGN KEY (job_name, record_id)
                    REFERENCES parser_records(job_name, record_id))""",
            "CREATE INDEX IF NOT EXISTS parser_records_seen ON parser_records(last_seen_at)",
            """CREATE INDEX IF NOT EXISTS parser_snapshots_record
                ON parser_snapshots(job_name, record_id, snapshot_id)""",
            "CREATE INDEX IF NOT EXISTS parser_runs_started ON parser_runs(started_at)",
        ]
        if self.database_url:
            statements.append(
                "CREATE INDEX IF NOT EXISTS parser_records_data ON parser_records USING GIN(data)"
            )
        with self._connection(write=True) as connection:
            for statement in statements:
                self._execute(connection, statement)
            for table in ("parser_records", "parser_snapshots"):
                if self.database_url:
                    self._execute(
                        connection, f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS img_url TEXT"
                    )
                else:
                    columns = self._execute(connection, f"PRAGMA table_info({table})").fetchall()
                    if not any(column["name"] == "img_url" for column in columns):
                        self._execute(connection, f"ALTER TABLE {table} ADD COLUMN img_url TEXT")
            if self.database_url:
                self._execute(
                    connection,
                    "ALTER TABLE parser_records ADD COLUMN IF NOT EXISTS excluded_at TEXT",
                )
            else:
                columns = self._execute(connection, "PRAGMA table_info(parser_records)").fetchall()
                if not any(column["name"] == "excluded_at" for column in columns):
                    self._execute(
                        connection, "ALTER TABLE parser_records ADD COLUMN excluded_at TEXT"
                    )

    def start_run(self, run_id: str, job_name: str, config: dict[str, Any]) -> None:
        with self._connection(write=True) as connection:
            self._execute(
                connection,
                """INSERT INTO parser_runs(run_id, job_name, started_at, config)
                   VALUES (?, ?, ?, ?) ON CONFLICT(run_id) DO NOTHING""",
                (run_id, job_name, _now(), self._value(config)),
            )

    def finish_run(self, run_id: str, summary: dict[str, Any]) -> None:
        with self._connection(write=True) as connection:
            result = self._execute(
                connection,
                "UPDATE parser_runs SET finished_at = ?, summary = ? WHERE run_id = ?",
                (_now(), self._value(summary), run_id),
            )
            if result.rowcount != 1:
                raise ValueError(f"Unknown run: {run_id}")

    def save_records(
        self,
        run_id: str,
        records: Sequence[Record],
        assessments: Sequence[Assessment] = (),
    ) -> None:
        record_ids = {record.record_id for record in records}
        if any(assessment.record_id not in record_ids for assessment in assessments):
            raise ValueError("Assessment must reference a record in this batch")
        with self._connection(write=True) as connection:
            run = self._execute(
                connection, "SELECT job_name FROM parser_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if run is None:
                raise ValueError(f"Unknown run: {run_id}")
            if any(record.job_name != run["job_name"] for record in records):
                raise ValueError("Record job_name must match its run")
            for record in records:
                observed_at = _now()
                data_hash = hashlib.sha256(_json(record.data).encode("utf-8")).hexdigest()
                img_url = str(record.img_url) if record.img_url is not None else None
                provenance = record.provenance.model_dump(mode="json")
                selected = [item for item in assessments if item.record_id == record.record_id]
                if self.database_url:
                    self._execute(
                        connection,
                        "SELECT pg_advisory_xact_lock(hashtextextended(?, 0))",
                        (_json([record.job_name, record.record_id]),),
                    )
                previous = self._execute(
                    connection,
                    "SELECT data_hash, img_url FROM parser_records "
                    "WHERE job_name = ? AND record_id = ?",
                    (record.job_name, record.record_id),
                ).fetchone()
                self._execute(
                    connection,
                    """INSERT INTO parser_records
                        (job_name, record_id, data, provenance, assessments, data_hash,
                         first_seen_at, last_seen_at, run_id, img_url)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(job_name, record_id) DO UPDATE SET
                         data=excluded.data, provenance=excluded.provenance,
                         assessments=excluded.assessments, data_hash=excluded.data_hash,
                         last_seen_at=excluded.last_seen_at, run_id=excluded.run_id,
                         img_url=excluded.img_url, excluded_at=NULL""",
                    (
                        record.job_name,
                        record.record_id,
                        self._value(record.data),
                        self._value(provenance),
                        self._value([item.model_dump(mode="json") for item in selected]),
                        data_hash,
                        observed_at,
                        observed_at,
                        run_id,
                        img_url,
                    ),
                )
                if (
                    previous is None
                    or previous["data_hash"] != data_hash
                    or previous["img_url"] != img_url
                ):
                    self._execute(
                        connection,
                        """INSERT INTO parser_snapshots
                            (job_name, record_id, run_id, observed_at, data_hash,
                             data, provenance, img_url)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            record.job_name,
                            record.record_id,
                            run_id,
                            observed_at,
                            data_hash,
                            self._value(record.data),
                            self._value(provenance),
                            img_url,
                        ),
                    )
                for assessment in selected:
                    self._execute(
                        connection,
                        """INSERT INTO parser_assessments
                            (run_id, job_name, record_id, assessor, results, assessed_at)
                           VALUES (?, ?, ?, ?, ?, ?)
                           ON CONFLICT(run_id, job_name, record_id, assessor) DO UPDATE SET
                             results=excluded.results, assessed_at=excluded.assessed_at""",
                        (
                            run_id,
                            record.job_name,
                            record.record_id,
                            assessment.assessor,
                            self._value(assessment.results),
                            observed_at,
                        ),
                    )

    def _rows(self, sql: str, parameters: Sequence[Any] = ()) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = self._execute(connection, sql, parameters).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            for key in ("data", "provenance", "assessments", "config", "summary", "results"):
                if key in item and item[key] is not None:
                    item[key] = _decoded(item[key])
            result.append(item)
        return result

    def list_records(
        self, job_name: str | None = None, limit: int = 100, *, include_excluded: bool = False
    ) -> list[dict[str, Any]]:
        if limit < 0:
            raise ValueError("limit cannot be negative")
        conditions = [] if include_excluded else ["excluded_at IS NULL"]
        if job_name is not None:
            conditions.append("job_name = ?")
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        parameters = (job_name, limit) if job_name is not None else (limit,)
        return self._rows(
            f"SELECT * FROM parser_records{where} ORDER BY last_seen_at DESC, record_id LIMIT ?",
            parameters,
        )

    def list_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        if limit < 0:
            raise ValueError("limit cannot be negative")
        return self._rows("SELECT * FROM parser_runs ORDER BY started_at DESC LIMIT ?", (limit,))

    def active_record_ids(self, job_name: str) -> set[str]:
        """Return all current identities for completion validation, without a display limit."""
        with self._connection() as connection:
            return {
                row["record_id"]
                for row in self._execute(
                    connection,
                    "SELECT record_id FROM parser_records WHERE job_name=? AND excluded_at IS NULL",
                    (job_name,),
                )
            }

    def all_record_ids(self, job_name: str) -> set[str]:
        """Include quarantined identities so callers can distinguish exclusion from absence."""
        with self._connection() as connection:
            return {
                row["record_id"]
                for row in self._execute(
                    connection,
                    "SELECT record_id FROM parser_records WHERE job_name=?",
                    (job_name,),
                )
            }

    def list_history(self, record_id: str, job_name: str | None = None) -> list[dict[str, Any]]:
        where = "record_id = ?"
        parameters: tuple[Any, ...] = (record_id,)
        if job_name is not None:
            where += " AND job_name = ?"
            parameters += (job_name,)
        return self._rows(
            f"SELECT * FROM parser_snapshots WHERE {where} ORDER BY snapshot_id", parameters
        )

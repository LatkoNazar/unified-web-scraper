"""Explicit live PostgreSQL integration check; does not require DB for unit tests.

Run ``uv run python tests/integration/check_postgres.py`` with the bundled Compose
service, or set PARSER_TEST_DATABASE_URL to an explicitly designated test database.
Only records created under a random test job are removed after verification.
"""

import os
import tempfile
from pathlib import Path
from uuid import uuid4

import psycopg

from unified_web_parser.core.models import Assessment, Provenance, Record
from unified_web_parser.storage import DatasetStore


def check_postgres(database_url: str) -> None:
    job = f"storage-integration-{uuid4().hex}"
    run_ids = [f"{job}-first", f"{job}-repeat", f"{job}-changed"]
    with tempfile.TemporaryDirectory() as directory:
        store = DatasetStore(Path(directory), database_url)
        record = Record(
            record_id="item-1",
            job_name=job,
            data={"title": "Перевірка", "price": 100, "nested": {"ram": 24}},
            provenance=Provenance(
                page_id="snapshot-1",
                source_url="https://example.org/item-1",
                candidate_ids=("jsonld-0",),
                extractor="json_ld",
            ),
        )
        assessment = Assessment(record_id="item-1", assessor="rules", results={"relevant": True})
        try:
            for run_id, price in zip(run_ids, (100, 100, 80), strict=True):
                store.start_run(run_id, job, {"schema": {"type": "object"}})
                changed = record.model_copy(update={"data": {**record.data, "price": price}})
                store.save_records(run_id, [changed], [assessment])
                store.finish_run(run_id, {"records_written": 1, "status": "completed"})
            rows = store.list_records(job)
            assert len(rows) == 1 and rows[0]["data"]["price"] == 80
            assert rows[0]["data"]["nested"] == {"ram": 24}
            assert rows[0]["assessments"][0]["results"] == {"relevant": True}
            history = store.list_history("item-1", job)
            assert [snapshot["data"]["price"] for snapshot in history] == [100, 80]
            with psycopg.connect(database_url) as connection:
                row = connection.execute(
                    "SELECT pg_typeof(data)::text FROM parser_records WHERE job_name = %s", (job,)
                ).fetchone()
                assert row == ("jsonb",)
                count = connection.execute(
                    "SELECT COUNT(*) FROM parser_assessments WHERE job_name = %s", (job,)
                ).fetchone()
                assert count == (3,)
            assert len([run for run in store.list_runs(1000) if run["job_name"] == job]) == 3
        finally:
            with psycopg.connect(database_url) as connection:
                for table in (
                    "parser_assessments",
                    "parser_snapshots",
                    "parser_records",
                    "parser_runs",
                ):
                    connection.execute(
                        psycopg.sql.SQL("DELETE FROM {} WHERE job_name = %s").format(
                            psycopg.sql.Identifier(table)
                        ),
                        (job,),
                    )


if __name__ == "__main__":
    check_postgres(
        os.environ.get(
            "PARSER_TEST_DATABASE_URL",
            "postgresql://parser:parser_dev_only@127.0.0.1:55432/parser",
        )
    )
    print(
        "PostgreSQL: JSONB, idempotent upsert, changed history, assessments and run history passed"
    )

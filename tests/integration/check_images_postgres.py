"""Run an image-column legacy migration in an isolated local PostgreSQL schema.

Run: uv run python tests/integration/check_images_postgres.py
Only the randomly named schema created by this check is removed.
"""

import os
import tempfile
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

import psycopg
from psycopg import sql

from unified_web_parser.core.models import Provenance, Record
from unified_web_parser.storage import DatasetStore


def check_image_migration(database_url: str) -> None:
    schema = "parser_image_test_" + uuid4().hex
    with psycopg.connect(database_url, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    separator = "&" if "?" in database_url else "?"
    isolated_url = database_url + separator + "options=" + quote("-csearch_path=" + schema)
    try:
        with tempfile.TemporaryDirectory() as directory:
            store = DatasetStore(Path(directory), isolated_url)
            record = Record(
                record_id="source-record",
                job_name="images",
                data={"title": "Observed record"},
                provenance=Provenance(
                    page_id="page",
                    source_url="https://source.example/record",
                    candidate_ids=("block",),
                    extractor="json_ld",
                ),
            )
            store.start_run("legacy", "images", {})
            store.save_records("legacy", [record])
            original_hash = store.list_records()[0]["data_hash"]
            with psycopg.connect(isolated_url) as connection:
                for table in ("parser_records", "parser_snapshots"):
                    connection.execute(
                        sql.SQL("ALTER TABLE {} DROP COLUMN img_url").format(sql.Identifier(table))
                    )
            store = DatasetStore(Path(directory), isolated_url)
            assert DatasetStore(Path(directory), isolated_url).list_records()[0]["img_url"] is None
            store.start_run("with-image", "images", {})
            image = Record(
                **{**record.model_dump(), "img_url": "https://images.example/record.jpg"}
            )
            store.save_records("with-image", [image])
            store.save_records("with-image", [image])
            current = store.list_records()[0]
            assert current["img_url"] == str(image.img_url)
            assert current["data_hash"] == original_hash
            assert [row["img_url"] for row in store.list_history(record.record_id)] == [
                None,
                str(image.img_url),
            ]
            assert len(store.list_records()) == 1
    finally:
        with psycopg.connect(database_url, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


if __name__ == "__main__":
    check_image_migration(
        os.environ.get(
            "PARSER_TEST_DATABASE_URL", "postgresql://parser:parser_dev_only@127.0.0.1:55432/parser"
        )
    )
    print("PostgreSQL image migration: legacy/null/current/history/idempotence checks passed")

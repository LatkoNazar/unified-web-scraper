"""Explicit live acceptance check; default pytest never spends API credits.

Run from the repository root. --replay-run reuses saved HTML without crawling;
the provider cache is still available for the schema-driven AI stages.
"""

import argparse
import asyncio
import json
import os
from pathlib import Path

import pyarrow.parquet as pq
from dotenv import load_dotenv

from unified_web_parser.core.config import load_job
from unified_web_parser.pipeline.runner import PipelineRunner


async def check(replay_run: Path | None, data_dir: Path) -> None:
    load_dotenv(".env")
    if not os.getenv("OPENROUTER_API_KEY"):
        raise SystemExit("Live AI acceptance requires OPENROUTER_API_KEY in .env/environment")
    job = load_job("configs/jobs/macbooks-olx.yaml")
    report = await PipelineRunner(job, data_dir=data_dir).run(replay_dir=replay_run)
    print(report.model_dump_json(indent=2))
    if report.status != "success":
        raise SystemExit("Live acceptance failed: inspect the run summary")
    records = [
        json.loads(line)
        for line in Path(report.artifacts["jsonl"]).read_text(encoding="utf-8").splitlines()
        if line
    ]
    if not records or len({record["record_id"] for record in records}) != len(records):
        raise SystemExit("Dataset must contain distinct records")
    if pq.read_table(report.artifacts["parquet"]).num_rows != len(records):
        raise SystemExit("Parquet/JSONL row count mismatch")
    relevant = [
        record
        for record in records
        if any(
            assessment["results"].get("is_relevant") is True
            for assessment in record.get("assessments", [])
        )
    ]
    if not relevant:
        raise SystemExit("Search produced no relevant MacBook devices in the bounded sample")
    for record in relevant:
        if record["data"].get("price") is None or record["data"].get("currency") != "UAH":
            raise SystemExit("Relevant listing lacks an explicit UAH price")
        if not Path(record["provenance"]["snapshot_ref"]).is_file():
            raise SystemExit("Record source snapshot is missing")
    print(
        f"Acceptance passed: {len(records)} records, {len(relevant)} relevant; "
        "matching JSONL/Parquet, source evidence retained"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--replay-run", type=Path)
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    arguments = parser.parse_args()
    asyncio.run(check(arguments.replay_run, arguments.data_dir))

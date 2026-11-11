"""Replay a fixed corpus and compare cost plus source-reviewed quality invariants.

Explicit invocation only: this may use the configured paid AI providers. It never
fetches websites. Unit tests do not invoke this script or make paid requests.
"""

import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from unified_web_parser.core.config import load_job
from unified_web_parser.pipeline.runner import PipelineRunner

PRICES_PER_MILLION = {
    "qwen/qwen3.7-flash": (0.03, 0.13),
    "openai/gpt-6-luna-decisions": (0.10, 0.0),
}


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def read_records(directory: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in (directory / "records.jsonl").read_text(encoding="utf-8").splitlines()
        if line
    ]


def estimate(summary: dict) -> dict:
    stages = {}
    for name, stats in summary.get("ai", {}).get("stages", {}).items():
        model = stats["model"]
        if model not in PRICES_PER_MILLION:
            raise ValueError(f"No benchmark price snapshot for model {model}")
        input_price, output_price = PRICES_PER_MILLION[model]
        stages[name] = (
            stats.get("input_tokens", 0) * input_price
            + stats.get("output_tokens", 0) * output_price
        ) / 1_000_000
    return {
        "stages": stages,
        "total_usd": sum(stages.values()),
        "pricing_basis": "Base public token rates, excluding fees; estimates, not account billing",
    }


def reported_cost(summary: dict) -> float | None:
    """Unknown billing metadata must never be presented as a measured zero."""
    stages = summary.get("ai", {}).get("stages", {})
    if not stages:
        return None
    total = 0.0
    for stats in stages.values():
        if stats.get("calls", 0):
            if "cost_usd" not in stats:
                return None
            total += stats["cost_usd"]
    return total


def quality(records: list[dict], expected: dict) -> dict:
    failures = []
    by_id = {str(record["data"].get("external_id")): record for record in records}
    if len(records) != expected["expected_records"] or len(by_id) != len(records):
        failures.append("Record count or external-ID uniqueness changed")
    if set(by_id) != set(expected["records"]):
        failures.append("Dataset entity set changed")
    checks = relevant = 0
    for external_id, wanted in expected["records"].items():
        record = by_id.get(external_id)
        if record is None:
            continue
        data = record["data"]
        for field, value in wanted["fields"].items():
            checks += 1
            if field not in data or data[field] != value:
                failures.append(
                    {
                        "external_id": external_id,
                        "field": field,
                        "expected": value,
                        "actual": data.get(field),
                    }
                )
        for field, values in wanted.get("allowed_fields", {}).items():
            checks += 1
            if data.get(field) not in values:
                failures.append(
                    {
                        "external_id": external_id,
                        "field": field,
                        "allowed": values,
                        "actual": data.get(field),
                    }
                )
        observed = next(
            (
                assessment["results"].get("is_relevant")
                for assessment in record.get("assessments", [])
                if "is_relevant" in assessment["results"]
            ),
            None,
        )
        checks += 1
        if observed is not wanted["is_relevant"]:
            failures.append(
                {
                    "external_id": external_id,
                    "field": "is_relevant",
                    "expected": wanted["is_relevant"],
                    "actual": observed,
                }
            )
        relevant += observed is True
        conflict_text = " ".join(data.get("source_conflicts", []))
        for number in wanted.get("conflict_numbers", []):
            checks += 1
            if number not in conflict_text:
                failures.append(
                    {"external_id": external_id, "field": "source_conflicts", "missing": number}
                )
    if relevant != expected["expected_relevant"]:
        failures.append("Relevant record count changed")
    return {
        "passed": not failures,
        "checked_invariants": checks,
        "relevant": relevant,
        "failures": failures,
    }


def compare(baseline: list[dict], current: list[dict]) -> dict:
    old = {record["record_id"]: record for record in baseline}
    new = {record["record_id"]: record for record in current}
    immutable_changes = []
    field_changes = []
    immutable = {"title", "description", "url", "external_id", "price", "currency", "condition"}
    for identity in old.keys() & new.keys():
        before, after = old[identity]["data"], new[identity]["data"]
        for field in before.keys() | after.keys():
            if before.get(field) != after.get(field):
                change = {
                    "external_id": before.get("external_id"),
                    "field": field,
                    "before": before.get(field),
                    "after": after.get(field),
                }
                field_changes.append(change)
                if field in immutable:
                    immutable_changes.append(change)
    return {
        "identities_preserved": old.keys() == new.keys(),
        "immutable_changes": immutable_changes,
        "field_changes": field_changes,
    }


async def run(arguments) -> None:
    load_dotenv(".env")
    if not os.getenv("OPENROUTER_API_KEY"):
        raise SystemExit("OPENROUTER_API_KEY is required for this explicit AI benchmark")
    baseline_dir = arguments.baseline_run.resolve()
    baseline_summary = read_json(baseline_dir / "summary.json")
    job = load_job(arguments.config)
    if job.extraction_schema != read_json(baseline_dir / "job.json")["schema"]:
        raise SystemExit("Benchmark requires the same target schema as the baseline")
    report = await PipelineRunner(job, data_dir=arguments.data_dir).run(replay_dir=baseline_dir)
    current_dir = Path(report.artifacts["directory"])
    baseline_records, records = read_records(baseline_dir), read_records(current_dir)
    expected = read_json(arguments.expected)
    baseline_cost, cost = estimate(baseline_summary), estimate(report.model_dump(mode="json"))
    invariants = quality(records, expected)
    differences = compare(baseline_records, records)
    result = {
        "baseline_run": baseline_summary["run_id"],
        "optimized_run": report.run_id,
        "pipeline_status": report.status,
        "quality": invariants,
        "comparison": differences,
        "baseline_cost": baseline_cost,
        "optimized_cost": cost,
        "reduction_percent": (1 - cost["total_usd"] / baseline_cost["total_usd"]) * 100,
        "estimated_usd_per_1000_records": cost["total_usd"] / max(len(records), 1) * 1000,
        "optimized_ai": report.ai,
        "cache_regime": (
            "warm"
            if report.ai.get("calls", 0) == 0
            else "mixed"
            if report.ai.get("cache_hits", 0)
            else "cold"
        ),
        "provider_reported_cost_usd": {
            "baseline": reported_cost(baseline_summary),
            "optimized": reported_cost(report.model_dump(mode="json")),
        },
        "limits": "Five fixed saved OLX pages; does not establish quality for every domain/site",
    }
    path = current_dir / "cost-comparison.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"Comparison: {path}")
    if (
        report.status != "success"
        or not invariants["passed"]
        or not differences["identities_preserved"]
        or differences["immutable_changes"]
    ):
        raise SystemExit(
            "Quality gate failed; inspect cost-comparison.json before accepting optimization"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="configs/jobs/macbooks-olx.yaml")
    parser.add_argument(
        "--baseline-run", type=Path, default=Path("data/runs/9874455546d04ebf89af533504984a6e")
    )
    parser.add_argument(
        "--expected",
        type=Path,
        default=Path("tests/fixtures/benchmarks/olx_macbooks_expected.json"),
    )
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    asyncio.run(run(parser.parse_args()))

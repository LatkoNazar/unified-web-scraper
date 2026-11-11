"""Disk recovery and presentation snapshots, without task ownership."""

import heapq
import json
import os
import re
import sqlite3
from copy import deepcopy
from pathlib import Path
from typing import Any
from uuid import uuid4

from unified_web_parser.core.exceptions import PipelineError
from unified_web_parser.interfaces.live.state import TERMINAL, _LiveRun, _now
from unified_web_parser.interfaces.presentation import build_presentation
from unified_web_parser.pipeline.checkpoints import (
    CheckpointStore,
    has_checkpoint,
    recovery_snapshot,
)
from unified_web_parser.storage.run_lock import is_run_active

_RUN_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def snapshot_path(data_dir: Path, run_id: str) -> Path:
    if not _RUN_ID.fullmatch(run_id):
        raise KeyError(run_id)
    return data_dir / "live" / f"{run_id}.json"


def saved_schema(data_dir: Path, run_id: str, job: dict[str, Any] | None = None) -> dict[str, Any]:
    directory = data_dir / "runs" / run_id
    schema_path = directory / "schema.json"
    if schema_path.is_file():
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    else:
        job_path = directory / "job.json"
        if job is None and job_path.is_file():
            job = json.loads(job_path.read_text(encoding="utf-8"))
        schema = (job or {}).get("schema", {})
    return schema if isinstance(schema, dict) else {}


def live_snapshot(live: _LiveRun) -> dict[str, Any]:
    if live.presentation is None:
        live.presentation = build_presentation(
            live.schema,
            live.records.values(),
            question=live.state.get("question") or "",
            ready=live.state["status"] in TERMINAL,
        )
    return deepcopy(
        {
            **live.state,
            "records": list(live.records.values()),
            "presentation": live.presentation,
        }
    )


def _run_actions(state: dict[str, Any], directory: Path) -> dict[str, Any]:
    """Advertise explicit actions without starting work while reading history."""
    report = state.get("report") or {}
    incremental = state.get("incremental") or report.get("incremental", {})
    state["incremental"] = incremental
    state["incremental_enabled"] = bool(
        state.get("incremental_enabled") or incremental.get("enabled")
    )
    state["parent_run_id"] = state.get("parent_run_id") or incremental.get("parent_run_id")
    state["update_available"] = bool(
        state.get("status") in {"success", "partial"}
        and not state.get("resume_available")
        and not state.get("run_active")
        and (directory / "job.json").is_file()
    )
    return state


def persist_snapshot(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def recent_ids(data_dir: Path, limit: int) -> list[str]:
    observed: dict[str, float] = {}
    for path in (
        *(data_dir / "live").glob("*.json"),
        *(data_dir / "runs").glob("*/checkpoint.sqlite"),
    ):
        run_id = path.parent.name if path.name == "checkpoint.sqlite" else path.stem
        if not _RUN_ID.fullmatch(run_id):
            continue
        try:
            observed[run_id] = max(observed.get(run_id, 0), path.stat().st_mtime)
        except OSError:
            continue
    return heapq.nlargest(limit, observed, key=observed.__getitem__)


def restore_snapshot(data_dir: Path, run_id: str) -> dict[str, Any]:
    path = snapshot_path(data_dir, run_id)
    directory = (data_dir / "runs" / run_id).resolve()
    if directory.parent != (data_dir / "runs").resolve():
        raise KeyError(run_id)
    if has_checkpoint(directory):
        try:
            state = recovery_snapshot(data_dir, run_id)
            # Preserve the published live revision floor, taking all facts from
            # the checkpoint. Its internal write count is a separate counter.
            if path.is_file():
                try:
                    previous = json.loads(path.read_text(encoding="utf-8"))
                    floor = previous.get("revision") if isinstance(previous, dict) else None
                    if isinstance(floor, int) and not isinstance(floor, bool):
                        state["revision"] = max(state["revision"], floor)
                except (OSError, ValueError):
                    pass
            schema = state.pop("_presentation_schema", None)
            if schema is None:
                schema = CheckpointStore(directory).load().get("schema", {})
            state["presentation"] = build_presentation(
                schema,
                state.get("records", []),
                question=state.get("question") or "",
                ready=state["status"] in TERMINAL,
            )
        except sqlite3.Error as exc:
            raise PipelineError("Run checkpoint is damaged or unavailable") from exc
        return _run_actions(state, directory)
    summary = directory / "summary.json"
    report = None
    if summary.is_file():
        try:
            report = json.loads(summary.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeError):
            pass
    state = None
    if path.is_file():
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeError):
            pass
    if state is not None and not report:
        schema = state.pop("_presentation_schema", None)
        active = is_run_active(directory)
        state.update(resume_available=False, run_active=active)
        if state["status"] not in TERMINAL and not active:
            state.update(status="failed", stage="interrupted", finished_at=_now())
            state["stop_reason"] = "process_interrupted"
            state["errors"].append(
                {"stage": "run", "message": "Server stopped before run completion"}
            )
        state["presentation"] = build_presentation(
            schema if isinstance(schema, dict) else saved_schema(data_dir, run_id),
            state.get("records", []),
            question=state.get("question") or "",
            ready=state["status"] in TERMINAL,
        )
        return _run_actions(state, directory)
    if not report:
        raise KeyError(run_id)
    job = json.loads((directory / "job.json").read_text(encoding="utf-8"))
    records_path = directory / "records.jsonl"
    records = (
        [json.loads(line) for line in records_path.read_text(encoding="utf-8").splitlines()]
        if records_path.is_file()
        else []
    )
    if state and len(state.get("records", [])) == len(records):
        # Keep the live envelope shape only when every authoritative final fact agrees.
        by_id = {row["record_id"]: row for row in records}
        fields = ("record_id", "job_name", "data", "img_url", "provenance")
        if all(
            row.get("record_id") in by_id
            and all(row.get(key) == by_id[row["record_id"]].get(key) for key in fields)
            for row in state["records"]
        ):
            records = state["records"]
    return _run_actions(
        {
            "run_id": run_id,
            "job_name": report["job_name"],
            "question": job["config"].get("search"),
            "status": report["status"],
            "stop_reason": report.get("stop_reason"),
            "stop_message": report.get("stop_message"),
            "credit_limit_source": report.get("credit_limit_source"),
            "resume_available": False,
            "stage": "finished",
            "started_at": report["started_at"],
            "finished_at": report["finished_at"],
            "counters": {
                "pages_attempted": report["pages_attempted"],
                "pages_fetched": report["pages_fetched"],
                "records_found": len(records),
                "invalid_records": report["invalid_records"],
            },
            "limits": {
                key: job["config"]["crawl" if key != "max_calls" else "ai"][key]
                for key in ("max_pages", "max_records", "max_calls")
            },
            "records": records,
            "logs": [],
            "errors": report["errors"],
            "warnings": report["warnings"],
            "ai": report["ai"],
            "report": report,
            "revision": 1,
            "persisted": bool(report["records_written"]),
            "records_persisted": report["records_written"],
            "presentation": build_presentation(
                saved_schema(data_dir, run_id, job),
                records,
                question=job["config"].get("search") or "",
                ready=True,
            ),
        },
        directory,
    )

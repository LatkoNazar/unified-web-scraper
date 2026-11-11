"""In-memory run state shared by the supervisor and snapshot serialization."""

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from unified_web_parser.core.config import LoadedJob

TERMINAL = {"success", "partial", "failed", "cancelled", "stopped", "interrupted"}


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class _LiveRun:
    state: dict[str, Any]
    records: dict[str, dict[str, Any]] = field(default_factory=dict)
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    schema: dict[str, Any] = field(default_factory=dict)
    presentation: dict[str, Any] | None = None


def initial_state(job: LoadedJob, question: str, run_id: str) -> dict[str, Any]:
    """Construct the original public snapshot envelope for a newly queued run."""
    return {
        "run_id": run_id,
        "job_name": job.config.name,
        "question": question,
        "status": "queued",
        "stop_reason": None,
        "stop_message": None,
        "credit_limit_source": None,
        "resume_available": False,
        "update_available": False,
        "incremental_enabled": job.config.incremental.enabled,
        "parent_run_id": job.update_from_run.name if job.update_from_run else None,
        "incremental": {},
        "stage": "queued",
        "started_at": _now(),
        "finished_at": None,
        "counters": {
            "pages_attempted": 0,
            "pages_fetched": 0,
            "records_found": 0,
            "invalid_records": 0,
        },
        "limits": {
            "max_pages": job.config.crawl.max_pages,
            "max_records": job.config.crawl.max_records,
            "max_calls": job.config.ai.max_calls,
        },
        "records": [],
        "logs": [],
        "errors": [],
        "warnings": [],
        "ai": {},
        "report": None,
        "revision": 1,
        "persisted": False,
        "records_persisted": 0,
    }

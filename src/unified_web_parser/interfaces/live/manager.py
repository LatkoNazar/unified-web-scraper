"""Supervise bounded run tasks and publish coalesced reconnectable snapshots."""

import asyncio
import json
import sqlite3
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable
from copy import deepcopy
from pathlib import Path
from typing import Any
from uuid import uuid4

from unified_web_parser.core.config import LoadedJob
from unified_web_parser.core.exceptions import PipelineError
from unified_web_parser.interfaces.live.snapshots import (
    live_snapshot,
    persist_snapshot,
    recent_ids,
    restore_snapshot,
    saved_schema,
    snapshot_path,
)
from unified_web_parser.interfaces.live.state import (
    TERMINAL,
    _LiveRun,
    _now,
    initial_state,
)
from unified_web_parser.pipeline.checkpoints import CheckpointStore, has_checkpoint, load_resume_job
from unified_web_parser.pipeline.runner import PipelineRunner
from unified_web_parser.storage.run_lock import RunLease


class RunCapacityError(RuntimeError):
    """The configured active-run bound is already occupied."""


class LiveRunManager:
    def __init__(
        self,
        data_dir: Path,
        *,
        max_active: int = 1,
        max_retained: int = 20,
        max_logs: int = 200,
        runner_factory: Callable[..., Any] = PipelineRunner,
    ):
        if not 1 <= max_active <= 2 or max_retained < max_active or max_logs < 1:
            raise ValueError("Invalid live-run bounds")
        self.data_dir = Path(data_dir).resolve()
        self.max_active = max_active
        self.max_retained = max_retained
        self.max_logs = max_logs
        self.runner_factory = runner_factory
        self.states: OrderedDict[str, _LiveRun] = OrderedDict()
        self.tasks: dict[str, asyncio.Task] = {}
        self._leases: dict[str, RunLease] = {}
        self.closing = False

    def submit(self, job: LoadedJob, question: str) -> dict[str, Any]:
        if self.closing or len(self.tasks) >= self.max_active:
            raise RunCapacityError("Active run limit reached")
        run_id = uuid4().hex
        state = initial_state(job, question, run_id)
        live = _LiveRun(state, schema=deepcopy(job.extraction_schema))
        self.states[run_id] = live
        try:
            self._persist(live)
        except OSError:
            self.states.pop(run_id)
            raise
        task = asyncio.create_task(self._execute(job, live), name=f"parser:{run_id}")
        self.tasks[run_id] = task
        task.add_done_callback(lambda done: self._done(run_id, done))
        self._trim()
        return self.snapshot(run_id)

    def resume(self, run_id: str) -> dict[str, Any]:
        """Reserve exclusive ownership before accepting a continuation request."""
        if self.closing or len(self.tasks) >= self.max_active:
            raise RunCapacityError("Active run limit reached")
        self._path(run_id)  # Validate the public identifier before resolving a run directory.
        directory = self.data_dir / "runs" / run_id
        if not has_checkpoint(directory):
            raise PipelineError("This run has no recoverable checkpoint")
        lease = RunLease(directory).acquire()
        try:
            previous = restore_snapshot(self.data_dir, run_id)
            # The lease is ours now; use saved work to decide whether continuation exists.
            job = load_resume_job(directory)
            if CheckpointStore(directory).load().get("completed", False):
                raise PipelineError("This run has already completed")
            state = {
                **previous,
                "status": "queued",
                "stage": "resuming",
                "report": None,
                "finished_at": None,
                "resume_available": False,
                "stop_reason": None,
                "stop_message": None,
                "credit_limit_source": None,
                "revision": previous.get("revision", 0) + 1,
            }
            live = _LiveRun(
                state,
                records={row["record_id"]: row for row in previous.get("records", [])},
                schema=deepcopy(job.extraction_schema),
            )
            self._persist(live)
            self.states[run_id] = live
            self._leases[run_id] = lease
            task = asyncio.create_task(
                self._execute(job, live, resume_dir=directory, lease=lease),
                name=f"parser:{run_id}",
            )
            self.tasks[run_id] = task
            task.add_done_callback(lambda done: self._done(run_id, done))
            self._trim()
            return self.snapshot(run_id)
        except BaseException:
            lease.release()
            raise

    async def update(self, run_id: str) -> dict[str, Any]:
        """Fork a completed collection into a new incremental delta run."""
        if self.closing or len(self.tasks) >= self.max_active:
            raise RunCapacityError("Active run limit reached")
        previous = self.snapshot(run_id)
        if not previous.get("update_available"):
            raise PipelineError("Finish or resume this run before starting an incremental update")
        from unified_web_parser.pipeline.incremental import load_update_job

        directory = self.data_dir / "runs" / run_id

        def load_parent():
            with RunLease(directory) as lease:
                return load_update_job(directory, lease=lease)

        try:
            job = await asyncio.to_thread(load_parent)
        except sqlite3.Error as exc:
            raise PipelineError("Run checkpoint cannot be restored safely") from exc
        return self.submit(job, job.config.search or job.config.name)

    def _trim(self) -> None:
        for run_id in list(self.states):
            if len(self.states) <= self.max_retained:
                break
            if run_id not in self.tasks:
                self.states.pop(run_id)

    def _touch(self, live: _LiveRun) -> None:
        live.state["revision"] += 1
        live.changed.set()

    def _event(self, live: _LiveRun, kind: str, data: dict[str, Any]) -> None:
        state = live.state
        if state["status"] in TERMINAL:
            return
        for name in (
            "stage",
            "counters",
            "errors",
            "warnings",
            "ai",
            "records_persisted",
            "stop_reason",
            "stop_message",
            "credit_limit_source",
            "incremental",
        ):
            if name in data:
                state[name] = data[name]
        state["persisted"] = bool(state["records_persisted"])
        if kind == "record":
            record = data["record"]
            if (
                record["record_id"] in live.records
                or len(live.records) < state["limits"]["max_records"]
            ):
                live.records[record["record_id"]] = record
                live.presentation = None
        elif kind == "schema":
            live.schema = deepcopy(data["schema"])
            live.presentation = None
        elif kind == "log":
            state["logs"].append(
                {
                    "time": _now(),
                    "level": data.get("level", "info"),
                    "message": str(data.get("message", ""))[:2000],
                }
            )
            del state["logs"][: -self.max_logs]
        elif kind == "finished":
            state["report"] = data["report"]
        self._touch(live)

    async def _execute(
        self,
        job: LoadedJob,
        live: _LiveRun,
        *,
        resume_dir: Path | None = None,
        lease: RunLease | None = None,
    ) -> None:
        state = live.state
        state["status"] = "running"
        self._touch(live)
        try:
            runner = self.runner_factory(
                job,
                data_dir=self.data_dir,
                on_event=lambda kind, data: self._event(live, kind, data),
            )
            options = {"resume_dir": resume_dir, "lease": lease} if resume_dir else {}
            report = await runner.run(run_id=state["run_id"], **options)
            state["report"] = report.model_dump(mode="json")
            self._finish(live, report.status)
        except asyncio.CancelledError:
            self._finish(live, "cancelled")
            raise
        except Exception as exc:
            self._finish(
                live,
                "failed",
                error={"stage": "run", "type": type(exc).__name__, "message": str(exc)},
            )

    def _finish(self, live: _LiveRun, status: str, *, error: dict[str, Any] | None = None) -> None:
        state = live.state
        report = state.get("report")
        if report:
            state.update(
                errors=deepcopy(report["errors"]),
                warnings=deepcopy(report["warnings"]),
                ai=report["ai"],
                stop_reason=report.get("stop_reason"),
                stop_message=report.get("stop_message"),
                credit_limit_source=report.get("credit_limit_source"),
                incremental=deepcopy(report.get("incremental", {})),
            )
            state["counters"].update(
                pages_attempted=report["pages_attempted"],
                pages_fetched=report["pages_fetched"],
                invalid_records=report["invalid_records"],
                records_found=len(live.records),
            )
            state["records_persisted"] = report["records_written"]
            state["persisted"] = bool(report["records_written"])
        state.update(status=status, stage="finished", finished_at=_now())
        if status == "cancelled" and not state.get("stop_reason"):
            state["stop_reason"] = "user_cancelled"
        live.presentation = None
        if error is not None:
            state["errors"].append(error)
        self._touch(live)
        try:
            self._persist(live)
        except OSError as exc:
            state["errors"].append(
                {
                    "stage": "live_storage",
                    "type": type(exc).__name__,
                    "message": "Unable to save the live run snapshot",
                }
            )
            if status != "cancelled" and not has_checkpoint(
                self.data_dir / "runs" / state["run_id"]
            ):
                state["status"] = "failed"
            self._touch(live)

    def _done(self, run_id: str, task: asyncio.Task) -> None:
        self.tasks.pop(run_id, None)
        lease = self._leases.pop(run_id, None)
        live = self.states.get(run_id)
        error = None if task.cancelled() else task.exception()
        try:
            if live is not None and live.state["status"] not in TERMINAL:
                # A task cancelled before its first await never enters _execute's handler.
                if task.cancelled():
                    self._finish(live, "cancelled")
                else:
                    self._finish(
                        live,
                        "failed",
                        error={"stage": "task", "message": str(error or "Run stopped")},
                    )
            if lease is not None and lease.held and live and live.state.get("report") is None:
                # Pre-start cancellation/factory failure still owns its reserved checkpoint.
                checkpoint = CheckpointStore(lease.run_dir)
                saved = checkpoint.load()
                report = {
                    **saved.get("report", {}),
                    "status": live.state["status"],
                    "stop_reason": live.state.get("stop_reason"),
                    "finished_at": live.state.get("finished_at"),
                    "errors": live.state["errors"],
                }
                checkpoint.update(
                    report=report,
                    phase="interrupted",
                    completed=False,
                    revision=saved.get("revision", 0) + 1,
                )
        except Exception as exc:
            if live:
                live.state["errors"].append(
                    {
                        "stage": "checkpoint",
                        "type": type(exc).__name__,
                        "message": "Unable to persist the stopped run checkpoint",
                    }
                )
                self._touch(live)
        finally:
            if lease is not None:
                lease.release()
        self._trim()

    def cancel(self, run_id: str) -> dict[str, Any]:
        task = self.tasks.get(run_id)
        if (
            task is not None
            and not task.done()
            and self.states[run_id].state["status"] not in TERMINAL | {"cancelling"}
        ):
            live = self.states[run_id]
            live.state.update(status="cancelling", stage="cancelling")
            self._touch(live)
            task.cancel()
        return self.snapshot(run_id)

    async def wait(self, run_id: str) -> None:
        task = self.tasks.get(run_id)
        if task is not None:
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                if not task.cancelled():
                    raise

    async def close(self) -> None:
        self.closing = True
        tasks = list(self.tasks.values())
        for run_id in list(self.tasks):
            self.cancel(run_id)
        await asyncio.gather(*tasks, return_exceptions=True)

    def _path(self, run_id: str) -> Path:
        return snapshot_path(self.data_dir, run_id)

    def _persist(self, live: _LiveRun) -> None:
        payload = {**self._snapshot(live), "_presentation_schema": live.schema}
        persist_snapshot(self._path(live.state["run_id"]), payload)

    @staticmethod
    def _snapshot(live: _LiveRun) -> dict[str, Any]:
        return live_snapshot(live)

    def _saved_schema(self, run_id: str, job: dict[str, Any] | None = None) -> dict[str, Any]:
        return saved_schema(self.data_dir, run_id, job)

    def recent(self, limit: int) -> list[dict[str, Any]]:
        ids = dict.fromkeys([*self.states, *recent_ids(self.data_dir, limit)])
        snapshots = []
        for run_id in ids:
            try:
                snapshot = self.snapshot(run_id)
            except (OSError, ValueError, KeyError, PipelineError):
                # A damaged historical run must not hide every other search.
                continue
            snapshots.append(
                {key: value for key, value in snapshot.items() if key not in {"records", "logs"}}
            )
        return [
            snapshot
            for snapshot in sorted(snapshots, key=lambda item: item["started_at"], reverse=True)[
                :limit
            ]
        ]

    def snapshot(self, run_id: str) -> dict[str, Any]:
        self._path(run_id)
        if run_id not in self.tasks and has_checkpoint(self.data_dir / "runs" / run_id):
            snapshot = restore_snapshot(self.data_dir, run_id)
            if run_id in self.states:
                snapshot["revision"] = max(
                    snapshot["revision"], self.states[run_id].state["revision"]
                )
            return snapshot
        if run_id in self.states:
            return self._snapshot(self.states[run_id])
        return restore_snapshot(self.data_dir, run_id)

    async def events(self, run_id: str, *, heartbeat: float = 15) -> AsyncIterator[str]:
        previous_snapshot = None
        while True:
            snapshot = self.snapshot(run_id)
            live = self.states.get(run_id)
            if live is not None:
                live.changed.clear()
            if snapshot != previous_snapshot:
                previous_snapshot = snapshot
                revision = snapshot["revision"]
                data = json.dumps(snapshot, ensure_ascii=False)
                yield f"event: snapshot\nid: {revision}\ndata: {data}\n\n"
            if snapshot["status"] in TERMINAL:
                return
            if live is None:
                if not snapshot.get("run_active"):
                    return
                # Another CLI/API worker owns the run; observe its durable checkpoints.
                await asyncio.sleep(min(heartbeat, 1))
                continue
            try:
                await asyncio.wait_for(live.changed.wait(), timeout=heartbeat)
            except TimeoutError:
                yield ": heartbeat\n\n"

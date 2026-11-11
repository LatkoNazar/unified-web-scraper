"""Durable request accounting and response recovery, separate from optional AI cache."""

import hashlib
import json
import math
import os
import re
from collections import Counter
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any
from uuid import uuid4

from unified_web_parser.core.exceptions import DurabilityError, ProviderError


class UsageJournalError(DurabilityError):
    """Durability failed; another paid request must not be sent through this journal."""


class RequestOutcomeUnknown(ProviderError):
    """A prior request may have been billed; automatic resubmission is blocked."""


def request_fingerprint(endpoint: str, request: dict[str, Any]) -> str:
    encoded = json.dumps(
        {"endpoint": endpoint, "request": request},
        sort_keys=True,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _usage(value: Any) -> dict[str, int | float]:
    if not isinstance(value, dict):
        return {}
    result = {}
    for key, aliases in {
        "input_tokens": ("prompt_tokens", "input_tokens"),
        "output_tokens": ("completion_tokens", "output_tokens"),
    }.items():
        for alias in aliases:
            if type(value.get(alias)) is int and value[alias] >= 0:
                result[key] = value[alias]
                break
    cost = value.get("cost")
    if type(cost) in {int, float} and math.isfinite(cost) and cost >= 0:
        result["cost_usd"] = cost
    details = value.get("completion_tokens_details")
    if isinstance(details, dict) and type(details.get("reasoning_tokens")) is int:
        if details["reasoning_tokens"] >= 0:
            result["reasoning_tokens"] = details["reasoning_tokens"]
    counters = [
        value[alias].get("web_search_requests")
        for alias in ("server_tool_use_details", "server_tool_use")
        if isinstance(value.get(alias), dict)
    ]
    if counters and all(type(count) is int and count >= 0 for count in counters):
        if len(set(counters)) == 1:
            result["web_search_requests"] = counters[0]
    return result


class UsageJournal:
    """One run's fsynced metadata log plus atomic, non-optional response spool.

    The log contains no request bodies, headers, keys or prompts. The separate
    response spool contains model outputs and therefore remains local run data.
    A run lease must exclude a second process writing the same journal.
    """

    def __init__(self, path: Path, *, read_only: bool = False):
        self.path = Path(path)
        self.read_only = read_only
        self.responses_dir = self.path.parent / (self.path.stem + "-responses")
        self._lock = RLock()
        self._attempts: dict[str, dict[str, Any]] = {}
        self._active: set[str] = set()
        self._credit_retry_attempts: set[str] = set()
        self._resume_count = 0
        self._failed = False
        self.recovery_hits = 0
        self.last_replayed_at: str | None = None
        self.quarantined_tail: Path | None = None
        try:
            if self.path.exists():
                raw = self.path.read_bytes()
                # A killed append cannot authorize HTTP until its complete,
                # newline-terminated intent has been flushed and fsynced.
                end = raw.rfind(b"\n") + 1
                if end != len(raw) and not read_only:
                    quarantine = self.path.with_name(self.path.name + ".torn-" + uuid4().hex)
                    with quarantine.open("xb") as stream:
                        stream.write(raw[end:])
                        stream.flush()
                        os.fsync(stream.fileno())
                    self.quarantined_tail = quarantine
                    with self.path.open("r+b") as stream:
                        stream.truncate(end)
                        stream.flush()
                        os.fsync(stream.fileno())
                for line in raw[:end].splitlines():
                    self._apply(json.loads(line))
            # A kill between atomic response replacement and log append must
            # still recover both the paid output and its reported usage.
            for attempt_id, state in self._attempts.items():
                spool = self.responses_dir / f"{attempt_id}.json"
                if spool.exists():
                    payload = json.loads(spool.read_text(encoding="utf-8"))
                    if (
                        payload.get("attempt_id") != attempt_id
                        or payload.get("fingerprint") != state["fingerprint"]
                        or type(payload.get("status_code")) is not int
                        or not isinstance(payload.get("envelope"), dict | type(None))
                    ):
                        raise ValueError("Invalid response spool binding")
                    state["response"] = payload
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise UsageJournalError("Unable to recover the durable AI request journal") from exc

    def _apply(self, event: dict[str, Any]) -> None:
        if not isinstance(event, dict) or event.get("version") != 1:
            raise ValueError("Invalid journal event")
        if event.get("event") == "resume":
            attempts = event.get("credit_retry_attempts")
            if not isinstance(attempts, list) or any(
                not isinstance(attempt, str) or attempt not in self._attempts
                for attempt in attempts
            ):
                raise ValueError("Invalid credit retry boundary")
            self._credit_retry_attempts.update(attempts)
            self._resume_count += 1
            return
        attempt_id = event.get("attempt_id")
        if not isinstance(attempt_id, str) or not re.fullmatch(r"[a-f0-9]{32}", attempt_id):
            raise ValueError("Invalid request identifier")
        if event.get("event") == "started":
            if attempt_id in self._attempts:
                raise ValueError("Repeated request intent")
            self._attempts[attempt_id] = dict(event)
        elif event.get("event") in {"response", "failure"}:
            state = self._attempts[attempt_id]
            state[event["event"]] = dict(event)
        else:
            raise ValueError("Invalid journal event type")

    def _append(self, event: dict[str, Any]) -> None:
        if self.read_only:
            raise UsageJournalError("The AI request journal was opened read-only")
        if self._failed:
            raise UsageJournalError("The durable AI request journal is unavailable")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            encoded = (json.dumps(event, allow_nan=False, separators=(",", ":")) + "\n").encode()
            with self.path.open("ab") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            self._apply(event)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            self._failed = True
            raise UsageJournalError("Unable to persist the durable AI request journal") from exc

    def begin(
        self, *, model: str, endpoint: str, fingerprint: str, stage: str | None = None
    ) -> str:
        with self._lock:
            if not re.fullmatch(r"[a-f0-9]{64}", fingerprint):
                raise UsageJournalError("Invalid durable request fingerprint")
            attempt_id = uuid4().hex
            self._append(
                {
                    "version": 1,
                    "event": "started",
                    "attempt_id": attempt_id,
                    "at": _now(),
                    "model": model,
                    "endpoint": endpoint,
                    "fingerprint": fingerprint,
                    "stage": stage,
                }
            )
            self._active.add(attempt_id)
            return attempt_id

    def begin_resume(self) -> None:
        """Explicit user resume may retry known credit rejections, never unknown sends.

        Call under the run lease before constructing the restored RequestBudget.
        Historical attempts and their possibly unknown billing remain unchanged.
        """
        with self._lock:
            allowed = [
                attempt
                for attempt, state in self._attempts.items()
                if self._credit(state) is not None
            ]
            self._append(
                {
                    "version": 1,
                    "event": "resume",
                    "at": _now(),
                    "credit_retry_attempts": allowed,
                }
            )

    @staticmethod
    def _credit(state: dict[str, Any]):
        from unified_web_parser.providers.openrouter import _credit_error

        response = state.get("response", {})
        return _credit_error(response.get("status_code", 0), response.get("envelope"))

    def response(
        self, attempt_id: str, *, status_code: int, envelope: dict[str, Any] | None
    ) -> None:
        with self._lock:
            if self.read_only or self._failed:
                raise UsageJournalError("The durable AI request journal is not writable")
            state = self._attempts[attempt_id]
            payload = {
                "version": 1,
                "attempt_id": attempt_id,
                "fingerprint": state["fingerprint"],
                "status_code": status_code,
                "at": _now(),
                "envelope": deepcopy(envelope),
            }
            temporary = self.responses_dir / f".{attempt_id}.{uuid4().hex}.tmp"
            try:
                self.responses_dir.mkdir(parents=True, exist_ok=True)
                with temporary.open("x", encoding="utf-8") as stream:
                    json.dump(payload, stream, ensure_ascii=False, allow_nan=False)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.responses_dir / f"{attempt_id}.json")
                state["response"] = payload
                response_id = envelope.get("id") if envelope else None
                # Keep only conventional opaque provider identifiers in the log.
                if not isinstance(response_id, str) or not re.fullmatch(
                    r"[\w.:/-]{1,256}", response_id
                ):
                    response_id = None
                self._append(
                    {
                        "version": 1,
                        "event": "response",
                        "attempt_id": attempt_id,
                        "at": payload["at"],
                        "status_code": status_code,
                        "response_id": response_id,
                        "usage": _usage(envelope.get("usage") if envelope else None),
                    }
                )
                # _apply installs the log metadata; keep the durable envelope too.
                state["response"] = payload
            except (OSError, ValueError, TypeError) as exc:
                self._failed = True
                raise UsageJournalError("Unable to persist the paid AI response") from exc
            finally:
                self._active.discard(attempt_id)
                temporary.unlink(missing_ok=True)

    def failure(self, attempt_id: str, *, kind: str, retry_safe: bool) -> None:
        with self._lock:
            self._active.discard(attempt_id)
            self._append(
                {
                    "version": 1,
                    "event": "failure",
                    "attempt_id": attempt_id,
                    "at": _now(),
                    "kind": kind,
                    "retry_safe": retry_safe,
                }
            )

    def lookup(self, fingerprint: str) -> tuple[int, dict[str, Any] | None] | None:
        with self._lock:
            matches = [
                state for state in self._attempts.values() if state["fingerprint"] == fingerprint
            ]
            if not matches:
                return None
            state = matches[-1]
            if (
                state["attempt_id"] in self._credit_retry_attempts
                and self._credit(state) is not None
            ):
                return None
            response = state.get("response")
            if response is not None and "envelope" in response:
                self.recovery_hits += 1
                self.last_replayed_at = response.get("at")
                return response["status_code"], deepcopy(response["envelope"])
            if state.get("failure", {}).get("retry_safe") is True:
                return None
            raise RequestOutcomeUnknown(
                "A previous AI request has an unknown outcome; "
                "automatic paid resubmission is blocked"
            )

    def summary(self) -> dict[str, Any]:
        with self._lock:
            states = list(self._attempts.values())
            totals: dict[str, Any] = {
                "calls": len(states),
                "cache_hits": self.recovery_hits,
                "input_tokens": 0,
                "output_tokens": 0,
                "known_cost_usd": 0.0,
                "cost_pending_calls": 0,
                "in_flight_calls": len(self._active),
                "stages": {},
            }
            outcomes = Counter()
            for state in states:
                response = state.get("response", {})
                envelope = response.get("envelope")
                usage = _usage(envelope.get("usage") if isinstance(envelope, dict) else None)
                if not usage:
                    usage = response.get("usage", {})
                safe_failure = state.get("failure", {}).get("retry_safe") is True
                known = "cost_usd" in usage or safe_failure
                outcomes["response" if response else "not_sent" if safe_failure else "unknown"] += 1
                totals["input_tokens"] += usage.get("input_tokens", 0)
                totals["output_tokens"] += usage.get("output_tokens", 0)
                totals["known_cost_usd"] += usage.get("cost_usd", 0)
                totals["cost_pending_calls"] += not known
                for name in ("reasoning_tokens", "web_search_requests"):
                    if name in usage:
                        totals[name] = totals.get(name, 0) + usage[name]
                stage = state.get("stage") or state["endpoint"]
                group = totals["stages"].setdefault(
                    stage,
                    {
                        "calls": 0,
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "known_cost_usd": 0.0,
                        "cost_pending_calls": 0,
                        "models": [],
                    },
                )
                group["calls"] += 1
                group["input_tokens"] += usage.get("input_tokens", 0)
                group["output_tokens"] += usage.get("output_tokens", 0)
                group["known_cost_usd"] += usage.get("cost_usd", 0)
                group["cost_pending_calls"] += not known
                if state["model"] not in group["models"]:
                    group["models"].append(state["model"])
            totals["cost_complete"] = totals["cost_pending_calls"] == 0
            if totals["cost_complete"]:
                totals["cost_usd"] = totals["known_cost_usd"]
            totals["request_outcomes"] = dict(outcomes)
            totals["resume_boundaries"] = self._resume_count
            for group in totals["stages"].values():
                group["cost_complete"] = group["cost_pending_calls"] == 0
                if group["cost_complete"]:
                    group["cost_usd"] = group["known_cost_usd"]
                group["model"] = group["models"][0] if len(group["models"]) == 1 else None
            return totals

    def credit_limit_source(self) -> tuple[bool, str | None]:
        for state in self._attempts.values():
            if state["attempt_id"] in self._credit_retry_attempts:
                continue
            credit = self._credit(state)
            if credit is not None:
                return True, credit.limit_source
        return False, None

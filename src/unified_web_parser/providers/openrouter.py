"""Small, bounded OpenRouter JSON client; credentials never enter logs or cache keys."""

import asyncio
import hashlib
import json
import math
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from threading import Lock
from typing import Any, Literal

import httpx
from jsonschema import ValidationError
from jsonschema.validators import validator_for
from pydantic import SecretStr

from unified_web_parser.core.exceptions import CreditsExhausted, ProviderError
from unified_web_parser.providers.usage_journal import (
    RequestOutcomeUnknown,
    UsageJournal,
    request_fingerprint,
)


class RequestBudget:
    """A shared, thread-safe cap on attempts across provider-backed pipeline stages."""

    def __init__(self, max_calls: int, journal: UsageJournal | None = None) -> None:
        if isinstance(max_calls, bool) or not isinstance(max_calls, int) or max_calls < 0:
            raise ValueError("max_calls must be a nonnegative integer")
        self.max_calls = max_calls
        self.journal = journal
        self._used = journal.summary()["calls"] if journal is not None else 0
        self._lock = Lock()
        self._credit_source: str | None = None
        self._credits_stopped = False
        self._observers: list[Callable[[], None]] = []
        if journal is not None:
            self._credits_stopped, self._credit_source = journal.credit_limit_source()

    @property
    def used(self) -> int:
        with self._lock:
            return self._used

    @property
    def remaining(self) -> int:
        return self.max_calls - self.used

    def consume(self) -> None:
        with self._lock:
            if self._credits_stopped:
                raise CreditsExhausted(self._credit_source)
            if self._used >= self.max_calls:
                raise ProviderError(f"OpenRouter request budget exhausted ({self.max_calls})")
            self._used += 1

    @property
    def stop_reason(self) -> str | None:
        return "credits_exhausted" if self._credits_stopped else None

    @property
    def credit_limit_source(self) -> str | None:
        return self._credit_source

    def check_stop(self) -> None:
        if self._credits_stopped:
            raise CreditsExhausted(self._credit_source)

    def stop_credits(self, limit_source: str | None) -> None:
        with self._lock:
            self._credits_stopped = True
            self._credit_source = CreditsExhausted(limit_source).limit_source
        self.notify()

    def observe(self, callback: Callable[[], None]) -> Callable[[], None]:
        self._observers.append(callback)

        def remove() -> None:
            if callback in self._observers:
                self._observers.remove(callback)

        return remove

    def notify(self) -> None:
        for callback in tuple(self._observers):
            try:
                callback()
            except Exception:
                # Observers must never retry or invalidate an already billed call.
                pass


def _credit_error(status: int, envelope: dict[str, Any] | None) -> CreditsExhausted | None:
    """Branch on documented structured codes/limit sources, never message substrings."""
    error = (envelope or {}).get("error")
    error = error if isinstance(error, dict) else {}
    metadata = error.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    source = metadata.get("limit_source")
    code = error.get("code")
    payment_code = type(code) is int and code == 402 or code == "402"
    error_type = error.get("error_type", error.get("type"))
    payment_type = isinstance(error_type, str) and error_type in {
        "payment_required",
        "insufficient_credits",
    }
    credit_source = isinstance(source, str) and source in {
        "openrouter_credits",
        "openrouter_key_limit",
        "openrouter_in_flight_budget",
    }
    if status == 402 or payment_code or payment_type or credit_source:
        return CreditsExhausted(source)
    return None


def _wire_schema(value: Any) -> Any:
    """Avoid combinatorial array bounds in provider grammars; enforce them locally."""
    if isinstance(value, dict):
        return {
            key: _wire_schema(child)
            for key, child in value.items()
            if key not in {"maxItems", "minItems"} or not isinstance(child, int)
        }
    if isinstance(value, list):
        return [_wire_schema(child) for child in value]
    return value


def _retry_delay(response: httpx.Response | None, attempt: int) -> float:
    """Respect Retry-After within a ten-second per-attempt wait bound."""
    delay = min(2.0 * 2**attempt, 10.0)
    if response is None:
        return delay
    retry_after = response.headers.get("Retry-After")
    if retry_after is not None:
        try:
            requested = float(retry_after)
        except ValueError:
            try:
                deadline = parsedate_to_datetime(retry_after)
                requested = (deadline - datetime.now(UTC)).total_seconds()
            except (ValueError, TypeError, OverflowError):
                return delay
        if requested >= 0:
            return min(max(requested, 2.0), 10.0)
    return delay


def _http_failure(status: int, attempts: int) -> str:
    categories = {
        400: "invalid request or response schema",
        401: "authentication rejected",
        402: "insufficient credit",
        403: "access denied",
        408: "request timed out",
        429: "rate limited",
        500: "provider server error",
        502: "upstream provider error",
        503: "provider unavailable",
        504: "upstream timeout",
    }
    category = categories.get(status, "request rejected")
    return f"OpenRouter returned HTTP {status} ({category}; {attempts} attempt(s))"


class OpenRouterClient:
    def __init__(
        self,
        api_key: str | SecretStr,
        model: str,
        max_calls: int = 30,
        cache_dir: Path | None = None,
        timeout: float = 60,
        client: httpx.AsyncClient | None = None,
        budget: RequestBudget | None = None,
        response_mode: Literal["auto", "json_schema", "json_object"] = "auto",
        temperature: float | None = 0,
        reasoning: bool | None = None,
        max_output_tokens: int = 6000,
        stage: str | None = None,
    ) -> None:
        self._api_key = api_key.get_secret_value() if isinstance(api_key, SecretStr) else api_key
        if not self._api_key:
            raise ProviderError("OPENROUTER_API_KEY is empty")
        if max_calls < 1:
            raise ValueError("max_calls must be positive")
        if reasoning is not None and not isinstance(reasoning, bool):
            raise ValueError("reasoning must be a boolean or None")
        if (
            isinstance(max_output_tokens, bool)
            or not isinstance(max_output_tokens, int)
            or max_output_tokens < 1
        ):
            raise ValueError("max_output_tokens must be a positive integer")
        self.model = model
        self.stage = stage
        if response_mode not in {"auto", "json_schema", "json_object"}:
            raise ValueError("response_mode must be auto, json_schema or json_object")
        self.response_mode = (
            "json_object"
            if response_mode == "auto" and model == "qwen/qwen3.7-flash"
            else "json_schema"
            if response_mode == "auto"
            else response_mode
        )
        self.temperature = None if model.startswith("openai/gpt-6") else temperature
        self.reasoning = reasoning
        self.max_output_tokens = max_output_tokens
        self.max_calls = max_calls
        self.budget = budget if budget is not None else RequestBudget(max_calls)
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._owns_client = client is None
        self.calls = 0
        self.in_flight_calls = 0
        self.cache_hits = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.reasoning_tokens = 0
        self._reasoning_usage_responses = 0
        self._cost_usage_responses = 0
        self._cost_usd_total = 0.0
        self._last_journal_replay = False
        self._last_journal_response_at: str | None = None

    @property
    def stats(self) -> dict[str, int | float]:
        result: dict[str, int | float] = {
            "calls": self.calls,
            "cache_hits": self.cache_hits,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "known_cost_usd": self._cost_usd_total,
            "cost_complete": self._cost_usage_responses == self.calls,
            "cost_pending_calls": max(0, self.calls - self._cost_usage_responses),
            "in_flight_calls": self.in_flight_calls,
        }
        # Missing metadata is unknown, not a zero-cost/zero-reasoning observation.
        # Publish a total only when every actual attempt reported that metric.
        if self.calls and self._reasoning_usage_responses == self.calls:
            result["reasoning_tokens"] = self.reasoning_tokens
        if self.calls and self._cost_usage_responses == self.calls:
            result["cost_usd"] = self._cost_usd_total
        elif self.calls == 0 and self.cache_hits:
            result["cost_usd"] = 0.0
        return result

    async def __aenter__(self) -> "OpenRouterClient":
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def complete_json(
        self,
        system: str,
        payload: dict[str, Any],
        schema: dict[str, Any],
        name: str = "structured_result",
    ) -> dict[str, Any]:
        self.budget.check_stop()
        if self.model.endswith("-decisions"):
            raise ProviderError(
                "Native Decisions models require OpenRouterDecisionsClient.decide()"
            )
        validator = validator_for(schema)(schema)
        request = {
            "model": self.model,
            "max_completion_tokens": self.max_output_tokens,
            "messages": [
                {"role": "system", "content": system},
                {
                    "role": "user",
                    "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                },
            ],
            # Arbitrary user schemas can have optional fields; validate locally regardless.
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": name,
                    "strict": False,
                    "schema": _wire_schema(schema),
                },
            },
            "provider": {"require_parameters": True},
        }
        if self.temperature is not None:
            request["temperature"] = self.temperature
        if self.reasoning is not None:
            request["reasoning"] = {"enabled": self.reasoning}
        if self.response_mode == "json_object":
            request["response_format"] = {"type": "json_object"}
            request["messages"][0]["content"] += (
                " Return a JSON object that conforms to this full JSON Schema: "
                + json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
            )
        endpoint = "https://openrouter.ai/api/v1/chat/completions"
        fingerprint = hashlib.sha256(
            json.dumps(
                {"endpoint": endpoint, "request": request},
                sort_keys=True,
            ).encode()
        ).hexdigest()
        cache_path = self.cache_dir / f"{fingerprint}.json" if self.cache_dir else None
        if cache_path and cache_path.is_file():
            try:
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
                validator.validate(cached)
                if isinstance(cached, dict):
                    self.cache_hits += 1
                    self.budget.notify()
                    return cached
            except (OSError, ValueError, ValidationError):
                pass
        envelope = await self._post_json(endpoint, request)
        try:
            choice = envelope["choices"][0]
            if choice.get("finish_reason") == "length":
                raise ProviderError("OpenRouter JSON response exceeded output token limit")
            content = choice["message"]["content"]
            result = json.loads(content) if isinstance(content, str) else content
            validator.validate(result)
            if not isinstance(result, dict):
                raise ProviderError("OpenRouter response must be a JSON object")
        except (ValueError, KeyError, TypeError, IndexError, ValidationError) as exc:
            raise ProviderError("OpenRouter returned invalid structured JSON") from exc
        if cache_path:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        return result

    def _record_usage(self, usage: Any) -> None:
        if not isinstance(usage, dict):
            return
        for attribute, aliases in (
            ("input_tokens", ("prompt_tokens", "input_tokens")),
            ("output_tokens", ("completion_tokens", "output_tokens")),
        ):
            value = next(
                (usage[key] for key in aliases if type(usage.get(key)) is int and usage[key] >= 0),
                None,
            )
            if type(value) is int and value >= 0:
                setattr(self, attribute, getattr(self, attribute) + value)
        self._record_optional_usage(usage)

    async def _attempt_json(
        self, endpoint: str, request: dict[str, Any], *, fingerprint: str | None = None
    ) -> tuple[httpx.Response, dict[str, Any] | None]:
        self.budget.check_stop()
        if self.calls >= self.max_calls:
            raise ProviderError(f"OpenRouter request budget exhausted ({self.max_calls})")
        self.budget.consume()
        journal = self.budget.journal
        attempt_id = (
            journal.begin(
                model=self.model,
                endpoint=endpoint,
                fingerprint=fingerprint or request_fingerprint(endpoint, request),
                stage=self.stage,
            )
            if journal is not None
            else None
        )
        self.calls += 1
        self.in_flight_calls += 1
        self.budget.notify()
        try:
            response = await self._client.post(
                endpoint,
                json=request,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
            )
            try:
                envelope = response.json()
            except (ValueError, TypeError):
                envelope = None
            if not isinstance(envelope, dict):
                envelope = None
            if envelope is not None:
                self._record_usage(envelope.get("usage"))
            if journal is not None:
                durable_envelope = envelope
                if envelope is not None and isinstance(envelope.get("error"), dict):
                    # Error bodies may echo request content. Recovery only needs
                    # structured classification, never arbitrary provider text.
                    error = envelope["error"]
                    credit_error = _credit_error(response.status_code, envelope)
                    durable_envelope = {"error": {}}
                    code = error.get("code")
                    if type(code) is int or isinstance(code, str) and code.isdecimal():
                        durable_envelope["error"]["code"] = code
                    if credit_error is not None:
                        durable_envelope["error"]["code"] = 402
                        durable_envelope["error"]["metadata"] = {
                            "limit_source": credit_error.limit_source
                        }
                    if "usage" in envelope:
                        durable_envelope["usage"] = envelope["usage"]
                journal.response(
                    attempt_id, status_code=response.status_code, envelope=durable_envelope
                )
            credit = _credit_error(response.status_code, envelope)
            if credit is not None:
                self.budget.stop_credits(credit.limit_source)
                raise credit
            return response, envelope
        except httpx.RequestError as exc:
            if journal is not None:
                safe = isinstance(
                    exc, httpx.ConnectError | httpx.ConnectTimeout | httpx.PoolTimeout
                )
                journal.failure(attempt_id, kind=type(exc).__name__, retry_safe=safe)
                if not safe:
                    raise RequestOutcomeUnknown(
                        "The AI request outcome is unknown; automatic paid retry is blocked"
                    ) from exc
            raise
        except asyncio.CancelledError:
            if journal is not None:
                journal.failure(attempt_id, kind="cancelled", retry_safe=False)
            raise
        finally:
            self.in_flight_calls -= 1
            self.budget.notify()

    def _record_optional_usage(self, usage: dict[str, Any]) -> None:
        details = usage.get("completion_tokens_details")
        reasoning = details.get("reasoning_tokens") if isinstance(details, dict) else None
        if isinstance(reasoning, int) and not isinstance(reasoning, bool) and reasoning >= 0:
            self.reasoning_tokens += reasoning
            self._reasoning_usage_responses += 1
        cost = usage.get("cost")
        if (
            isinstance(cost, int | float)
            and not isinstance(cost, bool)
            and math.isfinite(cost)
            and cost >= 0
        ):
            self._cost_usd_total += cost
            self._cost_usage_responses += 1

    async def _post_json(self, endpoint: str, request: dict[str, Any]) -> dict[str, Any]:
        """Transport shared by chat and native Decisions, with one attempt budget."""
        fingerprint = request_fingerprint(endpoint, request)
        self._last_journal_replay = False
        self._last_journal_response_at = None
        journal = self.budget.journal
        if journal is not None:
            restored = journal.lookup(fingerprint)
            if restored is not None:
                self._last_journal_replay = True
                self._last_journal_response_at = journal.last_replayed_at
                self.cache_hits += 1
                self.budget.notify()
                status, envelope = restored
                if status != 200:
                    raise ProviderError(_http_failure(status, 1))
                if envelope is None:
                    raise ProviderError("OpenRouter returned invalid JSON response envelope")
                if envelope.get("error") is not None:
                    raise ProviderError("OpenRouter returned a structured provider error")
                return envelope
        for attempt in range(3):
            try:
                response, envelope = await self._attempt_json(
                    endpoint, request, fingerprint=fingerprint
                )
            except httpx.RequestError as exc:
                if attempt < 2:
                    if self.calls >= self.max_calls or self.budget.remaining <= 0:
                        raise ProviderError(
                            f"OpenRouter request budget exhausted ({self.max_calls})"
                        ) from exc
                    await asyncio.sleep(_retry_delay(None, attempt))
                    continue
                raise ProviderError("OpenRouter request failed after bounded retries") from exc
            if response.status_code in {408, 429, 500, 502, 503, 504} and attempt < 2:
                if self.calls >= self.max_calls or self.budget.remaining <= 0:
                    raise ProviderError(
                        f"OpenRouter request budget exhausted ({self.max_calls}); "
                        + _http_failure(response.status_code, attempt + 1)
                    )
                await asyncio.sleep(_retry_delay(response, attempt))
                continue
            if response.status_code != 200:
                raise ProviderError(_http_failure(response.status_code, attempt + 1))
            if envelope is None:
                raise ProviderError("OpenRouter returned invalid JSON response envelope")
            if envelope.get("error") is not None:
                raise ProviderError("OpenRouter returned a structured provider error")
            return envelope
        raise ProviderError("OpenRouter retry budget exhausted")

"""Native typed OpenRouter Decisions endpoint, independent of chat completion JSON.

Contract: https://openrouter.ai/openapi.json (DecisionsRequest/DecisionsResponse).
"""

import hashlib
import json
import math
from typing import Any

from unified_web_parser.core.exceptions import ProviderError
from unified_web_parser.providers.openrouter import OpenRouterClient


def _finite(value: Any, low: float = 0, high: float = 1) -> bool:
    return (
        isinstance(value, int | float)
        and not isinstance(value, bool)
        and math.isfinite(value)
        and low <= value <= high
    )


def validate_questions(questions: dict[str, Any]) -> None:
    if not isinstance(questions, dict) or not 1 <= len(questions) <= 200:
        raise ProviderError("Decisions requires 1 to 200 named questions")
    for name, question in questions.items():
        if not isinstance(name, str) or not name or not isinstance(question, dict):
            raise ProviderError("Invalid Decisions question")
        if set(question) - {"type", "instructions", "criteria"}:
            raise ProviderError("Unknown Decisions question fields")
        if not isinstance(question.get("instructions"), str | dict | list):
            raise ProviderError("Decisions question instructions are required")
        kind = question.get("type")
        criteria = question.get("criteria")
        if kind == "choice":
            if (
                not isinstance(criteria, dict)
                or not 1 <= len(criteria) <= 255
                or any(not isinstance(key, str) or not key for key in criteria)
            ):
                raise ProviderError("Choice questions require named criteria")
        elif kind == "noul":
            if criteria is not None and (
                not isinstance(criteria, dict) or set(criteria) != {"true", "false"}
            ):
                raise ProviderError("Noul criteria must define true and false")
        elif kind == "score":
            if not isinstance(criteria, list) or not criteria:
                raise ProviderError("Score questions require an ordered criteria list")
        else:
            raise ProviderError("Unsupported Decisions question type")


def validate_answers(answers: Any, questions: dict[str, Any]) -> dict[str, Any]:
    """Check optional confidence separately from each option's probability."""
    if not isinstance(answers, dict) or set(answers) != set(questions):
        raise ProviderError("Decisions answers must match exactly the requested questions")
    for name, question in questions.items():
        answer = answers[name]
        kind = question["type"]
        if not isinstance(answer, dict) or answer.get("type") != kind:
            raise ProviderError("Decisions answer type does not match its question")
        allowed = {"type", kind}
        if kind in {"choice", "score"}:
            allowed |= {"confidence", "probabilities"}
        if kind == "score":
            allowed.add("legend")
        if set(answer) - allowed:
            raise ProviderError("Unknown Decisions answer fields")
        if kind == "choice":
            options = set(question["criteria"])
            if not isinstance(answer.get("choice"), str) or answer["choice"] not in options:
                raise ProviderError("Decisions returned an unknown choice")
        elif kind == "noul":
            options = set()
            if not _finite(answer.get("noul")):
                raise ProviderError("Invalid Decisions noul probability")
        else:
            options = {str(index) for index in range(len(question["criteria"]))}
            if not _finite(answer.get("score"), 0, len(options) - 1):
                raise ProviderError("Invalid Decisions score")
            if "legend" in answer and (
                not isinstance(answer["legend"], dict) or set(answer["legend"]) - options
            ):
                raise ProviderError("Invalid Decisions score legend")
        if "confidence" in answer and not _finite(answer["confidence"]):
            raise ProviderError("Invalid Decisions confidence")
        if "probabilities" in answer:
            probabilities = answer["probabilities"]
            if (
                not isinstance(probabilities, dict)
                or set(probabilities) - options
                or any(not _finite(value) for value in probabilities.values())
            ):
                raise ProviderError("Invalid Decisions probability distribution")
    return answers


def _validated_response(response: Any, questions: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(response, dict) or set(response) - {
        "model",
        "answers",
        "usage",
        "id",
        "provider",
    }:
        raise ProviderError("Invalid native Decisions response envelope")
    if not isinstance(response.get("model"), str) or not response["model"]:
        raise ProviderError("Native Decisions response is missing model metadata")
    for key in ("id", "provider"):
        if key in response and not isinstance(response[key], str):
            raise ProviderError("Invalid native Decisions response metadata")
    usage = response.get("usage")
    if not isinstance(usage, dict) or set(usage) - {"input_tokens", "output_tokens", "cost"}:
        raise ProviderError("Invalid native Decisions usage metadata")
    for key in ("input_tokens", "output_tokens"):
        value = usage.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ProviderError("Invalid native Decisions token counts")
    if "cost" in usage and not _finite(usage["cost"], 0, float("inf")):
        raise ProviderError("Invalid native Decisions cost metadata")
    return validate_answers(response.get("answers"), questions)


class OpenRouterDecisionsClient(OpenRouterClient):
    """Same lifecycle/budget arguments as OpenRouterClient; sends no chat parameters."""

    endpoint = "https://openrouter.ai/api/alpha/decisions"

    async def decide(
        self, state: str | dict[str, Any] | list[Any], questions: dict[str, Any]
    ) -> dict[str, Any]:
        self.budget.check_stop()
        validate_questions(questions)
        if not isinstance(state, str | dict | list):
            raise ProviderError("Decisions state must be text, an object or an array")
        request = {"model": self.model, "state": state, "questions": questions}
        try:
            serialized = json.dumps(
                {"endpoint": self.endpoint, "request": request}, sort_keys=True, allow_nan=False
            )
        except (TypeError, ValueError) as exc:
            raise ProviderError("Decisions request must contain finite JSON data") from exc
        fingerprint = hashlib.sha256(serialized.encode()).hexdigest()
        cache_path = self.cache_dir / f"{fingerprint}.json" if self.cache_dir else None
        if cache_path and cache_path.is_file():
            try:
                envelope = json.loads(cache_path.read_text(encoding="utf-8"))
                answers = _validated_response(envelope, questions)
                self.cache_hits += 1
                self.budget.notify()
                self.response_model = envelope["model"]
                self.response_id = envelope.get("id")
                return answers
            except (OSError, ValueError, TypeError, ProviderError):
                pass
        envelope = await self._post_json(self.endpoint, request)
        answers = _validated_response(envelope, questions)
        self.response_model = envelope["model"]
        self.response_id = envelope.get("id")
        if cache_path:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(
                json.dumps(envelope, ensure_ascii=False, allow_nan=False), encoding="utf-8"
            )
        return answers

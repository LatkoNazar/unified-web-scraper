"""Structured credit limits latch globally; billing observations survive failed outputs."""

import asyncio

import httpx
import pytest

from unified_web_parser.core.exceptions import CreditsExhausted, ProviderError
from unified_web_parser.providers.decisions import OpenRouterDecisionsClient
from unified_web_parser.providers.openrouter import OpenRouterClient, RequestBudget, _credit_error
from unified_web_parser.providers.search import OpenRouterSearchClient

SCHEMA = {"type": "object", "required": ["answer"], "properties": {"answer": {"type": "integer"}}}
QUESTIONS = {
    "match": {
        "type": "choice",
        "instructions": "Match?",
        "criteria": {"yes": "Match", "no": "Other"},
    }
}


@pytest.mark.parametrize(
    "body",
    [
        {"error": {"code": 402}},
        {"error": {"code": "402", "metadata": {"limit_source": "openrouter_key_limit"}}},
        {"error": {"error_type": "payment_required"}},
        {"error": {"metadata": {"limit_source": "openrouter_in_flight_budget"}}},
    ],
)
def test_credit_classifier_accepts_only_structured_credit_signals(body):
    assert isinstance(_credit_error(200, body), CreditsExhausted)
    assert _credit_error(401, {"error": {"code": 401, "message": "credit secret"}}) is None
    assert _credit_error(429, {"error": {"code": 429, "message": "insufficient credits"}}) is None
    assert (
        _credit_error(400, {"error": {"code": True, "type": [], "metadata": {"limit_source": {}}}})
        is None
    )


@pytest.mark.parametrize("source", [{}, [], 8, None])
def test_http402_stops_even_when_optional_metadata_is_malformed(source):
    error = _credit_error(402, {"error": {"type": {}, "metadata": {"limit_source": source}}})
    assert isinstance(error, CreditsExhausted)
    assert error.limit_source is None


@pytest.mark.asyncio
async def test_credit_latch_blocks_chat_native_search_and_keeps_known_subtotal():
    responses = [
        httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"answer":1}'}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 3, "cost": 0.02},
            },
        ),
        httpx.Response(
            402,
            json={
                "error": {
                    "code": 402,
                    "message": "private-api-key",
                    "metadata": {"limit_source": "openrouter_key_limit"},
                }
            },
        ),
    ]
    sent, snapshots = [], []

    def transport(request):
        sent.append(request)
        return responses.pop(0)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        budget = RequestBudget(10)
        chat = OpenRouterClient("key", "model", client=http, budget=budget)
        native = OpenRouterDecisionsClient("key", "model-decisions", client=http, budget=budget)
        search = OpenRouterSearchClient("key", "model", client=http, budget=budget)
        budget.observe(lambda: snapshots.append(dict(chat.stats)))
        await chat.complete_json("Extract", {}, SCHEMA)
        with pytest.raises(CreditsExhausted) as stopped:
            await chat.complete_json("Another", {}, SCHEMA)
        with pytest.raises(CreditsExhausted):
            await native.decide({}, QUESTIONS)
        with pytest.raises(CreditsExhausted):
            await search.search("query", limit=1)
        assert len(sent) == budget.used == 2
        assert chat.stats["known_cost_usd"] == 0.02
        assert chat.stats["cost_complete"] is False and chat.stats["cost_pending_calls"] == 1
        assert "cost_usd" not in chat.stats
        assert "private-api-key" not in str(stopped.value)
        assert snapshots[0]["in_flight_calls"] == 1 and snapshots[-1]["in_flight_calls"] == 0
        assert any(snapshot["known_cost_usd"] == 0.02 for snapshot in snapshots)


@pytest.mark.asyncio
async def test_cost_from_retry_and_invalid_native_answer_is_recorded_once(monkeypatch):
    async def no_wait(_delay):
        pass

    monkeypatch.setattr(asyncio, "sleep", no_wait)
    responses = [
        httpx.Response(429, json={"error": {"code": 429}, "usage": {"cost": 0.001}}),
        httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"answer":1}'}}], "usage": {"cost": 0.002}},
        ),
        httpx.Response(
            200,
            json={
                "model": "model-decisions",
                "answers": {},
                "usage": {"input_tokens": 11, "output_tokens": 0, "cost": 0.004},
            },
        ),
    ]
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: responses.pop(0))
    ) as http:
        chat = OpenRouterClient("key", "model", client=http)
        chat.budget.observe(lambda: (_ for _ in ()).throw(RuntimeError("broken observer")))
        await chat.complete_json("Extract", {}, SCHEMA)
        assert chat.calls == 2 and chat.stats["cost_usd"] == pytest.approx(0.003)
        native = OpenRouterDecisionsClient("key", "model-decisions", client=http)
        with pytest.raises(ProviderError):
            await native.decide({}, QUESTIONS)
        assert native.calls == 1 and native.input_tokens == 11
        assert native.stats["cost_usd"] == 0.004


@pytest.mark.asyncio
async def test_200_credit_error_preserves_response_usage_and_budget_limit_is_distinct():
    body = {
        "error": {"code": 402},
        "usage": {"prompt_tokens": 3, "completion_tokens": 0, "cost": 0.01},
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=body))
    ) as http:
        client = OpenRouterClient("key", "model", client=http)
        with pytest.raises(CreditsExhausted):
            await client.complete_json("Extract", {}, SCHEMA)
        assert client.stats["cost_usd"] == 0.01 and client.input_tokens == 3
    with pytest.raises(ProviderError) as exhausted:
        RequestBudget(0).consume()
    assert not isinstance(exhausted.value, CreditsExhausted)


def test_token_aliases_use_first_valid_count_instead_of_null_or_duplicate_aliases():
    client = OpenRouterClient("key", "model")
    client._record_usage(
        {"prompt_tokens": None, "input_tokens": 12, "completion_tokens": None, "output_tokens": 4}
    )
    client._record_usage(
        {"prompt_tokens": 3, "input_tokens": 50, "completion_tokens": 1, "output_tokens": 20}
    )
    assert client.input_tokens == 15 and client.output_tokens == 5

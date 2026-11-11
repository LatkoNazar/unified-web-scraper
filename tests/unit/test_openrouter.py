import asyncio
import json

import httpx
import pytest

from unified_web_parser.core.exceptions import ProviderError
from unified_web_parser.providers.openrouter import OpenRouterClient, RequestBudget

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["answer"],
    "properties": {"answer": {"type": "integer"}},
}


def test_structured_request_cache_usage_and_secret_exclusion(tmp_path):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"answer": 7}'}}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 3},
            },
        )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            client = OpenRouterClient(
                "secret-test-key", "example/model", cache_dir=tmp_path, client=http
            )
            assert await client.complete_json("instruction", {"data": "evidence"}, SCHEMA) == {
                "answer": 7
            }
            assert await client.complete_json("instruction", {"data": "evidence"}, SCHEMA) == {
                "answer": 7
            }
            assert client.stats == {
                "calls": 1,
                "cache_hits": 1,
                "input_tokens": 20,
                "output_tokens": 3,
                "known_cost_usd": 0.0,
                "cost_complete": False,
                "cost_pending_calls": 1,
                "in_flight_calls": 0,
            }

    asyncio.run(run())
    request = json.loads(requests[0].content)
    assert request["response_format"]["type"] == "json_schema"
    assert request["provider"]["require_parameters"] is True
    assert requests[0].headers["authorization"] == "Bearer secret-test-key"
    assert all("secret-test-key" not in path.read_text() for path in tmp_path.iterdir())


@pytest.mark.parametrize("content", ["not json", '{"answer":"bad"}', "[]"])
def test_malformed_or_schema_invalid_provider_output_fails(content):
    async def run():
        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"choices": [{"message": {"content": content}}]}
            )
        )
        async with httpx.AsyncClient(transport=transport) as http:
            client = OpenRouterClient("secret", "model", client=http)
            with pytest.raises(ProviderError, match="invalid structured JSON"):
                await client.complete_json("system", {}, SCHEMA)

    asyncio.run(run())


def test_retry_attempts_respect_hard_budget():
    async def run():
        transport = httpx.MockTransport(lambda request: httpx.Response(503))
        async with httpx.AsyncClient(transport=transport) as http:
            client = OpenRouterClient("secret", "model", max_calls=1, client=http)
            with pytest.raises(ProviderError, match="budget exhausted"):
                await client.complete_json("system", {}, SCHEMA)
            assert client.calls == 1

    asyncio.run(run())


def test_provider_array_bounds_are_relaxed_only_on_wire():
    schema = {
        "type": "object",
        "properties": {"items": {"type": "array", "items": {"type": "integer"}, "maxItems": 1}},
    }
    captured = []

    def respond(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"items":[1,2]}'}}]})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            client = OpenRouterClient("secret", "model", client=http)
            with pytest.raises(ProviderError, match="invalid structured JSON"):
                await client.complete_json("system", {}, schema)

    asyncio.run(run())
    wire = captured[0]["response_format"]["json_schema"]["schema"]
    assert "maxItems" not in wire["properties"]["items"]
    assert schema["properties"]["items"]["maxItems"] == 1


@pytest.mark.parametrize(
    "retry_after, expected",
    [(None, [2.0, 4.0]), ("7", [7.0, 7.0]), ("99999", [10.0, 10.0]), ("invalid", [2.0, 4.0])],
)
def test_rate_limit_retry_after(monkeypatch, retry_after, expected):
    waits = []

    async def fake_sleep(seconds):
        waits.append(seconds)

    monkeypatch.setattr("unified_web_parser.providers.openrouter.asyncio.sleep", fake_sleep)

    async def run():
        headers = {"Retry-After": retry_after} if retry_after is not None else {}
        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                429,
                headers=headers,
                json={"error": {"message": "secret-test-key must never print"}},
            )
        )
        async with httpx.AsyncClient(transport=transport) as http:
            client = OpenRouterClient("secret-test-key", "model", max_calls=3, client=http)
            with pytest.raises(ProviderError, match=r"HTTP 429.*rate limited.*3 attempt") as error:
                await client.complete_json("system", {}, SCHEMA)
            assert "secret-test-key" not in str(error.value)

    asyncio.run(run())
    assert waits == expected


@pytest.mark.parametrize(
    "model,expected_mode,temperature",
    [
        ("qwen/qwen3.7-flash", "json_object", True),
        ("openai/gpt-6-luna", "json_schema", False),
        ("example/model", "json_schema", True),
    ],
)
def test_auto_response_capabilities_preserve_local_schema_validation(
    model, expected_mode, temperature
):
    captured = []

    def respond(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"answer":"bad"}'}}]})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            client = OpenRouterClient("secret", model, client=http)
            with pytest.raises(ProviderError, match="invalid structured JSON"):
                await client.complete_json("system", {"evidence": "text"}, SCHEMA)

    asyncio.run(run())
    assert captured[0]["response_format"]["type"] == expected_mode
    assert ("temperature" in captured[0]) is temperature
    if expected_mode == "json_object":
        schema_text = captured[0]["messages"][0]["content"].split(
            "Return a JSON object that conforms to this full JSON Schema: ", 1
        )[1]
        assert json.loads(schema_text) == SCHEMA
        assert "json_schema" not in captured[0]["response_format"]


def test_zero_shared_budget_blocks_request_before_transport():
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: pytest.fail("Zero-budget request reached transport")
            )
        ) as http:
            budget = RequestBudget(0)
            client = OpenRouterClient("secret", "model", client=http, budget=budget)
            with pytest.raises(ProviderError, match="budget exhausted"):
                await client.complete_json("system", {}, SCHEMA)
            assert client.calls == budget.used == budget.remaining == 0

    asyncio.run(run())


def test_native_decisions_model_cannot_accidentally_use_chat():
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: pytest.fail("Native model reached chat transport")
            )
        ) as http:
            client = OpenRouterClient("secret", "openai/gpt-6-luna-decisions", client=http)
            with pytest.raises(ProviderError, match="Native Decisions models require"):
                await client.complete_json("system", {}, SCHEMA)

    asyncio.run(run())

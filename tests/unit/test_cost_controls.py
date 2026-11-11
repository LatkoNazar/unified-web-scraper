import asyncio
import copy
import json

import httpx
import pytest

from unified_web_parser.core.models import CandidateBlock, RoutingContext
from unified_web_parser.providers.decisions import OpenRouterDecisionsClient
from unified_web_parser.providers.openrouter import OpenRouterClient
from unified_web_parser.routing.engines import LunaDecisionEngine

SCHEMA = {"type": "object", "properties": {"answer": {"type": "integer"}}, "required": ["answer"]}


def response(*, usage=None):
    result = {"choices": [{"message": {"content": '{"answer":7}'}}]}
    if usage is not None:
        result["usage"] = usage
    return httpx.Response(200, json=result)


@pytest.mark.parametrize("reasoning", [None, False, True])
def test_reasoning_controls_and_compact_json_preserve_values(reasoning):
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return response()

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            client = OpenRouterClient(
                "test-secret",
                "qwen/qwen3.7-flash",
                client=http,
                reasoning=reasoning,
                max_output_tokens=4096,
            )
            await client.complete_json(
                "Extract", {"description": "Clear text, with spaces"}, SCHEMA
            )

    asyncio.run(run())
    request = requests[0]
    assert request["max_completion_tokens"] == 4096
    assert "max_tokens" not in request
    if reasoning is None:
        assert "reasoning" not in request
    else:
        assert request["reasoning"] == {"enabled": reasoning}
    content = request["messages"][1]["content"]
    assert content == '{"description":"Clear text, with spaces"}'
    schema_text = request["messages"][0]["content"].split(
        "Return a JSON object that conforms to this full JSON Schema: ", 1
    )[1]
    assert json.loads(schema_text) == SCHEMA


def test_reasoning_and_token_limit_are_in_chat_cache_fingerprint(tmp_path):
    captured = []

    def respond(request):
        captured.append(json.loads(request.content))
        return response()

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            for reasoning, limit in [(False, 4096), (False, 4096), (True, 4096), (True, 6000)]:
                client = OpenRouterClient(
                    "test-secret",
                    "qwen/qwen3.7-flash",
                    client=http,
                    cache_dir=tmp_path,
                    reasoning=reasoning,
                    max_output_tokens=limit,
                )
                await client.complete_json("Extract", {"fact": "same"}, SCHEMA)

    asyncio.run(run())
    assert len(captured) == 3
    assert len(list(tmp_path.glob("*.json"))) == 3


def test_reasoning_and_cost_usage_are_totals_for_actual_requests_only(tmp_path):
    async def run():
        transport = httpx.MockTransport(
            lambda request: response(
                usage={
                    "prompt_tokens": 10,
                    "completion_tokens": 12,
                    "completion_tokens_details": {"reasoning_tokens": 5},
                    "cost": 0.0003,
                }
            )
        )
        async with httpx.AsyncClient(transport=transport) as http:
            client = OpenRouterClient(
                "test-secret", "qwen/qwen3.7-flash", client=http, cache_dir=tmp_path
            )
            await client.complete_json("Extract", {"item": 1}, SCHEMA)
            await client.complete_json("Extract", {"item": 1}, SCHEMA)
            await client.complete_json("Extract", {"item": 2}, SCHEMA)
            assert client.stats == {
                "calls": 2,
                "cache_hits": 1,
                "input_tokens": 20,
                "output_tokens": 24,
                "reasoning_tokens": 10,
                "cost_usd": 0.0006,
                "known_cost_usd": 0.0006,
                "cost_complete": True,
                "cost_pending_calls": 0,
                "in_flight_calls": 0,
            }

    asyncio.run(run())


def test_missing_usage_metadata_is_unknown_instead_of_a_partial_cost_total():
    requests = []

    def respond(request):
        requests.append(request)
        usage = {"completion_tokens_details": {"reasoning_tokens": 0}, "cost": 0.0003}
        return response(usage=usage if len(requests) == 1 else {})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            client = OpenRouterClient("test-secret", "qwen/qwen3.7-flash", client=http)
            await client.complete_json("Extract", {"item": 1}, SCHEMA)
            assert client.stats["reasoning_tokens"] == 0
            assert client.stats["cost_usd"] == 0.0003
            await client.complete_json("Extract", {"item": 2}, SCHEMA)
            assert "reasoning_tokens" not in client.stats
            assert "cost_usd" not in client.stats

    asyncio.run(run())


@pytest.mark.parametrize(
    "options",
    [
        {"reasoning": "false"},
        {"max_output_tokens": 0},
        {"max_output_tokens": True},
        {"max_output_tokens": 1.5},
    ],
)
def test_cost_control_options_reject_invalid_values(options):
    with pytest.raises(ValueError):
        OpenRouterClient("test-secret", "model", **options)


def test_native_routing_cache_rebinds_current_ids_but_never_changed_evidence(tmp_path):
    requests = []

    def respond(request):
        payload = json.loads(request.content)
        requests.append(payload)
        assert set(payload) == {"model", "state", "questions"}
        return httpx.Response(
            200,
            json={
                "model": "openai/gpt-6-luna-decisions",
                "answers": {
                    name: {"type": "choice", "choice": "json_ld"} for name in payload["questions"]
                },
                "usage": {"input_tokens": 20, "output_tokens": 0, "cost": 0.000002},
            },
        )

    def context(page_id, content='{"name":"Camera","description":"Clear evidence"}', schema=None):
        return RoutingContext(
            page_id=page_id,
            extraction_schema=copy.deepcopy(schema or SCHEMA),
            candidates=(
                CandidateBlock(
                    candidate_id=f"{page_id}:jsonld:block", kind="json_ld", content=content
                ),
            ),
        )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            client = OpenRouterDecisionsClient(
                "test-secret",
                "openai/gpt-6-luna-decisions",
                client=http,
                cache_dir=tmp_path,
                reasoning=False,
                max_output_tokens=4096,
            )
            engine = LunaDecisionEngine(client)
            first = await engine.select(context("snapshot-a"))
            second_context = context("snapshot-b")
            second = await engine.select(second_context)
            assert client.calls == 1 and client.cache_hits == 1
            assert first.targets[0].candidate_id == "snapshot-a:jsonld:block"
            assert second.targets[0].candidate_id == "snapshot-b:jsonld:block"
            assert second.validate_against(second_context) is second
            assert "candidate_id" not in requests[0]["state"]["candidates"]["candidate_000"]
            await engine.select(context("snapshot-c", content='{"description":"Different facts"}'))
            changed_schema = {**SCHEMA, "description": "Different extraction task"}
            await engine.select(context("snapshot-d", schema=changed_schema))
            assert client.calls == 3 and client.cache_hits == 1
            common_prefix = "X" * 4500
            await engine.select(context("snapshot-e", content=common_prefix + "first facts"))
            await engine.select(context("snapshot-f", content=common_prefix + "changed facts"))
            assert client.calls == 5  # Changes outside the sent preview also invalidate cache.

    asyncio.run(run())


def test_native_cost_requires_all_live_call_metadata_and_cache_only_is_free(tmp_path):
    calls = []
    questions = {"route": {"type": "choice", "instructions": "Route", "criteria": {"skip": "Skip"}}}

    def respond(request):
        calls.append(request)
        usage = {"input_tokens": 10, "output_tokens": 0}
        if len(calls) == 1:
            usage["cost"] = 0.000001
        return httpx.Response(
            200,
            json={
                "model": "openai/gpt-6-luna-decisions",
                "answers": {"route": {"type": "choice", "choice": "skip"}},
                "usage": usage,
            },
        )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            client = OpenRouterDecisionsClient(
                "test-secret",
                "openai/gpt-6-luna-decisions",
                client=http,
                cache_dir=tmp_path,
            )
            await client.decide({"item": 1}, questions)
            assert client.stats["cost_usd"] == 0.000001
            await client.decide({"item": 2}, questions)
            assert "cost_usd" not in client.stats
            cached = OpenRouterDecisionsClient(
                "test-secret",
                "openai/gpt-6-luna-decisions",
                client=http,
                cache_dir=tmp_path,
            )
            await cached.decide({"item": 2}, questions)
            assert cached.stats["calls"] == 0
            assert cached.stats["cache_hits"] == 1
            assert cached.stats["cost_usd"] == 0.0

    asyncio.run(run())


def test_native_routing_cache_isolates_canonical_source_urls(tmp_path):
    requests = []

    def respond(request):
        payload = json.loads(request.content)
        requests.append(payload)
        return httpx.Response(
            200,
            json={
                "model": "openai/gpt-6-luna-decisions",
                "answers": {"candidate_000": {"type": "choice", "choice": "json_ld"}},
                "usage": {"input_tokens": 10, "output_tokens": 0},
            },
        )

    def context(page_id, url):
        return RoutingContext(
            page_id=page_id,
            source_url=url,
            extraction_schema=SCHEMA,
            candidates=(
                CandidateBlock(
                    candidate_id=f"{page_id}:block",
                    kind="json_ld",
                    content='{"name":"Same camera"}',
                ),
            ),
        )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            client = OpenRouterDecisionsClient(
                "test-secret", "openai/gpt-6-luna-decisions", client=http, cache_dir=tmp_path
            )
            engine = LunaDecisionEngine(client)
            await engine.select(
                context("snapshot-a", "https://example.org/products/camera?utm_source=a")
            )
            current = context(
                "snapshot-b", "https://example.org/products/camera?utm_source=b#photos"
            )
            rebound = await engine.select(current)
            assert client.calls == 1 and client.cache_hits == 1
            assert rebound.targets[0].candidate_id == "snapshot-b:block"
            assert rebound.validate_against(current) is rebound
            await engine.select(context("snapshot-c", "https://example.org/products/other-camera"))
            assert client.calls == 2 and client.cache_hits == 1

    asyncio.run(run())
    assert requests[0]["state"]["source_url"] == "https://example.org/products/camera"
    assert requests[1]["state"]["source_url"] == "https://example.org/products/other-camera"


def test_retry_without_usage_prevents_claiming_a_complete_cost_total(monkeypatch):
    calls = []

    async def no_wait(_delay):
        return None

    monkeypatch.setattr("unified_web_parser.providers.openrouter.asyncio.sleep", no_wait)

    def respond(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(503)
        return response(usage={"cost": 0.0001})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            client = OpenRouterClient("test-secret", "qwen/qwen3.7-flash", client=http)
            await client.complete_json("Extract", {}, SCHEMA)
            assert client.calls == 2
            assert "cost_usd" not in client.stats

    asyncio.run(run())

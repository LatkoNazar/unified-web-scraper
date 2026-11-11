"""Independent ServerTool boundary checks; all HTTP responses are local fakes."""

import json

import httpx
import pytest

from unified_web_parser.core.exceptions import ProviderError
from unified_web_parser.providers.openrouter import RequestBudget
from unified_web_parser.providers.search import OpenRouterSearchClient


def response(*, counter=1, annotations=None):
    return {
        "usage": {
            "prompt_tokens": 17,
            "completion_tokens": 9,
            "cost": 0.02,
            "server_tool_use": {"web_search_requests": counter},
        },
        "choices": [
            {
                "message": {
                    "content": "https://invented.example/",
                    "annotations": (
                        annotations
                        if annotations is not None
                        else [
                            {
                                "type": "url_citation",
                                "url_citation": {"url": "https://books.example/one"},
                            }
                        ]
                    ),
                }
            }
        ],
    }


async def test_native_search_wire_forces_one_tool_and_keeps_grounded_citations_only():
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=response())

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = OpenRouterSearchClient("fake-key", "model", client=http)
        batch = await client.search("travel books", limit=3)
    wire = requests[0]
    assert "plugins" not in wire
    assert wire["max_tool_calls"] == 1
    assert wire["tool_choice"] == "required"
    assert len(wire["tools"]) == 1
    tool = wire["tools"][0]
    assert tool["type"] == "openrouter:web_search"
    assert tool["parameters"]["max_uses"] == 1
    assert tool["parameters"]["max_total_results"] == 3
    assert tool["parameters"]["max_results"] == 3
    assert [item.url for item in batch.results] == ["https://books.example/one"]
    assert client.stats["cost_usd"] == pytest.approx(0.02)


@pytest.mark.parametrize("counter", [None, 0, 2, -1, True, 1.0, "1", {}, []])
async def test_invalid_native_usage_cannot_succeed_or_cache_but_still_accounts_cost(
    tmp_path, counter
):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response(counter=counter)))
    ) as http:
        budget = RequestBudget(1)
        client = OpenRouterSearchClient(
            "fake-key", "model", client=http, budget=budget, cache_dir=tmp_path
        )
        with pytest.raises(ProviderError):
            await client.search("travel books", limit=3)
    assert client.calls == 1 and budget.remaining == 0
    assert client.input_tokens == 17 and client.output_tokens == 9
    assert client.stats["cost_usd"] == pytest.approx(0.02)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("missing", ["usage", "server_tool_use", "web_search_requests"])
async def test_missing_native_usage_counter_is_an_error(tmp_path, missing):
    body = response()
    if missing == "usage":
        del body["usage"]
    elif missing == "server_tool_use":
        del body["usage"]["server_tool_use"]
    else:
        del body["usage"]["server_tool_use"]["web_search_requests"]
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body))
    ) as http:
        client = OpenRouterSearchClient("fake-key", "model", client=http, cache_dir=tmp_path)
        with pytest.raises(ProviderError):
            await client.search("travel books", limit=3)
    assert client.calls == 1
    assert list(tmp_path.iterdir()) == []
    if missing != "usage":
        assert client.stats["cost_usd"] == pytest.approx(0.02)


@pytest.mark.parametrize("alternate", [False, True])
async def test_documented_and_actual_usage_aliases_are_not_summed(alternate):
    body = response()
    body["usage"]["server_tool_use_details"] = {"web_search_requests": 1}
    if alternate:
        del body["usage"]["server_tool_use"]
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body))
    ) as http:
        client = OpenRouterSearchClient("fake-key", "model", client=http)
        batch = await client.search("travel books", limit=3)
    assert batch.web_search_requests == 1
    assert client.stats["web_search_requests"] == 1
    assert len(batch.results) == 1


async def test_conflicting_usage_aliases_fail_after_preserving_token_cost_accounting(tmp_path):
    body = response()
    body["usage"]["server_tool_use_details"] = {"web_search_requests": 2}
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body))
    ) as http:
        client = OpenRouterSearchClient("fake-key", "model", client=http, cache_dir=tmp_path)
        with pytest.raises(ProviderError):
            await client.search("travel books", limit=3)
    assert client.input_tokens == 17 and client.output_tokens == 9
    assert client.stats["cost_usd"] == pytest.approx(0.02)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("malformed", [[], "1", True])
async def test_valid_alias_cannot_hide_a_malformed_secondary_usage_alias(malformed):
    body = response()
    body["usage"]["server_tool_use_details"] = malformed
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body))
    ) as http:
        client = OpenRouterSearchClient("fake-key", "model", client=http)
        with pytest.raises(ProviderError):
            await client.search("travel books", limit=3)
    assert client.stats["cost_usd"] == pytest.approx(0.02)
    assert client.stats["search_usage_violations"] == 1


@pytest.mark.parametrize("choices", [[], [{"message": []}]])
async def test_invalid_response_shape_preserves_paid_usage_without_writing_cache(tmp_path, choices):
    body = response()
    body["choices"] = choices
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body))
    ) as http:
        client = OpenRouterSearchClient("fake-key", "model", client=http, cache_dir=tmp_path)
        with pytest.raises(ProviderError):
            await client.search("travel books", limit=3)
    assert client.input_tokens == 17 and client.output_tokens == 9
    assert client.stats["cost_usd"] == pytest.approx(0.02)
    assert client.stats["web_search_requests"] == 1
    assert list(tmp_path.iterdir()) == []


async def test_actual_input_output_token_aliases_are_accounted():
    body = response()
    body["usage"]["input_tokens"] = body["usage"].pop("prompt_tokens")
    body["usage"]["output_tokens"] = body["usage"].pop("completion_tokens")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body))
    ) as http:
        client = OpenRouterSearchClient("fake-key", "model", client=http)
        await client.search("travel books", limit=3)
    assert client.input_tokens == 17 and client.output_tokens == 9


async def test_context_changes_cache_identity_but_same_context_cache_is_free(tmp_path):
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=response())

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        cold = OpenRouterSearchClient("fake-key", "model", client=http, cache_dir=tmp_path)
        original = await cold.search("travel books", limit=3, search_context_size="low")
        free = OpenRouterSearchClient(
            "fake-key", "model", client=http, cache_dir=tmp_path, budget=RequestBudget(0)
        )
        cached = await free.search("travel books", limit=3, search_context_size="low")
        changed = OpenRouterSearchClient("fake-key", "model", client=http, cache_dir=tmp_path)
        await changed.search("travel books", limit=3, search_context_size="high")
    assert len(requests) == 2
    assert original.web_search_requests == original.original_web_search_requests == 1
    assert cached.web_search_requests == 0 and cached.original_web_search_requests == 1
    assert cached.cache_hit
    assert free.calls == 0 and free.stats["cost_usd"] == 0
    assert free.stats["cached_web_search_requests"] == 1


@pytest.mark.parametrize("invalid", ["counter", "missing", "contract", "legacy_version"])
async def test_cached_native_provenance_cannot_be_missing_or_invalid(tmp_path, invalid):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, json=response())

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = OpenRouterSearchClient("fake-key", "model", client=http, cache_dir=tmp_path)
        await client.search("travel books", limit=3)
        cache_file = next(tmp_path.iterdir())
        corrupted = json.loads(cache_file.read_text(encoding="utf-8"))
        if invalid == "counter":
            corrupted["batch"]["original_web_search_requests"] = 2
        elif invalid == "missing":
            del corrupted["batch"]["original_web_search_requests"]
        elif invalid == "contract":
            corrupted["batch"]["search_contract"] = None
        else:
            corrupted["identity"]["version"] = "web-search.v2"
        cache_file.write_text(json.dumps(corrupted), encoding="utf-8")
        await client.search("travel books", limit=3)
    assert len(calls) == 2 and client.cache_hits == 0


@pytest.mark.parametrize("model", ["model:online", "model:ONLINE"])
async def test_implicit_online_plugin_models_are_rejected_before_transport_or_cache(
    tmp_path, model
):
    def unexpected(_request):
        pytest.fail("An :online model must never reach transport")

    async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected)) as http:
        client = OpenRouterSearchClient("fake-key", model, client=http, cache_dir=tmp_path)
        with pytest.raises((ProviderError, ValueError)):
            await client.search("travel books", limit=3)
    assert client.calls == 0 and client.cache_hits == 0
    assert list(tmp_path.iterdir()) == []

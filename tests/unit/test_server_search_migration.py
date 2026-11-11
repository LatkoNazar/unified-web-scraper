"""No legacy search fallback; verified cache and usage survive the migration."""

import hashlib
import json
from copy import deepcopy

import httpx
import pytest
from pydantic import ValidationError

from unified_web_parser.core.config import AIConfig, DiscoveryConfig, load_job
from unified_web_parser.core.exceptions import ProviderError
from unified_web_parser.interfaces.cli import _overrides, _parser
from unified_web_parser.providers.openrouter import RequestBudget
from unified_web_parser.providers.search import OpenRouterSearchClient


def response():
    return {
        "usage": {
            "input_tokens": 20,
            "output_tokens": 5,
            "cost": 0.005,
            "server_tool_use_details": {"web_search_requests": 1},
        },
        "choices": [
            {
                "message": {
                    "annotations": [
                        {
                            "type": "url_citation",
                            "url_citation": {"url": "https://books.example/item"},
                        }
                    ]
                }
            }
        ],
    }


def test_context_is_strict_config_and_explicit_cli_override():
    assert DiscoveryConfig().search_context_size == "low"
    with pytest.raises(ValidationError):
        DiscoveryConfig(search_context_size="unbounded")
    job = _overrides(
        load_job("configs/jobs/web.yaml"),
        _parser().parse_args(["discover", "books", "--search-context-size", "high"]),
    )
    assert job.config.discovery.search_context_size == "high"


@pytest.mark.parametrize("model", ["model:online", "model:online:free", "model:free:ONLINE"])
def test_legacy_model_modifier_is_rejected_before_schema_or_query_planning(model):
    with pytest.raises(ValidationError, match="implicit web search"):
        AIConfig(model=model)


@pytest.mark.asyncio
async def test_cache_keeps_original_one_search_but_current_usage_zero(tmp_path):
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=response())

    budget = RequestBudget(1)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        cold = OpenRouterSearchClient(
            "test", "model", client=http, budget=budget, cache_dir=tmp_path
        )
        live = await cold.search("books", limit=25)
        warm = OpenRouterSearchClient(
            "test", "model", client=http, budget=budget, cache_dir=tmp_path
        )
        cached = await warm.search("books", limit=25)
    assert live.web_search_requests == live.original_web_search_requests == 1
    assert cached.web_search_requests == 0 and cached.original_web_search_requests == 1
    assert cached.search_contract == "server-tool.v1"
    assert len(requests) == budget.used == 1
    assert warm.stats["web_search_requests"] == 0
    assert warm.stats["cached_web_search_requests"] == 1
    assert warm.stats["cost_usd"] == 0


@pytest.mark.asyncio
async def test_context_and_per_request_token_budget_are_isolated_cache_inputs(tmp_path):
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=response())

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        first = OpenRouterSearchClient(
            "test", "model", client=http, cache_dir=tmp_path, max_output_tokens=1024
        )
        await first.search("books", limit=3, search_context_size="low")
        await first.search("books", limit=3, search_context_size="high")
        larger = OpenRouterSearchClient(
            "test", "model", client=http, cache_dir=tmp_path, max_output_tokens=4096
        )
        await larger.search("books", limit=3, search_context_size="low")
    assert len(requests) == 3
    assert [item["tools"][0]["parameters"]["search_context_size"] for item in requests] == [
        "low",
        "high",
        "low",
    ]


@pytest.mark.asyncio
async def test_deprecated_cache_contract_is_not_accepted(tmp_path):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json=response())

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = OpenRouterSearchClient("test", "model", client=http, cache_dir=tmp_path)
        await client.search("books", limit=3)
        path = next(tmp_path.glob("search-*.json"))
        old = json.loads(path.read_text())
        old["identity"]["version"] = "web-search.v2"
        old["batch"].pop("search_contract")
        path.write_text(json.dumps(old))
        assert not (await client.search("books", limit=3)).cache_hit
    assert len(requests) == 2


@pytest.mark.asyncio
async def test_http_retry_is_not_misreported_as_exactly_one_total_search(tmp_path):
    count = 0

    def respond(_request):
        nonlocal count
        count += 1
        return (
            httpx.Response(429, headers={"Retry-After": "0"})
            if count == 1
            else httpx.Response(200, json=response())
        )

    budget = RequestBudget(2)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = OpenRouterSearchClient("test", "model", client=http, budget=budget)
        batch = await client.search("books", limit=3)
    assert batch.web_search_requests == 1
    assert client.stats["calls"] == budget.used == 2
    assert client.stats["reported_web_search_requests"] == 1
    assert "web_search_requests" not in client.stats  # Earlier failed attempt is unknown.
    assert "cost_usd" not in client.stats


@pytest.mark.parametrize("limit,expected", [(1024, 1024), (9000, 8192)])
async def test_search_uses_current_completion_limit_with_all_server_tool_bounds(limit, expected):
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=response())

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = OpenRouterSearchClient("test", "model", client=http, max_output_tokens=limit)
        await client.search("books", limit=25)
    body = requests[0]
    assert body["max_completion_tokens"] == expected
    assert "max_tokens" not in body
    assert body["max_tool_calls"] == 1
    assert body["tools"][0]["parameters"] == {
        "engine": "parallel",
        "mode": "basic",
        "max_results": 25,
        "max_total_results": 25,
        "max_uses": 1,
        "search_context_size": "low",
    }


async def test_v3_token_limit_cache_is_isolated_and_v4_warm_cache_needs_no_budget(tmp_path):
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=response())

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        cold = OpenRouterSearchClient("test", "model", client=http, cache_dir=tmp_path)
        await cold.search("books", limit=3)
        current_path = next(tmp_path.glob("search-*.json"))
        legacy = deepcopy(json.loads(current_path.read_text(encoding="utf-8")))
        assert legacy["identity"]["version"] == "web-search.v4-server-tool"
        legacy["identity"]["version"] = "web-search.v3-server-tool"
        legacy["identity"]["max_tokens"] = legacy["identity"].pop("max_completion_tokens")
        digest = hashlib.sha256(
            json.dumps(legacy["identity"], sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        legacy_path = tmp_path / f"search-{digest}.json"
        legacy_path.write_text(json.dumps(legacy), encoding="utf-8")
        current_path.unlink()
        empty = OpenRouterSearchClient(
            "test", "model", client=http, cache_dir=tmp_path, budget=RequestBudget(0)
        )
        with pytest.raises(ProviderError, match="budget exhausted"):
            await empty.search("books", limit=3)
        assert len(requests) == 1 and empty.calls == empty.cache_hits == 0
        fresh = OpenRouterSearchClient("test", "model", client=http, cache_dir=tmp_path)
        assert not (await fresh.search("books", limit=3)).cache_hit
        warm = OpenRouterSearchClient(
            "test", "model", client=http, cache_dir=tmp_path, budget=RequestBudget(0)
        )
        cached = await warm.search("books", limit=3)
    assert len(requests) == 2
    assert cached.cache_hit and cached.web_search_requests == 0
    assert cached.original_web_search_requests == 1
    assert warm.calls == 0 and warm.stats["cost_usd"] == 0
    assert legacy_path.is_file()

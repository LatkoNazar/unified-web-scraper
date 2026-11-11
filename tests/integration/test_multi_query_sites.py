"""All variants run with independent result limits and one attributed site ledger."""

from contextlib import AsyncExitStack
from datetime import UTC, datetime

import httpx
import pytest

from unified_web_parser.core.config import JobConfig, LoadedJob
from unified_web_parser.core.exceptions import ProviderError
from unified_web_parser.discovery.search import plan_queries
from unified_web_parser.pipeline.discovery import discover_sources
from unified_web_parser.pipeline.report import RunReport
from unified_web_parser.providers.openrouter import RequestBudget
from unified_web_parser.providers.search import OpenRouterSearchClient, SearchBatch, SearchResult

QUERIES = [
    "used acoustic guitar Ukraine",
    "secondhand acoustic guitars Ukraine",
    "preowned acoustic guitar listings Ukraine",
]


class Planner:
    async def complete_json(self, *_args, **_kwargs):
        return {"queries": QUERIES, "site_search": "used acoustic guitar"}


async def discover(tmp_path, responses, *, sources=None, max_sites=25, budget_limit=10):
    loaded = LoadedJob(
        config=JobConfig(
            name="sites",
            sources=sources or [],
            schema_path="schema.json",
            search="Find used acoustic guitars in Ukraine",
            discovery={"enabled": True, "max_sites": max_sites},
        ),
        config_path=tmp_path / "job.yaml",
        extraction_schema={"type": "object"},
    )
    report = RunReport(run_id="test", job_name="sites", started_at=datetime.now(UTC))
    budget, calls = RequestBudget(budget_limit), []

    class Search:
        async def search(self, query, **options):
            calls.append((query, options))
            budget.consume()
            response = responses[query]
            if isinstance(response, Exception):
                raise response
            return SearchBatch(
                query=query,
                fetched_at=datetime.now(UTC),
                results=[
                    SearchResult(url=url, title="Guitar source", citation_order=i)
                    for i, url in enumerate(response, 1)
                ],
            )

    async with AsyncExitStack() as stack:
        effective = await discover_sources(
            loaded.config,
            loaded.extraction_schema,
            data_dir=tmp_path,
            injected_llm=None,
            progress=lambda _message: None,
            stack=stack,
            key=None,
            model="model",
            budget=budget,
            clients={"query_planning": Planner(), "web_search": Search()},
            report=report,
            run_dir=tmp_path,
        )
    return effective, report.discovery, calls


@pytest.mark.asyncio
async def test_every_variant_runs_after_cap_and_all_citations_remain_attributed(tmp_path):
    effective, audit, calls = await discover(
        tmp_path,
        {
            QUERIES[0]: [
                "https://shop.strings.co.uk:8443/products/1?q=yes",
                "https://guitars.com/item",
            ],
            QUERIES[1]: ["https://www.strings.co.uk/other", "https://newsource.net/item"],
            QUERIES[2]: ["https://guitars.com/second", "https://lastsource.org/item"],
        },
        max_sites=2,
    )
    assert [query for query, _ in calls] == QUERIES
    assert [options["limit"] for _, options in calls] == [25, 25, 25]
    assert [str(source.url) for source in effective.sources] == [
        "https://shop.strings.co.uk:8443/",
        "https://guitars.com/",
    ]
    assert audit["results_per_query"] == 25 and audit["max_sites"] == 2
    assert len(audit["results"]) == 6
    assert audit["sites"][0]["queries"] == QUERIES[:2]
    assert len(audit["sites"][0]["citations"]) == 2
    assert len(audit["excluded_sites"]) == 2
    assert all(not item["selected"] for item in audit["excluded_sites"])


@pytest.mark.asyncio
async def test_explicit_sources_and_private_hosted_tenants_merge_without_losing_attribution(
    tmp_path,
):
    effective, audit, _ = await discover(
        tmp_path,
        {
            QUERIES[0]: ["https://www.guitars.com/second", "https://one.github.io/product"],
            QUERIES[1]: ["https://shop.one.github.io/another", "https://two.github.io/product"],
            QUERIES[2]: ["https://guitars.com/third"],
        },
        sources=[{"url": "https://shop.guitars.com/catalog"}],
    )
    assert [str(source.url) for source in effective.sources] == [
        "https://shop.guitars.com/",
        "https://one.github.io/",
        "https://two.github.io/",
    ]
    assert audit["sites"][0]["configured_urls"] == ["https://shop.guitars.com/catalog"]
    assert audit["sites"][0]["queries"] == [QUERIES[0], QUERIES[2]]


@pytest.mark.asyncio
async def test_recoverable_middle_failure_does_not_suppress_last_variant(tmp_path):
    effective, audit, calls = await discover(
        tmp_path,
        {
            QUERIES[0]: ["https://first.com/item"],
            QUERIES[1]: ProviderError("Temporary search error"),
            QUERIES[2]: ["https://third.com/item"],
        },
    )
    assert [query for query, _ in calls] == QUERIES
    assert audit["status"] == "partial"
    assert audit["resolved_sources"] == ["https://first.com/", "https://third.com/"]
    assert len(effective.sources) == 2


@pytest.mark.asyncio
async def test_budget_stop_keeps_prior_sites_and_audits_unattempted_variants(tmp_path):
    effective, audit, calls = await discover(
        tmp_path, {query: ["https://first.com/item"] for query in QUERIES}, budget_limit=1
    )
    assert len(calls) == 2  # The exhausted call never reaches provider transport.
    assert len(effective.sources) == 1
    assert audit["status"] == "partial"
    assert audit["query_outcomes"][-1] == {
        "query": QUERIES[2],
        "status": "not_attempted",
        "reason": "request_budget_exhausted",
    }


@pytest.mark.asyncio
async def test_search_transport_supports_25_real_annotation_results():
    def respond(request):
        import json

        body = json.loads(request.content)
        assert body["tools"][0]["parameters"]["max_results"] == 25
        assert "plugins" not in body
        assert body["max_completion_tokens"] == 4096
        return httpx.Response(
            200,
            json={
                "usage": {"server_tool_use_details": {"web_search_requests": 1}},
                "choices": [
                    {
                        "message": {
                            "annotations": [
                                {
                                    "type": "url_citation",
                                    "url_citation": {"url": f"https://site{i}.com/item"},
                                }
                                for i in range(25)
                            ]
                        }
                    }
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = OpenRouterSearchClient("test", "model", client=http, max_output_tokens=4096)
        batch = await client.search("guitars", limit=25)
    assert len(batch.results) == 25


@pytest.mark.asyncio
@pytest.mark.parametrize("variants", [QUERIES[:1], ["used guitar", "USED   GUITAR", "guitar used"]])
async def test_multi_query_plan_rejects_missing_or_cosmetic_variants(variants):
    class Invalid:
        async def complete_json(self, *_args, **_kwargs):
            return {"queries": variants, "site_search": "guitar"}

    with pytest.raises(ProviderError):
        await plan_queries("Find used guitars", {}, client=Invalid(), max_queries=3)


@pytest.mark.asyncio
async def test_literal_still_runs_one_original_query_without_planner():
    plan = await plan_queries("Find used guitars", {}, max_queries=3, mode="literal")
    assert plan.queries == ("Find used guitars",)

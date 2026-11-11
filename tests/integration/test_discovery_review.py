"""Independent discovery contract tests; all external services are replaced locally."""

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from unified_web_parser.core.config import JobConfig, LoadedJob
from unified_web_parser.core.exceptions import ProviderError
from unified_web_parser.core.models import ExtractionStrategy
from unified_web_parser.crawling.pages import make_page
from unified_web_parser.extraction.schema import SchemaExtractor
from unified_web_parser.interfaces.cli import _overrides, _parser
from unified_web_parser.navigation.inventory import InventoryBuilder
from unified_web_parser.pipeline.runner import PipelineRunner
from unified_web_parser.providers.openrouter import OpenRouterClient
from unified_web_parser.providers.search import SearchBatch, SearchResult
from unified_web_parser.routing.candidates import CandidateBuilder
from unified_web_parser.storage import DatasetStore


def discovery_job(tmp_path, **updates):
    values = {
        "name": "discovery-review",
        "sources": [],
        "search": "Find travel books available locally",
        "schema_path": "schema.json",
        "content_profile": "commerce",
        "currency_aliases": {"GBP": "GBP", "£": "GBP"},
        "discovery": {
            "enabled": True,
            "search_results": 2,
            "pages_per_site": 3,
            "max_queries": 1,
            "entrypoint": "result_url",
        },
        "ai": {"enabled": False, "max_calls": 5, "routing": "rules"},
        "navigation": {"mode": "rules"},
        "crawl": {"backend": "http", "max_pages": 8, "delay": 0, "concurrency": 1},
    }
    for key, value in updates.items():
        if isinstance(value, dict):
            values[key] = {**values.get(key, {}), **value}
        else:
            values[key] = value
    return LoadedJob(
        config=JobConfig.model_validate(values),
        config_path=tmp_path / "job.yaml",
        schema_path=tmp_path / "schema.json",
        extraction_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["title", "price", "currency"],
            "properties": {
                "title": {"type": "string"},
                "price": {"type": "number"},
                "currency": {"type": "string"},
            },
        },
    )


def batch(query, urls, *, cached=False):
    return SearchBatch(
        query=query,
        fetched_at=datetime.now(UTC),
        cache_hit=cached,
        results=[SearchResult(url=url, citation_order=index) for index, url in enumerate(urls, 1)],
    )


class Pages:
    def __init__(self):
        self.calls = []

    async def fetch(self, url):
        self.calls.append(url)
        return make_page(
            url,
            '<script type="application/ld+json">'
            + json.dumps(
                {
                    "@type": "Product",
                    "name": "Travel book",
                    "url": url,
                    "offers": {"price": 15, "priceCurrency": "GBP"},
                }
            )
            + "</script>",
            200,
        )


class NoInference:
    async def complete_json(self, *_args, **_kwargs):
        pytest.fail("Complete structured evidence unexpectedly required inference")


def test_discover_replaces_profile_sources_and_invalid_bounds_fail_before_execution(tmp_path):
    job = discovery_job(tmp_path, sources=[{"url": "https://old.example/"}])
    arguments = _parser().parse_args(["discover", "New question", "--search-results", "3"])
    selected = _overrides(job, arguments)
    assert selected.config.sources == []
    assert selected.config.search == "New question"
    assert selected.config.discovery.search_results == 3
    assert str(job.config.sources[0].url) == "https://old.example/"
    document = selected.config.model_dump(mode="json")
    document["discovery"]["enabled"] = False
    with pytest.raises(ValidationError):
        JobConfig.model_validate(document)
    document["discovery"].update(enabled=True, cache_ttl_seconds=604801)
    with pytest.raises(ValidationError):
        JobConfig.model_validate(document)


@pytest.mark.asyncio
async def test_no_ai_uses_literal_search_but_replay_never_searches_or_fetches(tmp_path):
    job = discovery_job(tmp_path)
    pages = Pages()
    queries = []

    class Search:
        async def search(self, query, **_options):
            queries.append(query)
            return batch(query, ["https://books.example/item?id=one"])

    first = await PipelineRunner(
        job,
        data_dir=tmp_path / "data",
        fetcher=pages,
        search_client=Search(),
        query_client=NoInference(),
    ).run()
    assert first.status == "success", first.errors
    assert queries == [job.config.search]
    assert first.discovery["effective_query_mode"] == "literal"
    assert job.config.sources == []
    assert len(pages.calls) == 1

    class NoNetwork:
        async def fetch(self, *_args):
            pytest.fail("Replay fetched a page")

        async def search(self, *_args, **_kwargs):
            pytest.fail("Replay searched the web")

        async def complete_json(self, *_args, **_kwargs):
            pytest.fail("Replay planned discovery")

    replay = await PipelineRunner(
        job,
        data_dir=tmp_path / "data",
        fetcher=NoNetwork(),
        search_client=NoNetwork(),
        query_client=NoNetwork(),
    ).run(replay_dir=Path(first.artifacts["directory"]))
    assert replay.status == "success", replay.errors
    assert replay.discovery["status"] == "replayed"
    assert replay.discovery["network_used"] is False
    assert replay.records_written == 1
    assert len(DatasetStore(tmp_path / "data").list_records()) == 1


@pytest.mark.asyncio
async def test_later_query_failure_preserves_earlier_sources_as_partial_result(tmp_path):
    class Planner:
        async def complete_json(self, *_args, **_kwargs):
            return {"queries": ["travel books", "travel guides"], "site_search": "Travel"}

    class Search:
        async def search(self, query, **_options):
            if query == "travel guides":
                raise ProviderError("OpenRouter request budget exhausted (5)")
            return batch(query, ["https://books.example/item?id=one"])

    pages = Pages()
    job = discovery_job(
        tmp_path, ai={"enabled": True}, discovery={"max_queries": 2}, assessment=["is_relevant"]
    )
    report = await PipelineRunner(
        job,
        data_dir=tmp_path / "data",
        fetcher=pages,
        llm=NoInference(),
        query_client=Planner(),
        search_client=Search(),
    ).run()
    assert report.status == "partial", report.errors
    assert report.records_written == 1
    assert pages.calls == ["https://books.example/item?id=one"]
    assert report.discovery["resolved_sources"] == pages.calls
    assert any(error["stage"] == "discovery" for error in report.errors)
    records = DatasetStore(tmp_path / "data").list_records()
    assert records[0]["assessments"][0]["results"]["is_relevant"] is True

    class NoSearch:
        async def search(self, *_args, **_kwargs):
            pytest.fail("Replay attempted search after a partial discovery run")

    replay = await PipelineRunner(
        job,
        data_dir=tmp_path / "data",
        fetcher=Pages(),
        llm=NoInference(),
        query_client=NoInference(),
        search_client=NoSearch(),
    ).run(replay_dir=Path(report.artifacts["directory"]))
    assert replay.status == "success", replay.errors
    records = DatasetStore(tmp_path / "data").list_records()
    assert records[0]["assessments"][0]["results"]["is_relevant"] is True


@pytest.mark.asyncio
async def test_production_clients_share_one_global_planning_search_extraction_budget(
    tmp_path, monkeypatch
):
    from unified_web_parser.providers import openrouter, search

    requests = []
    budgets = []

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        native_search = bool(body.get("tools"))
        if native_search:
            assert body["tool_choice"] == "required"
            message = {
                "annotations": [
                    {
                        "type": "url_citation",
                        "url_citation": {
                            "url": "https://books.example/item?id=one",
                        },
                    }
                ]
            }
        else:
            assert body["response_format"]["type"] == "json_object"
            message = {"content": json.dumps({"queries": ["Travel"], "site_search": "Travel"})}
        envelope = {"choices": [{"message": message}]}
        if native_search:
            envelope["usage"] = {"server_tool_use_details": {"web_search_requests": 1}}
        return httpx.Response(200, json=envelope)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:

        class LocalChat(openrouter.OpenRouterClient):
            def __init__(self, **kwargs):
                budgets.append(kwargs["budget"])
                super().__init__(client=http, **kwargs)

        class LocalSearch(search.OpenRouterSearchClient):
            def __init__(self, **kwargs):
                budgets.append(kwargs["budget"])
                super().__init__(client=http, **kwargs)

        monkeypatch.setattr(openrouter, "OpenRouterClient", LocalChat)
        monkeypatch.setattr(search, "OpenRouterSearchClient", LocalSearch)
        monkeypatch.setenv("OPENROUTER_API_KEY", "review-test-secret")
        monkeypatch.setenv("OPENROUTER_MODEL", "qwen/qwen3.7-flash")
        job = discovery_job(tmp_path, ai={"enabled": True, "max_calls": 2})
        job.extraction_schema["properties"]["description"] = {"type": ["string", "null"]}
        report = await PipelineRunner(job, data_dir=tmp_path / "data", fetcher=Pages()).run()
    assert report.status == "partial", report.errors
    assert report.records_written == 1
    assert len(requests) == 2
    assert len(budgets) == 3 and all(budget is budgets[0] for budget in budgets)
    assert report.ai["budget_used"] == report.ai["calls"] == 2
    assert report.ai["stages"]["query_planning"]["calls"] == 1
    assert report.ai["stages"]["web_search"]["calls"] == 1
    assert report.ai["stages"]["extraction"]["calls"] == 0
    assert any("budget exhausted" in error["message"] for error in report.errors)
    assert sum(source["errors"] for source in report.discovery["source_outcomes"]) >= 1


@pytest.mark.asyncio
async def test_paid_query_planning_is_not_reported_as_no_network_when_search_is_cached(tmp_path):
    def respond(_request):
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "queries": ["travel books"],
                                    "site_search": "Travel",
                                }
                            )
                        }
                    }
                ]
            },
        )

    class CachedSearch:
        async def search(self, query, **_options):
            return batch(query, ["https://books.example/item?id=one"], cached=True)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        planner = OpenRouterClient("test-secret", "model", client=http)
        report = await PipelineRunner(
            discovery_job(tmp_path, ai={"enabled": True}),
            data_dir=tmp_path / "data",
            fetcher=Pages(),
            llm=NoInference(),
            query_client=planner,
            search_client=CachedSearch(),
        ).run()
    assert report.status == "success", report.errors
    assert planner.calls == 1
    assert report.discovery["network_used"] is True


@pytest.mark.asyncio
async def test_missing_discovery_key_does_not_silently_crawl_old_profile_sources(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    pages = Pages()
    report = await PipelineRunner(
        discovery_job(tmp_path, sources=[{"url": "https://old.example/"}]),
        data_dir=tmp_path / "data",
        fetcher=pages,
    ).run()
    assert report.status == "failed"
    assert pages.calls == []
    assert report.discovery["status"] == "failed"
    assert any(error["stage"] == "discovery" for error in report.errors)


@pytest.mark.asyncio
async def test_redirected_descendants_share_their_actual_destination_hostname_cap(tmp_path):
    first = "https://redirect.example/start"
    second = "https://books.example/start"

    class Search:
        async def search(self, query, **_options):
            return batch(query, [first, second])

    class RedirectedPages(Pages):
        async def fetch(self, url):
            if url in {first, second}:
                self.calls.append(url)
                numbers = (1, 2) if url == first else (3, 4)
                content = (
                    "<main><h1>Travel books</h1>"
                    + "".join(
                        f'<article><h2><a href="/products/{number}">Travel book {number}</a>'
                        "</h2><p>15 GBP</p></article>"
                        for number in numbers
                    )
                    + "</main>"
                )
                return make_page(
                    "https://books.example/results" if url == first else url, content, 200
                )
            return await super().fetch(url)

    pages = RedirectedPages()
    report = await PipelineRunner(
        discovery_job(tmp_path, search="Travel", discovery={"pages_per_site": 2}),
        data_dir=tmp_path / "data",
        fetcher=pages,
        search_client=Search(),
    ).run()
    assert report.status == "success", report.errors
    # The initial redirect attempt belongs to its requested URL; later target-host
    # requests share the budget with the separately selected target-host root.
    assert pages.calls[:2] == [first, second]
    target_requests = [url for url in pages.calls if url.startswith("https://books.example/")]
    assert len(target_requests) <= 2, pages.calls
    assert report.navigation["site_attempts"]["books.example"] == len(target_requests)


@pytest.mark.asyncio
@pytest.mark.parametrize("interaction_fails", [False, True])
async def test_all_selected_roots_get_turn_before_ui_and_ui_attempt_uses_site_cap(
    tmp_path, interaction_fails
):
    first, second = "https://catalog.example/", "https://books.example/item"

    class Search:
        async def search(self, query, **_options):
            return batch(query, [first, second])

    class Browser(Pages):
        def __init__(self):
            super().__init__()
            self.events = []

        async def fetch(self, url):
            self.events.append(("fetch", url))
            if url == first:
                return make_page(url, '<form><input type="search" name="q"></form>', 200)
            return await super().fetch(url)

        async def interact(self, page, action, query):
            self.events.append(("interact", str(page.url)))
            assert action.kind == "search" and query == "Travel"
            if interaction_fails:
                raise ProviderError("Local fixture refused UI action")
            return make_page(
                first,
                '<article><h2><a href="/products/1">Travel book</a></h2><p>15 GBP</p></article>',
                200,
            )

    browser = Browser()
    report = await PipelineRunner(
        discovery_job(
            tmp_path,
            search="Travel",
            discovery={"pages_per_site": 2},
            crawl={"backend": "browser"},
            navigation={"allow_scroll": False},
        ),
        data_dir=tmp_path / "data",
        fetcher=browser,
        search_client=Search(),
    ).run()
    assert report.status == ("partial" if interaction_fails else "success"), report.errors
    assert browser.events == [("fetch", first), ("fetch", second), ("interact", first)]
    assert report.records_written == 1
    assert report.pages_attempted == 3
    assert report.navigation["site_attempts"] == {"catalog.example": 2, "books.example": 1}
    assert report.navigation["interactions_attempted"] == 1


@pytest.mark.asyncio
async def test_invalid_planner_phrase_uses_one_audited_literal_fallback_without_replanning(
    tmp_path,
):
    class InvalidPlanner:
        calls = 0

        async def complete_json(self, *_args, **_kwargs):
            self.calls += 1
            return {"queries": ["travel books"], "site_search": ""}

    searches = []

    class Search:
        async def search(self, query, **_options):
            searches.append(query)
            return batch(query, ["https://books.example/item?id=one"])

    planner = InvalidPlanner()
    job = discovery_job(tmp_path, ai={"enabled": True})
    report = await PipelineRunner(
        job,
        data_dir=tmp_path / "data",
        fetcher=Pages(),
        llm=NoInference(),
        query_client=planner,
        search_client=Search(),
    ).run()
    assert report.status == "success", report.errors
    assert planner.calls == 1
    assert searches == [job.config.search]
    assert report.discovery["effective_query_mode"] == "literal_fallback"
    assert report.discovery["site_search"] == job.config.search
    assert report.discovery["planning_error"]["type"] == "ProviderError"
    assert any(warning["stage"] == "query_planning" for warning in report.warnings)


@pytest.mark.parametrize("shape", ["unpriced_images", "filter_price", "variant_table"])
def test_catalog_structures_are_not_terminal_entities_even_with_headline_or_product_jsonld(shape):
    url = "https://catalog.example/family"
    if shape == "unpriced_images":
        descriptor = (
            '<script type="application/ld+json">'
            + json.dumps(
                {
                    "@type": "Product",
                    "name": "Travel books",
                    "url": url,
                }
            )
            + "</script>"
        )
        content = (
            descriptor + '<body><div><ul><li><a href="/categories/fiction"><img src="fiction.jpg">'
            'Fiction books</a></li><li><a href="/categories/history"><img src="history.jpg">'
            "History books</a></li></ul></div><div><h1>Travel books</h1><div>"
            + "".join(
                f'<div class="arbitrary"><a href="/volume-{number}.html"><img src="cover.jpg"></a>'
                f'<div><a href="/volume-{number}.html">Travel book {number}</a></div>'
                "<p>Not available</p></div>"
                for number in (1, 2)
            )
            + "</div></div></body>"
        )
    elif shape == "filter_price":
        content = (
            "<main><h1>Travel books</h1><div><span>Price</span>10 GBP — 200 GBP</div>"
            + "".join(
                f'<div class="arbitrary"><img src="cover.jpg"><div><a href="/volume-{number}.html">'
                f"Travel book {number}</a></div><span>15 GBP</span></div>"
                for number in (1, 2)
            )
            + "</main>"
        )
    else:
        content = (
            "<main><h1>Travel book collection</h1><table><tr><td>From 15 to 40 GBP"
            '<div><a href="/features/hardcover">Hardcover binding</a></div>'
            "<p>Two editions available</p></td></tr></table><table><tbody>"
            + "".join(
                f'<tr><td><a href="/volume-{number}.html">Edition {number}</a></td>'
                "<td>Hardcover</td><td>30 GBP</td></tr>"
                for number in (1, 2)
            )
            + "</tbody></table></main>"
        )
    inventory = InventoryBuilder(
        allow_scroll=False, content_profile="commerce", currency_aliases={"GBP": "GBP"}
    ).build(make_page(url, content, 200))
    assert inventory.has_entity is False
    assert inventory.has_results is True
    assert {action.url for action in inventory.actions if action.hint == "item"} == {
        "https://catalog.example/volume-1.html",
        "https://catalog.example/volume-2.html",
    }


@pytest.mark.parametrize("video_type", ["VideoObject", "https://schema.org/VideoObject"])
def test_commerce_evidence_ignores_supplemental_video_but_media_schema_keeps_it(video_type):
    page = make_page(
        "https://catalog.example/item",
        "<main><h1>Travel book</h1>"
        '<p>15 GBP</p></main><script type="application/ld+json">'
        + json.dumps(
            {
                "@type": video_type,
                "name": "Travel book unboxing",
                "contentUrl": "https://video.example/unboxing",
            }
        )
        + "</script>",
        200,
    )
    commerce = CandidateBuilder(
        content_profile="commerce",
        currency_aliases={"GBP": "GBP"},
        schema={
            "properties": {
                "title": {"type": "string"},
                "price": {"type": "number"},
                "currency": {"type": "string"},
            }
        },
    ).build(page)
    assert not any(block.kind.value == "json_ld" for block in commerce)
    media = CandidateBuilder(
        schema={
            "properties": {
                "title": {"type": "string"},
                "contentUrl": {"type": "string"},
            }
        }
    ).build(page)
    assert any(
        "Travel book unboxing" in block.content for block in media if block.kind.value == "json_ld"
    )


@pytest.mark.asyncio
async def test_supplemental_media_cannot_make_one_product_change_identity_when_its_price_changes(
    tmp_path,
):
    schema = discovery_job(tmp_path).extraction_schema
    url = "https://catalog.example/volume-1"
    records = []
    for price in (15, 16):
        page = make_page(
            url,
            '<script type="application/ld+json">'
            + json.dumps(
                [
                    {
                        "@type": "Product",
                        "name": "Travel book",
                        "url": url,
                        "offers": {"price": price, "priceCurrency": "GBP"},
                    },
                    {"@type": "VideoObject", "name": "Travel book unboxing", "@id": url + "#video"},
                ]
            )
            + "</script>",
            200,
        )
        candidate = next(
            block
            for block in CandidateBuilder(
                schema=schema, content_profile="commerce", currency_aliases={"GBP": "GBP"}
            ).build(page)
            if block.kind.value == "json_ld"
        )
        extracted = await SchemaExtractor("discovery-review", content_profile="commerce").extract(
            page,
            candidate,
            ExtractionStrategy.JSON_LD,
            schema,
        )
        assert len(extracted) == 1
        records.extend(extracted)
    assert records[0].record_id == records[1].record_id
    assert [record.data["price"] for record in records] == [15, 16]
    assert all(record.provenance.identity_basis == "entity_url" for record in records)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "title,price",
    [
        ("Travel guide 2025-2026", 15),
        ("ThinkPad X1-2025", 15),
        ("Travel guide 2025", 15),
        ("Travel guide 2025", 100),
    ],
)
async def test_heading_years_are_not_price_ranges_or_part_of_the_own_dom_price(
    tmp_path, title, price
):
    class Detail:
        async def fetch(self, url):
            return make_page(url, f"<article><h1>{title}</h1><p>{price} GBP</p></article>", 200)

    job = discovery_job(
        tmp_path,
        sources=[{"url": "https://catalog.example/one"}],
        discovery={"enabled": False},
        search=None,
    )
    report = await PipelineRunner(job, data_dir=tmp_path / "data", fetcher=Detail()).run()
    assert report.status == "success", report.errors
    records = DatasetStore(tmp_path / "data").list_records()
    assert len(records) == 1
    assert records[0]["data"] == {"title": title, "price": price, "currency": "GBP"}


@pytest.mark.asyncio
async def test_merchant_cards_keep_prices_after_decoration_compaction_and_stop_before_related_items(
    tmp_path,
):
    schema = discovery_job(tmp_path).extraction_schema
    document = {
        "@type": "Product",
        "name": "Travel book family",
        "offers": {
            "@type": "AggregateOffer",
            "lowPrice": 15,
            "highPrice": 16,
            "priceCurrency": "GBP",
        },
    }
    html = '<script type="application/ld+json">' + json.dumps(document) + "</script>"
    html += (
        "<main><h1>Travel book family</h1><table><tbody>"
        + "".join(
            '<tr><td><svg><path d="' + "M" * 5000 + '"></path></svg></td>'
            f'<td><a href="/offer-{price}">Travel book hardcover '
            f"merchant edition number {price}</a></td>"
            f"<td>{price} GBP</td></tr>"
            for price in (15, 16)
        )
        + '</tbody></table><h2>Related books</h2><article><h3><a href="/other">'
    )
    html += "Unrelated vintage book</a></h3><p>999 GBP</p></article></main>"
    page = make_page("https://catalog.example/family", html, 200)
    candidates = CandidateBuilder(
        max_chars=2000, schema=schema, content_profile="commerce", currency_aliases={"GBP": "GBP"}
    ).build(page)
    extracted = []
    for candidate in candidates:
        assert "Unrelated vintage book" not in candidate.content
        extracted.extend(
            await SchemaExtractor(
                "review", content_profile="commerce", currency_aliases={"GBP": "GBP"}
            ).extract(
                page,
                candidate,
                ExtractionStrategy.DOM,
                schema,
            )
        )
    assert {record.data["price"] for record in extracted} == {15, 16}
    assert len(extracted) == 2


def test_explicit_current_single_offer_remains_entity_despite_many_recommendation_cards():
    url = "https://catalog.example/volume-one"
    entity = {
        "@type": "Product",
        "name": "Travel book",
        "url": url,
        "offers": {"@type": "Offer", "price": 15, "priceCurrency": "GBP"},
    }
    html = '<script type="application/ld+json">' + json.dumps(entity) + "</script>"
    html += (
        "<main><h1>Travel book</h1><section><h2>Related books</h2>"
        + "".join(
            f'<article><h3><a href="/related-{index}">Other book {index}</a></h3>'
            "<p>25 GBP</p></article>"
            for index in range(20)
        )
        + "</section></main>"
    )
    inventory = InventoryBuilder(content_profile="commerce", currency_aliases={"GBP": "GBP"}).build(
        make_page(url, html, 200)
    )
    assert inventory.has_entity is True
    assert inventory.has_results is False

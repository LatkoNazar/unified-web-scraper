"""Discovery seeds share bounded navigation and leave an auditable source ledger."""

import json
from datetime import UTC, datetime

import pytest

from unified_web_parser.core.config import JobConfig, LoadedJob
from unified_web_parser.core.exceptions import ProviderError
from unified_web_parser.crawling.pages import make_page
from unified_web_parser.pipeline.runner import PipelineRunner
from unified_web_parser.providers.search import SearchBatch, SearchResult


def job(tmp_path, **updates):
    values = {
        "name": "discovered-books",
        "schema_path": "schema.json",
        "search": "Books",
        "sources": [],
        "ai": {"enabled": False, "max_calls": 5, "routing": "rules"},
        "navigation": {"mode": "rules"},
        "discovery": {
            "enabled": True,
            "search_results": 3,
            "pages_per_site": 2,
            "max_queries": 1,
            "entrypoint": "result_url",
        },
        "crawl": {"max_pages": 10, "concurrency": 1, "delay": 0},
    }
    for key, value in updates.items():
        values[key] = {**values.get(key, {}), **value} if isinstance(value, dict) else value
    return LoadedJob(
        config=JobConfig.model_validate(values),
        config_path=tmp_path / "job.yaml",
        schema_path=tmp_path / "schema.json",
        extraction_schema={
            "type": "object",
            "required": ["title"],
            "properties": {"title": {"type": "string"}},
        },
    )


class Search:
    def __init__(self, urls):
        self.urls = urls
        self.calls = []

    async def search(self, query, **options):
        self.calls.append((query, options))
        return SearchBatch(
            query=query,
            fetched_at=datetime.now(UTC),
            results=[
                SearchResult(url=url, citation_order=index)
                for index, url in enumerate(self.urls, 1)
            ],
        )


class Fetcher:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    async def fetch(self, url):
        self.calls.append(url)
        html = self.pages[url]
        if isinstance(html, Exception):
            raise html
        return make_page(url, html, 200)


def entity(url):
    return (
        '<script type="application/ld+json">'
        + json.dumps(
            {"@type": "Product", "name": "Books", "url": url, "description": "Books in stock"}
        )
        + "</script>"
    )


def listings(*urls):
    return (
        "<main><h1>Books</h1>"
        + "".join(
            f'<article><h2><a href="{url}">Books</a></h2><p>10 EUR</p></article>' for url in urls
        )
        + "</main>"
    )


@pytest.mark.asyncio
async def test_all_seeds_precede_descendants_and_www_shares_one_site_cap(tmp_path):
    first, second, third = (
        "https://www.books.example/start",
        "https://books.example/other",
        "https://guides.example/start",
    )
    pages = Fetcher(
        {
            first: listings("https://www.books.example/products/1"),
            second: listings("https://books.example/products/2"),
            third: entity(third),
        }
    )
    report = await PipelineRunner(
        job(tmp_path),
        data_dir=tmp_path / "data",
        fetcher=pages,
        search_client=Search([first, second, third]),
    ).run()
    assert report.status == "success", report.errors
    assert pages.calls == [first, second, third]
    assert report.navigation["site_attempts"] == {"books.example": 2, "guides.example": 1}
    outcomes = report.navigation["source_outcomes"]
    assert len(outcomes) == 3
    assert [item["attempted"] for item in outcomes] == [1, 1, 1]
    assert outcomes[0]["stop_reason"] == outcomes[1]["stop_reason"] == "pages_per_site"
    assert outcomes[2]["records_added"] == 1


@pytest.mark.asyncio
async def test_global_cap_marks_later_selected_sources_unvisited(tmp_path):
    urls = [f"https://site{index}.example/item" for index in range(3)]
    pages = Fetcher({url: entity(url) for url in urls})
    report = await PipelineRunner(
        job(tmp_path, crawl={"max_pages": 1}),
        data_dir=tmp_path / "data",
        fetcher=pages,
        search_client=Search(urls),
    ).run()
    assert pages.calls == urls[:1]
    assert report.pages_attempted == 1
    assert report.discovery["resolved_sources"] == urls
    assert report.navigation["stop_reason"] == "max_pages"
    for source in report.navigation["source_outcomes"][1:]:
        assert source["status"] == "not_visited"
        assert source["attempted"] == 0
        assert source["stop_reason"] == "max_pages"


@pytest.mark.asyncio
async def test_failed_requests_and_ui_transitions_consume_per_site_cap(tmp_path):
    root, failure = "https://books.example/", "https://books.example/unavailable"

    class Browser(Fetcher):
        async def interact(self, page, action, query=None):
            self.calls.append("interaction:" + action.kind)
            return make_page(str(page.url), '<input type="search" name="q">', 200)

    pages = Browser(
        {root: '<button aria-label="Search">Search</button>', failure: OSError("offline")}
    )
    report = await PipelineRunner(
        job(tmp_path, crawl={"backend": "browser"}, discovery={"pages_per_site": 3}),
        data_dir=tmp_path / "data",
        fetcher=pages,
        search_client=Search([root, failure]),
    ).run()
    assert report.status == "failed"  # No valid records were produced by these UI-only states.
    assert pages.calls == [root, failure, "interaction:click"]
    assert report.pages_attempted == 3
    assert report.navigation["site_attempts"] == {"books.example": 3}
    assert report.navigation["interactions_attempted"] == 1
    assert report.navigation["source_outcomes"][1]["status"] == "failed"


@pytest.mark.asyncio
async def test_planner_failure_is_explicit_literal_fallback_without_another_model_call(tmp_path):
    url = "https://books.example/item"

    class BadPlan:
        calls = 0

        async def complete_json(self, *_args, **_kwargs):
            self.calls += 1
            raise ProviderError("Invalid structured JSON")

    planner, search = BadPlan(), Search([url])
    report = await PipelineRunner(
        job(tmp_path, ai={"enabled": True}),
        data_dir=tmp_path / "data",
        fetcher=Fetcher({url: entity(url)}),
        llm=BadPlan(),
        query_client=planner,
        search_client=search,
    ).run()
    assert report.status == "success", report.errors
    assert planner.calls == 1
    assert search.calls[0][0] == "Books"
    assert report.discovery["effective_query_mode"] == "literal_fallback"
    assert report.discovery["planning_error"]["message"] == "Invalid structured JSON"
    assert report.discovery["network_used"] is True
    assert any(item["stage"] == "query_planning" for item in report.warnings)


@pytest.mark.asyncio
async def test_union_preserves_explicit_sources_but_deduplicates_tracking_citations(tmp_path):
    explicit, discovered = "https://books.example/item", "https://guides.example/item"
    search = Search([explicit + "?utm_source=search", discovered])
    pages = Fetcher({url: entity(url) for url in [explicit, discovered]})
    loaded = job(tmp_path, sources=[{"url": explicit}])
    report = await PipelineRunner(
        loaded,
        data_dir=tmp_path / "data",
        fetcher=pages,
        search_client=search,
    ).run()
    assert report.status == "success", report.errors
    assert report.discovery["resolved_sources"] == [explicit, discovered]
    assert pages.calls == [explicit, discovered]
    assert len(loaded.config.sources) == 1

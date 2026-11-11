"""Independent site-first discovery checks using local clients and fake pages only."""

import json
from datetime import UTC, datetime

import pytest

from unified_web_parser.core.config import JobConfig, LoadedJob
from unified_web_parser.core.exceptions import ProviderError
from unified_web_parser.crawling.pages import make_page
from unified_web_parser.discovery.sites import site_identity, site_root
from unified_web_parser.pipeline.runner import PipelineRunner
from unified_web_parser.providers.search import SearchBatch, SearchResult


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://shop.alpha.co.uk/path", "alpha.co.uk"),
        ("https://www.alpha.co.uk/other", "alpha.co.uk"),
        ("https://tenant-a.github.io/path", "tenant-a.github.io"),
        ("https://tenant-b.github.io/path", "tenant-b.github.io"),
        ("https://tenant-a.appspot.com/path", "tenant-a.appspot.com"),
        ("https://a.notarealsuffix/path", "a.notarealsuffix"),
        ("https://b.notarealsuffix/path", "b.notarealsuffix"),
        ("https://shop.a.notarealsuffix/path", "shop.a.notarealsuffix"),
        ("https://BÜCHER.DE./path", "xn--bcher-kva.de"),
        ("https://8.8.8.8/path", "8.8.8.8"),
        ("https://[2606:4700:4700::1111]/path", "2606:4700:4700::1111"),
    ],
)
def test_site_identity_keeps_private_tenants_and_unknown_suffix_hosts_separate(url, expected):
    assert site_identity(url) == expected


def test_root_preserves_observed_origin_instead_of_inventing_registrable_apex():
    assert site_root("https://shop.alpha.co.uk:8443/special/item?query=secret#part") == (
        "https://shop.alpha.co.uk:8443/"
    )


def loaded_job(tmp_path):
    return LoadedJob(
        config=JobConfig.model_validate(
            {
                "name": "site-first-review",
                "sources": [],
                "schema_path": "schema.json",
                "search": "Find travel books",
                "discovery": {"enabled": True, "max_sites": 2},
                "ai": {"enabled": True, "max_calls": 10, "routing": "rules"},
                "navigation": {"mode": "rules"},
                "crawl": {"max_pages": 4, "concurrency": 1, "delay": 0},
            }
        ),
        config_path=tmp_path / "job.yaml",
        schema_path=tmp_path / "schema.json",
        extraction_schema={
            "type": "object",
            "required": ["title", "url"],
            "properties": {"title": {"type": "string"}, "url": {"type": "string"}},
        },
    )


QUERIES = ["travel books available", "journey guidebooks offered", "tourism reading catalog"]


class Planner:
    async def complete_json(self, *_args, **_kwargs):
        return {"queries": QUERIES, "site_search": "Travel books"}


class NoInference:
    async def complete_json(self, *_args, **_kwargs):
        pytest.fail("Explicit structured fields unexpectedly required inference")


class RootPages:
    def __init__(self):
        self.urls = []

    async def fetch(self, url):
        self.urls.append(url)
        return make_page(
            url,
            '<script type="application/ld+json">'
            + json.dumps(
                {
                    "@type": "Product",
                    "name": "Travel books",
                    "url": url,
                }
            )
            + "</script>",
            200,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_first", [False, True])
async def test_all_three_full_queries_run_before_site_cap_with_first_origin_and_full_attribution(
    tmp_path,
    failed_first,
):
    returned = {
        QUERIES[0]: [
            "https://shop.alpha.co.uk/offer/one?utm_source=ads",
            "https://tenant-a.github.io/book/one",
        ]
        + [f"https://first-{index}.org/catalog/item" for index in range(23)],
        QUERIES[1]: ["http://www.alpha.co.uk/another/path", "https://tenant-b.github.io/book/two"]
        + [f"https://second-{index}.net/catalog/item" for index in range(23)],
        QUERIES[2]: ["https://sub.alpha.co.uk/last/path", "https://beta.github.io/book/three"]
        + [f"https://third-{index}.org/catalog/item" for index in range(23)],
    }
    requests = []

    class Search:
        async def search(self, query, **options):
            requests.append((query, options["limit"]))
            if failed_first and query == QUERIES[0]:
                raise ProviderError("Temporary upstream failure")
            return SearchBatch(
                query=query,
                fetched_at=datetime.now(UTC),
                results=[
                    SearchResult(url=url, citation_order=index)
                    for index, url in enumerate(returned[query], 1)
                ],
            )

    pages = RootPages()
    report = await PipelineRunner(
        loaded_job(tmp_path),
        data_dir=tmp_path / "data",
        fetcher=pages,
        query_client=Planner(),
        search_client=Search(),
        llm=NoInference(),
    ).run()
    assert report.status == ("partial" if failed_first else "success"), report.errors
    assert requests == [(query, 25) for query in QUERIES]
    assert len(report.discovery["sites"]) == 2
    assert len(report.discovery["results"]) == (50 if failed_first else 75)
    expected = (
        ["http://www.alpha.co.uk/", "https://tenant-b.github.io/"]
        if failed_first
        else ["https://shop.alpha.co.uk/", "https://tenant-a.github.io/"]
    )
    assert pages.urls == report.discovery["resolved_sources"] == expected
    alpha = next(site for site in report.discovery["sites"] if site["site"] == "alpha.co.uk")
    expected_queries = QUERIES[1:] if failed_first else QUERIES
    assert alpha["queries"] == expected_queries
    assert alpha["root_url"] == expected[0]
    assert all(
        site["selection_reason"] == "max_sites" for site in report.discovery["excluded_sites"]
    )
    assert all(result["entrypoint_url"].endswith("/") for result in report.discovery["results"])
    assert report.records_written == 2


@pytest.mark.asyncio
async def test_exhausted_search_budget_marks_remaining_planned_variants_not_attempted(tmp_path):
    class Search:
        async def search(self, *_args, **_kwargs):
            raise ProviderError("OpenRouter request budget exhausted (10)")

    pages = RootPages()
    report = await PipelineRunner(
        loaded_job(tmp_path),
        data_dir=tmp_path / "data",
        fetcher=pages,
        query_client=Planner(),
        search_client=Search(),
        llm=NoInference(),
    ).run()
    assert report.status == "failed"
    assert pages.urls == []
    assert [outcome["status"] for outcome in report.discovery["query_outcomes"]] == [
        "failed",
        "not_attempted",
        "not_attempted",
    ]


@pytest.mark.asyncio
async def test_visited_site_with_unexecuted_search_reports_global_page_cap(tmp_path):
    class Search:
        async def search(self, query, **_options):
            return SearchBatch(
                query=query,
                fetched_at=datetime.now(UTC),
                results=[SearchResult(url="https://books.example/deep/item", citation_order=1)],
            )

    class Pages:
        async def fetch(self, url):
            assert url == "https://books.example/"
            return make_page(
                url,
                '<form action="/search"><input type="search" name="q">'
                "<button>Search</button></form>",
                200,
            )

    job = loaded_job(tmp_path)
    job = job.model_copy(
        update={
            "config": job.config.model_copy(
                update={"crawl": job.config.crawl.model_copy(update={"max_pages": 1})}
            )
        }
    )
    report = await PipelineRunner(
        job,
        data_dir=tmp_path / "data",
        fetcher=Pages(),
        query_client=Planner(),
        search_client=Search(),
        llm=NoInference(),
    ).run()
    source = report.discovery["source_outcomes"][0]
    assert source["status"] == "visited"
    assert source["fetched"] == 1
    assert source["pending_states"] == 1
    assert source["stop_reason"] == "max_pages"
    assert report.records_written == 0

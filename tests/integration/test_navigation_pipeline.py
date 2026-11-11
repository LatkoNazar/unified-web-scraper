"""State-graph execution uses observed actions, not predetermined site URLs."""

import asyncio
import json
from collections import OrderedDict
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from unified_web_parser.core.config import JobConfig, LoadedJob
from unified_web_parser.crawling.pages import make_page
from unified_web_parser.navigation import NavigationPlan, NavigationPlanner
from unified_web_parser.pipeline.crawl import CrawlEngine
from unified_web_parser.pipeline.report import RunReport
from unified_web_parser.pipeline.runner import PipelineRunner
from unified_web_parser.providers import decisions, openrouter
from unified_web_parser.storage import DatasetStore


def loaded_job(tmp_path, *, query=None, crawl=None, navigation=None, sources=None, ai=None):
    return LoadedJob(
        config=JobConfig.model_validate(
            {
                "name": "navigation-test",
                "schema_path": "schema.json",
                "search": query,
                "sources": sources or [{"url": "https://market.example/"}],
                "crawl": {
                    "max_pages": 20,
                    "max_records": 10,
                    "max_depth": 2,
                    "concurrency": 1,
                    "delay": 0,
                    **(crawl or {}),
                },
                "navigation": {"mode": "rules", **(navigation or {})},
                "ai": {"enabled": False, **(ai or {})},
            }
        ),
        config_path=tmp_path / "job.yaml",
        schema_path=tmp_path / "schema.json",
        extraction_schema={
            "type": "object",
            "required": ["title"],
            "properties": {"title": {"type": "string"}},
        },
    )


def entity(name, url):
    return (
        '<script type="application/ld+json">'
        + json.dumps(
            {
                "@type": "Product",
                "name": name,
                "url": url,
                "description": f"Details about {name}",
            }
        )
        + "</script>"
    )


def audit(report):
    return [
        json.loads(line)
        for line in Path(report.artifacts["navigation"]).read_text(encoding="utf-8").splitlines()
    ]


class Fetcher:
    def __init__(self, pages):
        self.pages = pages
        self.urls = []

    async def fetch(self, url):
        self.urls.append(url)
        return make_page(url, self.pages[url], 200)


@pytest.mark.asyncio
async def test_pagination_does_not_consume_entity_depth_and_precedes_detail_frontier(tmp_path):
    root = "https://market.example/catalog"
    pages = {}
    catalog_urls = [root] + [root + f"?page={index}" for index in range(2, 7)]
    for index, url in enumerate(catalog_urls, 1):
        detail = f"https://market.example/products/{index}"
        pages[url] = f'<a href="{detail}">Entity {index}</a>' + (
            f'<a rel="next" href="{catalog_urls[index]}">Next</a>' if index < 6 else ""
        )
        pages[detail] = entity(f"Entity {index}", detail)
    fetcher = Fetcher(pages)
    job = loaded_job(tmp_path, sources=[{"url": root}], crawl={"max_depth": 1})
    report = await PipelineRunner(job, data_dir=tmp_path / "data", fetcher=fetcher).run()
    assert report.records_written == 6 and report.status == "success", report.errors
    assert fetcher.urls[:6] == catalog_urls
    assert report.navigation["coverage_status"] == "observed_frontier_exhausted"
    assert report.navigation["incomplete_reasons"] == []
    assert all(
        row["depth"] == (1 if row["inventory"]["has_entity"] else 0)
        for row in audit(report)
        if row["event"] == "inventory"
    )


@pytest.mark.asyncio
async def test_empty_frontier_does_not_hide_inventory_truncation(tmp_path):
    root = "https://market.example/catalog"
    pages = {root: "".join(f'<a href="/products/{index}">Item {index}</a>' for index in range(5))}
    for index in range(5):
        url = f"https://market.example/products/{index}"
        pages[url] = entity(f"Item {index}", url)
    report = await PipelineRunner(
        loaded_job(tmp_path, sources=[{"url": root}], navigation={"max_actions": 2}),
        data_dir=tmp_path / "data",
        fetcher=Fetcher(pages),
    ).run()
    assert report.records_written == 2
    assert report.navigation["stop_reason"] == "frontier_exhausted"
    assert report.navigation["coverage_scope"] == "observed_navigation"
    assert report.navigation["coverage_status"] == "incomplete"
    assert "inventory_limit" in report.navigation["incomplete_reasons"]


async def test_global_record_limit_marks_visited_root_with_queued_search_as_incomplete(tmp_path):
    first, second = "https://first.example/", "https://second.example/products/book"
    job = loaded_job(
        tmp_path,
        query="book",
        crawl={"max_records": 1, "concurrency": 2},
        sources=[{"url": first}, {"url": second}],
    )
    fetcher = Fetcher(
        {
            first: '<form action="/find"><input type="search" name="q"></form>',
            second: entity("Book", second),
        }
    )
    report = await PipelineRunner(job, data_dir=tmp_path / "data", fetcher=fetcher).run()
    sources = report.navigation["source_outcomes"]
    root = next(source for source in sources if source["url"] == first)
    assert root["status"] == "visited" and root["fetched"] == 1
    assert root["pending_states"] == 1 and root["stop_reason"] == "max_records"
    assert first + "find?q=book" not in fetcher.urls


@pytest.mark.asyncio
async def test_search_icon_input_results_detail_uses_current_state_and_user_query(tmp_path):
    class Browser(Fetcher):
        def __init__(self):
            super().__init__(
                {
                    "https://market.example/": '<button aria-label="Search">Search</button>',
                    "https://market.example/view?id=42": entity(
                        "Actual camera", "https://market.example/view?id=42"
                    ),
                }
            )
            self.actions = []
            self.current_page_id = None

        async def fetch(self, url):
            page = await super().fetch(url)
            self.current_page_id = page.page_id
            return page

        async def interact(self, page, action, query=None):
            assert page.page_id == action.page_id == self.current_page_id
            assert query == "camera 42"
            self.actions.append(action.kind)
            if action.kind == "click":
                html = '<input type="search" name="keyword" aria-label="Search products">'
            else:
                assert action.kind == "search"
                html = '<article><a href="/view?id=42"><h2>Actual camera</h2></a></article>'
            result = make_page(str(page.url), html, 200)
            self.current_page_id = result.page_id
            return result

    fetcher = Browser()
    loaded = loaded_job(tmp_path, query="camera 42", crawl={"backend": "browser", "max_records": 1})
    report = await PipelineRunner(loaded, data_dir=tmp_path / "data", fetcher=fetcher).run()
    assert report.status == "success", report.errors
    assert fetcher.urls == ["https://market.example/", "https://market.example/view?id=42"]
    assert fetcher.actions == ["click", "search"]
    assert report.pages_attempted == 4
    assert report.navigation["interactions_attempted"] == 2
    assert report.navigation["actions_attempted"] == 3
    assert report.navigation["stop_reason"] == "max_records"
    assert DatasetStore(tmp_path / "data").list_records()[0]["data"]["title"] == "Actual camera"
    events = {event["event"] for event in audit(report)}
    assert {"inventory", "plan", "action", "execute", "result", "stop"} <= events


@pytest.mark.asyncio
async def test_get_search_is_observed_preserves_hidden_fields_and_ignores_featured_links(tmp_path):
    root = "https://market.example/"
    search = "https://market.example/custom-find?department=photos&keywords=camera+42"
    detail = "https://market.example/products/42"
    fetcher = Fetcher(
        {
            root: '<form method="get" action="/custom-find"><input type="hidden" name="department" '
            'value="photos"><input type="search" name="keywords"></form>'
            '<a href="/products/unrelated">Featured phone</a>',
            search: '<a href="/products/42">Actual camera</a>',
            detail: entity("Camera 42", detail),
        }
    )
    report = await PipelineRunner(
        loaded_job(tmp_path, query="camera 42"), data_dir=tmp_path / "data", fetcher=fetcher
    ).run()
    assert report.status == "success", report.errors
    assert fetcher.urls == [root, search, detail]
    assert all("unrelated" not in url for url in fetcher.urls)


@pytest.mark.asyncio
async def test_provided_search_page_with_matching_observed_get_form_is_not_resubmitted(tmp_path):
    source = "https://market.example/custom-find?q=camera"
    detail = "https://market.example/products/camera"
    fetcher = Fetcher(
        {
            source: '<form action="/custom-find">'
            '<input type="search" name="q" value="camera"></form>'
            '<a href="/products/camera">Camera</a>',
            detail: entity("Camera", detail),
        }
    )
    report = await PipelineRunner(
        loaded_job(tmp_path, query="camera", sources=[{"url": source}]),
        data_dir=tmp_path / "data",
        fetcher=fetcher,
    ).run()
    assert report.status == "success", report.errors
    assert fetcher.urls == [source, detail]
    plans = [event for event in audit(report) if event["event"] == "inventory"]
    assert plans[0]["search_done"] is True


@pytest.mark.asyncio
async def test_primary_search_defers_featured_links_even_from_a_mixed_model_plan(
    tmp_path, monkeypatch
):
    class MixedPlanner(NavigationPlanner):
        async def plan(self, inventory, query, schema, **kwargs):
            if not kwargs.get("search_done"):
                return NavigationPlan(
                    page_id=inventory.page_id,
                    action_ids=tuple(
                        action.action_id
                        for action in inventory.actions
                        if action.kind in {"search", "open"}
                    ),
                )
            return await super().plan(inventory, query, schema, **kwargs)

    monkeypatch.setattr("unified_web_parser.pipeline.crawl.NavigationPlanner", MixedPlanner)
    root = "https://market.example/"
    search = "https://market.example/find?q=camera"
    detail = "https://market.example/products/camera"
    fetcher = Fetcher(
        {
            root: '<form action="/find"><input type="search" name="q"></form>'
            '<a href="/products/phone">Featured unrelated phone</a>',
            search: '<a href="/products/camera">Actual camera</a>',
            detail: entity("Camera", detail),
        }
    )
    report = await PipelineRunner(
        loaded_job(tmp_path, query="camera"), data_dir=tmp_path / "data", fetcher=fetcher
    ).run()
    assert report.status == "success", report.errors
    assert fetcher.urls == [root, search, detail]
    assert report.navigation["branch_stops"]["state_transition_deferred"] == 1


@pytest.mark.asyncio
async def test_unknown_navigation_action_is_never_executed_and_falls_back_to_rules(
    tmp_path,
):
    class InvalidPlanner:
        def __init__(self, **_kwargs):
            return None

        async def plan(self, inventory, *_args, **_kwargs):
            return NavigationPlan(page_id=inventory.page_id, action_ids=("invented-action",))

    # Keep fallback rules real while the injected primary planner returns a bad ID.
    loaded = loaded_job(tmp_path)
    report = RunReport(run_id="test", job_name=loaded.config.name, started_at=datetime.now(UTC))
    root = "https://market.example/"
    fetched = Fetcher({root: entity("Camera", root)})
    extracted = []

    async def extract(page):
        extracted.append(page)

    await CrawlEngine(
        loaded.config,
        loaded.extraction_schema,
        fetcher=fetched,
        report=report,
        run_dir=tmp_path,
        save_page=lambda page, **_kwargs: page,
        extract_page=extract,
        record_count=lambda: len(extracted),
        planner=InvalidPlanner(),
    ).run()
    assert len(extracted) == 1
    assert fetched.urls == [root]
    assert any("planner failed" in warning["message"] for warning in report.warnings)
    assert not any(event.get("action_id") == "invented-action" for event in audit(report))


@pytest.mark.asyncio
async def test_unchanged_interaction_stops_instead_of_looping(tmp_path):
    html = '<button aria-label="Search">Search</button>'

    class Browser(Fetcher):
        async def interact(self, page, action, query=None):
            return make_page(str(page.url), html, 200)

    fetcher = Browser({"https://market.example/": html})
    report = await PipelineRunner(
        loaded_job(tmp_path, query="camera", crawl={"backend": "browser"}),
        data_dir=tmp_path / "data",
        fetcher=fetcher,
    ).run()
    assert report.status == "failed"
    assert report.pages_attempted == 2
    assert report.navigation["unchanged_states"] == 1
    assert any(item.get("reason") == "unchanged_state" for item in audit(report))


@pytest.mark.asyncio
async def test_changing_same_url_states_respect_interaction_and_total_page_budgets(tmp_path):
    class Browser(Fetcher):
        attempts = 0

        async def interact(self, page, action, query=None):
            self.attempts += 1
            return make_page(
                str(page.url), f"<h1>State {self.attempts}</h1><button>Search</button>", 200
            )

    fetcher = Browser({"https://market.example/": "<h1>State 0</h1><button>Search</button>"})
    report = await PipelineRunner(
        loaded_job(
            tmp_path,
            query="camera",
            crawl={"backend": "browser", "max_pages": 10},
            navigation={"max_interactions_per_url": 2},
        ),
        data_dir=tmp_path / "data",
        fetcher=fetcher,
    ).run()
    assert report.pages_attempted == 3
    assert fetcher.attempts == 2
    assert report.navigation["branch_stops"]["interaction_limit"] == 1
    limited = await PipelineRunner(
        loaded_job(tmp_path, query="camera", crawl={"backend": "browser", "max_pages": 2}),
        data_dir=tmp_path / "limited",
        fetcher=Browser(fetcher.pages),
    ).run()
    assert limited.pages_attempted == 2
    assert limited.navigation["stop_reason"] == "max_pages"


@pytest.mark.asyncio
async def test_pagination_does_not_consume_entity_depth_on_every_page(tmp_path):
    pages = {}
    for index in range(1, 4):
        url = "https://market.example/" if index == 1 else f"https://market.example/?page={index}"
        pages[url] = f'<a href="/products/{index}">Camera {index}</a>'
        if index < 3:
            pages[url] += f'<a rel="next" href="?page={index + 1}">Next</a>'
        detail = f"https://market.example/products/{index}"
        pages[detail] = entity(f"Camera {index}", detail)
    fetcher = Fetcher(pages)
    report = await PipelineRunner(
        loaded_job(tmp_path, crawl={"max_depth": 1, "max_pages": 6, "concurrency": 2}),
        data_dir=tmp_path / "data",
        fetcher=fetcher,
    ).run()
    assert report.status == "success", report.errors
    assert report.records_written == 3
    assert report.pages_attempted == 6
    assert "https://market.example/products/3" in fetcher.urls


@pytest.mark.asyncio
async def test_http_browser_only_action_reports_explicit_failure_without_guessing_url(tmp_path):
    fetcher = Fetcher({"https://market.example/": '<button aria-label="Search">Search</button>'})
    report = await PipelineRunner(
        loaded_job(tmp_path, query="camera"), data_dir=tmp_path / "data", fetcher=fetcher
    ).run()
    assert report.status == "failed"
    assert fetcher.urls == ["https://market.example/"]
    assert report.pages_attempted == 1  # Capability preflight made no fake interaction attempt.
    assert any("browser backend" in warning["message"] for warning in report.warnings)
    assert report.navigation["branch_stops"]["unsupported_backend"] == 1


@pytest.mark.asyncio
async def test_independent_http_opens_keep_configured_concurrency_and_page_bound(tmp_path):
    class ConcurrentFetcher(Fetcher):
        active = peak = 0

        async def fetch(self, url):
            self.active += 1
            self.peak = max(self.active, self.peak)
            await asyncio.sleep(0.01)
            result = await super().fetch(url)
            self.active -= 1
            return result

    urls = [f"https://market.example/products/{index}" for index in range(3)]
    fetcher = ConcurrentFetcher({url: entity(url, url) for url in urls})
    report = await PipelineRunner(
        loaded_job(
            tmp_path,
            sources=[{"url": url} for url in urls],
            crawl={"concurrency": 2, "max_pages": 2},
        ),
        data_dir=tmp_path / "data",
        fetcher=fetcher,
    ).run()
    assert report.status == "success", report.errors
    assert fetcher.peak == 2
    assert report.pages_attempted == report.pages_fetched == 2
    assert len(fetcher.urls) == 2


@pytest.mark.asyncio
async def test_navigation_native_client_has_own_stats_and_shared_attempt_budget(
    tmp_path, monkeypatch
):
    captured = []
    created = {}
    real_chat = openrouter.OpenRouterClient
    real_native = decisions.OpenRouterDecisionsClient

    def respond(request):
        payload = json.loads(request.content)
        captured.append(payload)
        assert request.url.path == "/api/alpha/decisions"
        return httpx.Response(
            200,
            json={
                "model": payload["model"],
                "answers": {
                    "next_action": {"type": "choice", "choice": "extract"},
                    "extract_current": {"type": "choice", "choice": "yes"},
                },
                "usage": {"input_tokens": 12, "output_tokens": 1, "cost": 0.0000012},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:

        def chat(**kwargs):
            created["extraction"] = real_chat(**kwargs, client=http)
            return created["extraction"]

        def native(**kwargs):
            created["navigation"] = real_native(**kwargs, client=http)
            return created["navigation"]

        monkeypatch.setattr(openrouter, "OpenRouterClient", chat)
        monkeypatch.setattr(decisions, "OpenRouterDecisionsClient", native)
        monkeypatch.setenv("OPENROUTER_API_KEY", "mock-navigation-only")
        monkeypatch.delenv("OPENROUTER_MODEL", raising=False)
        monkeypatch.delenv("OPENROUTER_DECISION_MODEL", raising=False)
        root = "https://market.example/"
        report = await PipelineRunner(
            loaded_job(
                tmp_path,
                navigation={"mode": "ai"},
                ai={"enabled": True, "max_calls": 1, "routing": "rules"},
            ),
            data_dir=tmp_path / "data",
            fetcher=Fetcher({root: entity("Camera", root)}),
        ).run()
    assert report.status == "success", report.errors
    assert len(captured) == 1
    assert created["extraction"].budget is created["navigation"].budget
    assert report.ai["calls"] == report.ai["budget_used"] == 1
    assert report.ai["stages"]["navigation"]["calls"] == 1
    assert report.ai["stages"]["extraction"]["calls"] == 0
    assert "routing" not in report.ai["stages"]


@pytest.mark.asyncio
async def test_replay_uses_saved_entity_roles_and_never_invokes_navigation_client(tmp_path):
    root = "https://market.example/"
    detail = "https://market.example/products/camera"
    loaded = loaded_job(tmp_path)
    first = await PipelineRunner(
        loaded,
        data_dir=tmp_path / "data",
        fetcher=Fetcher(
            {root: '<a href="/products/camera">Camera</a>', detail: entity("Camera", detail)}
        ),
    ).run()

    class NoNavigation:
        async def decide(self, *_args):
            pytest.fail("Replay performed navigation inference")

    replay = await PipelineRunner(
        loaded, data_dir=tmp_path / "data", navigation_client=NoNavigation()
    ).run(replay_dir=Path(first.artifacts["directory"]))
    assert first.status == replay.status == "success"
    assert first.records_written == replay.records_written == 1
    assert "navigation" not in replay.ai.get("stages", {})
    assert len(DatasetStore(tmp_path / "data").list_records()) == 1


@pytest.mark.asyncio
async def test_terminal_children_are_released_before_they_can_evict_parent_continuation(tmp_path):
    root = "https://market.example/"
    initial = "".join(f'<a href="/products/{index}">Camera {index}</a>' for index in range(1, 4))
    initial += '<button id="more">Load more</button>'
    pages = {
        root: initial,
        **{
            f"https://market.example/products/{index}": entity(
                f"Camera {index}", f"https://market.example/products/{index}"
            )
            for index in range(1, 5)
        },
    }

    class SmallBrowser(Fetcher):
        def __init__(self):
            super().__init__(pages)
            self.sessions = OrderedDict()
            self.evicted = []
            self.released = []
            self.interactions = 0

        def retain(self, page):
            if len(self.sessions) >= 2:
                self.evicted.append(self.sessions.popitem(last=False)[0])
            self.sessions[page.page_id] = page

        async def fetch(self, url):
            page = await super().fetch(url)
            self.retain(page)
            return page

        async def release(self, page):
            if page.page_id not in self.sessions:
                return False
            self.sessions.pop(page.page_id)
            self.released.append(page.page_id)
            return True

        async def interact(self, page, action, query=None):
            assert page.page_id in self.sessions, "Parent was evicted by terminal child tabs"
            assert action.kind == "click" and action.hint == "load_more"
            self.interactions += 1
            self.sessions.pop(page.page_id)
            updated = make_page(root, '<a href="/products/4">Camera 4</a>', 200)
            self.retain(updated)
            return updated

    fetcher = SmallBrowser()
    report = await PipelineRunner(
        loaded_job(tmp_path, crawl={"backend": "browser"}, navigation={"allow_scroll": False}),
        data_dir=tmp_path / "data",
        fetcher=fetcher,
    ).run()
    assert report.status == "success", report.errors
    assert report.records_written == 4
    assert fetcher.interactions == 1
    assert fetcher.evicted == []
    assert fetcher.sessions == {}
    assert report.navigation["pages_released"] >= 5

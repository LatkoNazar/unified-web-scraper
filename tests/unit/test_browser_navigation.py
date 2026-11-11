import json
from types import SimpleNamespace

import pytest

from unified_web_parser.crawling.errors import FetchError
from unified_web_parser.navigation.inventory import InventoryBuilder
from unified_web_parser.providers.crawl4ai import Crawl4AIFetcher

HTML = """<html><body><h1>Catalog</h1>
<button id="more">Load more</button>
<select name="condition"><option value="all" selected>All</option>
<option value="used">Used</option></select>
<input type="search" name="q" aria-label="Search">
<a href="/products/one">Open product one</a></body></html>"""


@pytest.fixture
def browser(monkeypatch):
    class Crawler:
        def __init__(self, **_kwargs):
            self.calls = []
            self.closed_sessions = []
            self.next_execution = None
            self.next_html = None
            self.redirect = None
            self.crawler_strategy = SimpleNamespace(kill_session=self.kill_session)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def kill_session(self, session_id):
            self.closed_sessions.append(session_id)

        async def arun(self, *, url, config):
            self.calls.append((url, config))
            if config.get("js_only"):
                parameters = json.loads(config["js_code"].split(";\n", 1)[0][10:])
                html = HTML.replace("</body>", f"<p>{parameters['kind']}: changed</p></body>")
                if self.next_html is not None:
                    html = self.next_html
                    self.next_html = None
                execution = self.next_execution or {
                    "success": True,
                    "results": [{"ok": True, "changed": True}],
                }
                self.next_execution = None
            else:
                html, execution = HTML, None
            return SimpleNamespace(
                success=True,
                status_code=200,
                html=html,
                url=url,
                redirected_url=self.redirect or url,
                js_execution_result=execution,
            )

    sdk = SimpleNamespace(
        BrowserConfig=lambda **kwargs: kwargs,
        CrawlerRunConfig=lambda **kwargs: kwargs,
        AsyncWebCrawler=Crawler,
        CacheMode=SimpleNamespace(BYPASS="bypass"),
    )
    monkeypatch.setattr(
        "unified_web_parser.providers.crawl4ai.importlib.import_module", lambda _: sdk
    )


def action(page, kind, *, allow_filters=False, **matching):
    return next(
        item
        for item in InventoryBuilder(allow_filters=allow_filters).build(page).actions
        if item.kind == kind and all(getattr(item, key) == value for key, value in matching.items())
    )


async def test_retained_tabs_isolate_branches_and_mutations_reject_stale_state(browser):
    async with Crawl4AIFetcher(delay=0) as fetcher:
        parent = await fetcher.fetch("https://example.org/catalog")
        parent_session = fetcher._page_sessions[parent.page_id]
        child = await fetcher.interact(parent, action(parent, "open"))
        assert fetcher._page_sessions[child.page_id] != parent_session
        # Opening the child did not navigate the retained parent tab.
        updated = await fetcher.interact(parent, action(parent, "click"))
        assert fetcher._page_sessions[updated.page_id] == parent_session
        assert fetcher._crawler.calls[-1][1]["js_only"] is True
        assert fetcher.interaction_metadata[updated.page_id]["http_status_observed"] is False
        with pytest.raises(FetchError, match="Stale"):
            await fetcher.interact(parent, action(parent, "click"))
        crawler = fetcher._crawler
    assert len(crawler.closed_sessions) == 2


async def test_release_closes_only_latest_matching_snapshot_and_is_idempotent(browser):
    async with Crawl4AIFetcher(delay=0) as fetcher:
        original = await fetcher.fetch("https://example.org/catalog")
        updated = await fetcher.interact(original, action(original, "click"))
        session_id = fetcher._page_sessions[updated.page_id]
        assert await fetcher.release(original) is False
        assert session_id in fetcher._sessions
        forged = updated.model_copy(update={"html": "different snapshot with copied identity"})
        assert await fetcher.release(forged) is False
        assert await fetcher.release(updated) is True
        assert session_id not in fetcher._sessions
        assert updated.page_id not in fetcher.interaction_metadata
        assert await fetcher.release(updated) is False
        assert fetcher._crawler.closed_sessions.count(session_id) == 1


async def test_search_query_is_json_data_not_executable_source(browser):
    query = 'book "; window.attack=true; // <script>'
    async with Crawl4AIFetcher(delay=0) as fetcher:
        page = await fetcher.fetch("https://example.org/catalog")
        await fetcher.interact(page, action(page, "search"), query)
        script = fetcher._crawler.calls[-1][1]["js_code"]
        parameters = json.loads(script.split(";\n", 1)[0][10:])
        assert parameters["query"] == query
        assert "new KeyboardEvent" in script
        assert all(legacy not in script for legacy in ("keypress", "keyCode:", "which:"))
        assert "requestSubmit" in script
        assert "_self" in script


async def test_select_observed_option_and_reject_fabricated_bindings(browser):
    async with Crawl4AIFetcher(delay=0, allow_filters=True) as fetcher:
        page = await fetcher.fetch("https://example.org/catalog")
        selected = action(page, "select", option_value="used", allow_filters=True)
        with pytest.raises(FetchError, match="not observed"):
            await fetcher.interact(page, selected.model_copy(update={"option_value": "invented"}))
        with pytest.raises(FetchError, match="not observed"):
            await fetcher.interact(page, selected.model_copy(update={"selector": "#invented"}))
        updated = await fetcher.interact(page, selected)
        assert "select: changed" in updated.html
        assert "toggleAttribute('selected'" in fetcher._crawler.calls[-1][1]["js_code"]


async def test_default_provider_rejects_enabled_filter_and_dirty_search_bindings(browser):
    async with Crawl4AIFetcher(delay=0) as fetcher:
        page = await fetcher.fetch("https://example.org/catalog")
        enabled = InventoryBuilder(allow_filters=True).build(page)
        for candidate in enabled.actions:
            if candidate.kind in {"select", "search"}:
                with pytest.raises(FetchError, match="not observed"):
                    await fetcher.interact(page, candidate, "book")
        assert len(fetcher._crawler.calls) == 1


@pytest.mark.parametrize(
    "execution",
    [
        False,
        {"success": False, "error": "evaluation failed"},
        {"success": True, "results": [{"success": False, "error": "script failed"}]},
        {"success": True, "results": [{"ok": False, "error": "stale browser element"}]},
    ],
)
async def test_failed_script_never_masquerades_as_success_and_invalidates_tab(browser, execution):
    async with Crawl4AIFetcher(delay=0) as fetcher:
        page = await fetcher.fetch("https://example.org/catalog")
        # A false value must survive the fake transport's default selection.
        fetcher._crawler.next_execution = execution
        if execution is False:
            fetcher._crawler.next_execution = {"success": False}
        with pytest.raises(FetchError, match=r"JavaScript|rejected"):
            await fetcher.interact(page, action(page, "click"))
        assert page.page_id not in fetcher._page_sessions


async def test_external_and_cross_snapshot_actions_rejected(browser):
    async with Crawl4AIFetcher(delay=0) as fetcher:
        page = await fetcher.fetch("https://example.org/catalog")
        candidate = action(page, "open")
        with pytest.raises(FetchError, match="not observed"):
            await fetcher.interact(page, candidate.model_copy(update={"url": "https://other.org/"}))
        with pytest.raises(FetchError, match="Stale"):
            await fetcher.interact(page, candidate.model_copy(update={"page_id": "another"}))


async def test_session_eviction_rejects_expired_snapshot(browser):
    async with Crawl4AIFetcher(delay=0, max_sessions=1) as fetcher:
        first = await fetcher.fetch("https://example.org/first")
        await fetcher.fetch("https://example.org/second")
        with pytest.raises(FetchError, match="Stale"):
            await fetcher.interact(first, action(first, "click"))
        assert len(fetcher._crawler.closed_sessions) == 1


async def test_viewport_only_progress_survives_identical_snapshot_ids(browser):
    async with Crawl4AIFetcher(delay=0) as fetcher:
        page = await fetcher.fetch("https://example.org/catalog")
        for scroll_y in (720, 1440):
            fetcher._crawler.next_html = HTML
            fetcher._crawler.next_execution = {
                "success": True,
                "results": [
                    {
                        "ok": True,
                        "changed": True,
                        "scroll_y": scroll_y,
                        "scroll_height": 5000,
                        "viewport_height": 720,
                    }
                ],
            }
            updated = await fetcher.interact(page, action(page, "scroll"))
            assert updated.page_id == page.page_id
            assert fetcher.interaction_metadata[updated.page_id] == {
                "action_id": action(page, "scroll").action_id,
                "status_kind": "browser_snapshot",
                "http_status_observed": False,
                "javascript_only": True,
                "scroll_y": scroll_y,
                "scroll_height": 5000,
                "viewport_height": 720,
            }
            script = fetcher._crawler.calls[-1][1]["js_code"]
            assert "scroll_y:window.scrollY" in script
            assert "document.documentElement.scrollHeight" in script
            assert "viewport_height:window.innerHeight" in script
            page = updated


async def test_unobserved_or_nonfinite_viewport_metrics_remain_unknown(browser):
    async with Crawl4AIFetcher(delay=0) as fetcher:
        page = await fetcher.fetch("https://example.org/catalog")
        fetcher._crawler.next_execution = {
            "success": True,
            "results": [
                {
                    "ok": True,
                    "changed": True,
                    "scroll_y": float("nan"),
                    "scroll_height": "5000",
                    "viewport_height": True,
                }
            ],
        }
        updated = await fetcher.interact(page, action(page, "click"))
        metadata = fetcher.interaction_metadata[updated.page_id]
        assert not {"scroll_y", "scroll_height", "viewport_height"} & metadata.keys()

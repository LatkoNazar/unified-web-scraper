from types import SimpleNamespace

from unified_web_parser.navigation import InventoryBuilder
from unified_web_parser.providers.crawl4ai import Crawl4AIFetcher


async def test_html_only_sdk_processing_preserves_fetch_and_interaction_metadata(monkeypatch):
    calls = []
    html = '<html><body><h1>Catalog</h1><button id="more">Load more</button></body></html>'

    class Crawler:
        def __init__(self, **_kwargs):
            self.crawler_strategy = SimpleNamespace(kill_session=self.kill_session)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def kill_session(self, _session_id):
            return None

        async def arun(self, *, url, config):
            calls.append(config)
            assert config["prefetch"] is True
            interactive = config.get("js_only", False)
            return SimpleNamespace(
                success=True,
                status_code=200,
                redirected_url="https://example.org/catalog",
                html=html.replace("</body>", "<p>More observed results</p></body>")
                if interactive
                else html,
                js_execution_result={
                    "success": True,
                    "results": [{"ok": True, "changed": True}],
                }
                if interactive
                else None,
                markdown=None,
                cleaned_html=None,
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
    async with Crawl4AIFetcher(delay=0) as fetcher:
        page = await fetcher.fetch("https://example.org/start")
        assert str(page.url) == "https://example.org/catalog"
        assert page.status_code == 200
        assert page.html == html
        action = next(
            action for action in InventoryBuilder().build(page).actions if action.kind == "click"
        )
        updated = await fetcher.interact(page, action)
        assert "More observed results" in updated.html
        assert str(updated.url) == str(page.url)
        assert updated.page_id != page.page_id
        assert fetcher.interaction_metadata[updated.page_id]["javascript_only"] is True
        assert fetcher.interaction_metadata[updated.page_id]["http_status_observed"] is False
    assert len(calls) == 2
    assert calls[0]["session_id"] == calls[1]["session_id"]

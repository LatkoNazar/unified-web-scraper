import asyncio
from types import SimpleNamespace

import pytest

from unified_web_parser.crawling.errors import FetchError
from unified_web_parser.providers.crawl4ai import Crawl4AIFetcher


def test_browser_provider_lifecycle_and_error_status(monkeypatch):
    instances = []

    class Crawler:
        def __init__(self, *, config):
            self.closed = False
            instances.append(self)
            assert config["headless"] is True

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            self.closed = True

        async def arun(self, *, url, config):
            assert config["page_timeout"] == 15000
            assert config["delay_before_return_html"] == 3
            assert config["wait_until"] == "domcontentloaded"
            if url.endswith("denied"):
                return SimpleNamespace(success=True, status_code=403, error_message="Forbidden")
            if url.endswith("challenge"):
                return SimpleNamespace(
                    success=True, status_code=200, html="<title>CAPTCHA</title>", url=url
                )
            if url.endswith("redirect"):
                return SimpleNamespace(
                    success=True,
                    status_code=301,
                    html="<h1>Final product</h1>",
                    url=url,
                    redirected_url="https://example.org/final-product",
                    redirected_status_code=200,
                )
            return SimpleNamespace(
                success=True, status_code=200, html="<h1>A product</h1>", url=url
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

    async def check():
        async with Crawl4AIFetcher(timeout=15, delay=0) as fetcher:
            page = await fetcher.fetch("https://example.org/product")
            assert page.html == "<h1>A product</h1>"
            with pytest.raises(FetchError, match="HTTP 403"):
                await fetcher.fetch("https://example.org/denied")
            with pytest.raises(FetchError, match="Access challenge"):
                await fetcher.fetch("https://example.org/challenge")
            redirected = await fetcher.fetch("https://example.org/redirect")
            assert redirected.status_code == 200
            assert str(redirected.url) == "https://example.org/final-product"
        assert instances[0].closed

    asyncio.run(check())


def test_browser_provider_missing_optional_sdk(monkeypatch):
    def unavailable(_name):
        raise ImportError("not installed")

    monkeypatch.setattr(
        "unified_web_parser.providers.crawl4ai.importlib.import_module", unavailable
    )

    async def check():
        with pytest.raises(RuntimeError, match="optional"):
            async with Crawl4AIFetcher():
                pytest.fail("Missing dependency should prevent entering the provider")

    asyncio.run(check())

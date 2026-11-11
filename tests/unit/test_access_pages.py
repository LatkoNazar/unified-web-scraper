import asyncio

import httpx
import pytest

from unified_web_parser.crawling.access import check_access_page
from unified_web_parser.crawling.errors import FetchError
from unified_web_parser.providers.http import HttpFetcher
from unified_web_parser.sources.navigation import canonical_url


@pytest.mark.parametrize(
    "html",
    [
        "<title>Access Denied</title><body>Request blocked</body>",
        "<title>Just a moment...</title><body>Checking your browser</body>",
        "<title>CAPTCHA</title><body>Complete the check</body>",
        "<title>Prom.ua</title><body>Підтвердіть, що ви не робот</body>",
    ],
)
def test_explicit_challenge_is_rejected_even_with_http_200(html):
    with pytest.raises(FetchError, match="Access challenge") as exc:
        check_access_page("https://example.org/search", html, 200)
    assert exc.value.status_code == 200


def test_normal_product_mentioning_captcha_is_accepted():
    check_access_page(
        "https://example.org/products/keyboard",
        "<title>Keyboard for CAPTCHA operators</title>"
        "<body>This laptop has a camera for security checks. No CAPTCHA software included."
        "<script>const warning = 'please confirm you are not a robot';</script></body>",
        200,
    )


def test_http_fetcher_rejects_successful_challenge_response():
    async def check():
        transport = httpx.MockTransport(
            lambda request: httpx.Response(200, text="<title>Security check</title>")
        )
        async with (
            httpx.AsyncClient(transport=transport) as client,
            HttpFetcher(client=client, delay=0) as fetcher,
        ):
            with pytest.raises(FetchError, match="Access challenge"):
                await fetcher.fetch("https://example.org")

    asyncio.run(check())


def test_canonical_urls_remove_tracking_and_preserve_resource_parameters():
    url = "https://www.olx.ua/d/uk/obyavlenie/macbook-ID123.html"
    assert canonical_url(url + "?search_reason=search%7Cpromoted&utm_source=ads#gallery") == url
    assert canonical_url("https://example.org/item?id=42&variant=blue&fbclid=tracking") == (
        "https://example.org/item?id=42&variant=blue"
    )

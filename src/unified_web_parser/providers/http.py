"""Bounded ordinary HTTP fetching without browser evasion or access-control bypass."""

import asyncio
from urllib.parse import urlsplit

import httpx

from unified_web_parser.core.models import Page
from unified_web_parser.crawling.access import check_access_page
from unified_web_parser.crawling.errors import FetchError
from unified_web_parser.crawling.pages import make_page
from unified_web_parser.crawling.rate_limit import HostRateLimiter

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
RETRY_STATUSES = {429, 500, 502, 503, 504}


class HttpFetcher:
    def __init__(
        self,
        *,
        timeout: float = 30,
        retries: int = 2,
        delay: float = 1,
        max_bytes: int = 5_000_000,
        client: httpx.AsyncClient | None = None,
    ):
        if timeout <= 0 or retries < 0 or delay < 0 or max_bytes <= 0:
            raise ValueError("timeout/max_bytes must be positive; retries/delay nonnegative")
        self.timeout = timeout
        self.retries = retries
        self.delay = delay
        self.max_bytes = max_bytes
        self._client = client or httpx.AsyncClient(max_redirects=10)
        self._owns_client = client is None
        self._rate_limit = HostRateLimiter(delay)

    async def __aenter__(self) -> "HttpFetcher":
        return self

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def fetch(self, url: str) -> Page:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise FetchError(url, "Only absolute HTTP(S) URLs are supported")
        for attempt in range(self.retries + 1):
            await self._rate_limit.wait(parsed.hostname)
            try:
                async with self._client.stream(
                    "GET",
                    url,
                    timeout=self.timeout,
                    follow_redirects=True,
                    headers={
                        "User-Agent": USER_AGENT,
                        "Accept": "text/html,application/xml;q=0.9,*/*;q=0.8",
                    },
                ) as response:
                    if response.status_code in RETRY_STATUSES and attempt < self.retries:
                        retry_after = response.headers.get("retry-after", "")
                        pause = min(float(retry_after), 60) if retry_after.isdigit() else 2**attempt
                        await asyncio.sleep(pause)
                        continue
                    if response.status_code >= 400:
                        raise FetchError(
                            str(response.url),
                            f"HTTP {response.status_code} {response.reason_phrase}",
                            status_code=response.status_code,
                        )
                    chunks: list[bytes] = []
                    size = 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > self.max_bytes:
                            raise FetchError(
                                url, f"Response exceeds {self.max_bytes} decoded bytes"
                            )
                        chunks.append(chunk)
                    html = b"".join(chunks).decode(response.encoding or "utf-8", errors="replace")
                    check_access_page(str(response.url), html, response.status_code)
                    return make_page(str(response.url), html, response.status_code)
            except httpx.TransportError as exc:
                if attempt == self.retries:
                    raise FetchError(
                        url, f"Transport failure after {attempt + 1} attempts: {exc}"
                    ) from exc
                await asyncio.sleep(min(2**attempt, 30))
        raise AssertionError("Unreachable retry state")

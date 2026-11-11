import asyncio

import httpx
import pytest

from unified_web_parser.crawling.errors import FetchError
from unified_web_parser.providers.http import HttpFetcher


def test_fetch_redirect_and_stable_snapshot_hash():
    def handler(request):
        if request.url.path == "/redirect":
            return httpx.Response(302, headers={"location": "/item"})
        return httpx.Response(200, text="<h1>MacBook Air</h1>")

    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            async with HttpFetcher(client=client, delay=0) as fetcher:
                first = await fetcher.fetch("https://example.org/redirect")
                second = await fetcher.fetch("https://example.org/item")
                assert str(first.url) == "https://example.org/item"
                assert first.page_id == second.page_id
                assert first.fetched_at.tzinfo is not None
                assert first.html == "<h1>MacBook Air</h1>"
            assert not client.is_closed  # Injected clients remain owned by the caller.

    asyncio.run(check())


def test_access_denied_not_retried():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(403)

    async def check():
        async with (
            httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client,
            HttpFetcher(client=client, delay=0) as fetcher,
        ):
            with pytest.raises(FetchError, match="HTTP 403") as exc:
                await fetcher.fetch("https://example.org/item")
            assert exc.value.status_code == 403
        assert len(calls) == 1

    asyncio.run(check())


def test_transient_response_retried_and_size_bounded(monkeypatch):
    calls = []

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr("unified_web_parser.providers.http.asyncio.sleep", no_sleep)

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(503)
        return httpx.Response(200, text="content too large")

    async def check():
        async with (
            httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client,
            HttpFetcher(client=client, delay=0, max_bytes=5) as fetcher,
        ):
            with pytest.raises(FetchError, match="exceeds 5"):
                await fetcher.fetch("https://example.org/item")
        assert len(calls) == 2

    asyncio.run(check())


def test_transport_retry_exhaustion(monkeypatch):
    calls = []

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr("unified_web_parser.providers.http.asyncio.sleep", no_sleep)

    def handler(request):
        calls.append(request)
        raise httpx.ConnectError("offline", request=request)

    async def check():
        async with (
            httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client,
            HttpFetcher(client=client, delay=0, retries=1) as fetcher,
        ):
            with pytest.raises(FetchError, match="2 attempts"):
                await fetcher.fetch("https://example.org/item")
        assert len(calls) == 2

    asyncio.run(check())


def test_owned_client_closed_by_context():
    async def check():
        async with HttpFetcher() as fetcher:
            client = fetcher._client
        assert client.is_closed

    asyncio.run(check())

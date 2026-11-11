"""Optional standard headless Crawl4AI provider, imported only when selected."""

import asyncio
import importlib
import math
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit
from uuid import uuid4

from unified_web_parser.core.models import Page
from unified_web_parser.crawling.access import check_access_page
from unified_web_parser.crawling.errors import FetchError
from unified_web_parser.crawling.pages import make_page
from unified_web_parser.crawling.rate_limit import HostRateLimiter
from unified_web_parser.providers.browser_actions import (
    SERIALIZE_CONTROLS,
    build_action_script,
    check_execution,
)
from unified_web_parser.providers.http import USER_AGENT

if TYPE_CHECKING:
    from unified_web_parser.navigation.models import NavigationAction


@dataclass
class _Session:
    page: Page
    touched: float


class Crawl4AIFetcher:
    def __init__(
        self,
        *,
        timeout: float = 30,
        retries: int = 2,
        delay: float = 1,
        max_bytes: int = 5_000_000,
        render_delay: float = 3,
        max_sessions: int = 64,
        session_ttl: float = 900,
        allow_filters: bool = False,
        schema: dict[str, Any] | None = None,
        content_profile: str = "generic",
        currency_aliases: dict[str, str] | None = None,
    ):
        if timeout <= 0 or retries < 0 or delay < 0 or max_bytes <= 0:
            raise ValueError("timeout/max_bytes must be positive; retries/delay nonnegative")
        if not 0 <= render_delay <= 10:
            raise ValueError("render_delay must be between 0 and 10 seconds")
        if max_sessions < 1 or session_ttl <= 0:
            raise ValueError("max_sessions and session_ttl must be positive")
        self.timeout = timeout
        self.retries = retries
        self.delay = delay
        self.max_bytes = max_bytes
        self.render_delay = render_delay
        self.max_sessions = max_sessions
        self.session_ttl = session_ttl
        self.allow_filters = allow_filters
        self.schema = schema
        self.content_profile = content_profile
        self.currency_aliases = dict(currency_aliases or {})
        self._crawler = None
        self._sdk = None
        self._rate_limit = HostRateLimiter(delay)
        self._sessions: dict[str, _Session] = {}
        self._page_sessions: dict[str, str] = {}
        self._lock = asyncio.Lock()
        self.interaction_metadata: dict[str, dict[str, Any]] = {}

    async def __aenter__(self) -> "Crawl4AIFetcher":
        try:
            self._sdk = importlib.import_module("crawl4ai")
        except ImportError as exc:
            raise RuntimeError(
                "Crawl4AI is optional. Install the browser extra "
                "and run playwright install chromium."
            ) from exc
        config = self._sdk.BrowserConfig(headless=True, user_agent=USER_AGENT)
        self._crawler = self._sdk.AsyncWebCrawler(config=config)
        await self._crawler.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        if self._crawler is not None:
            try:
                for session_id in list(self._sessions):
                    try:
                        await self._release(session_id)
                    except Exception:
                        # Shutdown below still closes a session whose page already crashed.
                        pass
                await self._crawler.__aexit__(exc_type, exc_value, traceback)
            finally:
                self._crawler = None
                self._sessions.clear()
                self._page_sessions.clear()

    async def _release(self, session_id: str) -> None:
        state = self._sessions.pop(session_id, None)
        if state and self._page_sessions.get(state.page.page_id) == session_id:
            self._page_sessions.pop(state.page.page_id, None)
            self.interaction_metadata.pop(state.page.page_id, None)
        strategy = getattr(self._crawler, "crawler_strategy", None)
        if strategy and hasattr(strategy, "kill_session"):
            await strategy.kill_session(session_id)

    async def release(self, page: Page) -> bool:
        """Close an idle page's retained tab; a stale snapshot cannot close its replacement."""
        async with self._lock:
            session_id = self._page_sessions.get(page.page_id)
            state = self._sessions.get(session_id) if session_id else None
            if (
                state is None
                or state.page.page_id != page.page_id
                or state.page.html != page.html
                or str(state.page.url) != str(page.url)
            ):
                return False
            await self._release(session_id)
            return True

    async def _expire(self, *, make_room: bool = False) -> None:
        now = time.monotonic()
        for session_id, state in list(self._sessions.items()):
            if now - state.touched >= self.session_ttl:
                await self._release(session_id)
        if make_room and len(self._sessions) >= self.max_sessions:
            oldest = min(self._sessions, key=lambda item: self._sessions[item].touched)
            await self._release(oldest)

    async def _retain(self, session_id: str, page: Page) -> None:
        duplicate = self._page_sessions.get(page.page_id)
        if duplicate and duplicate != session_id:
            await self._release(duplicate)
        previous = self._sessions.get(session_id)
        if previous:
            self._page_sessions.pop(previous.page.page_id, None)
            self.interaction_metadata.pop(previous.page.page_id, None)
        self._sessions[session_id] = _Session(page, time.monotonic())
        self._page_sessions[page.page_id] = session_id

    @staticmethod
    def _same_host(source: str, target: str) -> bool:
        parsed = urlsplit(target)
        return (
            parsed.scheme in {"http", "https"}
            and parsed.hostname == urlsplit(source).hostname
            and parsed.port == urlsplit(source).port
            and not parsed.username
            and not parsed.password
        )

    def _page_from_result(self, url: str, result: Any) -> Page:
        status = (
            getattr(result, "redirected_status_code", None)
            or getattr(result, "status_code", None)
            or (200 if result.success else 500)
        )
        final_url = getattr(result, "redirected_url", None) or getattr(result, "url", None) or url
        if not result.success or status >= 400:
            error = getattr(result, "error_message", None) or "no page returned"
            raise FetchError(url, f"Crawl4AI failed (HTTP {status}): {error}", status_code=status)
        html = result.html or ""
        if len(html.encode("utf-8")) > self.max_bytes:
            raise FetchError(url, f"Rendered page exceeds {self.max_bytes} bytes")
        check_access_page(final_url, html, status)
        return make_page(final_url, html, status)

    async def fetch(self, url: str) -> Page:
        async with self._lock:
            return await self._fetch(url)

    async def _fetch(self, url: str) -> Page:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise FetchError(url, "Only absolute HTTP(S) URLs are supported")
        if self._crawler is None or self._sdk is None:
            raise RuntimeError("Use Crawl4AIFetcher inside async with")
        await self._expire(make_room=True)
        session_id = uuid4().hex
        config = self._sdk.CrawlerRunConfig(
            session_id=session_id,
            cache_mode=self._sdk.CacheMode.BYPASS,
            prefetch=True,
            page_timeout=int(self.timeout * 1000),
            wait_until="domcontentloaded",
            delay_before_return_html=self.render_delay,
            js_code=SERIALIZE_CONTROLS,
        )
        for attempt in range(self.retries + 1):
            await self._rate_limit.wait(parsed.hostname)
            try:
                result = await self._crawler.arun(url=url, config=config)
            except Exception:
                await self._release(session_id)
                raise
            status = (
                getattr(result, "redirected_status_code", None)
                or result.status_code
                or (200 if result.success else 500)
            )
            if result.success and status < 400:
                try:
                    page = self._page_from_result(url, result)
                    if getattr(result, "js_execution_result", None) is not None:
                        check_execution(page, result)
                except Exception:
                    await self._release(session_id)
                    raise
                await self._retain(session_id, page)
                return page
            if status not in {429, 500, 502, 503, 504} or attempt == self.retries:
                error = result.error_message or "no page returned"
                await self._release(session_id)
                raise FetchError(
                    url,
                    f"Crawl4AI failed (HTTP {status}): {error}",
                    status_code=status,
                )
            await asyncio.sleep(min(2**attempt, 30))
        raise AssertionError("Unreachable retry state")

    async def interact(
        self, page: Page, action: "NavigationAction", query: str | None = None
    ) -> Page:
        """Execute one inventoried action in its retained tab, never model-authored JS.

        JS-only SDK responses report synthetic HTTP 200; ``interaction_metadata``
        labels these as browser snapshots, not observed navigation HTTP responses.
        Opening an observed link gets a separate tab, preserving the parent state.
        Mutating a tab replaces its current snapshot; earlier snapshots become stale.
        """
        async with self._lock:
            if self._crawler is None or self._sdk is None:
                raise RuntimeError("Use Crawl4AIFetcher inside async with")
            await self._expire()
            session_id = self._page_sessions.get(page.page_id)
            state = self._sessions.get(session_id) if session_id else None
            if (
                action.page_id != page.page_id
                or state is None
                or state.page.html != page.html
                or str(state.page.url) != str(page.url)
                or make_page(str(page.url), page.html, page.status_code).page_id != page.page_id
            ):
                raise FetchError(str(page.url), "Stale or unretained browser navigation snapshot")
            from unified_web_parser.navigation.inventory import InventoryBuilder
            from unified_web_parser.navigation.urls import resolve_action_url

            # Rebuild capabilities from the exact source snapshot, never trust a fabricated binding.
            observed_actions = (
                InventoryBuilder(
                    max_actions=200,
                    allow_filters=self.allow_filters,
                    schema=self.schema,
                    content_profile=self.content_profile,
                    currency_aliases=self.currency_aliases,
                )
                .build(page, query)
                .by_id()
            )
            if observed_actions.get(action.action_id) != action:
                raise FetchError(
                    str(page.url), "Navigation action was not observed on this snapshot"
                )

            if action.kind == "search" and (not isinstance(query, str) or not query.strip()):
                raise FetchError(str(page.url), "Search navigation requires a nonempty query")
            target = resolve_action_url(action, query)
            if target and not self._same_host(str(page.url), target):
                raise FetchError(str(page.url), "External navigation target is not allowed")
            if action.kind == "open":
                if not target:
                    raise FetchError(str(page.url), "Open action URL was not observed on the page")
                opened = await self._fetch(target)
                if not self._same_host(str(page.url), str(opened.url)):
                    await self._release(self._page_sessions[opened.page_id])
                    raise FetchError(str(page.url), "Browser navigated to an external host")
                return opened
            if action.kind == "search" and action.url_template:
                if not target:
                    raise FetchError(str(page.url), "Search template was not observed on the page")
                # SearchAction templates are source-authored URLs, not executable code.
                searched = await self._fetch(target)
                if not self._same_host(str(page.url), str(searched.url)):
                    await self._release(self._page_sessions[searched.page_id])
                    raise FetchError(
                        str(page.url), "Search template redirected to an external host"
                    )
                return searched
            if action.kind == "search" and not action.input_selector:
                raise FetchError(str(page.url), "Search action needs its observed input selector")
            if action.kind in {"click", "select"} and not action.selector:
                raise FetchError(str(page.url), "Browser action needs its observed selector")
            script = build_action_script(
                page, action, query, timeout=self.timeout, render_delay=self.render_delay
            )
            await self._rate_limit.wait(urlsplit(str(page.url)).hostname or "")
            config = self._sdk.CrawlerRunConfig(
                session_id=session_id,
                js_only=True,
                cache_mode=self._sdk.CacheMode.BYPASS,
                prefetch=True,
                page_timeout=int(self.timeout * 1000),
                wait_until="domcontentloaded",
                delay_before_return_html=0,
                js_code=script,
            )
            # Actions are not retried: repeating a click can advance pagination twice.
            try:
                result = await self._crawler.arun(url=str(page.url), config=config)
                outcome = check_execution(page, result)
                updated = self._page_from_result(str(page.url), result)
            except Exception:
                # A failed script may have partly mutated state; invalidate its old snapshot.
                await self._release(session_id)
                raise
            if not self._same_host(str(page.url), str(updated.url)):
                await self._release(session_id)
                raise FetchError(str(page.url), "Browser action navigated to an external host")
            if (outcome and not outcome.get("changed")) or (
                outcome is None and updated.page_id == page.page_id
            ):
                raise FetchError(
                    str(page.url), "Browser action produced no observable state change"
                )
            await self._retain(session_id, updated)
            self.interaction_metadata[updated.page_id] = {
                "action_id": action.action_id,
                "status_kind": "browser_snapshot",
                "http_status_observed": False,
                "javascript_only": True,
            }
            if outcome is not None:
                # Viewport progress is observable even when HTML (and its page ID) is unchanged.
                # Navigation may destroy the execution context; absent metrics remain unknown.
                for name in ("scroll_y", "scroll_height", "viewport_height"):
                    value = outcome.get(name)
                    if type(value) in {int, float} and math.isfinite(value):
                        self.interaction_metadata[updated.page_id][name] = value
            return updated

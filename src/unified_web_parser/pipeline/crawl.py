"""Bounded navigation of observed page actions, independent of extraction providers."""

import asyncio
import hashlib
import json
from collections import deque
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4
from xml.etree.ElementTree import ParseError

from unified_web_parser.core.exceptions import CreditsExhausted, DurabilityError
from unified_web_parser.core.models import Page
from unified_web_parser.discovery.sitemap import extract_sitemap_urls
from unified_web_parser.discovery.sites import site_identity
from unified_web_parser.navigation import (
    InventoryBuilder,
    NavigationAction,
    NavigationPlan,
    NavigationPlanner,
    resolve_action_url,
)
from unified_web_parser.navigation.models import is_filter_action
from unified_web_parser.pipeline.checkpoints import CheckpointStore
from unified_web_parser.pipeline.events import EventCallback, emit_event
from unified_web_parser.sources.navigation import canonical_url


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").removeprefix("www.")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def refresh_coverage(stats: dict[str, Any], errors: list[dict[str, Any]]) -> None:
    """Describe only observed exploration, including failures after crawl returns."""
    incomplete_stops = {
        "max_pages",
        "max_records",
        "pages_per_site",
        "depth_limit",
        "depth_or_host_limit",
        "host_limit",
        "inventory_limit",
        "interaction_limit",
        "interaction_deferred",
        "unsupported_backend",
        "state_error",
        "invalid_sitemap",
        "no_relevant_action",
        "decision_stop",
        "credits_exhausted",
        "user_cancelled",
        "resume_browser_state_unavailable",
    }
    observed = set(stats.get("branch_stops", {})) | {stats.get("stop_reason")}
    reasons = sorted(incomplete_stops.intersection(observed))
    if errors:
        reasons.append("reported_errors")
    stats.update(
        coverage_scope="observed_navigation",
        coverage_status="incomplete" if reasons else "observed_frontier_exhausted",
        incomplete_reasons=reasons,
    )


@dataclass(frozen=True)
class _Work:
    url: str
    depth: int = 0
    search_done: bool = False
    kind: str = "url"
    page: Page | None = None
    action: Any = None
    interactive: bool = False
    previous_observation: str | None = None
    source_url: str | None = None
    seed: bool = False
    token: str = ""
    fetched: Page | None = None
    recovered: bool = False


class CrawlEngine:
    """Explore an explicit source root through code-built, validated action IDs.

    URL opens and UI mutations share one page-attempt budget. Interactive states
    may share an address; their inventories, not their address alone, drive loops.
    """

    def __init__(
        self,
        config,
        schema: dict[str, Any],
        *,
        fetcher,
        report,
        run_dir: Path,
        save_page: Callable[..., Page],
        extract_page: Callable[[Page], Awaitable[None]],
        record_count: Callable[[], int],
        record_pages: Callable[[], Iterable[str]] | None = None,
        navigation_client=None,
        planner=None,
        inventory_builder=None,
        progress: Callable[[str], None] | None = None,
        on_event: EventCallback | None = None,
        checkpoint: CheckpointStore | None = None,
        incremental: Any = None,
    ):
        self.config = config
        self.schema = schema
        self.fetcher = fetcher
        self.report = report
        self.run_dir = run_dir
        self.save_page = save_page
        self.extract_page = extract_page
        self.record_count = record_count
        self.record_pages = record_pages
        self.page_sources: dict[str, str] = {}
        self.url_sources: dict[str, str] = {}
        self._fetch_error_indices: set[int] = set()
        self.progress = progress or (lambda _message: None)
        self.on_event = on_event
        self.checkpoint = checkpoint
        self.incremental = incremental
        self._queue: deque[_Work] = deque()
        self._inflight: dict[str, _Work] = {}
        self.builder = inventory_builder or InventoryBuilder(
            schema=schema,
            content_profile=config.content_profile,
            currency_aliases=config.currency_aliases,
            max_actions=config.navigation.max_actions,
            max_summary_chars=config.navigation.max_summary_chars,
            allow_scroll=config.navigation.allow_scroll and hasattr(fetcher, "interact"),
            allow_filters=config.navigation.allow_filters,
            skip_url=incremental.known_detail if incremental else None,
        )
        self.planner = planner or NavigationPlanner(
            client=navigation_client,
            mode=config.navigation.mode,
            allow_filters=config.navigation.allow_filters,
        )
        self.allowed_hosts = {_host(str(source.url)) for source in config.sources}
        self.visited_urls: set[str] = set()
        self.seen_states: set[str] = set()
        self._completed_states: set[str] = set()
        self.visited_actions: set[str] = set()
        self.interactions_per_url: dict[str, int] = {}
        self._deferred_releases: list[Page] = []
        self.site_attempts: dict[str, int] = {}
        self.source_outcomes = {
            canonical_url(str(source.url)): {
                "url": str(source.url),
                "site": site_identity(str(source.url)),
                "attempted": 0,
                "fetched": 0,
                "errors": 0,
                "fetch_errors": 0,
                "processing_errors": 0,
                "records_added": 0,
                "status": "not_visited",
                "stop_reason": None,
                "pending_states": 0,
            }
            for source in config.sources
        }
        self.audit_path = run_dir / "navigation.jsonl"
        self.report.artifacts["navigation"] = str(self.audit_path)
        self.stats = {
            "states": 0,
            "actions_attempted": 0,
            "interactions_attempted": 0,
            "unchanged_states": 0,
            "duplicate_states": 0,
            "stop_reason": None,
            "branch_stops": {},
            "pages_released": 0,
            "site_attempts": self.site_attempts,
            "source_outcomes": list(self.source_outcomes.values()),
        }
        self.report.navigation = self.stats
        if self.checkpoint:
            saved = self.checkpoint.load().get("crawl")
            if saved:
                self.visited_urls = set(saved.get("visited_urls", []))
                self.seen_states = set(saved.get("seen_states", []))
                self._completed_states = set(self.seen_states)
                self.visited_actions = set(saved.get("visited_actions", []))
                self.interactions_per_url = saved.get("interactions_per_url", {})
                self.site_attempts.update(saved.get("site_attempts", {}))
                self.page_sources = saved.get("page_sources", {})
                self.url_sources = saved.get("url_sources", {})
                self._fetch_error_indices = set(saved.get("fetch_error_indices", []))
                for source in saved.get("source_outcomes", []):
                    self.source_outcomes[canonical_url(source["url"])] = source
                self.stats.update(saved.get("stats", {}))
                self.stats["site_attempts"] = self.site_attempts
                self.stats["source_outcomes"] = list(self.source_outcomes.values())
                previous_stop = self.stats.get("stop_reason")
                self.stats["stop_reason"] = None
                resumable_stops = {
                    "max_pages",
                    "max_records",
                    "credits_exhausted",
                    "user_cancelled",
                }
                self.stats["resume_previous_stops"] = [
                    *self.stats.get("resume_previous_stops", []),
                    previous_stop,
                ]
                for reason in resumable_stops:
                    self.stats["branch_stops"].pop(reason, None)
                for source in self.source_outcomes.values():
                    if source.get("stop_reason") in resumable_stops:
                        source["stop_reason"] = None
                for item in [*saved.get("inflight", []), *saved.get("queue", [])]:
                    work = self._decode_work(item)
                    self._queue.append(work)
                    if work.fetched is None and not work.interactive:
                        self.visited_urls.discard(canonical_url(work.url))
                self._restored = True
            else:
                self._restored = False
        else:
            self._restored = False
        if config.discovery.enabled:
            self.report.discovery["source_outcomes"] = self.stats["source_outcomes"]
            self.report.discovery["site_attempts"] = self.site_attempts
            self._audit(
                "discovery",
                discovery=self.report.discovery,
                effective_search=config.search,
                resolved_sources=[str(source.url) for source in config.sources],
            )

    @staticmethod
    def _encode_page(page: Page | None) -> dict[str, Any] | None:
        if page is None:
            return None
        return page.model_dump(mode="json", exclude={"html"}) | {
            "html": "" if page.snapshot_ref else page.html
        }

    @staticmethod
    def _decode_page(value: dict[str, Any] | None) -> Page | None:
        if value is None:
            return None
        page = Page.model_validate(value)
        if not page.html and page.snapshot_ref:
            page = page.model_copy(
                update={"html": Path(page.snapshot_ref).read_text(encoding="utf-8")}
            )
        return page

    @classmethod
    def _encode_work(cls, work: _Work) -> dict[str, Any]:
        return {
            **{
                name: getattr(work, name)
                for name in (
                    "url",
                    "depth",
                    "search_done",
                    "kind",
                    "interactive",
                    "previous_observation",
                    "source_url",
                    "seed",
                    "token",
                )
            },
            "page": cls._encode_page(work.page),
            "fetched": cls._encode_page(work.fetched),
            "action": work.action.model_dump(mode="json") if work.action else None,
        }

    @classmethod
    def _decode_work(cls, value: dict[str, Any]) -> _Work:
        value = dict(value)
        value["page"] = cls._decode_page(value.get("page"))
        value["fetched"] = cls._decode_page(value.get("fetched"))
        value["action"] = (
            NavigationAction.model_validate(value["action"]) if value.get("action") else None
        )
        return _Work(**value, recovered=True)

    def _checkpoint(self) -> None:
        if self.checkpoint:
            self.checkpoint.update(
                report=self.report.model_dump(mode="json"),
                crawl={
                    "queue": [self._encode_work(work) for work in self._queue],
                    "inflight": [self._encode_work(work) for work in self._inflight.values()],
                    "visited_urls": sorted(self.visited_urls),
                    "seen_states": sorted(self._completed_states),
                    "visited_actions": sorted(self.visited_actions),
                    "interactions_per_url": self.interactions_per_url,
                    "site_attempts": self.site_attempts,
                    "page_sources": self.page_sources,
                    "url_sources": self.url_sources,
                    "fetch_error_indices": sorted(self._fetch_error_indices),
                    "source_outcomes": list(self.source_outcomes.values()),
                    "stats": self.stats,
                },
            )

    async def _rebind_interaction(self, work: _Work) -> _Work:
        # Browser sessions cannot survive a process. Re-open the exact observed
        # address, then bind only an action present in an unchanged fresh state.
        if self.report.pages_attempted + 2 > self.config.crawl.max_pages:
            raise RuntimeError("Resume has no page budget to restore the browser state")
        site = site_identity(work.url)
        if (
            self.config.discovery.enabled
            and self.site_attempts.get(site, 0) + 2 > self.config.discovery.pages_per_site
        ):
            raise RuntimeError("Resume has no site budget to restore the browser state")
        self.report.pages_attempted += 1
        self.site_attempts[site] = self.site_attempts.get(site, 0) + 1
        source = self._source(work)
        if source is not None:
            source["attempted"] += 1
        self._checkpoint()
        fresh = await self.fetcher.fetch(work.url)
        fresh = self.save_page(fresh, extractable=False)
        self.report.pages_fetched += 1
        if source is not None:
            source["fetched"] += 1
        inventory = self.builder.build(fresh, query=self.config.search)
        previous = self.builder.build(work.page, query=self.config.search)
        if self._observation(inventory) != self._observation(previous):
            raise RuntimeError(
                "Pending browser state changed after interruption; "
                "unsafe stale action was not executed"
            )
        binding = work.action.model_dump(mode="json", exclude={"page_id", "action_id"})
        matches = [
            action
            for action in inventory.actions
            if action.model_dump(mode="json", exclude={"page_id", "action_id"}) == binding
        ]
        if len(matches) != 1:
            raise RuntimeError(
                "Pending browser action cannot be rebound uniquely after interruption"
            )
        self._audit(
            "resume_rebind",
            old_page_id=work.page.page_id,
            page_id=fresh.page_id,
            action_id=matches[0].action_id,
        )
        return replace(work, page=fresh, action=matches[0], recovered=False)

    def _source(self, work: _Work):
        return self.source_outcomes.get(canonical_url(work.source_url or work.url))

    def reconcile_processing(self) -> None:
        """Attribute deferred records/errors to their evidence source, not callback timing."""
        for source in self.source_outcomes.values():
            source["processing_errors"] = 0
            source["errors"] = source["fetch_errors"]
            if self.record_pages is not None:
                source["records_added"] = 0
        if self.record_pages is not None:
            for page_id in self.record_pages():
                source = self.source_outcomes.get(self.page_sources.get(page_id, ""))
                if source is not None:
                    source["records_added"] += 1
        for index, error in enumerate(self.report.errors):
            if index in self._fetch_error_indices or not error.get("url"):
                continue
            source = self.source_outcomes.get(self.url_sources.get(canonical_url(error["url"]), ""))
            if source is not None:
                source["processing_errors"] += 1
                source["errors"] += 1

    def _audit(self, event: str, **details: Any) -> None:
        with self.audit_path.open("a", encoding="utf-8") as stream:
            stream.write(_json({"event": event, **details}) + "\n")

    def _stop(self, reason: str, *, page_id: str | None = None, **details) -> None:
        counts = self.stats["branch_stops"]
        counts[reason] = counts.get(reason, 0) + 1
        self._audit("stop", reason=reason, page_id=page_id, **details)

    def _warning(self, message: str, **details) -> None:
        self.report.warnings.append({"stage": "navigation", "message": message, **details})

    async def _release_idle(self, page: Page, queue: deque[_Work]) -> None:
        if any(
            work.interactive and work.page is not None and work.page.page_id == page.page_id
            for work in queue
        ):
            return
        release = getattr(self.fetcher, "release", None)
        if release is None:
            return
        try:
            if await release(page):
                self.stats["pages_released"] += 1
                self._audit("release", page_id=page.page_id, url=str(page.url))
        except (CreditsExhausted, DurabilityError):
            raise
        except Exception as exc:
            self._warning(
                "Could not release an idle browser page",
                page_id=page.page_id,
                type=type(exc).__name__,
                detail=str(exc),
            )

    @staticmethod
    def _observation(inventory, browser_state: dict[str, Any] | None = None) -> str:
        observed = {
            "url": canonical_url(str(inventory.url)),
            "title": inventory.title,
            "summary": inventory.summary,
            "has_entity": inventory.has_entity,
            "has_results": inventory.has_results,
            "viewport": {
                key: (browser_state or {}).get(key)
                for key in ("scroll_y", "scroll_height", "viewport_height")
            },
            "actions": [
                action.model_dump(mode="json", exclude={"action_id", "page_id"})
                for action in inventory.actions
            ],
        }
        return hashlib.sha256(_json(observed).encode("utf-8")).hexdigest()

    @staticmethod
    def _next_depth(work: _Work, action, target_url: str | None) -> int:
        hint = str(action.hint or "").lower()
        group = str(action.group or "").lower()
        # Searching, pagination, filtering, and in-place UI changes are navigation
        # states at one level; opening a new entity/category descends one level.
        if action.kind in {"search", "scroll", "select"}:
            return work.depth
        if hint in {"search", "next", "pagination", "load_more", "filter", "sort"}:
            return work.depth
        if "pagination" in group or (action.kind == "click" and hint not in {"item", "category"}):
            return work.depth
        if target_url and canonical_url(target_url) == canonical_url(work.url):
            return work.depth
        return work.depth + 1

    async def _request(self, work: _Work) -> Page | None:
        if work.fetched is not None:
            return work.fetched
        if work.recovered and work.interactive:
            try:
                work = await self._rebind_interaction(work)
            except DurabilityError:
                raise
            except Exception as exc:
                self.report.errors.append(
                    {
                        "stage": "resume",
                        "url": work.url,
                        "message": str(exc),
                        "type": type(exc).__name__,
                    }
                )
                self._stop("resume_browser_state_unavailable", url=work.url)
                return None
        self.report.pages_attempted += 1
        emit_event(self.on_event, "counters", {"stage": "fetching", "url": work.url})
        site = site_identity(work.url)
        self.site_attempts[site] = self.site_attempts.get(site, 0) + 1
        source = self._source(work)
        source_key = canonical_url(work.source_url or work.url)
        self.url_sources.setdefault(canonical_url(work.url), source_key)
        if source is not None:
            source["attempted"] += 1
            source["status"] = "attempted"
        interaction = work.interactive
        if work.action is not None:
            self.stats["actions_attempted"] += 1
        if interaction:
            self.stats["interactions_attempted"] += 1
            self.progress(f"Interacting: {work.action.kind} {work.action.label}")
        else:
            self.progress(f"Fetching {work.url}")
        self._audit(
            "execute",
            url=work.url,
            interaction=interaction,
            action_id=getattr(work.action, "action_id", None),
        )
        self._checkpoint()
        try:
            if interaction:
                if not hasattr(self.fetcher, "interact"):
                    raise RuntimeError(
                        f"{type(self.fetcher).__name__} cannot execute JavaScript/browser "
                        f"action {work.action.kind}; use the browser backend"
                    )
                page = await self.fetcher.interact(work.page, work.action, self.config.search)
            else:
                page = await self.fetcher.fetch(work.url)
            if not isinstance(page, Page):
                raise TypeError("Fetcher must return a Page")
            if _host(str(page.url)) not in self.allowed_hosts:
                raise RuntimeError("Navigation redirected outside configured source hosts")
            page = self.save_page(page, extractable=False)
            self.report.pages_fetched += 1
            self._inflight[work.token] = replace(work, fetched=page)
            self.page_sources.setdefault(page.page_id, source_key)
            self.url_sources.setdefault(canonical_url(str(page.url)), source_key)
            emit_event(self.on_event, "counters", {"stage": "fetching", "url": str(page.url)})
            if source is not None:
                source["fetched"] += 1
                source["status"] = "visited"
            self.visited_urls.add(canonical_url(str(page.url)))
            self._audit(
                "result",
                url=str(page.url),
                page_id=page.page_id,
                action_id=getattr(work.action, "action_id", None),
                status_code=page.status_code,
                interaction_metadata=getattr(self.fetcher, "interaction_metadata", {}).get(
                    page.page_id
                ),
                outcome="fetched",
            )
            self._checkpoint()
            return page
        except (CreditsExhausted, DurabilityError):
            raise
        except Exception as exc:
            if source is not None:
                source["errors"] += 1
                source["fetch_errors"] += 1
                source["status"] = "visited" if source["fetched"] else "failed"
            self._fetch_error_indices.add(len(self.report.errors))
            self.report.errors.append(
                {
                    "stage": "navigation" if interaction else "fetch",
                    "url": work.url,
                    "type": type(exc).__name__,
                    "message": str(exc),
                    "status_code": getattr(exc, "status_code", None),
                }
            )
            self._audit(
                "result",
                url=work.url,
                outcome="error",
                message=str(exc),
                action_id=getattr(work.action, "action_id", None),
            )
            self.progress(f"Navigation failed: {work.url} ({type(exc).__name__})")
            emit_event(self.on_event, "counters", {"stage": "fetching"})
            return None

    async def _plan(self, inventory, search_done: bool):
        emit_event(self.on_event, "stage", {"stage": "navigation", "url": str(inventory.url)})
        saved = self.checkpoint.plan("navigation:" + inventory.page_id) if self.checkpoint else None
        if saved is not None:
            return NavigationPlan.model_validate(saved).validate_against(
                inventory, allow_filters=self.config.navigation.allow_filters
            )
        try:
            return (
                await self.planner.plan(
                    inventory,
                    self.config.search,
                    self.schema,
                    search_done=search_done,
                    visited_urls=self.visited_urls,
                )
            ).validate_against(inventory, allow_filters=self.config.navigation.allow_filters)
        except (CreditsExhausted, DurabilityError):
            raise
        except Exception as exc:
            self._warning(
                "Navigation planner failed; using observed-action rules",
                url=str(inventory.url),
                type=type(exc).__name__,
                detail=str(exc),
            )
            fallback = NavigationPlanner(
                client=None, mode="rules", allow_filters=self.config.navigation.allow_filters
            )
            return (
                await fallback.plan(
                    inventory,
                    self.config.search,
                    self.schema,
                    search_done=search_done,
                    visited_urls=self.visited_urls,
                )
            ).validate_against(inventory, allow_filters=self.config.navigation.allow_filters)

    def _sitemap(self, work: _Work, page: Page, queue: deque[_Work]) -> None:
        self.save_page(page, extractable=False)
        try:
            urls = extract_sitemap_urls(
                page.html, base_url=str(page.url), max_urls=self.config.crawl.max_pages
            )
        except (ValueError, ParseError) as exc:
            self.report.errors.append(
                {
                    "stage": "discovery",
                    "url": str(page.url),
                    "type": type(exc).__name__,
                    "message": str(exc),
                }
            )
            self._stop("invalid_sitemap", page_id=page.page_id)
            return
        self._audit("sitemap", page_id=page.page_id, urls=urls)
        for url in urls:
            sitemap = urlsplit(url).path.lower().endswith((".xml", ".xml.gz"))
            queue.append(
                _Work(
                    url=url,
                    depth=work.depth + (0 if sitemap else 1),
                    search_done=work.search_done,
                    kind="sitemap" if sitemap else "url",
                    source_url=work.source_url,
                )
            )

    def _batch(self, queue: deque[_Work]) -> list[_Work]:
        limit = 1 if self.config.crawl.backend == "browser" else self.config.crawl.concurrency
        batch = []
        reserved: dict[str, int] = {}
        while queue and len(batch) < limit:
            if self.report.pages_attempted + len(batch) >= self.config.crawl.max_pages:
                ready = next((work for work in queue if work.fetched is not None), None)
                if ready is None:
                    break
                queue.remove(ready)
                batch.append(ready)
                continue
            seed_index = next((index for index, item in enumerate(queue) if item.seed), None)
            if self.config.discovery.enabled and seed_index is not None:
                work = queue[seed_index]
                del queue[seed_index]
            else:
                work = queue.popleft()
            if work.fetched is not None:
                batch.append(work)
                continue
            if (
                self.incremental
                and not work.interactive
                and work.kind != "sitemap"
                and self.incremental.known_detail(work.url)
            ):
                self._audit("incremental_skip", url=work.url, reason="completed_detail")
                self.visited_urls.add(canonical_url(work.url))
                source = self._source(work)
                if source is not None and not source["attempted"]:
                    source["status"] = "unchanged"
                    source["stop_reason"] = "incremental_completed"
                continue
            url = canonical_url(work.url)
            site = site_identity(work.url)
            if (
                self.config.discovery.enabled
                and self.site_attempts.get(site, 0) + reserved.get(site, 0)
                >= self.config.discovery.pages_per_site
            ):
                source = self._source(work)
                if source is not None:
                    source["stop_reason"] = "pages_per_site"
                    if not source["attempted"]:
                        source["status"] = "skipped"
                self._stop("pages_per_site", url=url, site=site)
                if work.page is not None:
                    # No further request can consume this parent within the site budget.
                    self._deferred_releases.append(work.page)
                continue
            if work.depth > self.config.crawl.max_depth or _host(url) not in self.allowed_hosts:
                self._stop("depth_or_host_limit", url=url)
                continue
            if work.interactive:
                if batch:
                    queue.appendleft(work)
                    break
                return [work]  # UI mutations always execute against one concrete state.
            if url in self.visited_urls:
                source = self._source(work)
                if work.seed and source is not None and not source["attempted"]:
                    source["stop_reason"] = "already_visited"
                self._stop("visited_url", url=url)
                continue
            self.visited_urls.add(url)
            batch.append(work)
            reserved[site] = reserved.get(site, 0) + 1
        return batch

    async def _process_page(self, work: _Work, page: Page, queue: deque[_Work]) -> None:
        work = replace(work, url=str(page.url))
        if work.kind == "sitemap":
            self._sitemap(work, page, queue)
            return
        inventory = self.builder.build(page, query=self.config.search)
        if inventory.omitted_action_count:
            self._stop(
                "inventory_limit",
                page_id=page.page_id,
                observed_actions=inventory.observed_action_count,
                omitted_actions=inventory.omitted_action_count,
            )
        if self.config.search and not work.search_done:
            current_url = canonical_url(str(page.url))
            observed_search_done = any(
                action.kind == "search"
                and (target := resolve_action_url(action, self.config.search))
                and canonical_url(target) == current_url
                for action in inventory.actions
            )
            if observed_search_done:
                work = replace(work, search_done=True)
        browser_state = getattr(self.fetcher, "interaction_metadata", {}).get(page.page_id, {})
        observation = self._observation(inventory, browser_state)
        state = canonical_url(str(page.url)) + ":" + observation
        self._audit(
            "inventory",
            inventory=inventory.model_dump(mode="json"),
            state_id=state,
            depth=work.depth,
            search_done=work.search_done,
        )
        if work.previous_observation == observation or (
            work.interactive and browser_state.get("changed") is False
        ):
            self.stats["unchanged_states"] += 1
            self.save_page(page, extractable=False)
            self._stop("unchanged_state", page_id=page.page_id)
            return
        if state in self.seen_states:
            self.stats["duplicate_states"] += 1
            self.save_page(page, extractable=False)
            self._stop("visited_state", page_id=page.page_id)
            return
        self.seen_states.add(state)
        self.stats["states"] += 1
        plan = await self._plan(inventory, work.search_done)
        if self.checkpoint:
            self.checkpoint.save_plan("navigation:" + page.page_id, plan.model_dump(mode="json"))
        self._audit("plan", plan=plan.model_dump(mode="json"), state_id=state)
        by_id = inventory.by_id()
        actions = [by_id[action_id] for action_id in plan.action_ids]
        primary = [
            action
            for action in actions
            if action.kind in {"search", "select"}
            or (action.kind == "click" and action.hint not in {"load_more", "pagination"})
        ]
        pending_search = bool(
            self.config.search
            and not work.search_done
            and any(
                action.kind == "search" or (action.kind == "click" and action.hint == "search")
                for action in primary
            )
        )
        pending_filter = any(
            action.kind == "select" or action.hint in {"filter", "sort"} for action in primary
        )
        # An explicit depth-zero extraction may read real listing cards, but
        # an arbitrary homepage heading never becomes a record by force.
        at_depth_limit = work.depth >= self.config.crawl.max_depth
        extractable = (
            plan.extract
            or (
                at_depth_limit
                and inventory.has_results
                and (work.search_done or not self.config.search)
            )
        ) and not (pending_search or pending_filter)
        page = self.save_page(page, extractable=extractable)
        if self.incremental and self.checkpoint:
            self.checkpoint.save_plan(
                "detail:" + page.page_id,
                {
                    "confirmed": bool(
                        extractable and inventory.has_entity and not inventory.has_results
                    ),
                    "url": str(page.url),
                },
            )
        if extractable:
            before = self.record_count()
            await self.extract_page(page)
            source = self._source(work)
            if source is not None and self.record_pages is None:
                source["records_added"] += max(0, self.record_count() - before)
            self.reconcile_processing()
            if self.record_count() >= self.config.crawl.max_records:
                self.stats["stop_reason"] = "max_records"
                self._stop("max_records", page_id=page.page_id)
                return
        if plan.terminal:
            self._stop(plan.stop_reason or "terminal", page_id=page.page_id)
            return
        if primary:
            for action in actions:
                if action is not primary[0]:
                    self._stop(
                        "state_transition_deferred",
                        page_id=page.page_id,
                        action_id=action.action_id,
                    )
            actions = primary[:1]
        else:
            # Retained browser sessions let us visit existing item URLs before
            # optional continuation controls, without restoring a guessed root.
            actions.sort(key=lambda action: resolve_action_url(action, self.config.search) is None)
        scheduled_interaction = False
        for action in actions:
            if not self.config.navigation.allow_filters and is_filter_action(action):
                self._stop("filters_disabled", page_id=page.page_id, action_id=action.action_id)
                continue
            action_id = action.action_id
            identity = (
                state
                + ":"
                + _json(
                    action.model_dump(
                        mode="json",
                        exclude={"action_id", "page_id"},
                    )
                )
            )
            if identity in self.visited_actions:
                self._stop("visited_action", page_id=page.page_id, action_id=action_id)
                continue
            if action.kind == "scroll" and not self.config.navigation.allow_scroll:
                self._stop("scroll_disabled", page_id=page.page_id, action_id=action_id)
                continue
            target = resolve_action_url(action, self.config.search)
            if target is None and self.config.crawl.backend == "http":
                self._warning(
                    "Observed action requires the browser backend; HTTP cannot execute it",
                    url=str(page.url),
                    action_id=action_id,
                    kind=action.kind,
                )
                self._stop("unsupported_backend", page_id=page.page_id, action_id=action_id)
                continue
            depth = self._next_depth(work, action, target)
            if depth > self.config.crawl.max_depth:
                self._stop("depth_limit", page_id=page.page_id, action_id=action_id)
                continue
            search_done = work.search_done or action.kind == "search"
            if target is not None:
                if _host(target) not in self.allowed_hosts:
                    self._stop("host_limit", page_id=page.page_id, action_id=action_id)
                    continue
                if canonical_url(target) in self.visited_urls:
                    self._stop("visited_url", page_id=page.page_id, action_id=action_id)
                    continue
                queued = _Work(
                    url=target,
                    depth=depth,
                    search_done=search_done,
                    action=action,
                    source_url=work.source_url,
                )
            else:
                if scheduled_interaction:
                    self._stop("interaction_deferred", page_id=page.page_id, action_id=action_id)
                    continue
                page_url = canonical_url(str(page.url))
                count = self.interactions_per_url.get(page_url, 0)
                if count >= self.config.navigation.max_interactions_per_url:
                    self._stop("interaction_limit", page_id=page.page_id, action_id=action_id)
                    continue
                self.interactions_per_url[page_url] = count + 1
                scheduled_interaction = True
                queued = _Work(
                    url=str(page.url),
                    depth=depth,
                    search_done=search_done,
                    page=page,
                    action=action,
                    interactive=True,
                    previous_observation=observation,
                    source_url=work.source_url,
                )
            self.visited_actions.add(identity)
            self._audit(
                "action",
                page_id=page.page_id,
                action=action.model_dump(mode="json"),
                target_url=target,
                depth=depth,
                outcome="queued",
            )
            # Search/UI state transitions take precedence over featured links.
            if (
                action.kind == "search"
                or (target is None and action.hint == "search")
                or (target is not None and action.hint == "pagination")
            ):
                queue.appendleft(queued)
            else:
                queue.append(queued)
        if not plan.action_ids:
            self._stop(plan.stop_reason or "no_actions", page_id=page.page_id)

    async def run(self) -> None:
        queue = (
            self._queue
            if self._restored
            else deque(
                _Work(
                    url=str(source.url),
                    kind=source.kind,
                    source_url=str(source.url),
                    seed=True,
                )
                for source in self.config.sources
            )
        )
        self._queue = queue
        self._checkpoint()
        batch = []
        try:
            while queue and (
                self.report.pages_attempted < self.config.crawl.max_pages
                or any(work.fetched is not None for work in queue)
            ):
                if self.record_count() >= self.config.crawl.max_records:
                    self.stats["stop_reason"] = "max_records"
                    break
                batch = self._batch(queue)
                for page in self._deferred_releases:
                    await self._release_idle(page, queue)
                self._deferred_releases.clear()
                if not batch:
                    continue
                batch = [replace(work, token=work.token or uuid4().hex) for work in batch]
                self._inflight = {work.token: work for work in batch}
                self._checkpoint()
                tasks = [asyncio.create_task(self._request(work)) for work in batch]
                try:
                    outcomes = await asyncio.gather(*tasks)
                except BaseException:
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    raise
                for work, page in zip(batch, outcomes, strict=True):
                    if page is not None:
                        try:
                            await self._process_page(work, page, queue)
                        except (CreditsExhausted, DurabilityError):
                            raise
                        except Exception as exc:
                            self.report.errors.append(
                                {
                                    "stage": "navigation",
                                    "url": str(page.url),
                                    "type": type(exc).__name__,
                                    "message": str(exc),
                                }
                            )
                            self.save_page(page, extractable=False)
                            self._stop(
                                "state_error",
                                page_id=page.page_id,
                                type=type(exc).__name__,
                                message=str(exc),
                            )
                        finally:
                            self.reconcile_processing()
                            await self._release_idle(page, queue)
                    elif work.page is not None:
                        await self._release_idle(work.page, queue)
                    at_record_cap = self.record_count() >= self.config.crawl.max_records
                    if not at_record_cap:
                        self._completed_states = set(self.seen_states)
                        self._inflight.pop(work.token, None)
                    # At a record cap the page may still contain unprocessed
                    # candidates or actions. Keep its already-fetched snapshot
                    # until an explicit resume raises the cap.
                    self._checkpoint()
                    if at_record_cap:
                        self.stats["stop_reason"] = "max_records"
                        for remaining in outcomes:
                            if remaining is not None:
                                await self._release_idle(remaining, queue)
                        break
        except CreditsExhausted:
            self.stats["stop_reason"] = "credits_exhausted"
            raise
        except asyncio.CancelledError:
            self.stats["stop_reason"] = "user_cancelled"
            raise
        finally:
            # Inflight snapshots remain distinct from queued work until processing
            # and its incremental record transactions have completed.
            if self.stats["stop_reason"] is None:
                self.stats["stop_reason"] = (
                    "max_pages"
                    if queue and self.report.pages_attempted >= self.config.crawl.max_pages
                    else "frontier_exhausted"
                )
            pending = [*queue, *self._inflight.values()]
            self._stop(
                self.stats["stop_reason"],
                queued_states=len(pending),
                pages_attempted=self.report.pages_attempted,
                records=self.record_count(),
            )
            self.stats["pending_states"] = len(pending)
            refresh_coverage(self.stats, self.report.errors)
            for source in self.source_outcomes.values():
                source["pending_states"] = sum(
                    canonical_url(work.source_url or work.url) == canonical_url(source["url"])
                    for work in pending
                )
                if source["stop_reason"] is None and (
                    not source["attempted"] or source["pending_states"]
                ):
                    source["stop_reason"] = self.stats["stop_reason"]
            self._checkpoint()
            # The run is stopping: no pending action will consume its retained parent.
            for work in queue:
                if work.page is not None:
                    await self._release_idle(work.page, deque())

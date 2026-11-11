"""Assemble bounded HTML observations into navigation actions and page evidence."""

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from unified_web_parser.core.models import Page
from unified_web_parser.crawling.dom import (
    SECONDARY_SELECTOR,
    content_region,
    primary_detail_region,
    record_cards,
    record_container,
)
from unified_web_parser.navigation.controls import observe_controls
from unified_web_parser.navigation.html import is_visible, observed_label, selector_for
from unified_web_parser.navigation.models import NavigationAction, NavigationInventory
from unified_web_parser.navigation.policy import (
    FILTER_COMMAND,
    NEXT,
    collect_search_filters,
    has_filter_context,
    is_unsafe_control,
)
from unified_web_parser.navigation.urls import observed_url
from unified_web_parser.routing.candidates import (
    has_collection_markup,
    is_candidate_entity,
    is_page_offer,
    structured_entities,
)
from unified_web_parser.sources.navigation import canonical_url

_ENTITY_PATH = re.compile(r"/(?:products?|items?|listings?|jobs?|vacancies)/[^/]+/?$", re.I)
_CATEGORY_PATH = re.compile(r"/(?:categor(?:y|ies)|catalog|collections?|departments?)(?:/|$)", re.I)
_PAGE_LABEL = re.compile(r"^(?:(?:page|сторінка|страница)\s*)?\d+$", re.I)
_PAGINATION_CONTEXT = re.compile(
    r"paginat(?:ion|or)|page\s+navigation|навігаці\w*\s+сторін|навигаци\w*\s+страниц",
    re.I,
)


class InventoryBuilder:
    def __init__(
        self,
        max_actions: int = 80,
        max_summary_chars: int = 4000,
        *,
        allow_scroll: bool = True,
        allow_filters: bool = False,
        schema: dict[str, Any] | None = None,
        content_profile: str = "generic",
        currency_aliases: Mapping[str, str] | None = None,
        skip_url: Callable[[str], bool] | None = None,
    ) -> None:
        if not 1 <= max_actions <= 200 or max_summary_chars < 100:
            raise ValueError("Invalid navigation inventory limits")
        self.max_actions = max_actions
        self.max_summary_chars = max_summary_chars
        self.allow_scroll = allow_scroll
        self.allow_filters = allow_filters
        if content_profile not in {"generic", "commerce"}:
            raise ValueError("Unknown content profile")
        self.schema = schema
        self.content_profile = content_profile
        self.currency_aliases = currency_aliases
        self.skip_url = skip_url

    def build(self, page: Page, query: str | None = None) -> NavigationInventory:
        soup = BeautifulSoup(page.html, "html.parser")
        origin = str(page.url)
        filter_ids, filter_names, filter_bindings = collect_search_filters(
            soup, allow_filters=self.allow_filters
        )
        actions: list[NavigationAction] = []
        seen: set[str] = set()
        skipped_items: set[str] = set()

        def priority(kind: str, bindings: dict) -> int:
            # Retain a way forward even when a large result list fills the
            # bounded model inventory. Omitted items remain explicitly audited.
            if kind == "search" or bindings.get("hint") == "search":
                return 0
            if bindings.get("hint") in {"pagination", "load_more"}:
                return 1
            if bindings.get("hint") == "item":
                return 2
            if kind == "scroll":
                return 3
            return 4

        def add(kind: str, label: str, **bindings) -> None:
            identity = json.dumps([kind, label, bindings], sort_keys=True, ensure_ascii=False)
            digest = hashlib.sha256(identity.encode()).hexdigest()[:20]
            if digest not in seen:
                seen.add(digest)
                if len(actions) >= self.max_actions:
                    worst = max(
                        range(len(actions)),
                        key=lambda index: priority(
                            actions[index].kind, {"hint": actions[index].hint}
                        ),
                    )
                    if priority(kind, bindings) >= priority(
                        actions[worst].kind, {"hint": actions[worst].hint}
                    ):
                        return
                    actions.pop(worst)
                actions.append(
                    NavigationAction(
                        action_id="nav:" + digest,
                        page_id=page.page_id,
                        kind=kind,
                        label=label,
                        **bindings,
                    )
                )

        entities = []
        collection_markup = False
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                document = json.loads(script.string or script.get_text())
            except (ValueError, TypeError):
                continue
            collection_markup = collection_markup or has_collection_markup(
                document, content_profile=self.content_profile, schema=self.schema
            )
            nodes = document if isinstance(document, list) else [document]
            queue = list(nodes)
            while queue:
                node = queue.pop(0)
                if not isinstance(node, dict):
                    continue
                queue.extend(node.get("@graph", []))
                potential = node.get("potentialAction", [])
                for item in potential if isinstance(potential, list) else [potential]:
                    if not isinstance(item, dict) or item.get("@type") != "SearchAction":
                        continue
                    target = item.get("target")
                    template = target.get("urlTemplate") if isinstance(target, dict) else target
                    query_input = item.get("query-input", "")
                    if isinstance(query_input, dict):
                        query_input = query_input.get("valueName", "")
                    if not isinstance(template, str) or not isinstance(query_input, str):
                        continue
                    match = re.search(r"(?:name=)?([\w-]+)$", query_input.strip())
                    variable = match.group(1) if match else None
                    absolute = observed_url(origin, template)
                    if variable and absolute and "{" + variable + "}" in absolute:
                        add(
                            "search",
                            "Structured website search",
                            url=origin,
                            url_template=absolute,
                            query_name=variable,
                            hint="search",
                            **filter_bindings,
                        )
            for entity in structured_entities(
                document, content_profile=self.content_profile, schema=self.schema
            ):
                if is_candidate_entity(entity, self.schema, content_profile=self.content_profile):
                    entities.append(entity)

        entity_url = (
            (entities[0].get("url") or entities[0].get("@id")) if len(entities) == 1 else None
        )
        bound_entity_url = observed_url(origin, entity_url) if isinstance(entity_url, str) else None
        explicit_entity = bool(
            bound_entity_url and canonical_url(bound_entity_url) == canonical_url(origin)
        )
        own_offer = self.content_profile == "commerce" and any(
            is_page_offer(entity, origin) for entity in entities
        )
        detail_region = (
            None
            if own_offer
            else primary_detail_region(
                content_region(soup),
                content_profile=self.content_profile,
                schema=self.schema,
                currency_aliases=self.currency_aliases,
            )
        )
        page_cards = (
            []
            if own_offer
            else record_cards(
                content_region(soup),
                content_profile=self.content_profile,
                currency_aliases=self.currency_aliases,
            )
        )
        repeated_cards = len(page_cards) > 1
        strong_current_entity = own_offer or (
            not collection_markup
            and (
                detail_region is not None
                or (self.content_profile == "commerce" and explicit_entity and not repeated_cards)
            )
        )

        observe_controls(
            soup,
            origin,
            add,
            allow_filters=self.allow_filters,
            filter_ids=filter_ids,
            filter_names=filter_names,
            filter_bindings=filter_bindings,
        )

        links = []
        card_cache: dict = {}
        content_links = {
            id(anchor) for card in page_cards for anchor in card.find_all("a", href=True)
        }
        for anchor in soup.find_all("a", href=True):
            if not is_visible(anchor) or anchor.find_parent("footer"):
                continue
            label = observed_label(anchor)
            href = str(anchor["href"])
            url = observed_url(origin, href)
            if not url or not label or is_unsafe_control(anchor):
                continue
            relation = str(anchor.get("rel", ""))
            numbered_page = bool(
                _PAGE_LABEL.fullmatch(label)
                and any(
                    _PAGINATION_CONTEXT.search(
                        " ".join(
                            str(parent.get(key, ""))
                            for key in (
                                "aria-label",
                                "class",
                                "id",
                                "data-testid",
                                "data-cy",
                                "data-nx-name",
                            )
                        )
                    )
                    for parent in anchor.parents
                    if getattr(parent, "name", None) in {"nav", "ul", "ol", "div"}
                )
            )
            pagination = "next" in relation or bool(NEXT.search(label)) or numbered_page
            card = (
                record_container(
                    anchor,
                    _cache=card_cache,
                    content_profile=self.content_profile,
                    currency_aliases=self.currency_aliases,
                )
                if id(anchor) in content_links
                else None
            )
            card_item = card is not None
            explicit_item = bool(not page_cards and _ENTITY_PATH.search(urlsplit(url).path))
            filter_link = bool(
                (has_filter_context(anchor) or (FILTER_COMMAND.search(label) and not explicit_item))
                and not (card_item or _CATEGORY_PATH.search(urlsplit(url).path) or pagination)
            )
            if not self.allow_filters and filter_link:
                continue
            if card_item and card.name == "tr":
                heading = soup.find("h1")
                context = heading.get_text(" ", strip=True) if heading else ""
                label = (label + " — " + context + ": " + card.get_text(" ", strip=True))[:300]
            hint = (
                "filter"
                if filter_link
                else "pagination"
                if pagination
                else "item"
                if card_item or (not page_cards and _ENTITY_PATH.search(urlsplit(url).path))
                else "category"
                if _CATEGORY_PATH.search(urlsplit(url).path)
                else "unknown"
            )
            if hint == "item" and canonical_url(url) == canonical_url(origin):
                continue
            if strong_current_entity and hint == "item":
                continue
            if hint == "item" and self.skip_url is not None and self.skip_url(url):
                skipped_items.add(canonical_url(url))
                continue
            if href.startswith("#"):
                links.append((3, "click", label, {"selector": selector_for(anchor), "hint": hint}))
            else:
                links.append(
                    (
                        0 if hint == "item" else 1 if hint == "pagination" else 2,
                        "open",
                        label,
                        {
                            "url": url,
                            "selector": selector_for(anchor),
                            "hint": hint,
                            "group": "result_items" if hint == "item" else None,
                        },
                    )
                )
        unique_urls: set[str] = set()
        for _, kind, label, bindings in sorted(links, key=lambda value: value[0]):
            if bindings.get("url") in unique_urls:
                continue
            if bindings.get("url"):
                unique_urls.add(bindings["url"])
            add(kind, label, **bindings)

        item_actions = [action for action in actions if action.hint == "item"]
        meaningful_navigation = any(action.kind != "scroll" for action in actions)
        has_entity = strong_current_entity or bool(
            not collection_markup
            and not repeated_cards
            and len(entities) == 1
            and (
                (
                    explicit_entity
                    and (self.content_profile == "commerce" or not meaningful_navigation)
                )
                or (not entity_url and not meaningful_navigation)
            )
        )
        has_results = not has_entity and (
            bool(page_cards) or bool(item_actions) or len(entities) > 1
        )
        if self.allow_scroll and (has_results or len(page.html) > 4000):
            add("scroll", "Scroll to reveal more content", hint="scroll")
        for tag in soup.select("script, style, noscript, svg, " + SECONDARY_SELECTOR):
            tag.decompose()
        title = soup.find("title") or soup.find("h1")
        main = content_region(soup)
        summary = re.sub(r"\s+", " ", main.get_text(" ", strip=True))[: self.max_summary_chars]
        return NavigationInventory(
            page_id=page.page_id,
            url=origin,
            title=title.get_text(" ", strip=True) if title else "",
            summary=summary,
            actions=tuple(actions),
            has_entity=has_entity,
            has_results=has_results,
            observed_action_count=len(seen),
            omitted_action_count=max(0, len(seen) - len(actions)),
            skipped_item_count=len(skipped_items),
        )

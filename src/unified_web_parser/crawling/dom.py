"""Structural record evidence with optional, explicitly selected commerce policy."""

from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from bs4 import BeautifulSoup, NavigableString, Tag

from unified_web_parser.profiles.commerce import has_price_range, read_price

CARD_SELECTOR = 'article, [role="listitem"]'
SECONDARY_SELECTOR = (
    'nav, header, footer, aside, [role="navigation"], [role="banner"], '
    '[role="complementary"], [role="contentinfo"], dialog, [role="dialog"], [role="alertdialog"]'
)


def content_region(root: BeautifulSoup | Tag) -> BeautifulSoup | Tag:
    return root.find("main") or root.select_one('[role="main"]') or root.find("body") or root


def _secondary(tag: Tag) -> bool:
    return any(
        parent.name in {"nav", "header", "footer", "aside", "dialog"}
        or parent.get("role")
        in {"navigation", "banner", "complementary", "contentinfo", "dialog", "alertdialog"}
        or parent.has_attr("hidden")
        or parent.get("aria-hidden") == "true"
        or "display:none" in str(parent.get("style", "")).replace(" ", "").lower()
        or "visibility:hidden" in str(parent.get("style", "")).replace(" ", "").lower()
        for parent in [tag, *tag.parents]
        if isinstance(parent, Tag)
    )


def _money(text: str, aliases: Mapping[str, str] | None) -> bool:
    from unified_web_parser.profiles.commerce import has_money_evidence

    return has_money_evidence(text, currency_aliases=aliases)


def _data_rows(root: BeautifulSoup | Tag) -> list[Tag]:
    """Tabular records need cells/column structure, not links, images or money."""
    rows = []
    tables = ([root] if isinstance(root, Tag) and root.name == "table" else []) + root.find_all(
        "table"
    )
    for table in tables:
        if _secondary(table):
            continue
        peers = [row for row in table.find_all("tr") if row.find_parent("table") is table]
        has_columns = any(len(row.find_all("th", recursive=False)) >= 2 for row in peers)
        data = [row for row in peers if 2 <= len(row.find_all("td", recursive=False)) <= 32]
        if not has_columns and len(data) < 2:
            continue
        for row in data:
            cells = row.find_all("td", recursive=False)
            if sum(bool(cell.get_text(" ", strip=True)) for cell in cells) >= 2:
                rows.append(row)
    return rows


def record_container(
    anchor: Tag,
    *,
    _cache: dict | None = None,
    content_profile: str = "generic",
    currency_aliases: Mapping[str, str] | None = None,
) -> Tag | None:
    """Bind an anchor to a repeated/semantic record with independent supporting text."""
    if _secondary(anchor):
        return None
    cache = _cache if _cache is not None else {}
    commerce = content_profile == "commerce"
    label = anchor.get_text(" ", strip=True)
    if len(label) < 3 or (commerce and _money(label, currency_aliases)):
        return None
    row = anchor.find_parent("tr")
    if row is not None:
        if row.find("tr") or len(row.find_all("a", href=True, limit=9)) > 8:
            return None
        table = row.find_parent("table")
        if not commerce:
            return row if table and any(item is row for item in _data_rows(table)) else None
        peers = row.parent.find_all("tr", recursive=False) if row.parent else []
        priced = sum(
            bool(peer.find("a", href=True))
            and _money(peer.get_text(" ", strip=True), currency_aliases)
            for peer in peers
        )
        return (
            row if priced > 1 and _money(row.get_text(" ", strip=True), currency_aliases) else None
        )
    for ancestor in list(anchor.parents)[:7]:
        if not isinstance(ancestor, Tag) or ancestor.name in {"main", "body", "html", "[document]"}:
            break
        if ancestor.find("h1"):
            break
        if id(ancestor) in cache:
            if cache[id(ancestor)] is not None and cache[id(ancestor)] == anchor.get("href"):
                return ancestor
            continue
        cache[id(ancestor)] = None
        links = ancestor.find_all("a", href=True, limit=13)
        text = ancestor.get_text(" ", strip=True)
        if len(links) > 12 or len(text) > 6000:
            continue
        heading = ancestor.find(["h2", "h3", "h4", "h5", "h6"])
        semantic = (
            ancestor.name in {"article", "li", "tr"}
            or ancestor.get("role") == "listitem"
            or ancestor.has_attr("itemtype")
        )
        siblings = (
            ancestor.parent.find_all(ancestor.name, recursive=False) if ancestor.parent else []
        )
        peers = [
            sibling
            for sibling in siblings
            if sibling.get("class", []) == ancestor.get("class", [])
            and sibling.find("a", href=True)
        ]
        repeated = len(peers) > 1 and (bool(ancestor.get("class")) or semantic)
        if commerce:
            priced = bool(
                ancestor.select_one('[itemprop="price"], [itemprop="baseSalary"]')
            ) or _money(text, currency_aliases)
            image = ancestor.find("img") is not None or any(
                "background" in str(tag.get("style", "")) for tag in ancestor.find_all(True)
            )
            if heading is None:
                if not ((priced and (semantic or repeated or image)) or (repeated and image)):
                    continue
            elif not (semantic or priced or repeated):
                continue
        else:
            if ancestor.name in {"ul", "ol"} or (
                heading is None and ancestor.find(["ul", "ol"], recursive=False)
            ):
                continue
            if ancestor.name == "li" and not (
                heading
                or ancestor.has_attr("itemtype")
                or ancestor.find("time")
                or (repeated and ancestor.get("class"))
            ):
                continue
            supporting = " ".join(
                node.strip()
                for node in ancestor.find_all(string=True)
                if node.strip() and not node.find_parent("a")
            )
            # A repeated category/menu link alone is navigation, not a data record.
            linked_article = ancestor.name == "article" and heading is not None
            if not (semantic or repeated) or (
                not supporting and not ancestor.find("time") and not linked_article
            ):
                continue
        if heading is None:
            named = [
                link
                for link in links
                if len(link.get_text(" ", strip=True)) >= 3
                and not (commerce and _money(link.get_text(" ", strip=True), currency_aliases))
            ]
            primary = max(named, key=lambda link: len(link.get_text(" ", strip=True)), default=None)
        else:
            primary = heading.find("a", href=True) or heading.find_parent("a", href=True)
            primary = primary or ancestor.find("a", href=True)
        cache[id(ancestor)] = primary.get("href") if primary else None
        if primary is anchor or (primary and primary.get("href") == anchor.get("href")):
            return ancestor
    return None


def record_cards(
    root: BeautifulSoup | Tag,
    *,
    content_profile: str = "generic",
    currency_aliases: Mapping[str, str] | None = None,
) -> list[Tag]:
    """Bounded structural cards and data rows; retain one representation per record."""
    cards: list[Tag] = []
    seen: set[int] = set()
    cache: dict = {}
    heading = next((node for node in root.find_all("h1") if not _secondary(node)), None)
    after_heading = heading is None
    for anchor in root.find_all(["h1", "a"]):
        if anchor.name == "h1":
            after_heading = after_heading or anchor is heading
            continue
        if not after_heading or not anchor.get("href"):
            continue
        card = record_container(
            anchor, _cache=cache, content_profile=content_profile, currency_aliases=currency_aliases
        )
        if card is not None and id(card) not in seen:
            seen.add(id(card))
            cards.append(card)
    if content_profile == "generic":
        additional = _data_rows(root)
        for card in root.select(CARD_SELECTOR):
            if card.find("h1") or _secondary(card):
                continue
            if (
                card.find(["h2", "h3", "h4", "h5", "h6"])
                and len(card.get_text(" ", strip=True)) >= 12
            ):
                additional.append(card)
        for card in additional:
            if id(card) not in seen:
                seen.add(id(card))
                cards.append(card)
    return [card for card in cards if not any(id(parent) in seen for parent in card.parents)]


def _key(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())


def _schema_facts(candidate: Tag, schema: dict[str, Any] | None) -> bool:
    wanted = {
        _key(name)
        for name in (schema or {}).get("properties", {})
        if name not in {"title", "name", "url", "source_url"} and _key(name)
    }
    for node in candidate.select("[itemprop]"):
        if any(_key(prop) in wanted for prop in str(node.get("itemprop", "")).split()):
            return True
    for node in candidate.find_all(["dt", "th", "p"]):
        label = node.get_text(" ", strip=True).split(":", 1)[0]
        if _key(label) in wanted:
            return True
    return False


def primary_detail_region(
    root: BeautifulSoup | Tag,
    *,
    content_profile: str = "generic",
    schema: dict[str, Any] | None = None,
    currency_aliases: Mapping[str, str] | None = None,
) -> Tag | None:
    """Find an individual entity's own evidence, excluding nested recommendations.

    Generic records rely on semantic body/metadata and requested fields. Commerce
    explicitly requires an own offer price rather than borrowing a card's price.
    """
    cleaned = deepcopy(root)
    original_cards = record_cards(
        cleaned, content_profile=content_profile, currency_aliases=currency_aliases
    )
    for nested in original_cards:
        if nested.parent is not None:
            nested.decompose()
    for nested in list(cleaned.select(CARD_SELECTOR)):
        if nested.parent is not None and nested.find("h1") is None:
            nested.decompose()
    for secondary in list(cleaned.select(SECONDARY_SELECTOR + ", form")):
        if secondary.parent is not None:
            secondary.decompose()
    containers = list(cleaned.select("article, [itemtype]"))
    heading = cleaned.find("h1")
    if heading is not None:
        for ancestor in heading.parents:
            if isinstance(ancestor, Tag) and ancestor.name in {"main", "div", "section", "body"}:
                containers.append(ancestor)
            if ancestor is cleaned:
                break
    checked: set[int] = set()
    for candidate in containers:
        if id(candidate) in checked:
            continue
        checked.add(id(candidate))
        heading = candidate.find("h1")
        if heading is None:
            continue
        lead = []
        lead_has_price = False
        # Walk within the chosen container only; next_elements can leak into siblings.
        after_heading = False
        for element in candidate.descendants:
            if element is heading:
                after_heading = True
            if not after_heading:
                continue
            if isinstance(element, Tag) and element.name in {"h2", "h3", "h4", "h5", "h6"}:
                break
            if isinstance(element, Tag) and element.get("itemprop") in {"price", "baseSalary"}:
                lead_has_price = True
            if isinstance(element, NavigableString) and element.parent.name not in {
                "script",
                "style",
            }:
                lead.append(str(element))
        lead_text = " ".join(lead)
        if content_profile == "commerce":
            # An explicit own price is stronger than text that resembles a
            # range, such as an old/current price followed by a discount.
            # Related cards were removed before scanning this heading's lead.
            if lead_has_price or (
                not has_price_range(lead_text, currency_aliases=currency_aliases)
                and read_price(lead_text, currency_aliases=currency_aliases)
            ):
                return deepcopy(candidate)
            continue
        paragraphs = [node for node in candidate.find_all("p") if node.get_text(" ", strip=True)]
        body_text = (
            candidate.get_text(" ", strip=True)
            .replace(heading.get_text(" ", strip=True), "", 1)
            .strip()
        )
        link_text = " ".join(node.get_text(" ", strip=True) for node in candidate.find_all("a"))
        semantic_detail = (candidate.name == "article" or candidate.has_attr("itemtype")) and len(
            body_text
        ) >= 12
        temporal_detail = candidate.find("time", datetime=True) is not None and len(body_text) >= 12
        prose_detail = (
            not original_cards
            and len(body_text) >= 120
            and len(paragraphs) >= 2
            and len(link_text) < len(body_text) / 3
        )
        if semantic_detail or temporal_detail or _schema_facts(candidate, schema) or prose_detail:
            return deepcopy(candidate)
    return None

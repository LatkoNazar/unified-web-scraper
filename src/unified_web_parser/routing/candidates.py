"""Build bounded, reproducible evidence blocks before AI routing."""

import hashlib
import json
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from copy import deepcopy
from typing import Any
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup, Tag

from unified_web_parser.core.models import CandidateBlock, CandidateKind, Page
from unified_web_parser.crawling.dom import (
    CARD_SELECTOR,
    SECONDARY_SELECTOR,
    content_region,
    primary_detail_region,
    record_cards,
)
from unified_web_parser.profiles.commerce import read_price
from unified_web_parser.sources.navigation import canonical_url

_COMMERCE_DESCRIPTORS = {
    "WebSite",
    "WebPage",
    "Organization",
    "BreadcrumbList",
    "ItemList",
    "CollectionPage",
    "SearchResultsPage",
    "ProductGroup",
    "OfferCatalog",
    "AggregateOffer",
}
_GENERIC_CONTAINERS = {
    "WebSite",
    "BreadcrumbList",
    "ItemList",
    "CollectionPage",
    "SearchResultsPage",
}


def _entity_type(value: str) -> str:
    """Only schema.org HTTP(S) spellings are equivalent to local type names."""
    try:
        parsed = urlsplit(value)
    except ValueError:
        return value
    if parsed.scheme in {"http", "https"} and parsed.netloc.lower() in {
        "schema.org",
        "www.schema.org",
    }:
        return parsed.path.strip("/")
    return value


def is_collection_entity(entity: dict[str, Any], *, content_profile: str = "generic") -> bool:
    """A range or several distinct offers describes a collection, not one offer."""
    kind = entity.get("@type")
    if content_profile != "commerce":
        return kind in ("ItemList", "CollectionPage", "SearchResultsPage")
    if kind in ("ProductGroup", "CollectionPage", "SearchResultsPage", "OfferCatalog"):
        return True
    offers = entity.get("offers", entity.get("Offers"))
    if isinstance(offers, dict):
        return offers.get("@type") == "AggregateOffer"
    if isinstance(offers, list):
        urls = {
            offer.get("url")
            for offer in offers
            if isinstance(offer, dict) and isinstance(offer.get("url"), str)
        }
        return not offers or len(urls) > 1
    return False


def has_collection_markup(
    value: Any, *, content_profile: str = "generic", schema: dict[str, Any] | None = None
) -> bool:
    if isinstance(value, list):
        return any(
            has_collection_markup(item, content_profile=content_profile, schema=schema)
            for item in value
        )
    if isinstance(value, dict):
        if (schema or {}).get("x-entity-types") and value.get("@type") == "ItemList":
            members = value.get("itemListElement")
            if isinstance(members, list) and members:
                nodes = [
                    member.get("item", member) if isinstance(member, dict) else member
                    for member in members
                ]
                if all(isinstance(node, dict) and node.get("@type") for node in nodes) and not any(
                    is_candidate_entity(node, schema, content_profile=content_profile)
                    for node in nodes
                ):
                    # A typed recommendation list outside the requested entity
                    # types does not turn the primary page into a target catalog.
                    return has_collection_markup(
                        value.get("@graph", []), content_profile=content_profile, schema=schema
                    )
        if (schema or {}).get("x-entity-types") and is_candidate_entity(
            value, schema, content_profile=content_profile
        ):
            return False
        return is_collection_entity(
            value, content_profile=content_profile
        ) or has_collection_markup(
            value.get("@graph", []), content_profile=content_profile, schema=schema
        )
    return False


def is_page_offer(entity: dict[str, Any], source_url: str) -> bool:
    """One priced Offer explicitly belonging to this page outranks recommendations."""
    identities = [
        value
        for value in (entity.get("url"), entity.get("@id"))
        if isinstance(value, str) and value.strip()
    ]
    offer = entity.get("offers")
    return bool(
        identities
        and all(
            canonical_url(urljoin(source_url, value)) == canonical_url(source_url)
            for value in identities
        )
        and isinstance(offer, dict)
        and offer.get("@type") == "Offer"
        and offer.get("price") is not None
        and offer.get("priceCurrency")
    )


def is_record_entity(
    entity: dict[str, Any],
    *,
    content_profile: str = "generic",
    schema: dict[str, Any] | None = None,
) -> bool:
    """Keep named domain entities, rather than page/list navigation descriptors."""
    types = entity.get("@type", [])
    types = [types] if isinstance(types, str) else types
    if not isinstance(types, list) or not types or not all(isinstance(item, str) for item in types):
        return False
    names = {_entity_type(item) for item in types}
    requested = (schema or {}).get("x-entity-types")
    if isinstance(requested, list) and requested:
        return bool(names & {_entity_type(item) for item in requested if isinstance(item, str)})
    return bool(
        any(entity.get(key) for key in ("name", "headline", "title"))
        and not names
        <= (_COMMERCE_DESCRIPTORS if content_profile == "commerce" else _GENERIC_CONTAINERS)
    )


def is_candidate_entity(
    entity: dict[str, Any],
    schema: dict[str, Any] | None = None,
    *,
    content_profile: str = "generic",
) -> bool:
    """Apply the same schema-aware evidence policy to routing and record identity."""
    if not is_record_entity(entity, content_profile=content_profile, schema=schema):
        return False
    types = entity.get("@type", [])
    types = [types] if isinstance(types, str) else types
    if (schema or {}).get("x-entity-types"):
        return True
    names = {_entity_type(item) for item in types}
    return not (
        content_profile == "commerce"
        and names & {"VideoObject", "ImageObject", "AudioObject", "Review", "FAQPage"}
    )


def structured_entities(
    value: Any, *, content_profile: str = "generic", schema: dict[str, Any] | None = None
) -> Iterator[dict[str, Any]]:
    """Flatten JSON-LD containers without treating nested offers as separate records."""
    if isinstance(value, list):
        for item in value:
            yield from structured_entities(item, content_profile=content_profile, schema=schema)
    elif isinstance(value, dict):
        if (schema or {}).get("x-entity-types") and is_candidate_entity(
            value, schema, content_profile=content_profile
        ):
            yield value
        elif "@graph" in value:
            yield from structured_entities(
                value["@graph"], content_profile=content_profile, schema=schema
            )
        elif (
            content_profile == "commerce"
            and is_collection_entity(value, content_profile=content_profile)
            and isinstance(value.get("offers"), dict)
            and (value["offers"].get("@type") == "AggregateOffer")
        ):
            # A category's lowPrice is not the price of a single listing.
            yield from structured_entities(
                value["offers"].get("offers", []), content_profile=content_profile, schema=schema
            )
        elif (
            content_profile == "commerce"
            and is_collection_entity(value, content_profile=content_profile)
            and "itemListElement" not in value
        ):
            return
        elif content_profile == "commerce" and value.get("@type") == "AggregateOffer":
            yield from structured_entities(
                value.get("offers", []), content_profile=content_profile, schema=schema
            )
        elif value.get("@type") in ("ItemList", "CollectionPage") and "itemListElement" in value:
            yield from structured_entities(
                value["itemListElement"], content_profile=content_profile, schema=schema
            )
        elif value.get("@type") == "ListItem" and isinstance(value.get("item"), dict):
            yield from structured_entities(
                value["item"], content_profile=content_profile, schema=schema
            )
        else:
            yield value


class CandidateBuilder:
    def __init__(
        self,
        max_candidates: int = 40,
        max_chars: int = 16000,
        schema: dict[str, Any] | None = None,
        *,
        content_profile: str = "generic",
        currency_aliases: Mapping[str, str] | None = None,
        skip_url: Callable[[str], bool] | None = None,
    ) -> None:
        if max_candidates < 1 or max_chars < 100:
            raise ValueError("Candidate limits must be positive and max_chars at least 100")
        self.max_candidates = max_candidates
        self.max_chars = max_chars
        self.schema = schema
        if content_profile not in {"generic", "commerce"}:
            raise ValueError("Unknown content profile")
        self.content_profile = content_profile
        self.currency_aliases = currency_aliases
        self.skip_url = skip_url

    def build(self, page: Page) -> Sequence[CandidateBlock]:
        soup = BeautifulSoup(page.html, "html.parser")
        blocks: list[CandidateBlock] = []
        remaining = self.max_chars
        seen: set[str] = set()
        collection_markup = False
        own_offer = False

        def known_url(value: Any) -> bool:
            if self.skip_url is None or not isinstance(value, str) or not value.strip():
                return False
            try:
                resolved = urljoin(str(page.url), value)
                parsed = urlsplit(resolved)
                _ = parsed.port
                if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                    return False
            except ValueError:
                return False
            # Persistence failures must reach the caller, never become a cache miss.
            return self.skip_url(resolved)

        def add(kind: CandidateKind, content: str, locator: str, *, truncate: bool = True) -> None:
            nonlocal remaining
            content = content.strip()
            if not content or len(blocks) >= self.max_candidates or remaining < 100:
                return
            if not truncate and len(content) > remaining:
                return
            content = content[:remaining]
            digest = hashlib.sha256(content.encode()).hexdigest()[:16]
            if digest in seen:
                return
            seen.add(digest)
            candidate_id = f"{page.page_id}:{kind.value}:{digest}"
            blocks.append(
                CandidateBlock(
                    candidate_id=candidate_id, kind=kind, content=content, locator=locator
                )
            )
            remaining -= len(content)

        for index, script in enumerate(soup.find_all("script", type="application/ld+json")):
            try:
                document = json.loads(script.string or script.get_text())
            except (json.JSONDecodeError, TypeError):
                continue
            collection_markup = collection_markup or has_collection_markup(
                document, content_profile=self.content_profile, schema=self.schema
            )
            for entity_index, entity in enumerate(
                structured_entities(
                    document, content_profile=self.content_profile, schema=self.schema
                )
            ):
                if not is_candidate_entity(
                    entity, self.schema, content_profile=self.content_profile
                ):
                    continue
                own_offer = own_offer or (
                    self.content_profile == "commerce" and is_page_offer(entity, str(page.url))
                )
                if known_url(entity.get("url") or entity.get("@id")):
                    continue
                add(
                    CandidateKind.JSON_LD,
                    json.dumps(entity, ensure_ascii=False),
                    f"jsonld:{index}:{entity_index}",
                    truncate=False,
                )

        if own_offer:
            own_blocks = [
                block for block in blocks if is_page_offer(json.loads(block.content), str(page.url))
            ]
            blocks = own_blocks
            if own_blocks and all(
                json.loads(block.content).get("description") for block in own_blocks
            ):
                return own_blocks

        for tag in soup.select("script, style, noscript, svg, form, " + SECONDARY_SELECTOR):
            tag.decompose()
        main = content_region(soup)
        primary = (
            None
            if collection_markup
            else primary_detail_region(
                main,
                content_profile=self.content_profile,
                schema=self.schema,
                currency_aliases=self.currency_aliases,
            )
        )
        if own_offer and primary is None:
            return blocks  # Related cards are not alternative representations of this offer.
        if primary is not None:
            if known_url(str(page.url)):
                return blocks
            main = primary
        # Repeating semantic cards are useful on search/category pages across domains.
        cards = (
            record_cards(
                main, content_profile=self.content_profile, currency_aliases=self.currency_aliases
            )
            if primary is None
            else []
        )
        if not cards and primary is None:
            cards = main.select(CARD_SELECTOR)
        useful_cards = [
            tag
            for tag in cards
            if len(tag.get_text(" ", strip=True))
            >= (40 if self.content_profile == "commerce" else 3)
            and not tag.select(CARD_SELECTOR)
        ]
        if collection_markup and self.content_profile == "commerce":
            useful_cards = [
                card
                for card in useful_cards
                if read_price(
                    card.get_text(" ", strip=True), currency_aliases=self.currency_aliases
                )
                is not None
            ]
        if collection_markup and useful_cards:
            # A secondary section after the primary result group (price history,
            # related products, reviews) cannot contribute new offers. Headings
            # inside the result cards themselves remain part of their evidence.
            card_ids = {id(card) for card in useful_cards}
            before_secondary: list[Tag] = []
            started = False
            for tag in main.find_all(True):
                if id(tag) in card_ids:
                    before_secondary.append(tag)
                    started = True
                elif (
                    started
                    and tag.name in {"h2", "h3", "h4", "h5", "h6"}
                    and not any(id(parent) in card_ids for parent in tag.parents)
                ):
                    break
            useful_cards = before_secondary
        if self.skip_url is not None and useful_cards:
            remaining_cards = []
            for card in useful_cards:
                heading = card.find(["h1", "h2", "h3", "h4", "h5", "h6"])
                anchor = heading.find("a", href=True) if heading else None
                if anchor is None:
                    anchors = card.find_all("a", href=True)
                    targets = {str(item["href"]) for item in anchors}
                    anchor = anchors[0] if len(targets) == 1 else None
                if anchor is None or not known_url(str(anchor["href"])):
                    remaining_cards.append(card)
            if not remaining_cards:
                return blocks
            useful_cards = remaining_cards
        if useful_cards:
            per_card = max(200, remaining // min(len(useful_cards), self.max_candidates))
            for index, tag in enumerate(useful_cards):
                compact = deepcopy(tag)
                table = tag.find_parent("table") if tag.name == "tr" else None
                column_headers = table.find("tr") if table else None
                if column_headers is not None and column_headers.find("th"):
                    fragment = BeautifulSoup(
                        "<table><thead></thead><tbody></tbody></table>", "html.parser"
                    )
                    fragment.thead.append(deepcopy(column_headers))
                    fragment.tbody.append(compact)
                    compact = fragment.table
                for decoration in compact.select("svg, input, button, picture, img, source"):
                    decoration.decompose()
                for element in [compact, *compact.find_all(True)]:
                    element.attrs = {
                        key: value
                        for key, value in element.attrs.items()
                        if key in {"href", "content", "itemprop", "datetime", "alt"}
                    }
                add(CandidateKind.DOM, str(compact)[: min(per_card, 2400)], f"card:{index}")
        elif not collection_markup:
            # Keep headings, visible attributes and description together for detail extraction.
            # Remove bulky attributes rather than stripping meaningful text or links.
            for tag in main.find_all(True):
                if isinstance(tag, Tag):
                    tag.attrs = {
                        key: value
                        for key, value in tag.attrs.items()
                        if key in {"href", "content", "itemprop", "datetime", "alt"}
                    }
            text = re.sub(r"\s+", " ", str(main)).strip()
            add(CandidateKind.DOM, text, "entity" if primary is not None else "main")
        return blocks

"""Associate only observed HTTP(S) images with their source record."""

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import unquote, urljoin, urlsplit

from bs4 import BeautifulSoup, Tag

from unified_web_parser.core.models import CandidateBlock, CandidateKind, Page
from unified_web_parser.crawling.dom import (
    SECONDARY_SELECTOR,
    content_region,
    primary_detail_region,
    record_cards,
    record_container,
)
from unified_web_parser.routing.candidates import (
    is_candidate_entity,
    is_page_offer,
    structured_entities,
)
from unified_web_parser.sources.navigation import canonical_url

_PLACEHOLDER = re.compile(
    r"(?:^|[-_.])(?:placeholder|spacer|transparent|blank|"
    r"no[-_]?(?:image|thumbnail|photo|picture)|loading)(?:[-_.]|$)",
    re.I,
)


def image_url(value: Any, base_url: str) -> str | None:
    """Resolve an observed reference without accepting active/embedded URLs or credentials."""
    if not isinstance(value, str) or not value.strip() or value.strip().startswith("#"):
        return None
    if any(ord(character) < 32 for character in value) or "\\" in value:
        return None
    try:
        parsed = urlsplit(urljoin(base_url, value.strip()))
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or any(character.isspace() for character in parsed.netloc)
        ):
            return None
        _ = parsed.port  # Access validates a source-supplied port without using it.
    except ValueError:
        return None
    filename = unquote(parsed.path.rsplit("/", 1)[-1])
    if _PLACEHOLDER.search(filename):
        return None
    return parsed.geturl()


def _image_value(
    value: Any, base_url: str, references: Mapping[str, Any], seen: frozenset[str] = frozenset()
) -> str | None:
    if isinstance(value, list):
        return next(
            (found for item in value if (found := _image_value(item, base_url, references, seen))),
            None,
        )
    if isinstance(value, dict):
        reference = value.get("@id")
        if isinstance(reference, str) and reference.strip():
            identity = _reference_url(reference, base_url)
            if identity in references and identity not in seen:
                if found := _image_value(
                    references[identity], base_url, references, seen | {identity}
                ):
                    return found
        return next(
            (
                found
                for key in ("contentUrl", "url", "@id")
                if (found := _image_value(value.get(key), base_url, references, seen))
            ),
            None,
        )
    if isinstance(value, str):
        if not value.strip():
            return None
        identity = _reference_url(value, base_url)
        if identity is None:
            return None
        if identity in references:
            if identity in seen:
                return None
            return _image_value(references[identity], base_url, references, seen | {identity})
        # A graph-node identifier is not itself an observed image resource.
        if value.startswith("#") or (
            "#" in value
            and not re.search(r"\.(?:jpe?g|png|webp|avif|gif|svg)(?:[?#]|$)", value, re.I)
        ):
            return None
    return image_url(value, base_url)


def _reference_url(value: str, base_url: str) -> str | None:
    try:
        return urljoin(base_url, value.strip())
    except ValueError:
        return None


def entity_image(
    entity: Mapping[str, Any], base_url: str, *, references: Mapping[str, Any] | None = None
) -> str | None:
    references = references or {}
    for key in ("image", "thumbnailUrl", "img_url"):
        if found := _image_value(entity.get(key), base_url, references):
            return found
    kinds = entity.get("@type", [])
    kinds = [kinds] if isinstance(kinds, str) else kinds
    if isinstance(kinds, list) and any(
        kind in {"ImageObject", "http://schema.org/ImageObject", "https://schema.org/ImageObject"}
        for kind in kinds
        if isinstance(kind, str)
    ):
        return _image_value(entity, base_url, references)
    return None


def _record_url(value: Any, base_url: str) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        resolved = urljoin(base_url, value)
        parsed = urlsplit(resolved)
        _ = parsed.port
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return None
        return canonical_url(resolved)
    except ValueError:
        return None


def _srcset(value: str, base_url: str) -> str | None:
    # Select an actual candidate in document order; never manufacture an image variant.
    # Commas inside a URL (e.g. CDN transformations) are not URL separators.
    position = 0
    while position < len(value):
        while position < len(value) and (value[position].isspace() or value[position] == ","):
            position += 1
        start = position
        while position < len(value) and not value[position].isspace():
            position += 1
        token = value[start:position]
        if found := image_url(token.rstrip(","), base_url):
            return found
        if not token.endswith(","):
            while position < len(value) and value[position] != ",":
                position += 1
    return None


def dom_image(root: BeautifulSoup | Tag, base_url: str) -> str | None:
    secondary = {id(tag) for tag in root.select(SECONDARY_SELECTOR)}
    tags = root.select('[itemprop~="image"], img, picture source')
    # A source-declared main image outranks earlier gallery thumbnails.
    tags.sort(key=lambda tag: "image" not in str(tag.get("itemprop", "")).split())
    for tag in tags:
        if any(id(parent) in secondary for parent in tag.parents):
            continue
        identity = " ".join(str(tag.get(key, "")) for key in ("class", "id", "role", "itemprop"))
        if re.search(r"(?:^|[\W_])(?:logo|favicon|icon|avatar)(?:[\W_]|$)", identity, re.I):
            continue
        width, height = str(tag.get("width", "")), str(tag.get("height", ""))
        if width.isdecimal() and height.isdecimal() and int(width) <= 2 and int(height) <= 2:
            continue
        for key in (
            "data-src",
            "data-lazy-src",
            "data-original",
            "data-original-src",
            "data-zoom-image",
            "data-large-image",
            "content",
            "src",
            "href",
        ):
            if found := image_url(tag.get(key), base_url):
                return found
        for key in ("data-srcset", "srcset"):
            if isinstance(value := tag.get(key), str) and (found := _srcset(value, base_url)):
                return found
    return None


def _own_metadata_identity(source: BeautifulSoup, entity: Mapping[str, Any], base_url: str) -> bool:
    """An already known record may bind to its canonical metadata-only detail page."""
    kinds = entity.get("@type", [])
    kinds = [kinds] if isinstance(kinds, str) else kinds
    excluded = {
        "Organization",
        "Person",
        "WebSite",
        "ItemList",
        "CollectionPage",
        "SearchResultsPage",
    }
    if isinstance(kinds, list) and any(
        str(kind).rstrip("/").rsplit("/", 1)[-1] in excluded for kind in kinds
    ):
        return False
    own_url = _record_url(entity.get("url") or entity.get("@id"), base_url)
    if own_url != canonical_url(base_url):
        return False
    canonical = source.find("link", rel="canonical")
    if canonical is None or _record_url(canonical.get("href"), base_url) != own_url:
        return False
    name = entity.get("name") or entity.get("headline") or entity.get("title")
    if not isinstance(name, str) or not name.strip():
        return False
    normalized = " ".join(name.split()).casefold()
    for meta in source.select('meta[property="og:title"], meta[name="og:title"]'):
        title = " ".join(str(meta.get("content", "")).split()).casefold()
        if title == normalized or (
            title.startswith(normalized)
            and title[len(normalized) :].lstrip().startswith((":", "|", "—", "–", "-"))
        ):
            return True
    return False


def _matching_entities(
    entity: Mapping[str, Any], page_entities: Sequence[dict[str, Any]], base_url: str
) -> list[dict[str, Any]]:
    own_url = _record_url(entity.get("url") or entity.get("@id"), base_url)
    if own_url is None:
        return []
    name = entity.get("name") or entity.get("headline") or entity.get("title")
    return [
        item
        for item in page_entities
        if _record_url(item.get("url") or item.get("@id"), base_url) == own_url
        and (
            not name
            or " ".join(str(item.get("name") or item.get("headline") or "").split()).casefold()
            == " ".join(str(name).split()).casefold()
        )
    ]


def observed_images(
    page: Page,
    candidate: CandidateBlock,
    entities: Sequence[dict[str, Any]],
    *,
    schema: dict[str, Any],
    content_profile: str,
    currency_aliases: Mapping[str, str],
) -> list[str | None]:
    """Recover source card images stripped from text evidence, never share a list-page hero."""
    base_url = str(page.url)
    source = BeautifulSoup(page.html, "html.parser")
    main = content_region(source)
    selected = BeautifulSoup(candidate.content, "html.parser")
    is_card = bool(candidate.locator and candidate.locator.startswith("card:"))
    page_entities = []
    references: dict[str, dict[str, Any]] = {}
    for script in source.find_all("script", type="application/ld+json"):
        try:
            document = json.loads(script.string or script.get_text())
        except (ValueError, TypeError):
            continue
        pending = [document]
        while pending:
            node = pending.pop()
            if isinstance(node, list):
                pending.extend(node)
            elif isinstance(node, dict):
                if (
                    isinstance(identity := node.get("@id"), str)
                    and identity.strip()
                    and (reference_url := _reference_url(identity, base_url)) is not None
                ):
                    target = references.setdefault(reference_url, {})
                    for key, value in node.items():
                        target.setdefault(key, value)
                pending.extend(node.values())
        page_entities.extend(
            entity
            for entity in structured_entities(
                document, content_profile=content_profile, schema=schema
            )
            if is_candidate_entity(entity, schema, content_profile=content_profile)
        )
    primary = primary_detail_region(
        main, schema=schema, content_profile=content_profile, currency_aliases=currency_aliases
    )
    own_offer = False
    if content_profile == "commerce" and len(entities) == 1:
        try:
            matches = _matching_entities(entities[0], page_entities, base_url)
            bound = matches[0] if len(matches) == 1 else entities[0]
            own_offer = is_page_offer(bound, base_url)
        except ValueError:
            pass  # Malformed optional image associations do not invalidate the record.
    heading = primary.find("h1") if primary is not None else None
    entity_name = next(
        (
            entities[0][key]
            for key in ("name", "headline", "title")
            if len(entities) == 1 and isinstance(entities[0].get(key), str)
        ),
        None,
    )
    matching_heading = bool(
        heading is not None
        and entity_name
        and " ".join(heading.get_text(" ", strip=True).split()).casefold()
        == " ".join(entity_name.split()).casefold()
    )
    single_detail = bool(
        not is_card
        and len(entities) == 1
        and _record_url(entities[0].get("url") or entities[0].get("@id"), base_url)
        in {None, canonical_url(base_url)}
        and (
            (primary is not None and matching_heading)
            or own_offer
            or _own_metadata_identity(source, entities[0], base_url)
        )
    )
    cards = record_cards(main, content_profile=content_profile, currency_aliases=currency_aliases)
    result = []
    for entity in entities:
        found = entity_image(entity, base_url, references=references)
        raw_own_url = entity.get("url") or entity.get("@id")
        own_url = _record_url(raw_own_url, base_url)
        if found is None and own_url:
            matches = _matching_entities(entity, page_entities, base_url)
            if len(matches) == 1:
                found = entity_image(matches[0], base_url, references=references)
        if found is None and own_url and own_url != canonical_url(base_url):
            bound = []
            for anchor in main.find_all("a", href=True):
                if _record_url(anchor.get("href"), base_url) != own_url:
                    continue
                card = record_container(
                    anchor, content_profile=content_profile, currency_aliases=currency_aliases
                )
                if card is not None and all(card is not item for item in bound):
                    bound.append(card)
            if len(bound) == 1:
                found = dom_image(bound[0], base_url)
        if found is None and is_card and len(entities) == 1:
            text = selected.get_text(" ", strip=True)
            matching = [card for card in cards if card.get_text(" ", strip=True) == text]
            if len(matching) == 1:
                found = dom_image(matching[0], base_url)
        if (
            found is None
            and candidate.kind == CandidateKind.DOM
            and len(entities) == 1
            and (is_card or single_detail)
        ):
            found = dom_image(selected, base_url)
        if found is None and single_detail:
            if raw_own_url is None or own_url == canonical_url(base_url):
                found = dom_image(primary, base_url) if primary is not None else None
                if found is None:
                    for meta in source.select(
                        'meta[property="og:image:secure_url"], meta[property="og:image"], '
                        'meta[name="og:image"], meta[name="twitter:image"]'
                    ):
                        if found := image_url(meta.get("content"), base_url):
                            break
        result.append(found)
    return result


def recover_record_image(
    page: Page,
    data: Mapping[str, Any],
    *,
    schema: dict[str, Any],
    content_profile: str = "generic",
    currency_aliases: Mapping[str, str] | None = None,
) -> str | None:
    """Recover a record's observed image from a saved page without extraction or AI.

    Detail heroes require a proven own primary entity. A record URL differing
    from the saved page must bind uniquely to an observed source card.
    """
    entity = {
        "name": data.get("title") or data.get("name") or data.get("headline"),
        "url": data.get("url") or str(page.url),
    }
    candidate = CandidateBlock(
        candidate_id=f"image-recovery:{page.page_id}",
        kind=CandidateKind.DOM,
        content="",
        locator="record-image-recovery",
    )
    return observed_images(
        page,
        candidate,
        [entity],
        schema=schema,
        content_profile=content_profile,
        currency_aliases=dict(currency_aliases or {}),
    )[0]

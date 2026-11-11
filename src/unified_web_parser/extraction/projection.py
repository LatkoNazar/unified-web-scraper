"""Project observed JSON-LD, DOM and table values into a requested schema."""

import html
import re
from collections.abc import Mapping
from decimal import Decimal
from typing import Any
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from unified_web_parser.extraction.images import entity_image
from unified_web_parser.profiles.commerce import read_price
from unified_web_parser.sources.navigation import canonical_url

_ALIASES = {
    "title": ("name", "headline", "title"),
    "name": ("name", "headline", "title"),
    "description": ("description", "articleBody", "text"),
    "currency": ("priceCurrency", "currency"),
    "currency_original": ("priceCurrency", "currency"),
    "url": ("url", "mainEntityOfPage", "@id"),
    "source_url": ("url", "mainEntityOfPage", "@id"),
    "brand": ("brand",),
    "availability": ("availability",),
    "image": ("image",),
    "author": ("author",),
    "published_at": ("datePublished",),
    "date_posted": ("datePosted",),
    "company": ("hiringOrganization",),
    "location": ("jobLocation", "address", "location"),
}


def _key(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())


def plain_value(value: Any) -> Any:
    if isinstance(value, dict):
        return next(
            (
                value[key]
                for key in ("name", "value", "url", "@id")
                if key in value and value[key] is not None
            ),
            None,
        )
    if isinstance(value, list):
        return plain_value(value[0]) if value else None
    return value


def _coerce(
    value: Any, definition: dict[str, Any], *, commerce: bool = False, condition: bool = False
) -> Any:
    if value is None:
        return None
    types = definition.get("type", [])
    types = [types] if isinstance(types, str) else types
    if "object" in types and isinstance(value, dict):
        return value
    if "array" in types and isinstance(value, list):
        return value
    value = plain_value(value)
    if isinstance(value, str):
        if commerce:
            value = (
                BeautifulSoup(value, "html.parser").get_text(" ", strip=True)
                if "<" in value
                else html.unescape(value)
            )
        if commerce and condition and value.startswith("http") and "enum" in definition:
            value = value.rsplit("/", 1)[-1].removesuffix("Condition").lower()
        if "integer" in types or "number" in types:
            cleaned = value.replace("\xa0", "").replace(" ", "") if commerce else value
            pattern = r"-?\d+(?:[.,]\d+)?" if commerce else r"-?\d+(?:\.\d+)?"
            if re.fullmatch(pattern, cleaned):
                number = Decimal(cleaned.replace(",", "."))
                return int(number) if number == number.to_integral_value() else float(number)
            return None
    if "string" in types and value is not None and not isinstance(value, (dict, list)):
        return str(value)
    return value


_MISSING = object()


def _source_path(entity: Any, path: str) -> Any:
    """Resolve an explicit dot path or local JSON Pointer, never an expression."""
    if path == "":
        return entity
    parts = (
        [part.replace("~1", "/").replace("~0", "~") for part in path[1:].split("/")]
        if path.startswith("/")
        else path.split(".")
    )
    value = entity
    for part in parts:
        if isinstance(value, dict) and part in value:
            value = value[part]
        elif isinstance(value, list) and part.isdecimal() and int(part) < len(value):
            value = value[int(part)]
        else:
            return _MISSING
    return value


def map_entity(
    entity: dict[str, Any],
    schema: dict[str, Any],
    page_url: str,
    *,
    content_profile: str = "generic",
    observed_img_url: str | None = None,
) -> dict[str, Any]:
    commerce = content_profile == "commerce"
    flat = dict(entity)
    if commerce:
        offers = entity.get("offers", {})
        if isinstance(offers, list):
            offers = offers[0] if len(offers) == 1 else {}
        if isinstance(offers, dict):
            for key, value in offers.items():
                flat.setdefault(key, value)
        properties = entity.get("additionalProperty", []) or []
        for item in properties if isinstance(properties, list) else []:
            if isinstance(item, dict) and isinstance(item.get("name"), str):
                flat.setdefault(item["name"], item.get("value"))
    normalized: dict[str, list[str]] = {}
    for key in flat:
        normalized.setdefault(_key(key), []).append(key)
    data = {}
    for name, definition in schema.get("properties", {}).items():
        explicit_path = definition.get("x-source")
        if isinstance(explicit_path, str):
            value = _source_path(entity, explicit_path)
        else:
            aliases = (name, *_ALIASES.get(name, ()))
            if commerce and name == "condition":
                aliases += ("itemCondition",)
            if commerce and name == "external_id":
                aliases += ("productID", "sku", "identifier")
            value = _MISSING
            # Exact keys win before any normalized aliases; collisions remain unknown.
            for alias in aliases:
                if alias in flat:
                    value = flat[alias]
                    break
            if value is _MISSING:
                for alias in aliases:
                    matches = normalized.get(_key(alias), [])
                    if len(matches) == 1:
                        value = flat[matches[0]]
                        break
            if name in {"url", "source_url"}:
                value = canonical_url(
                    urljoin(
                        page_url,
                        str(
                            plain_value(value)
                            if value is not _MISSING and value is not None
                            else page_url
                        ),
                    )
                )
        if name == "img_url":
            value = observed_img_url or entity_image(entity, page_url)
        value = _coerce(
            None if value is _MISSING else value,
            definition,
            commerce=commerce,
            condition=name == "condition",
        )
        if value is not None:
            data[name] = value
        elif "null" in definition.get("type", []):
            data[name] = None
    return data


def dom_entity(
    content: str,
    page_url: str,
    *,
    card: bool = False,
    content_profile: str = "generic",
    currency_aliases: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    soup = BeautifulSoup(content, "html.parser")
    entity: dict[str, Any] = {"url": page_url}
    heading = soup.find("h1") or soup.find(["h2", "h3", "h4", "h5", "h6"]) or soup.find("title")
    if heading:
        entity["name"] = heading.get_text(" ", strip=True)
    if card:
        anchor = heading.find("a", href=True) if heading else None
        anchor = anchor or soup.find("a", href=True)
        if anchor:
            href = urljoin(page_url, str(anchor["href"]))
            if urlsplit(href).scheme in {"http", "https"}:
                entity["url"] = href

    def properties(root: Any) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for tag in root.select("[itemprop]"):
            # Nested microdata belongs to its enclosing property, not the entity.
            enclosing = tag.find_parent(attrs={"itemscope": True, "itemprop": True})
            if enclosing is not None and enclosing is not root:
                continue
            prop = str(tag.get("itemprop", ""))
            value = (
                properties(tag)
                if tag.has_attr("itemscope")
                else tag.get("content") or tag.get("href") or tag.get_text(" ", strip=True)
            )
            result.setdefault(prop, value)
        return result

    for prop, value in properties(soup).items():
        if prop in {"name", "url"} and entity.get(prop):
            # Selected heading and page/card URL bindings outrank incidental
            # breadcrumb names or nested image-object URLs.
            continue
        entity[prop] = value
    table = soup.find("table")
    if table is not None:
        headers = [cell.get_text(" ", strip=True) for cell in table.select("thead th")]
        rows = [row for row in table.find_all("tr") if row.find("td") is not None]
        if len(rows) == 1:
            cells = rows[0].find_all("td", recursive=False)
            if len(cells) == len(headers):
                for header, cell in zip(headers, cells, strict=True):
                    if header and headers.count(header) == 1:
                        entity.setdefault(header, cell.get_text(" ", strip=True))
    text = soup.get_text(" ", strip=True)
    if content_profile == "commerce":
        # Keep heading numerals separate from actual monetary evidence.
        monetary = BeautifulSoup(content, "html.parser")
        for tag in monetary.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "title"]):
            tag.decompose()
        price = read_price(monetary.get_text(" ", strip=True), currency_aliases=currency_aliases)
        if price:
            amount, currency = price
            entity.setdefault("price", amount)
            if currency is not None:
                entity.setdefault("priceCurrency", currency)
    description = soup.select_one('[itemprop="description"]')
    entity.setdefault("description", description.get_text(" ", strip=True) if description else text)
    return entity

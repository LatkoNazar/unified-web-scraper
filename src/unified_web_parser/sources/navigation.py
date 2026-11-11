"""Shared canonical URL identity; navigation actions are discovered from live pages."""

from collections.abc import Iterable
from urllib.parse import parse_qsl, unquote_plus, urlencode, urlsplit, urlunsplit


def without_query_keys(url: str, names: Iterable[str]) -> str:
    """Remove only keys identified by observed filter controls, preserving other bytes."""
    excluded = set(names)
    parsed = urlsplit(url)
    query = "&".join(
        part
        for part in parsed.query.split("&")
        if unquote_plus(part.split("=", 1)[0]) not in excluded
    )
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, parsed.fragment))


def canonical_url(url: str) -> str:
    """Drop navigation tracking while retaining parameters that identify a resource."""
    parsed = urlsplit(url)
    tracking_keys = {
        "search_reason",
        "fbclid",
        "gclid",
        "msclkid",
        "yclid",
        "srsltid",
    }
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in tracking_keys
    ]
    return urlunsplit(
        (parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, urlencode(query), "")
    )

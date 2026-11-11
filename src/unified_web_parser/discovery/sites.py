"""Offline PSL site identity and an ordered, bounded discovery attribution ledger."""

import ipaddress
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import tldextract

from unified_web_parser.sources.navigation import canonical_url

_EXTRACT = tldextract.TLDExtract(
    suffix_list_urls=(),
    cache_dir=None,
    include_psl_private_domains=True,
)


def site_identity(url: str) -> str:
    """Group registrable domains while preserving independent private-suffix tenants."""
    host = (urlsplit(url).hostname or "").rstrip(".").lower().encode("idna").decode("ascii")
    try:
        return ipaddress.ip_address(host).compressed
    except ValueError:
        extracted = _EXTRACT(host)
        # Unknown suffixes have no PSL registration boundary; retain the complete
        # hostname conservatively rather than merge independent unlisted tenants.
        return extracted.top_domain_under_public_suffix or host.removeprefix("www.")


def site_root(url: str) -> str:
    """Keep the observed storefront origin, without guessing a registrable apex URL."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, "/", "", ""))


class SiteLedger:
    """Keep every citation while selecting each site only once in encounter order."""

    def __init__(self, *, max_sites: int, entrypoint: str):
        self.max_sites = max_sites
        self.entrypoint = entrypoint
        self.sites: dict[str, dict[str, Any]] = {}
        self.results: list[dict[str, Any]] = []
        self.sources: list[tuple[str, str]] = []
        self._source_identities: set[str] = set()

    def add(
        self,
        url: str,
        *,
        kind: str = "url",
        configured: bool = False,
        citation: dict[str, Any] | None = None,
    ) -> None:
        identity = site_identity(url)
        if identity not in self.sites:
            selected = sum(site["selected"] for site in self.sites.values()) < self.max_sites
            root = site_root(url)
            self.sites[identity] = {
                "site": identity,
                "root_url": root,
                "entrypoint_url": url
                if self.entrypoint == "result_url" or kind == "sitemap"
                else root,
                "kind": kind,
                "first_result_url": None,
                "configured_urls": [],
                "entrypoints": [],
                "queries": [],
                "citations": [],
                "selected": selected,
                "selection_reason": "selected" if selected else "max_sites",
            }
        site = self.sites[identity]
        entrypoint = (
            url if self.entrypoint == "result_url" or kind == "sitemap" else site["entrypoint_url"]
        )
        source_identity = canonical_url(entrypoint)
        if site["selected"] and source_identity not in self._source_identities:
            self._source_identities.add(source_identity)
            self.sources.append((entrypoint, kind))
            site["entrypoints"].append(entrypoint)
        if configured and url not in site["configured_urls"]:
            site["configured_urls"].append(url)
        if citation is not None:
            if site["first_result_url"] is None:
                site["first_result_url"] = url
            if citation["query"] not in site["queries"]:
                site["queries"].append(citation["query"])
            attributed = {
                **citation,
                "site": identity,
                "root_url": site["root_url"],
                "entrypoint_url": entrypoint,
                "selected": site["selected"],
            }
            site["citations"].append(attributed)
            self.results.append(attributed)

    def report(self) -> dict[str, Any]:
        sites = list(self.sites.values())
        return {
            "results": self.results,
            "sites": [site for site in sites if site["selected"]],
            "excluded_sites": [site for site in sites if not site["selected"]],
            "resolved_sources": [url for url, _kind in self.sources],
        }

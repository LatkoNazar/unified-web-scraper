"""Bundled PSL identities merge subdomains without collapsing hosted tenants."""

import pytest

from unified_web_parser.discovery.sites import SiteLedger, site_identity, site_root


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://www.store.example.co.uk/product", "example.co.uk"),
        ("https://shop.example.co.uk/", "example.co.uk"),
        ("https://a.github.io/item", "a.github.io"),
        ("https://shop.a.github.io/item", "a.github.io"),
        ("https://b.github.io/item", "b.github.io"),
        ("https://BÜCHER.de./item", "xn--bcher-kva.de"),
        ("https://8.8.8.8/path", "8.8.8.8"),
        ("https://[2606:4700:4700::1111]/", "2606:4700:4700::1111"),
        ("https://first.unlisted-suffix/", "first.unlisted-suffix"),
        ("https://second.unlisted-suffix/", "second.unlisted-suffix"),
    ],
)
def test_offline_site_identity(url, expected, monkeypatch):
    import requests

    monkeypatch.setattr(
        requests.sessions.Session, "request", lambda *_a, **_k: pytest.fail("PSL network fetch")
    )
    assert site_identity(url) == expected


def test_root_preserves_observed_storefront_and_port():
    assert (
        site_root("https://shop.example.co.uk:8443/products/1?q=yes#spec")
        == "https://shop.example.co.uk:8443/"
    )


def test_legacy_result_urls_keep_multiple_deep_links_with_one_site_budget():
    ledger = SiteLedger(max_sites=1, entrypoint="result_url")
    ledger.add("https://shop.example.com/item/one", configured=True)
    ledger.add("https://www.example.com/item/two", configured=True)
    ledger.add("https://unselected.org/item", configured=True)
    report = ledger.report()
    assert report["resolved_sources"] == [
        "https://shop.example.com/item/one",
        "https://www.example.com/item/two",
    ]
    assert len(report["sites"]) == len(report["excluded_sites"]) == 1
    assert report["sites"][0]["entrypoints"] == report["resolved_sources"]

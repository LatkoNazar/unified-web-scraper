"""Captured and generic evidence recovers photos without cross-record borrowing or AI."""

import json
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from unified_web_parser.crawling.pages import make_page
from unified_web_parser.extraction.images import (
    dom_image,
    entity_image,
    image_url,
    recover_record_image,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures/images"
SCHEMA = {"type": "object", "properties": {"title": {"type": "string"}, "url": {"type": "string"}}}


@pytest.mark.parametrize(
    "fixture,url,title,expected",
    [
        (
            "merchant-gallery",
            "https://skay.ua/macbook-air/85493-apple-macbook-air-15-m5-chip-16512gb-2026-sky-blue-mdvq4.html",
            'Apple MacBook Air 15" M5 Chip 16/512GB 2026 Sky Blue (MDVQ4)',
            "https://skay.ua/358440-large_default/apple-macbook-air-15-m5-chip-16512gb-2026-sky-blue-mdvq4.jpg",
        ),
        (
            "merchant-relative-photo",
            "https://istory.com.ua/b-u-macbook-air-13-m4-16-512gb-sky-blue/",
            'Б/У MacBook Air 13" M4',
            "https://istory.com.ua/content/images/29/750x576l80mc0/91898460605162.webp",
        ),
        (
            "canonical-metadata-shell",
            "https://www.olx.ua/d/uk/obyavlenie/macbook-pro-m5-14-16gb-ID11lZAi.html",
            'Macbook Pro M5 14" 16GB',
            "https://ireland.apollo.olxcdn.com:443/v1/files/qbs8o1sdmtt43-UA/image;s=1200x0",
        ),
    ],
)
def test_captured_detail_images_recover_from_saved_evidence(fixture, url, title, expected):
    page = make_page(url, (FIXTURES / f"{fixture}.html").read_text(encoding="utf-8"), 200)
    assert (
        recover_record_image(
            page,
            {"url": url, "title": title},
            schema=SCHEMA,
            content_profile="commerce",
            currency_aliases={"грн.": "UAH"},
        )
        == expected
    )


@pytest.mark.parametrize(
    "name", ["no_thumbnail.abc.svg", "no-photo.png", "no_picture.jpg", "no-image.webp"]
)
def test_source_placeholder_variants_are_unknown_instead_of_photographs(name):
    assert image_url("https://source.example/static/" + name, "https://source.example/") is None


def test_local_jsonld_graph_image_reference_and_cycle_handling():
    url = "https://source.example/record"
    entity = {"@type": "Product", "name": "Actual item", "url": url, "image": {"@id": "#photo"}}
    graph = {
        "@graph": [entity, {"@id": "#photo", "@type": "ImageObject", "contentUrl": "/actual.webp"}]
    }
    page = make_page(
        url, '<script type="application/ld+json">' + json.dumps(graph) + "</script>", 200
    )
    assert (
        recover_record_image(
            page, {"url": url, "title": "Actual item"}, schema=SCHEMA, content_profile="commerce"
        )
        == "https://source.example/actual.webp"
    )
    references = {
        url + "#one": {"@id": "#one", "contentUrl": "#two"},
        url + "#two": {"@id": "#two", "contentUrl": "#one"},
    }
    assert entity_image({"image": {"@id": "#one"}}, url, references=references) is None
    assert entity_image({"image": "http://[bad"}, url) is None
    assert entity_image({"image": {"@id": "#unresolved"}}, url) is None


def test_catalog_placeholder_recovers_only_exact_jsonld_aggregate_offer_image():
    """Reduced saved marketplace behavior: unloaded cards still have JSON-LD photos."""
    url = "https://source.example/catalog"
    offers = [
        {
            "@type": "Offer",
            "name": name,
            "url": f"https://source.example/{key}",
            "image": [f"https://cdn.example/{key}/image"],
            "price": 1200,
            "priceCurrency": "UAH",
        }
        for key, name in (("one", "First item"), ("two", "Second item"))
    ]
    document = {
        "@type": "Product",
        "name": "Catalog",
        "offers": {"@type": "AggregateOffer", "offers": offers},
    }
    content = (
        '<meta property="og:image" content="/catalog-hero.jpg">'
        '<script type="application/ld+json">' + json.dumps(document) + "</script>"
    )
    page = make_page(url, content, 200)
    assert (
        recover_record_image(
            page,
            {"title": "Second item", "url": "https://source.example/two"},
            schema=SCHEMA,
            content_profile="commerce",
        )
        == "https://cdn.example/two/image"
    )
    assert (
        recover_record_image(
            page,
            {"title": "Different item", "url": "https://source.example/two"},
            schema=SCHEMA,
            content_profile="commerce",
        )
        is None
    )


def test_lazy_picture_srcset_preserves_observed_cdn_commas_and_skips_embedded_data():
    root = BeautifulSoup(
        '<picture><source data-srcset="data:image/png;base64,AAAA 1x, '
        'https://cdn.example/c_fill,w_600/photo.webp 2x"><img src="/blank.gif"></picture>',
        "html.parser",
    )
    assert (
        dom_image(root, "https://source.example/") == "https://cdn.example/c_fill,w_600/photo.webp"
    )


def test_catalog_record_only_uses_its_unique_card_photo_and_not_metadata_hero():
    url = "https://source.example/catalog"
    content = '<meta property="og:image" content="/catalog-hero.jpg"><main><h1>Records</h1>'
    content += (
        '<article><h2><a href="/one">First actual record</a></h2>'
        '<img data-src="/one.webp"><p>First record.</p></article>'
    )
    content += (
        '<article><h2><a href="/two">Second actual record</a></h2>'
        '<img src="/no_thumbnail.abc.svg"><p>Second record.</p></article></main>'
    )
    page = make_page(url, content, 200)
    assert (
        recover_record_image(
            page,
            {"url": "https://source.example/one", "title": "First actual record"},
            schema=SCHEMA,
        )
        == "https://source.example/one.webp"
    )
    assert (
        recover_record_image(
            page,
            {"url": "https://source.example/two", "title": "Second actual record"},
            schema=SCHEMA,
        )
        is None
    )
    assert (
        recover_record_image(
            page,
            {"url": "https://source.example/missing", "title": "Missing record"},
            schema=SCHEMA,
        )
        is None
    )


@pytest.mark.parametrize("change", ["wrong-title", "wrong-canonical", "publisher"])
def test_metadata_shell_cannot_donate_hero_to_an_unbound_record(change):
    url = "https://source.example/article"
    content = (
        '<link rel="canonical" href="'
        + url
        + '"><meta property="og:title" content="Actual article: More">'
    )
    content += '<meta property="og:image" content="/own.jpg">'
    data = {"url": url, "title": "Actual article"}
    if change == "wrong-title":
        data["title"] = "Unrelated article"
    elif change == "wrong-canonical":
        content = content.replace('href="' + url + '"', 'href="https://source.example/other"')
    else:
        data.update(title="Publisher")
    assert recover_record_image(make_page(url, content, 200), data, schema=SCHEMA) is None


def test_unique_exact_source_offer_binds_own_metadata_without_dom_price():
    url = "https://source.example/record"
    entity = {
        "@type": "Product",
        "name": "Actual item",
        "url": url,
        "offers": {"@type": "Offer", "price": 1200, "priceCurrency": "UAH"},
    }
    content = (
        '<meta property="og:image" content="/actual.webp">'
        '<script type="application/ld+json">'
        + json.dumps(entity)
        + "</script><main><h1>Actual item</h1></main>"
    )
    page = make_page(url, content, 200)
    assert (
        recover_record_image(
            page, {"title": "Actual item", "url": url}, schema=SCHEMA, content_profile="commerce"
        )
        == "https://source.example/actual.webp"
    )
    assert (
        recover_record_image(
            page, {"title": "Unrelated item", "url": url}, schema=SCHEMA, content_profile="commerce"
        )
        is None
    )

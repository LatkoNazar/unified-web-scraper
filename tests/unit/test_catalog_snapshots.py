"""Captured public catalog fragments preserve the structures missed in live discovery."""

import json
from pathlib import Path

import pytest

from unified_web_parser.core.config import load_job
from unified_web_parser.core.models import ExtractionStrategy
from unified_web_parser.crawling.dom import primary_detail_region
from unified_web_parser.crawling.pages import make_page
from unified_web_parser.extraction.schema import SchemaExtractor
from unified_web_parser.navigation.inventory import InventoryBuilder
from unified_web_parser.profiles.commerce import read_price
from unified_web_parser.routing.candidates import CandidateBuilder, is_candidate_entity

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures/catalogs"


@pytest.mark.parametrize("name", ["merchant-unpriced", "comparison-variants", "merchant-priced"])
def test_captured_catalogs_expose_actual_individual_links_without_terminal_category_record(name):
    html = (FIXTURES / f"{name}.html").read_text(encoding="utf-8")
    url = html.split("Captured public HTML fragments: ", 1)[1].split(";", 1)[0]
    inventory = InventoryBuilder(allow_scroll=False).build(make_page(url, html, 200))
    assert not inventory.has_entity
    assert inventory.has_results
    items = [action for action in inventory.actions if action.hint == "item"]
    assert len(items) >= (2 if name == "comparison-variants" else 3)
    assert all(action.url != url for action in items)
    if name == "merchant-unpriced":
        assert all("/catalog/item/" in action.url for action in items)
        assert all("comm=" not in action.url for action in items)
    if name == "comparison-variants":
        assert all("/list/" not in action.url for action in items)
        assert all("M4" in action.label and "ГБ" in action.label for action in items)
    if name == "merchant-priced":
        assert all("MacBook" in action.label for action in items)


def test_commerce_catalog_candidates_exclude_aggregate_product_and_supplemental_media():
    html = (FIXTURES / "comparison-variants.html").read_text(encoding="utf-8")
    schema = load_job("configs/jobs/macbooks-web.yaml").extraction_schema
    blocks = CandidateBuilder(
        schema=schema, content_profile="commerce", currency_aliases={"грн": "UAH", "USD": "USD"}
    ).build(make_page("https://catalog.example/family", html, 200))
    assert blocks
    assert all(block.kind.value != "json_ld" for block in blocks)
    assert all(block.locator.startswith("card:") for block in blocks)


@pytest.mark.asyncio
async def test_captured_merchant_offers_keep_real_prices_and_exclude_recommendations():
    html = (FIXTURES / "comparison-offers.html").read_text(encoding="utf-8")
    page = make_page("https://ek.ua/ua/APPLE-MW1J3.htm", html, 200)
    schema = {
        "type": "object",
        "required": ["title", "price", "currency"],
        "properties": {
            "title": {"type": "string"},
            "price": {"type": "number"},
            "currency": {"type": "string"},
        },
    }
    blocks = CandidateBuilder(
        schema=schema, content_profile="commerce", currency_aliases={"грн": "UAH", "USD": "USD"}
    ).build(page)
    assert len(blocks) == 3
    extractor = SchemaExtractor(
        "merchant-offers", content_profile="commerce", currency_aliases={"грн": "UAH"}
    )
    records = [
        record
        for block in blocks
        for record in await extractor.extract(page, block, ExtractionStrategy.DOM, schema)
    ]
    assert {record.data["price"] for record in records} == {69949, 66999, 70039}
    assert all(record.data["currency"] == "UAH" for record in records)
    assert all("MW1J3" in record.data["title"] for record in records)


def test_empty_commerce_category_does_not_become_a_record_during_replay():
    page = make_page(
        "https://shop.example/family",
        "<main><h1>Product family</h1></main>"
        '<script type="application/ld+json">'
        '{"@type":"Product","name":"Product family", "Offers":[],'
        '"url":"https://shop.example/family"}</script>',
        200,
    )
    schema = {
        "required": ["price", "currency"],
        "properties": {
            "price": {"type": ["number", "null"]},
            "currency": {"type": ["string", "null"]},
        },
    }
    assert (
        CandidateBuilder(
            schema=schema, content_profile="commerce", currency_aliases={"грн": "UAH", "USD": "USD"}
        ).build(page)
        == []
    )


def test_explicit_own_offer_outranks_modal_heading_and_related_cards():
    url = "https://market.example/detail?id=42"
    product = {
        "@type": "Product",
        "name": "Own device",
        "url": url,
        "description": "Only the current seller's complete description.",
        "offers": {"@type": "Offer", "price": 75000, "priceCurrency": "UAH"},
    }
    html = (
        "<aside><h1>Consent preferences</h1></aside><main>"
        + "".join(
            f'<article><h2><a href="/products/{number}">Recommended device {number}</a></h2>'
            "<p>500 USD</p></article>"
            for number in range(3)
        )
        + '</main><script type="application/ld+json">'
        + json.dumps(product)
        + "</script>"
    )
    page = make_page(url, html, 200)
    inventory = InventoryBuilder(allow_scroll=False, content_profile="commerce").build(page)
    assert inventory.has_entity and not inventory.has_results
    assert not any(action.hint == "item" for action in inventory.actions)
    candidates = CandidateBuilder(content_profile="commerce").build(page)
    assert len(candidates) == 1
    assert json.loads(candidates[0].content) == product


def test_generic_optional_nullable_price_schema_keeps_video_content():
    schema = load_job("configs/jobs/web.yaml").extraction_schema
    video = {"@type": "https://schema.org/VideoObject", "name": "An interview"}
    assert is_candidate_entity(video, schema)
    page = make_page(
        "https://media.example/interview",
        '<script type="application/ld+json">' + json.dumps(video) + "</script>",
        200,
    )
    assert any(
        block.kind.value == "json_ld" for block in CandidateBuilder(schema=schema).build(page)
    )


@pytest.mark.parametrize("text", ["від 40500 до 56000 грн", "10 GBP — 200 GBP", "$10 - $20"])
def test_price_ranges_remain_unknown_instead_of_becoming_one_offer_price(text):
    assert read_price(text, currency_aliases={"грн": "UAH", "GBP": "GBP"}) is None


def test_hardware_number_range_does_not_hide_an_actual_price():
    assert read_price("SSD 256-512 GB; price 1000 USD", currency_aliases={"USD": "USD"}) == (
        "1000",
        "USD",
    )


@pytest.mark.parametrize("label", ["Купить", "Заказать", "Оплатить", "Замовити", "Сплатити"])
def test_transaction_buttons_are_never_exposed_as_expand_actions(label):
    inventory = InventoryBuilder(allow_scroll=False).build(
        make_page("https://shop.example/", f"<button>{label}</button>", 200)
    )
    assert inventory.actions == ()


def test_primary_own_price_survives_heading_free_recommendations():
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(
        "<main><h1>Own product</h1><p>500 USD</p><section>"
        '<div class="tile"><a href="/one"><img src="one.jpg">Other one</a><p>20 USD</p></div>'
        '<div class="tile"><a href="/two"><img src="two.jpg">Other two</a><p>30 USD</p></div>'
        "</section></main>",
        "html.parser",
    )
    primary = primary_detail_region(
        soup, content_profile="commerce", currency_aliases={"USD": "USD"}
    )
    assert primary is not None
    assert "500 USD" in primary.get_text(" ", strip=True)
    assert "Other one" not in primary.get_text(" ", strip=True)

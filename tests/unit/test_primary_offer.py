"""Own offer metadata outranks discount text without borrowing related prices."""

import json

import pytest

from unified_web_parser.core.models import ExtractionStrategy
from unified_web_parser.crawling.pages import make_page
from unified_web_parser.extraction.projection import dom_entity
from unified_web_parser.extraction.schema import SchemaExtractor
from unified_web_parser.navigation.inventory import InventoryBuilder
from unified_web_parser.navigation.planner import NavigationPlanner
from unified_web_parser.routing.candidates import CandidateBuilder

SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "price": {"type": "number"},
        "currency": {"type": "string"},
    },
    "required": ["title", "price", "currency"],
}


@pytest.mark.parametrize("heading_property", ["", ' itemprop="name"'])
def test_primary_heading_survives_breadcrumb_and_conflicting_metadata_names(heading_property):
    entity = dom_entity(
        '<div itemscope itemtype="https://schema.org/Product">'
        '<ol><li><span itemprop="name">Home</span></li></ol>'
        f"<h1{heading_property}>Actual primary device</h1>"
        '<meta itemprop="name" content="Alternate metadata name">'
        '<div itemprop="offers" itemscope itemtype="https://schema.org/Offer">'
        '<meta itemprop="price" content="750"><meta itemprop="priceCurrency" content="GBP">'
        "</div></div>",
        "https://merchant.example/opaque/model",
        content_profile="commerce",
        currency_aliases={"GBP": "GBP"},
    )
    assert entity["name"] == "Actual primary device"
    assert entity["offers"]["price"] == "750"
    assert entity["offers"]["priceCurrency"] == "GBP"


def test_microdata_name_remains_fallback_without_a_heading():
    entity = dom_entity(
        '<meta itemprop="name" content="Observed metadata name">',
        "https://merchant.example/opaque/model",
    )
    assert entity["name"] == "Observed metadata name"


@pytest.mark.parametrize("card", [False, True])
def test_bound_page_or_card_url_survives_incidental_image_object_url(card):
    entity = dom_entity(
        '<div itemscope itemtype="https://schema.org/ImageObject">'
        '<meta itemprop="url" content="https://merchant.example/image.jpg"></div>'
        '<div itemscope itemtype="https://schema.org/Product">'
        '<h2><a href="/offers/actual">Actual device</a></h2>'
        '<meta itemprop="name" content="Actual device"></div>',
        "https://merchant.example/catalog",
        card=card,
    )
    assert entity["url"] == (
        "https://merchant.example/offers/actual" if card else "https://merchant.example/catalog"
    )
    assert entity["name"] == "Actual device"


def test_microdata_url_remains_fallback_without_a_bound_page_or_card_url():
    entity = dom_entity('<meta itemprop="url" content="https://merchant.example/actual">', "")
    assert entity["url"] == "https://merchant.example/actual"


@pytest.mark.asyncio
async def test_discount_text_does_not_turn_primary_offer_into_recommendation_catalog():
    page = make_page(
        "https://merchant.example/opaque/model",
        '<main><div itemscope itemtype="https://schema.org/Product">'
        '<h1 itemprop="name">Actual primary device</h1>'
        "<del>800 GBP</del><p>750 GBP -50 GBP</p>"
        '<div itemprop="offers" itemscope itemtype="https://schema.org/Offer">'
        '<meta itemprop="price" content="750"><meta itemprop="priceCurrency" content="GBP">'
        '<button>Buy</button></div><a href="/stores">Stores</a>'
        '<p itemprop="description">Description of the actual primary device.</p>'
        "<section><h2>Recommended devices</h2>"
        '<article><h3><a href="/related/a">Unrelated device A</a></h3><p>900 GBP</p></article>'
        '<article><h3><a href="/related/b">Unrelated device B</a></h3><p>950 GBP</p></article>'
        "</section></div></main>",
        200,
    )
    options = {"content_profile": "commerce", "currency_aliases": {"GBP": "GBP"}}
    inventory = InventoryBuilder(schema=SCHEMA, allow_scroll=False, **options).build(page)
    assert inventory.has_entity and not inventory.has_results
    assert not any(action.hint == "item" for action in inventory.actions)
    plan = await NavigationPlanner(mode="rules").plan(inventory, "device", SCHEMA)
    assert plan.extract and plan.terminal and not plan.action_ids
    candidates = CandidateBuilder(schema=SCHEMA, **options).build(page)
    assert len(candidates) == 1 and candidates[0].locator == "entity"
    assert "Actual primary device" in candidates[0].content
    assert "Unrelated device" not in candidates[0].content
    records = await SchemaExtractor("primary-offer", **options).extract(
        page, candidates[0], ExtractionStrategy.DOM, SCHEMA
    )
    assert records[0].data == {"title": "Actual primary device", "price": 750, "currency": "GBP"}


def test_category_range_does_not_borrow_metadata_from_related_item_cards():
    page = make_page(
        "https://merchant.example/catalog",
        "<main><h1>Available devices</h1><p>300 GBP - 900 GBP</p>"
        '<article><h2><a href="/one">First device</a></h2>'
        '<meta itemprop="price" content="300"><p>300 GBP</p></article>'
        '<article><h2><a href="/two">Second device</a></h2>'
        '<meta itemprop="price" content="900"><p>900 GBP</p></article></main>',
        200,
    )
    inventory = InventoryBuilder(
        schema=SCHEMA, content_profile="commerce", currency_aliases={"GBP": "GBP"}
    ).build(page)
    assert inventory.has_results and not inventory.has_entity
    assert len([action for action in inventory.actions if action.hint == "item"]) == 2


@pytest.mark.asyncio
async def test_explicit_article_target_does_not_follow_unrelated_product_itemlist():
    schema = {
        "type": "object",
        "x-entity-types": ["Article"],
        "properties": {"title": {"type": "string"}},
    }
    document = [
        {"@type": "Article", "headline": "Primary entity", "url": "https://publisher.example/one"},
        {
            "@type": "ItemList",
            "itemListElement": [
                {"@type": "Product", "name": "Other product A", "url": "/a"},
                {"@type": "Product", "name": "Other product B", "url": "/b"},
            ],
        },
    ]
    page = make_page(
        "https://publisher.example/one",
        '<script type="application/ld+json">' + json.dumps(document) + "</script>"
        "<main><article><h1>Primary entity</h1>"
        "<p>Own article prose for this primary entity.</p></article>"
        '<h2>Recommended</h2><article><h3><a href="/a">Other product A</a></h3></article>'
        '<article><h3><a href="/b">Other product B</a></h3></article></main>',
        200,
    )
    inventory = InventoryBuilder(schema=schema).build(page)
    assert inventory.has_entity and not inventory.has_results
    plan = await NavigationPlanner(mode="rules").plan(inventory, "article", schema)
    assert plan.extract and plan.terminal and not plan.action_ids
    candidates = CandidateBuilder(schema=schema).build(page)
    assert candidates and all("Other product" not in block.content for block in candidates)

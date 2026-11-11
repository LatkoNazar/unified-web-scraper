from urllib.parse import parse_qs, urlsplit

import pytest
from bs4 import BeautifulSoup

from unified_web_parser.core.exceptions import RoutingError
from unified_web_parser.crawling.pages import make_page
from unified_web_parser.navigation import InventoryBuilder, NavigationPlan, NavigationPlanner
from unified_web_parser.navigation.models import is_filter_action
from unified_web_parser.navigation.urls import resolve_action_url

FORM = """<html><body><header>
<select name="locale" aria-label="Interface language"><option selected value="uk">Ukrainian</option>
<option value="en">English</option></select></header>
<form action="/lookup?opaque=AB%2fC+Q&amp;condition=used&amp;route=store">
<input type="hidden" name="mode" value="catalog"><input type="hidden" name="category" value="all">
<input type="search" name="q">
<select name="condition"><option selected value="used">Used</option>
<option value="new">New</option></select>
<input name="min_price" type="number" value="1000">
<input name="seller_rating" value="five stars">
<label for="accept">Accept the privacy terms</label>
<input id="accept" name="accepted" type="checkbox" checked>
<button type="submit">Search</button></form>
<form action="/newsletter"><input name="email" type="email">
<input type="checkbox" name="subscribe" checked></form>
<aside class="filters"><button>Apply filters</button><a href="?sort=price">Sort by price</a></aside>
<a href="/category/books">Books</a><a rel="next" href="?page=2">Next</a>
<select name="page"><option selected value="1">Page 1</option>
<option value="2">Page 2</option></select>
</body></html>"""


def build(html=FORM, *, allow_filters=False, url="https://catalog.example/"):
    page = make_page(url, html, 200)
    return page, InventoryBuilder(allow_filters=allow_filters).build(page)


def search(inventory):
    return next(action for action in inventory.actions if action.kind == "search")


def test_default_clean_get_form_drops_only_observed_optional_fields_and_preserves_routing():
    page, inventory = build()
    action = search(inventory)
    target = resolve_action_url(action, "used guitar")
    assert "opaque=AB%2fC+Q" in target  # Unknown signed/opaque routing bytes are unchanged.
    assert parse_qs(urlsplit(target).query) == {
        "opaque": ["AB/C Q"],
        "route": ["store"],
        "mode": ["catalog"],
        "category": ["all"],
        "accepted": ["on"],
        "q": ["used guitar"],
    }
    soup = BeautifulSoup(page.html, "html.parser")
    cleaned = [soup.select_one(selector).get("name") for selector in action.filter_selectors]
    assert set(cleaned) == {"condition", "min_price", "seller_rating"}
    assert not any(is_filter_action(action) for action in inventory.actions)
    assert any(action.hint == "category" for action in inventory.actions)
    assert any(
        action.kind == "select" and action.hint == "pagination" for action in inventory.actions
    )
    assert any(
        action.kind == "open" and action.hint == "pagination" for action in inventory.actions
    )


def test_explicit_filter_opt_in_keeps_defaults_and_capabilities():
    _, inventory = build(allow_filters=True)
    action = search(inventory)
    assert not action.filter_selectors and not action.filter_query_names
    query = parse_qs(urlsplit(resolve_action_url(action, "guitar")).query)
    assert query["condition"] == ["used"]
    assert query["min_price"] == ["1000"] and query["seller_rating"] == ["five stars"]
    assert any(is_filter_action(action) for action in inventory.actions)
    filter_action = next(action for action in inventory.actions if is_filter_action(action))
    plan = NavigationPlan(page_id=inventory.page_id, action_ids=(filter_action.action_id,))
    with pytest.raises(RoutingError, match="filters are disabled"):
        plan.validate_against(inventory)
    plan.validate_against(inventory, allow_filters=True)


async def test_rules_prefer_clean_root_search_over_a_single_featured_entity():
    _, inventory = build(
        FORM.replace(
            "</body>",
            "<article><h1>Featured guitar</h1>"
            "<p>A detailed instrument description selected for the homepage.</p></article></body>",
        )
    )
    assert inventory.has_entity
    plan = await NavigationPlanner(mode="rules").plan(inventory, "used guitar", {})
    assert plan.action_ids == (search(inventory).action_id,) and not plan.extract


async def test_ai_does_not_advertise_filters_even_with_injected_opted_in_inventory():
    _, dirty = build(allow_filters=True)
    _, inventory = build()
    inventory = inventory.model_copy(
        update={
            "actions": inventory.actions
            + tuple(action for action in dirty.actions if is_filter_action(action))
        }
    )

    class Client:
        async def complete_json(self, _system, state, _schema, **_kwargs):
            assert all(action["hint"] != "filter" for action in state["actions"].values())
            assert all(
                action["kind"] != "select" or action["hint"] == "pagination"
                for action in state["actions"].values()
            )
            key = next(
                key for key, action in state["actions"].items() if action["kind"] == "search"
            )
            return {"actions": [key], "extract": False}

    plan = await NavigationPlanner(Client(), mode="ai").plan(inventory, "guitar", {})
    assert plan.action_ids == (search(inventory).action_id,)


def test_search_template_removes_structurally_identified_filter_keys_not_unknown_parameters():
    html = """<script type="application/ld+json">{"@type":"WebSite","potentialAction":{
    "@type":"SearchAction","target":"https://catalog.example/find?q={term}&sort=price&route=AB%2fC",
    "query-input":"required name=term"}}</script>
    <select name="sort"><option selected value="price">Price</option></select>"""
    _, inventory = build(html)
    assert resolve_action_url(search(inventory), "a & b") == (
        "https://catalog.example/find?q=a%20%26%20b&route=AB%2fC"
    )


def test_security_hidden_form_forces_browser_without_exposing_or_removing_token():
    page, inventory = build("""<form method="get" action="/find">
    <input name="q" type="search"><input name="csrf_token" type="hidden" value="secret-token">
    <input name="location" type="hidden" value="city"><button>Search</button></form>""")
    action = search(inventory)
    assert resolve_action_url(action, "book") is None
    assert "secret-token" not in str(action.model_dump())
    soup = BeautifulSoup(page.html, "html.parser")
    assert [soup.select_one(selector)["name"] for selector in action.filter_selectors] == [
        "location"
    ]


def test_product_link_with_price_or_location_words_is_not_a_filter():
    _, inventory = build('<a href="/products/location-guide">Location guide, price 50 USD</a>')
    assert any(action.hint == "item" for action in inventory.actions)


def test_resort_category_and_item_are_not_sort_filters():
    _, inventory = build("""<div class="resort-card">
    <a href="/products/resort-guide">Resort guide, price 50 USD</a>
    </div><a href="/category/resorts">Resort stays</a>""")
    assert len([action for action in inventory.actions if action.kind == "open"]) == 2

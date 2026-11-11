"""Independent checks that clean navigation retains ordinary browsing paths."""

from urllib.parse import parse_qs, urlsplit

import pytest

from unified_web_parser.crawling.pages import make_page
from unified_web_parser.navigation import InventoryBuilder, resolve_action_url


@pytest.mark.parametrize(
    "html",
    [
        '<a href="/travel/resorts">Resort stays</a>',
        '<div class="resort-card"><a href="/travel/coast">Coastal stays</a></div>',
        "<button>Explore resorts</button>",
    ],
)
def test_filter_words_do_not_remove_unrelated_browsing_controls(html):
    inventory = InventoryBuilder(allow_scroll=False).build(
        make_page("https://travel.example/", html, 200)
    )
    assert len(inventory.actions) == 1
    assert inventory.actions[0].hint not in {"filter", "sort"}


@pytest.mark.parametrize("title", ["Coffee filters", "Sort algorithms handbook"])
def test_product_titles_with_filter_vocabulary_remain_observed_items(title):
    inventory = InventoryBuilder(allow_scroll=False).build(
        make_page(
            "https://catalog.example/",
            f'<article><a href="/products/reference">{title}</a><p>Price 50 USD</p></article>',
            200,
        )
    )
    assert len(inventory.actions) == 1
    assert inventory.actions[0].hint == "item"


def test_clean_search_preserves_pagination_and_operational_form_values():
    page = make_page(
        "https://catalog.example/",
        """<form action="/lookup?price=400&amp;opaque=X%2fy+Z">
        <input type="search" name="q">
        <input type="hidden" name="price" value="400">
        <input type="hidden" name="route" value="products">
        <select name="language"><option selected value="uk">Ukrainian</option></select>
        <select name="page"><option selected value="2">2</option></select>
        <input type="checkbox" name="agree_terms" checked>
        <select name="condition"><option selected value="used">Used</option></select>
        <button>Search</button></form>""",
        200,
    )
    action = next(
        action for action in InventoryBuilder().build(page).actions if action.kind == "search"
    )
    target = resolve_action_url(action, "guitar")
    assert "opaque=X%2fy+Z" in target
    assert parse_qs(urlsplit(target).query) == {
        "opaque": ["X/y Z"],
        "route": ["products"],
        "language": ["uk"],
        "page": ["2"],
        "agree_terms": ["on"],
        "q": ["guitar"],
    }

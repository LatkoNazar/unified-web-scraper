import pytest

from unified_web_parser.crawling.pages import make_page
from unified_web_parser.discovery.sitemap import extract_sitemap_urls
from unified_web_parser.navigation import InventoryBuilder, resolve_action_url
from unified_web_parser.sources.navigation import canonical_url


def test_observed_generic_search_form_preserves_hidden_fields():
    page = make_page(
        "https://example.org/catalog",
        """
        <form action="/find"><input type="hidden" name="category" value="hardware">
        <input type="search" name="query"></form>
    """,
        200,
    )
    inventory = InventoryBuilder().build(page, query="Air M4")
    search = next(action for action in inventory.actions if action.kind == "search")
    assert resolve_action_url(search, "Air M4") == (
        "https://example.org/find?category=hardware&query=Air+M4"
    )


def test_item_and_pagination_actions_come_from_observed_links():
    page = make_page(
        "https://example.org/catalog",
        """
        <a href="https://external.org/products/trap">Other site</a>
        <a href="/products/one?utm_source=test#details">First</a>
        <a href="/products/two">Second</a>
        <a rel="next" href="?page=2">Next</a>
    """,
        200,
    )
    inventory = InventoryBuilder().build(page)
    items = [canonical_url(action.url) for action in inventory.actions if action.hint == "item"]
    assert items == ["https://example.org/products/one", "https://example.org/products/two"]
    pagination = next(action for action in inventory.actions if action.hint == "pagination")
    assert resolve_action_url(pagination) == "https://example.org/catalog?page=2"


def test_bounded_inventory_keeps_continuation_and_reports_omitted_links():
    html = "".join(f'<a href="/products/{number}">Item {number}</a>' for number in range(8))
    html += '<a rel="next" href="?page=2">Next</a>'
    inventory = InventoryBuilder(max_actions=3, allow_scroll=False).build(
        make_page("https://example.org/catalog", html, 200)
    )
    assert len(inventory.actions) == 3
    assert any(action.hint == "pagination" for action in inventory.actions)
    assert inventory.observed_action_count == 9 and inventory.omitted_action_count == 6


def test_semantic_numbered_pagination_and_localized_next_page_are_observed():
    inventory = InventoryBuilder(allow_scroll=False).build(
        make_page(
            "https://example.org/catalog",
            '<nav aria-label="Pagination"><a href="?page=2">2</a>'
            '<a href="?page=3">Page 3</a></nav>'
            '<a href="?page=4">Наступна сторінка</a>'
            '<a href="?page=5">Вперед</a>'
            '<div id="page"><a href="/products/42">42</a></div>',
            200,
        )
    )
    assert {action.url for action in inventory.actions if action.hint == "pagination"} == {
        "https://example.org/catalog?page=2",
        "https://example.org/catalog?page=3",
        "https://example.org/catalog?page=4",
        "https://example.org/catalog?page=5",
    }
    assert next(action for action in inventory.actions if action.label == "42").hint == "item"


@pytest.mark.parametrize("attribute", ["data-testid", "data-cy", "data-nx-name"])
def test_component_metadata_can_identify_number_only_pagination(attribute):
    inventory = InventoryBuilder(allow_scroll=False).build(
        make_page(
            "https://example.org/catalog?page=6",
            f'<nav aria-label="Оголошення" {attribute}="NexusPagination">'
            '<a href="?page=7">7</a><a href="?page=8">8</a></nav>',
            200,
        )
    )
    assert {action.hint for action in inventory.actions} == {"pagination"}
    assert {action.url for action in inventory.actions} == {
        "https://example.org/catalog?page=7",
        "https://example.org/catalog?page=8",
    }


def test_observed_link_outer_ascii_whitespace_is_trimmed_but_encoded_spaces_remain():
    inventory = InventoryBuilder(allow_scroll=False).build(
        make_page(
            "https://example.org/catalog",
            '<a href=" /products/a%20b.html \t">Product</a>',
            200,
        )
    )
    action = inventory.actions[0]
    assert action.url == "https://example.org/products/a%20b.html"
    assert resolve_action_url(action) == action.url


@pytest.mark.parametrize(
    "overlay", ['role="dialog"', 'role="alertdialog"', "hidden", 'aria-hidden="true"']
)
def test_late_modal_heading_does_not_hide_headingless_result_cards(overlay):
    # Reduced from a real second results page: its only h1 belongs to a late
    # consent overlay, while the results themselves have no page heading.
    inventory = InventoryBuilder(
        content_profile="commerce", currency_aliases={"грн.": "UAH"}, allow_scroll=False
    ).build(
        make_page(
            "https://example.org/results?page=2",
            '<div><h2><a href="/offer/a">Portable computer A</a></h2><p>45000 грн.</p></div>'
            '<div><h2><a href="/offer/b">Portable computer B</a></h2><p>46000 грн.</p></div>'
            f"<section {overlay}><h1>Your privacy choices</h1></section>",
            200,
        )
    )
    assert inventory.has_results and not inventory.has_entity
    assert {action.url for action in inventory.actions if action.hint == "item"} == {
        "https://example.org/offer/a",
        "https://example.org/offer/b",
    }


def test_sitemap_bounds_and_index_and_no_image_urls():
    xml = """<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
        xmlns:image="http://www.google.com/schemas/sitemap-image/1.1">
        <url><loc>/products/one</loc><image:image><image:loc>https://cdn.org/one.jpg</image:loc></image:image></url>
        <url><loc>/products/two</loc></url></urlset>"""
    assert extract_sitemap_urls(xml, base_url="https://example.org/sitemap.xml", max_urls=1) == [
        "https://example.org/products/one"
    ]
    index = "<sitemapindex><sitemap><loc>/child.xml</loc></sitemap></sitemapindex>"
    assert extract_sitemap_urls(index, base_url="https://example.org/sitemap.xml") == [
        "https://example.org/child.xml"
    ]


def test_sitemap_rejects_doctype():
    with pytest.raises(ValueError, match="entities"):
        extract_sitemap_urls("<!DOCTYPE urlset><urlset/>", base_url="https://example.org")

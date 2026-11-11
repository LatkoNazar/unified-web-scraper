"""Known detail entries must not consume bounded discovery/extraction inventories."""

import json

import pytest

from unified_web_parser.core.exceptions import DurabilityError
from unified_web_parser.crawling.pages import make_page
from unified_web_parser.navigation.inventory import InventoryBuilder
from unified_web_parser.navigation.planner import NavigationPlanner
from unified_web_parser.routing.candidates import CandidateBuilder

BASE = "https://source.example/catalog"
KNOWN = {"https://source.example/a", "https://source.example/b"}


def cards():
    return (
        "<main><h1>Catalog</h1>"
        + "".join(
            f'<article><h2><a href="/{key}">Actual {key}</a></h2>'
            f"<p>Descriptive record evidence for {key}, price 100 USD.</p></article>"
            for key in "abc"
        )
        + '</main><a rel="next" href="?page=2">Next</a>'
    )


def test_known_items_do_not_starve_new_item_or_pagination_action_slots():
    page = make_page(BASE, cards(), 200)
    seen = []

    def skip(url):
        seen.append(url)
        return url in KNOWN

    inventory = InventoryBuilder(max_actions=2, allow_scroll=False, skip_url=skip).build(page)
    assert [(action.hint, action.url) for action in inventory.actions] == [
        ("item", "https://source.example/c"),
        ("pagination", BASE + "?page=2"),
    ]
    assert BASE + "?page=2" not in seen
    assert inventory.omitted_action_count == 0
    assert inventory.has_results


def test_known_jsonld_entities_do_not_consume_candidate_or_character_budget():
    entities = [
        {"@type": "Product", "name": "Actual " + key, "url": "https://source.example/" + key}
        for key in "abc"
    ]
    page = make_page(
        BASE,
        '<script type="application/ld+json">'
        + json.dumps({"@type": "ItemList", "itemListElement": entities})
        + "</script>",
        200,
    )
    result = CandidateBuilder(max_candidates=1, max_chars=100, skip_url=KNOWN.__contains__).build(
        page
    )
    assert len(result) == 1
    assert json.loads(result[0].content)["url"] == "https://source.example/c"


def test_known_dom_cards_are_filtered_before_candidate_budget_and_main_fallback():
    page = make_page(BASE, cards(), 200)
    result = CandidateBuilder(max_candidates=1, skip_url=KNOWN.__contains__).build(page)
    assert len(result) == 1 and "/c" in result[0].content
    assert 'href="/a"' not in result[0].content and 'href="/b"' not in result[0].content
    assert CandidateBuilder(skip_url=lambda url: True).build(page) == []


@pytest.mark.parametrize("builder", [InventoryBuilder, CandidateBuilder])
def test_completion_lookup_durability_failures_escape_builder(builder):
    def broken(_):
        raise DurabilityError("ledger unavailable")

    with pytest.raises(DurabilityError):
        builder(skip_url=broken).build(make_page(BASE, cards(), 200))


@pytest.mark.parametrize("continuation", ["pages", "scroll"])
async def test_all_known_items_do_not_hide_later_result_pages_or_lazy_items(continuation):
    html = cards().split("</main>")[0] + "</main>"
    if continuation == "pages":
        html += '<nav aria-label="Pagination"><a href="?page=2">2</a><a href="?page=3">3</a></nav>'
    inventory = InventoryBuilder(
        skip_url=lambda url: True, allow_scroll=continuation == "scroll"
    ).build(make_page(BASE, html, 200))
    plan = await NavigationPlanner(mode="rules").plan(inventory, None, {})
    assert not plan.terminal
    assert len(plan.action_ids) == (2 if continuation == "pages" else 1)
    assert plan.extract

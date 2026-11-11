import asyncio
import json
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from bs4 import BeautifulSoup

from unified_web_parser.core.exceptions import RoutingError
from unified_web_parser.crawling.pages import make_page
from unified_web_parser.navigation import (
    InventoryBuilder,
    NavigationPlan,
    NavigationPlanner,
    resolve_action_url,
)
from unified_web_parser.providers.decisions import OpenRouterDecisionsClient


def inventory(html, *, url="https://directory.example/", **options):
    page = make_page(url, html, 200)
    return page, InventoryBuilder(**options).build(page, query="Львів")


def test_get_search_and_hidden_fields_are_observed_not_guessed():
    _, observed = inventory(
        '<form action="/lookup?existing=yes"><input type="hidden" name="kind" '
        'value="flat"><input type="search" name="needle"></form>'
    )
    action = next(action for action in observed.actions if action.kind == "search")
    target = resolve_action_url(action, "Львів & /?+#")
    assert urlsplit(target).path == "/lookup"
    assert parse_qs(urlsplit(target).query) == {
        "existing": ["yes"],
        "kind": ["flat"],
        "needle": ["Львів & /?+#"],
    }


@pytest.mark.parametrize(
    "target",
    ["/lookup?needle={term}", {"@type": "EntryPoint", "urlTemplate": "/lookup?needle={term}"}],
)
def test_schema_org_search_action_uses_observed_template_and_query_input(target):
    document = {
        "@context": "https://schema.org",
        "@type": "WebSite",
        "potentialAction": {
            "@type": "SearchAction",
            "target": target,
            "query-input": "required name=term",
        },
    }
    _, observed = inventory(
        '<script type="application/ld+json">' + json.dumps(document) + "</script>"
    )
    action = next(action for action in observed.actions if action.kind == "search")
    assert parse_qs(urlsplit(resolve_action_url(action, "MacBook Air 15")).query) == {
        "needle": ["MacBook Air 15"]
    }
    assert action.url_template.endswith("needle={term}")


def test_formless_search_binds_adjacent_observed_search_button():
    html = (
        '<main><label>Search apartments</label><input type="search" id="needle">'
        '<button id="search" onclick="search()">Search</button></main>'
    )
    _, observed = inventory(html)
    action = next(action for action in observed.actions if action.kind == "search")
    assert action.submit_selector is not None
    assert BeautifulSoup(html, "html.parser").select_one(action.submit_selector)["id"] == "search"
    assert not any(
        action.kind == "click" and action.hint == "search" for action in observed.actions
    )
    assert resolve_action_url(action, "Львів") is None


def test_inflected_search_combobox_without_name_binds_query_and_excludes_location():
    _, observed = inventory(
        '<main><input type="text" id="search" placeholder="Що шукаєте?" '
        'role="combobox" aria-label="Що шукаєте?">'
        '<input type="text" id="location-input" placeholder="Уся Україна" '
        'role="combobox" aria-label="Уся Україна"><button>Знайти</button></main>'
    )
    searches = [action for action in observed.actions if action.kind == "search"]
    assert len(searches) == 1
    assert searches[0].submit_selector is not None
    assert searches[0].query_name is None
    assert not any(action.kind == "click" for action in observed.actions)


def test_associated_public_search_label_supports_arbitrary_input_name():
    _, observed = inventory(
        '<form action="/lookup"><label for="needle">Find apartments</label>'
        '<input type="text" id="needle" name="needle"></form>'
    )
    search = next(action for action in observed.actions if action.kind == "search")
    assert search.query_name == "needle"


def test_hidden_controls_auth_forms_transaction_buttons_and_offsite_actions_are_not_candidates():
    _, observed = inventory(
        '<div hidden><input type="search"></div>'
        '<form action="https://attacker.example/search"><input type="search" '
        'name="q"></form><form><input type="password"><input name="q"></form>'
        "<button>Buy now</button><button>Delete item</button>"
        '<a href="https://attacker.example/product">External product</a>'
    )
    assert not observed.actions


def test_transaction_form_is_not_search_and_sensitive_hidden_values_are_not_in_inventory():
    _, observed = inventory(
        '<form action="/checkout"><input name="q"><button>Buy now</button></form>'
        '<form action="/lookup"><input type="search" name="q">'
        '<input type="hidden" name="csrf_token" value="private-token">'
        '<input type="hidden" name="kind" value="flat"></form>'
    )
    searches = [action for action in observed.actions if action.kind == "search"]
    assert len(searches) == 1
    assert searches[0].form_values == (("kind", "flat"),)
    assert "private-token" not in observed.model_dump_json()


def test_explicit_destructive_transaction_and_auth_bindings_cannot_hide_behind_neutral_labels():
    _, observed = inventory(
        '<a href="/account/delete">Details</a><a href="/login">Continue</a>'
        '<button name="add_to_cart">+</button><button onclick="addToCart(42)">+</button>'
        '<form action="/checkout"><input name="q"><button>Continue</button></form>'
        '<a href="/category/books">Books</a><button id="load-more">Load more</button>'
    )
    assert {action.label for action in observed.actions} == {"Books", "Load more"}


@pytest.mark.parametrize(
    "malformed", ["https://directory.example:notaport/x", "https://[invalid-address/x"]
)
def test_malformed_observed_url_is_rejected_without_losing_other_actions(malformed):
    _, observed = inventory(f'<a href="{malformed}">Invalid</a><a href="/category/flats">Flats</a>')
    assert [action.label for action in observed.actions] == ["Flats"]


def test_generic_div_cards_with_h6_prices_form_one_result_group_without_seller_links():
    _, observed = inventory(
        "".join(
            f'<div class="listing"><div><a href="/opaque/{number}"><h6>Flat {number}'
            f'</h6></a></div><p>{number} 000 грн</p><a href="/seller/{number}">Owner</a></div>'
            for number in [1, 2]
        )
    )
    items = [action for action in observed.actions if action.hint == "item"]
    assert len(items) == 2
    assert {action.group for action in items} == {"result_items"}
    assert all("/opaque/" in action.url for action in items)
    assert observed.has_results and not observed.has_entity


def test_semantic_detail_uses_own_price_and_suppresses_recommended_item_links():
    _, observed = inventory(
        "<article><h1>Book about housing</h1><p>£12.50</p><p>Detailed description.</p>"
        '<section><article><h2><a href="/books/unrelated">Other book</a></h2>'
        "<p>£4.00</p></article></section></article>"
    )
    assert observed.has_entity and not observed.has_results
    assert not any(action.hint == "item" for action in observed.actions)
    plan = asyncio.run(NavigationPlanner(mode="rules").plan(observed, "housing", {}))
    assert plan.extract and plan.terminal


def test_direct_entity_source_extracts_before_global_header_search():
    _, observed = inventory(
        '<header><form action="/lookup"><input type="search" name="q"></form></header>'
        "<article><h1>Book about housing</h1><p>£12.50</p></article>",
        url="https://directory.example/products/book",
        content_profile="commerce",
        currency_aliases={"£": "GBP"},
    )
    assert any(action.kind == "search" for action in observed.actions)
    plan = asyncio.run(NavigationPlanner(mode="rules").plan(observed, "housing", {}))
    assert plan.extract and plan.terminal and not plan.action_ids


@pytest.mark.parametrize("entity_url", ["https://directory.example/opaque/featured", None])
def test_homepage_featured_jsonld_does_not_override_observed_search(entity_url):
    entity = {"@type": "Product", "name": "Featured product"}
    if entity_url:
        entity["url"] = entity_url
    _, observed = inventory(
        '<script type="application/ld+json">' + json.dumps(entity) + "</script>"
        '<h1>Our catalog</h1><form action="/lookup"><input type="search" name="q"></form>'
    )
    assert not observed.has_entity
    plan = asyncio.run(NavigationPlanner(mode="rules").plan(observed, "housing", {}))
    assert not plan.extract
    assert observed.by_id()[plan.action_ids[0]].kind == "search"


def test_explicit_other_page_entity_is_never_bound_by_lack_of_navigation():
    _, observed = inventory(
        '<script type="application/ld+json">'
        '{"@type":"Product","name":"Featured","url":"/opaque/featured"}</script>'
    )
    assert not observed.has_entity


def test_strong_jsonld_entity_binding_suppresses_secondary_record_links():
    _, observed = inventory(
        '<script type="application/ld+json">'
        '{"@type":"Product","name":"Own product","url":"https://directory.example/"}'
        '</script><article><h2><a href="/opaque/related">Other item</a></h2><p>2 USD</p></article>',
        content_profile="commerce",
    )
    assert observed.has_entity and not observed.has_results
    assert not any(action.hint == "item" for action in observed.actions)


def test_category_heading_over_nested_cards_is_not_an_individual_entity():
    _, observed = inventory(
        '<article><h1>Books catalog</h1><article><h2><a href="/opaque/one">Book one</a>'
        '</h2><p>£4.00</p></article><article><h2><a href="/opaque/two">Book two</a>'
        "</h2><p>£5.00</p></article></article>"
    )
    assert observed.has_results and not observed.has_entity


def test_homepage_search_precedes_incidental_product_links():
    _, observed = inventory(
        '<h1>Welcome</h1><form action="/lookup"><input type="search" name="q">'
        '</form><article><h2><a href="/products/tokyo">Featured Tokyo flat</a>'
        "</h2><p>100 UAH</p></article>"
    )
    plan = asyncio.run(NavigationPlanner(mode="rules").plan(observed, "Львів", {}))
    assert len(plan.action_ids) == 1
    assert observed.by_id()[plan.action_ids[0]].kind == "search"
    assert plan.extract is False


def test_multiple_jsonld_entities_without_links_are_extractable_results():
    entities = [
        {"@type": "JobPosting", "title": title, "identifier": title}
        for title in ["Engineer", "Designer"]
    ]
    _, observed = inventory(
        '<script type="application/ld+json">' + json.dumps(entities) + "</script>"
    )
    assert observed.has_results and not observed.has_entity
    plan = asyncio.run(NavigationPlanner(mode="rules").plan(observed, None, {}))
    assert plan.extract and plan.terminal


def test_result_batch_includes_observed_load_more_and_all_item_links():
    html = (
        "".join(
            f'<article><h2><a href="/opaque/{number}">Flat {number}</a></h2>'
            "<p>100 UAH</p></article>"
            for number in [1, 2]
        )
        + "<button>Load more</button>"
    )
    _, observed = inventory(html)
    plan = asyncio.run(NavigationPlanner(mode="rules").plan(observed, "flat", {}, search_done=True))
    selected = [observed.by_id()[action_id] for action_id in plan.action_ids]
    assert selected[0].hint == "load_more"
    assert len([action for action in selected if action.kind == "open"]) == 2


@pytest.mark.parametrize("main_choice", ["item", "stop"])
def test_native_selected_result_batch_preserves_observed_next_page_unless_explicit_stop(
    main_choice,
):
    class Native:
        async def decide(self, state, questions):
            item = next(
                name for name, action in state["actions"].items() if action["hint"] == "item"
            )
            return {
                name: {
                    "type": "choice",
                    "choice": (item if main_choice == "item" else "stop")
                    if name == "next_action"
                    else "no"
                    if name == "extract_current"
                    else "follow",
                }
                for name in questions
            }

    _, observed = inventory(
        '<article><h2><a href="/opaque/one">Flat one</a></h2><p>100 UAH</p></article>'
        '<a rel="next" href="/results?page=2">Next</a>',
        allow_scroll=False,
    )
    plan = asyncio.run(
        NavigationPlanner(Native(), mode="ai").plan(observed, "flat", {}, search_done=True)
    )
    selected = [observed.by_id()[action_id] for action_id in plan.action_ids]
    assert any(action.hint == "pagination" for action in selected) is (main_choice == "item")


def test_scroll_is_scheduled_with_results_and_can_be_disabled():
    html = "".join(
        f'<article><h2><a href="/opaque/{number}">Flat {number}</a></h2><p>100 UAH</p></article>'
        for number in [1, 2]
    )
    _, observed = inventory(html)
    plan = asyncio.run(NavigationPlanner(mode="rules").plan(observed, "flat", {}, search_done=True))
    assert observed.by_id()[plan.action_ids[0]].kind == "scroll"
    _, disabled = inventory(html, allow_scroll=False)
    assert not any(action.kind == "scroll" for action in disabled.actions)


def test_simple_homepage_heading_is_never_implicitly_extracted():
    _, observed = inventory("<h1>Welcome to our directory</h1><p>Find something nice today.</p>")
    plan = asyncio.run(NavigationPlanner(mode="rules").plan(observed, "Львів", {}))
    assert plan.terminal and not plan.extract and not plan.action_ids


def test_unique_goal_matching_category_is_navigable_without_host_shortcuts():
    _, observed = inventory(
        '<a href="/category/housing">Львів apartments</a><a href="/category/cars">Cars</a>'
    )
    plan = asyncio.run(NavigationPlanner(mode="rules").plan(observed, "Львів", {}))
    assert observed.by_id()[plan.action_ids[0]].url.endswith("/category/housing")


def test_inventory_bounds_and_observed_select_option_values():
    _, observed = inventory(
        '<select aria-label="Sort"><option selected value="old">Old</option>'
        '<option value="price_asc">Cheapest first</option></select>',
        max_actions=1,
        allow_filters=True,
    )
    assert len(observed.actions) == 1
    assert observed.actions[0].kind == "select"
    assert observed.actions[0].option_value == "price_asc"


def test_plans_reject_unknown_duplicate_and_stale_action_ids():
    _, observed = inventory('<a href="/category/flats">Flats</a>')
    action_id = observed.actions[0].action_id
    for page_id, ids in [
        ("wrong", (action_id,)),
        (observed.page_id, ("unknown",)),
        (observed.page_id, (action_id, action_id)),
    ]:
        with pytest.raises(RoutingError):
            NavigationPlan(page_id=page_id, action_ids=ids).validate_against(observed)


def test_native_navigation_cache_is_scoped_to_query_and_only_maps_existing_aliases(tmp_path):
    requests = []

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        answers = {
            name: {"type": "choice", "choice": "stop" if name == "next_action" else "no"}
            for name in body["questions"]
        }
        return httpx.Response(
            200,
            json={
                "model": "openai/gpt-6-luna-decisions",
                "answers": answers,
                "usage": {"input_tokens": 10, "output_tokens": 0},
            },
        )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            client = OpenRouterDecisionsClient(
                "test-secret", "openai/gpt-6-luna-decisions", cache_dir=tmp_path, client=http
            )
            _, observed = inventory('<a href="/opaque">Open catalog</a>')
            planner = NavigationPlanner(client, mode="ai")
            await planner.plan(observed, "Львів", {})
            await planner.plan(observed, "Львів", {})
            await planner.plan(observed, "Kyiv", {})
            assert client.calls == 2 and client.cache_hits == 1

    asyncio.run(run())
    assert requests[0]["state"]["goal"] != requests[1]["state"]["goal"]
    assert all("selector" not in action for action in requests[0]["state"]["actions"].values())

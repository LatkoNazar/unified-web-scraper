"""Domain-independent structural evidence retains observed values and boundaries."""

import asyncio
import json

import pytest
from bs4 import BeautifulSoup

from unified_web_parser.crawling.dom import primary_detail_region
from unified_web_parser.crawling.pages import make_page
from unified_web_parser.navigation import InventoryBuilder, NavigationPlanner
from unified_web_parser.routing.candidates import CandidateBuilder

URL = "https://research.example/observations"


def page(html):
    return make_page(URL, html, 200)


def test_text_only_repeated_jobs_have_independent_item_actions_and_candidates():
    document = page(
        "<main><h1>Open positions</h1>"
        '<div class="vacancy"><a href="/alpha">Research engineer</a>'
        "<p>Institute Alpha, remote, full time</p></div>"
        '<div class="vacancy"><a href="/beta">Data archivist</a>'
        "<p>Institute Beta, part time, Cambridge</p></div></main>"
    )
    observed = InventoryBuilder(allow_scroll=False).build(document)
    assert observed.has_results and not observed.has_entity
    assert {action.url for action in observed.actions if action.hint == "item"} == {
        "https://research.example/alpha",
        "https://research.example/beta",
    }
    blocks = CandidateBuilder().build(document)
    assert len(blocks) == 2
    assert all(block.locator.startswith("card:") for block in blocks)
    assert all("Institute" in block.content for block in blocks)


@pytest.mark.parametrize(
    "columns,rows",
    [
        (["sample", "temperature", "unit"], [["A", "273.15", "K"], ["B", "298.15", "K"]]),
        (["unit", "sample", "temperature"], [["K", "A", "273.15"], ["K", "B", "298.15"]]),
    ],
)
def test_linkless_measurement_rows_preserve_original_reordered_column_headers(columns, rows):
    html = "<main><h1>Measurements</h1><table><thead><tr>"
    html += "".join(f"<th>{column}</th>" for column in columns)
    html += "</tr></thead><tbody>"
    for row in rows:
        html += "<tr>" + "".join(f"<td>{value}</td>" for value in row) + "</tr>"
    document = page(html + "</tbody></table></main>")
    blocks = CandidateBuilder().build(document)
    assert len(blocks) == 2
    decoded = []
    for block in blocks:
        evidence = BeautifulSoup(block.content, "html.parser")
        headers = [cell.get_text() for cell in evidence.select("thead th")]
        cells = [cell.get_text() for cell in evidence.select("tbody td")]
        assert headers == columns
        assert len(evidence.select("tbody tr")) == 1
        decoded.append(dict(zip(headers, cells, strict=True)))
    assert decoded == [
        {"sample": "A", "temperature": "273.15", "unit": "K"},
        {"sample": "B", "temperature": "298.15", "unit": "K"},
    ]
    observed = InventoryBuilder(allow_scroll=False).build(document)
    plan = asyncio.run(NavigationPlanner(mode="rules").plan(observed, None, {}))
    assert plan.extract and plan.terminal


def test_event_with_date_and_location_has_no_money_prerequisite():
    document = page(
        '<main><h1>Research evening</h1><time datetime="2026-11-20">20 November</time>'
        '<p itemprop="location">Public observatory, main hall</p></main>'
    )
    assert InventoryBuilder(allow_scroll=False).build(document).has_entity
    blocks = CandidateBuilder().build(document)
    assert len(blocks) == 1 and blocks[0].locator == "entity"
    assert "2026-11-20" in blocks[0].content


def test_schema_names_price_and_currency_do_not_activate_commerce_policy():
    schema = {"properties": {"price": {"type": "number"}, "currency": {"type": "string"}}}
    article = {
        "@type": "Article",
        "headline": "Inflation briefing",
        "offers": {"@type": "AggregateOffer", "lowPrice": 1, "highPrice": 4},
    }
    faq = {"@type": "FAQPage", "name": "Questions about the inflation figures"}
    document = page(
        '<script type="application/ld+json">' + json.dumps([article, faq]) + "</script>"
    )
    entities = [
        json.loads(block.content)
        for block in CandidateBuilder(schema=schema).build(document)
        if block.kind.value == "json_ld"
    ]
    assert entities == [article, faq]


def test_generic_detection_never_interprets_monetary_text(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Generic detection called commerce policy")

    monkeypatch.setattr("unified_web_parser.crawling.dom._money", forbidden)
    monkeypatch.setattr("unified_web_parser.crawling.dom.read_price", forbidden)
    document = page(
        "<article><h1>Budget news</h1><p>Researchers described the price of a new experiment "
        "and the public effects of inflation in this detailed briefing.</p></article>"
    )
    assert CandidateBuilder().build(document)[0].locator == "entity"
    assert InventoryBuilder(allow_scroll=False).build(document).has_entity


def test_unicode_schema_labels_match_only_the_same_observed_field():
    soup = BeautifulSoup("<main><h1>Запис</h1><p>Місто: Львів</p></main>", "html.parser")
    assert primary_detail_region(soup, schema={"properties": {"Температура": {}}}) is None
    assert primary_detail_region(soup, schema={"properties": {"Місто": {}}}) is not None


def test_plain_category_links_and_navigation_list_are_not_record_cards():
    document = page(
        "<main><h1>Research categories</h1><ul>"
        '<li><a href="/category/sky">Sky observations</a> — browse all</li>'
        '<li><a href="/category/sea">Sea observations</a> — browse all</li>'
        "</ul></main>"
    )
    inventory = InventoryBuilder(allow_scroll=False).build(document)
    assert not inventory.has_entity and not inventory.has_results
    assert all(action.hint == "category" for action in inventory.actions)


def test_reference_prose_keeps_own_heading_despite_link_lists_and_sidebar():
    document = page(
        '<div role="navigation"><div role="listitem"><h2>'
        '<a href="/index">Navigation</a></h2><p>Browse the index.</p></div></div>'
        '<div role="main"><section><h1>Asynchronous observations</h1>'
        "<p>This reference explains concurrent observations, asynchronous execution, "
        "and the design principles needed to coordinate independent research tasks.</p>"
        "<p>The following interfaces support advanced observations and provide "
        "a documented foundation for applications, monitoring tools and libraries.</p>"
        '<ul class="simple"><li><p><a href="/tasks">Start tasks</a> concurrently</p></li>'
        '<li><p><a href="/streams">Read streams</a> with bounded resources</p></li></ul>'
        '<ul class="simple"><li><p><a href="/timers">Use timers</a> for scheduling</p></li>'
        '<li><p><a href="/events">Inspect events</a> for diagnostics</p></li></ul>'
        '<aside><h2>Related resources</h2><a href="/unrelated">Unrelated sidebar</a></aside>'
        "</section></div>"
    )
    blocks = CandidateBuilder().build(document)
    assert len(blocks) == 1 and blocks[0].locator == "entity"
    assert "Asynchronous observations" in blocks[0].content
    assert "Read streams" in blocks[0].content
    assert "Navigation" not in blocks[0].content
    assert "Unrelated sidebar" not in blocks[0].content
    observed = InventoryBuilder(allow_scroll=False).build(document)
    assert observed.has_entity and not observed.has_results


def test_explicit_metadata_type_retains_selected_collection_wrapper():
    entity = {
        "@type": "ItemList",
        "numberOfItems": 2,
        "itemListElement": [{"@type": "Person", "name": "A"}],
    }
    blocks = CandidateBuilder(schema={"x-entity-types": ["ItemList"]}).build(
        page('<script type="application/ld+json">' + json.dumps(entity) + "</script>")
    )
    assert len(blocks) == 1 and json.loads(blocks[0].content) == entity
    observed = InventoryBuilder(schema={"x-entity-types": ["ItemList"]}).build(
        page('<script type="application/ld+json">' + json.dumps(entity) + "</script>")
    )
    assert observed.has_entity and not observed.has_results

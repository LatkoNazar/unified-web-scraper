"""Independent behavioral regressions for autonomous cross-domain navigation."""

import asyncio
import json

import pytest

from unified_web_parser.core.config import JobConfig, LoadedJob
from unified_web_parser.crawling.pages import make_page
from unified_web_parser.navigation.inventory import InventoryBuilder
from unified_web_parser.navigation.models import NavigationPlan
from unified_web_parser.navigation.planner import NavigationPlanner
from unified_web_parser.pipeline.runner import PipelineRunner
from unified_web_parser.storage import DatasetStore


def loaded_job(tmp_path):
    return LoadedJob(
        config=JobConfig.model_validate(
            {
                "name": "housing-review",
                "sources": [{"url": "https://housing.example/"}],
                "schema_path": "schema.json",
                "content_profile": "commerce",
                "currency_aliases": {"$": "USD", "USD": "USD"},
                "ai": {"enabled": False},
                "navigation": {"mode": "rules", "max_interactions_per_url": 5},
                "crawl": {
                    "backend": "browser",
                    "max_pages": 12,
                    "max_records": 2,
                    "max_depth": 2,
                    "delay": 0,
                },
            }
        ),
        config_path=tmp_path / "job.yaml",
        schema_path=tmp_path / "schema.json",
        extraction_schema={
            "type": "object",
            "required": ["title", "price", "currency"],
            "properties": {
                "title": {"type": "string"},
                "price": {"type": "number"},
                "currency": {"type": "string"},
            },
        },
    )


def result_html(*, second=False):
    cards = '<article><h2><a href="/view?ref=one">Bright house</a></h2><p>$100</p></article>'
    if second:
        cards += '<article><h2><a href="/view?ref=two">Quiet house</a></h2><p>$200</p></article>'
    return f"<main><h1>Available houses</h1>{cards}</main>"


def test_multiple_viewport_scrolls_can_reach_late_lazy_loaded_result(tmp_path):
    class Browser:
        def __init__(self):
            self.interaction_metadata = {}
            self.scrolls = 0
            self.urls = []

        async def fetch(self, url):
            self.urls.append(url)
            if url == "https://housing.example/":
                page = make_page(url, result_html(), 200)
                self.interaction_metadata[page.page_id] = {
                    "scroll_y": 0,
                    "scroll_height": 2000,
                    "viewport_height": 400,
                }
                return page
            title, price = ("Bright house", 100) if "one" in url else ("Quiet house", 200)
            return make_page(
                url,
                '<script type="application/ld+json">'
                + json.dumps(
                    {
                        "@type": "Product",
                        "name": title,
                        "url": url,
                        "offers": {"price": price, "priceCurrency": "USD"},
                    }
                )
                + "</script>",
                200,
            )

        async def interact(self, page, action, query=None):
            assert action.kind == "scroll"
            self.scrolls += 1
            updated = make_page(str(page.url), result_html(second=self.scrolls >= 3), 200)
            self.interaction_metadata[updated.page_id] = {
                "changed": True,
                "scroll_y": self.scrolls * 400,
                "scroll_height": 2000,
                "viewport_height": 400,
            }
            return updated

    browser = Browser()
    report = asyncio.run(
        PipelineRunner(loaded_job(tmp_path), data_dir=tmp_path / "data", fetcher=browser).run()
    )
    assert report.status == "success", report.errors
    assert report.records_written == 2
    assert browser.scrolls == 3
    assert set(browser.urls) == {
        "https://housing.example/",
        "https://housing.example/view?ref=one",
        "https://housing.example/view?ref=two",
    }
    records = DatasetStore(tmp_path / "data").list_records()
    assert {item["data"]["title"] for item in records} == {"Bright house", "Quiet house"}
    assert report.pages_attempted == 6


def test_observed_mutating_intent_is_rejected_even_with_neutral_labels():
    page = make_page(
        "https://housing.example/",
        '<a href="/account/delete">Details</a>'
        '<a href="/login">Continue</a>'
        '<button name="add_to_cart">+</button>'
        '<button onclick="addToCart(42)">+</button>'
        '<a href="/category/houses">Houses</a>'
        "<button>Load more</button>",
        200,
    )
    actions = (
        InventoryBuilder(content_profile="commerce", currency_aliases={"$": "USD"})
        .build(page)
        .actions
    )
    assert {action.label for action in actions} == {"Houses", "Load more"}


def test_one_malformed_observed_url_does_not_discard_safe_result_links():
    page = make_page(
        "https://housing.example/",
        '<a href="https://housing.example:notaport/bad">Malformed port</a>'
        '<a href="https://[broken-ipv6/example">Malformed host</a>' + result_html(),
        200,
    )
    actions = (
        InventoryBuilder(content_profile="commerce", currency_aliases={"$": "USD"})
        .build(page)
        .actions
    )
    assert any(action.url == "https://housing.example/view?ref=one" for action in actions)
    assert not any("bad" in (action.url or "") for action in actions)


@pytest.mark.parametrize("wrapper", ["main", "div"])
@pytest.mark.parametrize("with_recommendations", [False, True])
def test_direct_plain_dom_detail_keeps_own_price_outside_recommendation_cards(
    tmp_path, wrapper, with_recommendations
):
    class Fetcher:
        async def fetch(self, url):
            recommendations = (
                '<section><h2>Other offers</h2><article><h3><a href="/other">'
                "Unrelated apartment</a></h3><p>$900000</p></article></section>"
                if with_recommendations
                else ""
            )
            return make_page(
                url,
                f"<{wrapper}><h1>Quiet house</h1><p>$250000</p>"
                "<p>A detached house with a private garden and a large kitchen.</p>"
                + recommendations
                + f"</{wrapper}>",
                200,
            )

    job = loaded_job(tmp_path)
    config = job.config.model_dump(mode="json")
    config["crawl"].update({"backend": "http", "max_pages": 1, "max_depth": 0})
    config["sources"] = [{"url": "https://housing.example/detail?id=quiet"}]
    job.extraction_schema["properties"]["description"] = {"type": "string"}
    job = job.model_copy(update={"config": JobConfig.model_validate(config)})
    report = asyncio.run(PipelineRunner(job, data_dir=tmp_path / "data", fetcher=Fetcher()).run())
    assert report.status == "success", report.errors
    assert report.records_written == 1
    record = DatasetStore(tmp_path / "data").list_records()[0]
    assert record["data"]["title"] == "Quiet house"
    assert record["data"]["price"] == 250000
    assert record["data"]["currency"] == "USD"
    assert "private garden" in record["data"]["description"]
    assert "Unrelated apartment" not in record["data"]["description"]


@pytest.mark.parametrize("card_tag", ["article", "div"])
@pytest.mark.parametrize("microdata", [False, True])
def test_generic_homepage_heading_does_not_borrow_a_featured_card_price(card_tag, microdata):
    price = '<span itemprop="price">$250000</span>' if microdata else "<p>$250000</p>"
    page = make_page(
        "https://housing.example/",
        f"<main><h1>Welcome to the housing website</h1><{card_tag}>"
        '<h2><a href="/offer">Quiet house</a></h2>' + price + f"</{card_tag}></main>",
        200,
    )
    inventory = InventoryBuilder(content_profile="commerce", currency_aliases={"$": "USD"}).build(
        page
    )
    assert inventory.has_entity is False
    assert inventory.has_results is True


def test_pending_search_cannot_be_short_circuited_by_mixed_extract_plan(tmp_path, monkeypatch):
    class MixedPlanner(NavigationPlanner):
        async def plan(self, inventory, query, schema, **kwargs):
            if not kwargs.get("search_done"):
                search = next(action for action in inventory.actions if action.kind == "search")
                return NavigationPlan(
                    page_id=inventory.page_id,
                    action_ids=(search.action_id,),
                    extract=True,
                    reason="Mixed model outputs disagree about homepage extraction",
                )
            return await super().plan(inventory, query, schema, **kwargs)

    monkeypatch.setattr("unified_web_parser.pipeline.crawl.NavigationPlanner", MixedPlanner)
    root = "https://housing.example/"
    search = "https://housing.example/find?q=quiet+house"
    detail = "https://housing.example/view?ref=quiet"
    pages = {
        root: '<form action="/find"><input type="search" name="q"></form>'
        '<script type="application/ld+json">'
        '{"@type":"Product","name":"Unrelated featured car",'
        '"offers":{"price":99,"priceCurrency":"USD"}}</script>',
        search: '<article><h2><a href="/view?ref=quiet">Quiet house</a></h2><p>$200</p></article>',
        detail: "<article><h1>Quiet house</h1><p>$200</p></article>",
    }

    class Fetcher:
        def __init__(self):
            self.urls = []

        async def fetch(self, url):
            self.urls.append(url)
            return make_page(url, pages[url], 200)

    job = loaded_job(tmp_path)
    config = job.config.model_dump(mode="json")
    config["search"] = "quiet house"
    config["crawl"].update({"backend": "http", "max_records": 1})
    job = job.model_copy(update={"config": JobConfig.model_validate(config)})
    fetcher = Fetcher()
    report = asyncio.run(PipelineRunner(job, data_dir=tmp_path / "data", fetcher=fetcher).run())
    assert report.status == "success", report.errors
    assert fetcher.urls == [root, search, detail]
    record = DatasetStore(tmp_path / "data").list_records()[0]
    assert record["data"]["title"] == "Quiet house"


@pytest.mark.parametrize("descriptor_type", ["SearchResultsPage", "CollectionPage", "ItemList"])
def test_named_collection_descriptor_does_not_hide_its_real_result_links(tmp_path, descriptor_type):
    root = "https://housing.example/"
    detail = "https://housing.example/view?ref=quiet"
    pages = {
        root: '<script type="application/ld+json">'
        + json.dumps({"@type": descriptor_type, "name": "Available houses", "url": root})
        + "</script><main><h1>Available houses</h1><article><h2>"
        '<a href="/view?ref=quiet">Quiet house</a></h2><p>$200</p></article></main>',
        detail: "<article><h1>Quiet house</h1><p>$200</p></article>",
    }

    class Fetcher:
        async def fetch(self, url):
            return make_page(url, pages[url], 200)

    job = loaded_job(tmp_path)
    config = job.config.model_dump(mode="json")
    config["crawl"].update({"backend": "http", "max_records": 1})
    job = job.model_copy(update={"config": JobConfig.model_validate(config)})
    report = asyncio.run(PipelineRunner(job, data_dir=tmp_path / "data", fetcher=Fetcher()).run())
    assert report.status == "success", report.errors
    assert report.pages_attempted == 2
    records = DatasetStore(tmp_path / "data").list_records()
    assert len(records) == 1
    assert records[0]["data"]["title"] == "Quiet house"
    assert records[0]["data"]["price"] == 200

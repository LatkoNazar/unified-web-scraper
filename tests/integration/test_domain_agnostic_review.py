"""Independent generic/profile boundaries using local pages and no model calls."""

import json

import pytest

from unified_web_parser.core.config import JobConfig, LoadedJob
from unified_web_parser.crawling.pages import make_page
from unified_web_parser.pipeline.runner import PipelineRunner
from unified_web_parser.storage.dataset import DatasetStore

URL = "https://research.example/briefings/one"


def schema_for(**fields):
    properties = {"title": {"type": "string"}, "url": {"type": "string"}, **fields}
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(properties),
        "properties": properties,
    }


def nullable(kind="string"):
    return {"type": [kind, "null"]}


async def run_pages(tmp_path, pages, schema, **options):
    class LocalPages:
        async def fetch(self, url):
            assert url in pages, f"Unexpected navigation: {url}"
            return make_page(url, pages[url], 200)

    config = JobConfig.model_validate(
        {
            "name": "domain-review",
            "sources": [{"url": next(iter(pages))}],
            "schema_path": "schema.json",
            "ai": {"enabled": False, "routing": "rules"},
            "navigation": {"mode": "rules"},
            "crawl": {"max_pages": 5, "concurrency": 1, "delay": 0},
            **options,
        }
    )
    loaded = LoadedJob(
        config=config,
        config_path=tmp_path / "job.yaml",
        schema_path=tmp_path / "schema.json",
        extraction_schema=schema,
    )
    report = await PipelineRunner(loaded, data_dir=tmp_path / "data", fetcher=LocalPages()).run()
    assert report.status == "success", report.errors
    assert report.ai.get("calls", 0) == 0
    return report, DatasetStore(tmp_path / "data").list_records()


def structured(entity):
    return (
        '<script type="application/ld+json">' + json.dumps(entity, ensure_ascii=False) + "</script>"
    )


@pytest.mark.parametrize(
    "entity_type",
    ["Article", "JobPosting", "Event", "Person", "Organization", "Dataset", "FAQPage"],
)
async def test_generic_named_noncommerce_entities_do_not_require_prices(tmp_path, entity_type):
    report, records = await run_pages(
        tmp_path,
        {
            URL: structured(
                {
                    "@type": entity_type,
                    "name": "Research briefing",
                    "url": URL,
                    "topic_code": "ASTRO-42",
                    "description": "An observed research entity.",
                }
            )
        },
        schema_for(topic_code=nullable()),
    )
    assert report.records_written == 1
    assert records[0]["data"]["topic_code"] == "ASTRO-42"


async def test_exact_unicode_and_colliding_normalized_field_names_keep_distinct_values(tmp_path):
    fields = {"місто": "Львів", "автор": "Олена", "foo_bar": "underscored", "foobar": "plain"}
    _, records = await run_pages(
        tmp_path,
        {URL: structured({"@type": "Article", "name": "Research briefing", "url": URL, **fields})},
        schema_for(**{key: nullable() for key in fields}),
    )
    assert {key: records[0]["data"][key] for key in fields} == fields


@pytest.mark.parametrize("currency", ["JPY", "CAD", "XOF", "USDT", "$"])
async def test_generic_structured_currency_is_literal_not_a_fixed_supported_currency_list(
    tmp_path, currency
):
    _, records = await run_pages(
        tmp_path,
        {
            URL: structured(
                {
                    "@type": "Dataset",
                    "name": "Observed statistics",
                    "url": URL,
                    "price": 42.5,
                    "priceCurrency": currency,
                }
            )
        },
        schema_for(
            price=nullable("number"), priceCurrency=nullable(), price_uah=nullable("number")
        ),
    )
    assert records[0]["data"]["price"] == 42.5
    assert records[0]["data"]["priceCurrency"] == currency
    assert records[0]["data"]["price_uah"] is None


async def test_generic_does_not_synthesize_price_uah_even_when_source_says_uah(tmp_path):
    _, records = await run_pages(
        tmp_path,
        {
            URL: structured(
                {
                    "@type": "Dataset",
                    "name": "Observed statistics",
                    "url": URL,
                    "price": 42.5,
                    "priceCurrency": "UAH",
                }
            )
        },
        schema_for(price_uah=nullable("number")),
    )
    assert records[0]["data"]["price_uah"] is None


@pytest.mark.parametrize("profile", ["generic", "commerce"])
async def test_profile_mapping_never_overwrites_an_explicit_same_named_source_field(
    tmp_path, profile
):
    _, records = await run_pages(
        tmp_path,
        {
            URL: structured(
                {
                    "@type": "Dataset",
                    "name": "Observed statistics",
                    "url": URL,
                    "price": 42.5,
                    "priceCurrency": "UAH",
                    "price_uah": 7,
                }
            )
        },
        schema_for(price_uah=nullable("number")),
        content_profile=profile,
    )
    assert records[0]["data"]["price_uah"] == 7


async def test_generic_preserves_observed_dates_identifiers_arrays_and_objects(tmp_path):
    values = {
        "observed_date": "01/02/2026",
        "phone": "001234567",
        "tags": ["sky", "stars"],
        "statistics": {"total": 12, "verified": False},
    }
    _, records = await run_pages(
        tmp_path,
        {
            URL: structured(
                {"@type": "Dataset", "name": "Observed statistics", "url": URL, **values}
            )
        },
        schema_for(
            observed_date=nullable(),
            phone=nullable(),
            tags=nullable("array"),
            statistics=nullable("object"),
        ),
    )
    assert {key: records[0]["data"][key] for key in values} == values


async def test_generic_numeric_and_value_wrappers_preserve_large_integers_zero_and_false(tmp_path):
    _, records = await run_pages(
        tmp_path,
        {
            URL: structured(
                {
                    "@type": "Dataset",
                    "name": "Observed statistics",
                    "url": URL,
                    "large_count": "9007199254740993",
                    "wrapped_count": {"value": 0},
                    "wrapped_flag": {"value": False},
                    "counts": {"zero": 0, "done": False},
                }
            )
        },
        schema_for(
            large_count=nullable("integer"),
            wrapped_count=nullable("integer"),
            wrapped_flag=nullable("boolean"),
            pointer_count={**nullable("integer"), "x-source": "/counts/zero"},
            path_flag={**nullable("boolean"), "x-source": "counts.done"},
        ),
    )
    data = records[0]["data"]
    assert data["large_count"] == 9007199254740993
    assert data["wrapped_count"] == data["pointer_count"] == 0
    assert data["wrapped_flag"] is data["path_flag"] is False


async def test_generic_nonpriced_dom_detail_retains_custom_microdata_and_date(tmp_path):
    _, records = await run_pages(
        tmp_path,
        {
            URL: """<main><article><h1>Research briefing</h1>
        <p itemprop="description">An observed briefing about an astronomy research programme.</p>
        <span itemprop="paper_code">ASTRO-42</span>
        <time itemprop="published_at">2026-10-23</time></article></main>"""
        },
        schema_for(paper_code=nullable(), published_at=nullable()),
    )
    assert len(records) == 1
    assert records[0]["data"]["paper_code"] == "ASTRO-42"
    assert records[0]["data"]["published_at"] == "2026-10-23"


async def test_generic_can_navigate_nonpriced_article_cards_on_unrecognized_paths(tmp_path):
    root = "https://research.example/"
    paths = ["https://research.example/briefings/red", "https://research.example/briefings/blue"]
    pages = {
        root: "<main><h1>Research briefings</h1>"
        + "".join(
            f'<article><h2><a href="{url}">{colour} briefing</a></h2>'
            "<p>A detailed astronomy research briefing available to read without a price.</p>"
            "</article>"
            for colour, url in zip(["Red", "Blue"], paths, strict=True)
        )
        + "</main>"
    }
    for colour, url in zip(["Red", "Blue"], paths, strict=True):
        pages[url] = (
            f"<article><h1>{colour} briefing</h1><p>Detailed research observations "
            f'from the telescope programme.</p><span itemprop="paper_code">{colour}</span>'
            "</article>"
        )
    report, records = await run_pages(tmp_path, pages, schema_for(paper_code=nullable()))
    assert report.pages_fetched == 3
    assert {record["data"]["paper_code"] for record in records} == {"Red", "Blue"}


async def test_generic_arbitrary_field_relevance_and_device_named_fields_have_no_device_semantics(
    tmp_path,
):
    _, records = await run_pages(
        tmp_path,
        {
            URL: structured(
                {
                    "@type": "Service",
                    "name": "Consultancy",
                    "url": URL,
                    "event_topic": "Astrophysics",
                    "listing_type": "service",
                    "mdm_status": "claimed_managed",
                    "screen_inches": 15.3,
                }
            )
        },
        schema_for(
            event_topic=nullable(),
            listing_type=nullable(),
            mdm_status=nullable(),
            screen_inches=nullable("number"),
        ),
        search="Astrophysics",
        assessment=["is_relevant", "has_mdm_risk", "requires_manual_review"],
    )
    results = records[0]["assessments"][0]["results"]
    assert results["is_relevant"] is True
    assert results["has_mdm_risk"] is None
    assert results["requires_manual_review"] is None


@pytest.mark.parametrize("profile,expected", [("generic", False), ("device", True)])
async def test_nominal_screen_size_relevance_only_runs_in_explicit_device_profile(
    tmp_path, profile, expected
):
    _, records = await run_pages(
        tmp_path,
        {
            URL: structured(
                {"@type": "Product", "name": "Display", "url": URL, "screen_inches": 15.3}
            )
        },
        schema_for(screen_inches=nullable("number")),
        search="Display 15",
        assessment=["is_relevant"],
        assessment_profile=profile,
    )
    assert records[0]["assessments"][0]["results"]["is_relevant"] is expected


@pytest.mark.parametrize("aliases,expected", [({}, None), ({"$": "CAD"}, "CAD")])
async def test_commerce_symbol_interpretation_requires_explicit_aliases(
    tmp_path, aliases, expected
):
    _, records = await run_pages(
        tmp_path,
        {
            URL: "<article><h1>Printed atlas</h1><p>Price $12.00</p>"
            "<p>A detailed printed atlas of astronomy observations.</p></article>"
        },
        schema_for(price=nullable("number"), currency=nullable()),
        content_profile="commerce",
        currency_aliases=aliases,
    )
    assert records[0]["data"]["price"] == 12
    if expected is None:
        assert records[0]["data"]["currency"] in {None, "$"}
    else:
        assert records[0]["data"]["currency"] == expected


@pytest.mark.parametrize("text,full_amount", [("$1,234.56", 1234.56), ("$0.12345678", 0.12345678)])
def test_optional_commerce_parser_never_emits_a_partial_unsupported_amount(text, full_amount):
    from unified_web_parser.profiles.commerce import read_price

    parsed = read_price(text, currency_aliases={"$": "CAD"})
    assert parsed is None or float(parsed[0]) == full_amount


@pytest.mark.parametrize(
    "target,field,expected",
    [
        ("Article", "article_code", "A-1"),
        ("Organization", "registration", "R-2"),
    ],
)
async def test_explicit_entity_types_select_target_from_mixed_publisher_graph(
    tmp_path, target, field, expected
):
    graph = {
        "@graph": [
            {
                "@type": "Article",
                "name": "Research briefing",
                "url": URL,
                "article_code": "A-1",
                "publisher": {"@id": "#publisher"},
            },
            {
                "@type": "Organization",
                "@id": "#publisher",
                "name": "Research publisher",
                "url": URL,
                "registration": "R-2",
            },
            {"@type": "Person", "name": "Research author", "url": URL, "registration": "P-3"},
        ]
    }
    schema = {**schema_for(**{field: nullable()}), "x-entity-types": [target]}
    report, records = await run_pages(tmp_path, {URL: structured(graph)}, schema)
    assert report.records_written == 1
    assert records[0]["data"][field] == expected


async def test_explicit_unnamed_observation_does_not_require_title_in_manual_schema(tmp_path):
    schema = {
        "type": "object",
        "required": ["value", "unit"],
        "additionalProperties": False,
        "properties": {"value": {"type": "number"}, "unit": {"type": "string"}},
        "x-entity-types": ["Observation"],
    }
    _, records = await run_pages(
        tmp_path,
        {URL: structured({"@type": "Observation", "url": URL, "value": 12.5, "unit": "K"})},
        schema,
    )
    assert [record["data"] for record in records] == [{"value": 12.5, "unit": "K"}]


async def test_explicit_webpage_target_overrides_navigation_descriptor_exclusion(tmp_path):
    schema = {**schema_for(reference_code=nullable()), "x-entity-types": ["WebPage"]}
    _, records = await run_pages(
        tmp_path,
        {
            URL: structured(
                {
                    "@type": "WebPage",
                    "name": "Reference page",
                    "url": URL,
                    "reference_code": "REF-1",
                }
            )
        },
        schema,
    )
    assert [record["data"]["reference_code"] for record in records] == ["REF-1"]


@pytest.mark.parametrize("target", ["ItemList", "CollectionPage"])
async def test_explicit_container_target_keeps_its_own_summary_instead_of_unwrapping(
    tmp_path, target
):
    schema = {**schema_for(numberOfItems=nullable("integer")), "x-entity-types": [target]}
    entity = {
        "@type": target,
        "name": "Observed collection",
        "url": URL,
        "numberOfItems": 2,
        "itemListElement": [
            {
                "@type": "ListItem",
                "item": {"@type": "Article", "name": "A nested article", "url": URL + "/nested"},
            }
        ],
    }
    _, records = await run_pages(tmp_path, {URL: structured(entity)}, schema)
    assert len(records) == 1
    assert records[0]["data"]["title"] == "Observed collection"
    assert records[0]["data"]["numberOfItems"] == 2


@pytest.mark.parametrize(
    "target,matching,unrelated",
    [
        ("Article", "https://schema.org/Article", "https://unrelated.example/Article"),
        ("Article", "http://schema.org/Article", "https://unrelated.example/Article"),
        (
            "https://research.example/types/Reading",
            "https://research.example/types/Reading",
            "https://unrelated.example/types/Reading",
        ),
    ],
)
async def test_type_uri_normalization_does_not_collapse_arbitrary_namespaces(
    tmp_path, target, matching, unrelated
):
    graph = {
        "@graph": [
            {"@type": matching, "name": "Observed target", "url": URL, "reference_code": "RIGHT"},
            {"@type": unrelated, "name": "Unrelated type", "url": URL, "reference_code": "WRONG"},
        ]
    }
    schema = {**schema_for(reference_code=nullable()), "x-entity-types": [target]}
    _, records = await run_pages(tmp_path, {URL: structured(graph)}, schema)
    assert [record["data"]["reference_code"] for record in records] == ["RIGHT"]


async def test_generic_aria_main_content_excludes_navigation_roles_from_records(tmp_path):
    html = """<div role="navigation"><div role="listitem"><h2>
    <a href="/index">Navigation entry</a></h2>
    <p>Browse the complete navigation index and the supporting site menus.</p>
    <span itemprop="reference_code">NAVIGATION</span></div></div>
    <div role="main"><h1>Learning observations</h1>
    <p itemprop="description">A detailed reference about asynchronous research observations.</p>
    <span itemprop="reference_code">MAIN</span></div>"""
    _, records = await run_pages(
        tmp_path, {URL: html}, schema_for(reference_code=nullable(), description=nullable())
    )
    assert len(records) == 1
    assert records[0]["data"]["title"] == "Learning observations"
    assert records[0]["data"]["reference_code"] == "MAIN"
    assert "Navigation entry" not in records[0]["data"]["description"]


@pytest.mark.parametrize(
    "options,content,assessment,generated",
    [
        ([], "generic", "generic", True),
        (["--fixed-schema", "--no-ai"], "generic", "generic", False),
        (["--config", "configs/jobs/macbooks.yaml"], "commerce", "device", False),
    ],
)
def test_search_command_defaults_to_generic_question_schema_without_hidden_device_profile(
    monkeypatch, options, content, assessment, generated
):
    from unified_web_parser.interfaces import cli
    from unified_web_parser.pipeline import runner

    captured = []

    class SuccessfulReport:
        status = "success"

        def model_dump_json(self, **_kwargs):
            return "{}"

    class CaptureRunner:
        def __init__(self, job, **_kwargs):
            captured.append(job)

        async def run(self, **_kwargs):
            return SuccessfulReport()

    monkeypatch.setattr(cli, "load_dotenv", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runner, "PipelineRunner", CaptureRunner)
    assert cli.main(["search", "Astronomy observations", "--source", URL, *options]) == 0
    job = captured[0]
    assert job.config.content_profile == content
    assert job.config.assessment_profile == assessment
    assert job.config.schema_generation.enabled is generated
    assert job.config.discovery.enabled is False
    assert [str(source.url) for source in job.config.sources] == [URL]
    if content == "generic" and not generated:
        assert (
            not {"price", "currency", "ram_gb", "mdm_status"}
            & job.extraction_schema["properties"].keys()
        )

"""Generic source projection and explicitly enabled domain interpretation."""

import json

import pytest
from pydantic import ValidationError

from unified_web_parser.core.config import JobConfig, LoadedJob, validate_schema
from unified_web_parser.core.exceptions import ConfigurationError
from unified_web_parser.core.models import CandidateBlock, CandidateKind, ExtractionStrategy
from unified_web_parser.crawling.pages import make_page
from unified_web_parser.extraction.projection import map_entity
from unified_web_parser.extraction.schema import SchemaExtractor
from unified_web_parser.profiles.commerce import has_money_evidence, read_price


def test_explicit_paths_preserve_unicode_and_native_nested_values():
    source = {
        "місто": "Львів",
        "автор": "Олена",
        "a/b": {"~key": [3, False]},
        "stats": {"count": 9},
        "date": "01/02/2026",
    }
    schema = {
        "properties": {
            "місто": {"type": "string"},
            "автор": {"type": "string"},
            "count": {"type": "integer", "x-source": "/a~1b/~0key/0"},
            "flag": {"type": "boolean", "x-source": "a/b.~key.1"},
            "stats": {"type": "object"},
            "date": {"type": "string"},
            "missing": {"type": ["string", "null"], "x-source": "/missing"},
        }
    }
    assert map_entity(source, schema, "https://example.org/") == {
        "місто": "Львів",
        "автор": "Олена",
        "count": 3,
        "flag": False,
        "stats": {"count": 9},
        "date": "01/02/2026",
        "missing": None,
    }


@pytest.mark.parametrize("code", ["JPY", "INR", "EUR", "X-CUSTOM"])
async def test_structured_currency_codes_are_observed_values_without_a_registry(code):
    entity = {
        "@type": "Product",
        "name": "Observed entity",
        "offers": {"price": 42, "priceCurrency": code},
    }
    block = CandidateBlock(
        candidate_id="one", kind=CandidateKind.JSON_LD, content=json.dumps(entity)
    )
    page = make_page("https://example.org/entity", "", 200)
    schema = {
        "properties": {
            "price": {"type": "number", "x-source": "offers.price"},
            "currency": {"type": "string", "x-source": "offers.priceCurrency"},
        }
    }
    result = await SchemaExtractor("custom").extract(
        page, block, ExtractionStrategy.JSON_LD, schema
    )
    assert result[0].data == {"price": 42, "currency": code}


async def test_generic_microdata_keeps_nested_properties_until_explicitly_mapped():
    content = (
        '<article><h1>Observed entity</h1><div itemprop="offers" itemscope>'
        '<meta itemprop="price" content="42"><meta itemprop="priceCurrency" '
        'content="JPY"></div><p>$999 unrelated visible amount</p></article>'
    )
    page = make_page("https://example.org/entity", content, 200)
    block = CandidateBlock(candidate_id="one", kind=CandidateKind.DOM, content=content)
    schema = {
        "properties": {
            "price": {"type": ["number", "null"]},
            "currency": {"type": ["string", "null"]},
            "own_amount": {"type": "number", "x-source": "offers.price"},
        }
    }
    record = (await SchemaExtractor("custom").extract(page, block, ExtractionStrategy.DOM, schema))[
        0
    ]
    assert record.data == {"price": None, "currency": None, "own_amount": 42}


async def test_generic_table_projection_uses_observed_header_not_column_position():
    content = (
        "<table><thead><tr><th>Reading</th><th>Station</th></tr></thead>"
        "<tbody><tr><td>12.5</td><td>Polar North</td></tr></tbody></table>"
    )
    page = make_page("https://example.org/observations", content, 200)
    block = CandidateBlock(candidate_id="one", kind=CandidateKind.DOM, content=content)
    schema = {
        "properties": {
            "station": {"type": "string", "x-source": "Station"},
            "measurement": {"type": "number", "x-source": "Reading"},
        }
    }
    record = (await SchemaExtractor("custom").extract(page, block, ExtractionStrategy.DOM, schema))[
        0
    ]
    assert record.data == {"station": "Polar North", "measurement": 12.5}


def test_commerce_aliases_are_explicit_and_unconfigured_text_is_not_money():
    assert read_price("$100") == ("100", None)
    assert read_price("$100", currency_aliases={"$": "CAD"}) == ("100", "CAD")
    assert read_price("100 JPY", currency_aliases={"JPY": "JPY"}) == ("100", "JPY")
    assert not has_money_evidence("16 RAM 512 SSD")
    assert read_price("16 RAM 512 SSD") is None


@pytest.mark.parametrize("value", [[], [""], ["Article", "Article"], "Article", [True], ["A"] * 51])
def test_invalid_explicit_entity_type_selection_fails_locally(value):
    with pytest.raises(ConfigurationError, match="x-entity-types"):
        validate_schema({"type": "object", "x-entity-types": value})


def test_profiles_are_strict_opt_ins():
    base = {
        "name": "generic",
        "schema_path": "schema.json",
        "sources": [{"url": "https://example.org/"}],
    }
    config = JobConfig.model_validate(base)
    assert (config.content_profile, config.assessment_profile, config.currency_aliases) == (
        "generic",
        "generic",
        {},
    )
    for override in (
        {"content_profile": "devices"},
        {"assessment_profile": "commerce"},
        {"currency_aliases": {"$": 123}},
        {"relevance_fields": [""]},
    ):
        with pytest.raises(ValidationError):
            JobConfig.model_validate({**base, **override})


def test_cli_profile_overrides_are_explicit_and_independent(tmp_path):
    from unified_web_parser.interfaces.cli import _overrides, _parser

    config = JobConfig.model_validate(
        {
            "name": "profile",
            "schema_path": "schema.json",
            "sources": [{"url": "https://example.org/"}],
            "content_profile": "commerce",
            "assessment_profile": "device",
        }
    )
    loaded = LoadedJob(
        config=config,
        config_path=tmp_path / "job.yaml",
        schema_path=tmp_path / "schema.json",
        extraction_schema={},
    )
    parser = _parser()
    unchanged = _overrides(loaded, parser.parse_args(["run", "job.yaml"]))
    assert unchanged.config.content_profile == "commerce"
    assert unchanged.config.assessment_profile == "device"
    changed = _overrides(
        loaded,
        parser.parse_args(
            ["run", "job.yaml", "--content-profile", "generic", "--assessment-profile", "generic"]
        ),
    )
    assert changed.config.content_profile == changed.config.assessment_profile == "generic"
    assert loaded.config.content_profile == "commerce"

"""Observed images remain associated with records, outside arbitrary data schemas."""

import csv
import json
import sqlite3

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from unified_web_parser.core.models import CandidateBlock, CandidateKind, ExtractionStrategy
from unified_web_parser.crawling.pages import make_page
from unified_web_parser.extraction.images import dom_image, entity_image, image_url
from unified_web_parser.extraction.schema import SchemaExtractor
from unified_web_parser.routing.candidates import CandidateBuilder
from unified_web_parser.storage import DatasetStore, export_records

URL = "https://source.example/catalog/record"
SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["title", "url"],
    "properties": {"title": {"type": "string"}, "url": {"type": "string"}},
}


def page(content):
    return make_page(URL, content, 200)


async def extract_json(entity, *, extra="", schema=SCHEMA, llm=None):
    document = page(
        '<script type="application/ld+json">' + json.dumps(entity) + "</script>" + extra
    )
    candidate = CandidateBlock(
        candidate_id="block",
        kind=CandidateKind.JSON_LD,
        content=json.dumps(entity),
        locator="jsonld:0:0",
    )
    return await SchemaExtractor("images", llm).extract(
        document, candidate, ExtractionStrategy.JSON_LD, schema
    )


@pytest.mark.parametrize(
    "reference",
    [
        "",
        " ",
        "javascript:alert(1)",
        "data:image/png;base64,AAAA",
        "blob:https://example.org/id",
        "https://user:password@example.org/a.jpg",
        "https://example.org:invalid/a.jpg",
        "#image",
        "/placeholder.png",
        "/transparent.gif",
        "/no-image.jpg",
        "https://example.org/\nimage.jpg",
    ],
)
def test_invalid_and_placeholder_references_remain_unknown(reference):
    assert image_url(reference, URL) is None


@pytest.mark.parametrize(
    "value,expected",
    [
        ("../pictures/one.jpg", "https://source.example/pictures/one.jpg"),
        ([None, "data:image/png,AAAA", "//cdn.example/two.jpg"], "https://cdn.example/two.jpg"),
        ({"@type": "ImageObject", "contentUrl": "/three.jpg"}, "https://source.example/three.jpg"),
        ({"@type": "ImageObject", "url": "four.jpg"}, "https://source.example/catalog/four.jpg"),
    ],
)
def test_jsonld_image_forms_resolve_observed_references(value, expected):
    assert entity_image({"image": value}, URL) == expected


def test_dom_lazy_srcset_microdata_and_logo_boundaries():
    from bs4 import BeautifulSoup

    root = BeautifulSoup(
        '<header><img src="/site.jpg"></header><img class="site-logo" '
        'src="/logo.jpg"><img src="/blank.gif" data-src="/cover.jpg">',
        "html.parser",
    )
    assert dom_image(root, URL) == "https://source.example/cover.jpg"
    root = BeautifulSoup(
        '<img src="data:image/png,AAAA" srcset="/small.jpg 1x, /large.jpg 2x">', "html.parser"
    )
    assert dom_image(root, URL) == "https://source.example/small.jpg"
    root = BeautifulSoup('<meta itemprop="image" content="/microdata.jpg">', "html.parser")
    assert dom_image(root, URL) == "https://source.example/microdata.jpg"


async def test_image_is_metadata_without_schema_augmentation_and_has_stable_url_identity():
    original = {
        "@type": "Article",
        "headline": "Observed record",
        "url": URL,
        "image": "/first.jpg",
    }
    first = (await extract_json(original))[0]
    second = (await extract_json({**original, "image": "/second.jpg"}))[0]
    assert first.data == second.data == {"title": "Observed record", "url": URL}
    assert str(first.img_url) == "https://source.example/first.jpg"
    assert first.record_id == second.record_id
    assert first.model_dump(mode="json")["img_url"] == "https://source.example/first.jpg"
    assert (await extract_json({key: value for key, value in original.items() if key != "image"}))[
        0
    ].img_url is None


async def test_requested_img_url_is_source_mapped_without_invented_llm_image():
    class LLM:
        model = "test-model"

        async def complete_json(self, *_args, **_kwargs):
            return {
                "records": [
                    {
                        "title": "Observed record",
                        "url": URL,
                        "img_url": "https://invented.example/generated.jpg",
                        "description": None,
                    }
                ]
            }

    schema = {
        **SCHEMA,
        "properties": {
            **SCHEMA["properties"],
            "img_url": {"type": ["string", "null"]},
            "description": {"type": ["string", "null"]},
        },
    }
    record = (
        await extract_json(
            {"@type": "Article", "name": "Observed record", "url": URL}, schema=schema, llm=LLM()
        )
    )[0]
    assert record.img_url is None and record.data["img_url"] is None
    found = (
        await extract_json(
            {
                "@type": "Article",
                "name": "Observed record",
                "url": URL,
                "thumbnailUrl": "/thumb.jpg",
            },
            schema=schema,
        )
    )[0]
    assert found.data["img_url"] == str(found.img_url) == "https://source.example/thumb.jpg"


async def test_missing_image_alone_never_triggers_a_paid_extraction_call():
    class LLM:
        async def complete_json(self, *_args, **_kwargs):
            raise AssertionError("Image discovery must stay local")

    schema = {
        **SCHEMA,
        "properties": {**SCHEMA["properties"], "img_url": {"type": ["string", "null"]}},
    }
    record = (
        await extract_json(
            {"@type": "Article", "name": "Observed record", "url": URL}, schema=schema, llm=LLM()
        )
    )[0]
    assert record.img_url is None and record.data["img_url"] is None


async def test_malformed_unrelated_anchor_does_not_break_valid_card_image_association():
    record = (
        await extract_json(
            {
                "@type": "Article",
                "name": "Observed record",
                "url": "https://source.example/article",
            },
            extra='<main><h1>Records</h1><a href="http://[bad">Broken link</a>'
            '<article><h2><a href="/article">Observed record</a></h2><img src="/correct.jpg">'
            "<p>Actual record details.</p></article></main>",
        )
    )[0]
    assert str(record.img_url) == "https://source.example/correct.jpg"


async def test_two_stripped_dom_cards_keep_individual_images_without_global_hero():
    document = page(
        '<meta property="og:image" content="/global-hero.jpg"><header>'
        '<img src="/site-logo.jpg"></header><main><h1>Research records</h1>'
        '<article><h2><a href="/one">First research record</a></h2>'
        '<img data-src="/one.jpg"><p>Observed first record description.</p></article>'
        '<article><h2><a href="/two">Second research record</a></h2>'
        '<img srcset="/two.jpg 1x, /two-large.jpg 2x"><p>Observed second description.</p>'
        '</article><article><h2><a href="/three">Third research record</a></h2>'
        "<p>This record has no associated photograph.</p></article></main>"
    )
    candidates = CandidateBuilder().build(document)
    assert len(candidates) == 3 and all("<img" not in item.content for item in candidates)
    records = [
        record
        for item in candidates
        for record in await SchemaExtractor("images").extract(
            document, item, ExtractionStrategy.DOM, SCHEMA
        )
    ]
    assert [str(record.img_url) if record.img_url else None for record in records] == [
        "https://source.example/one.jpg",
        "https://source.example/two.jpg",
        None,
    ]


async def test_detail_dom_and_og_fallback_only_for_an_unambiguous_own_entity():
    entity = {"@type": "Article", "name": "Observed record", "url": URL}
    detail = (
        "<article><h1>Observed record</h1>"
        "<p>A detailed account of this particular research record.</p></article>"
    )
    hero = '<meta property="og:image" content="/hero.jpg">'
    own = (await extract_json(entity, extra=hero + detail))[0]
    assert str(own.img_url) == "https://source.example/hero.jpg"
    inline = (
        await extract_json(
            entity, extra=hero + detail.replace("</article>", '<img src="/own.jpg"></article>')
        )
    )[0]
    assert str(inline.img_url) == "https://source.example/own.jpg"
    multi = await extract_json(
        [entity, {**entity, "name": "Other", "url": "https://source.example/other"}],
        extra=hero + detail,
    )
    assert all(record.img_url is None for record in multi)


async def test_selected_publisher_does_not_inherit_article_hero():
    record = (
        await extract_json(
            {"@type": "Organization", "name": "Publisher", "url": URL},
            extra='<meta property="og:image" content="/article-hero.jpg"><article>'
            "<h1>Research article</h1><p>A detailed account of an individual research article.</p>"
            '<img src="/article-photo.jpg"></article>',
        )
    )[0]
    assert record.img_url is None


async def test_multiple_llm_records_never_share_the_single_candidate_image():
    class LLM:
        model = "test-model"

        async def complete_json(self, *_args, **_kwargs):
            return {
                "records": [
                    {"title": "First", "url": "https://source.example/first"},
                    {"title": "Second", "url": "https://source.example/second"},
                ]
            }

    content = (
        '<article><h2>Grouped results</h2><img src="/one-photo.jpg">'
        '<a href="/first">First</a><a href="/second">Second</a></article>'
    )
    document = page(content)
    candidate = CandidateBlock(
        candidate_id="block", kind=CandidateKind.DOM, content=content, locator="card:0"
    )
    records = await SchemaExtractor("images", LLM()).extract(
        document, candidate, ExtractionStrategy.LLM, SCHEMA
    )
    assert len(records) == 2 and all(record.img_url is None for record in records)


async def test_sqlite_legacy_migration_image_history_and_exports(tmp_path):
    store = DatasetStore(tmp_path)
    old = (await extract_json({"@type": "Article", "name": "Observed record", "url": URL}))[0]
    store.start_run("old", "images", {})
    store.save_records("old", [old])
    with sqlite3.connect(store.path) as connection:
        for table in ("parser_records", "parser_snapshots"):
            connection.execute(f"ALTER TABLE {table} DROP COLUMN img_url")
    store = DatasetStore(tmp_path)
    assert DatasetStore(tmp_path).list_records()[0]["img_url"] is None
    first_hash = store.list_records()[0]["data_hash"]
    store.start_run("new", "images", {})
    observed = (
        await extract_json(
            {"@type": "Article", "name": "Observed record", "url": URL, "image": "/photo.jpg"}
        )
    )[0]
    store.save_records("new", [observed])
    store.save_records("new", [observed])
    current = store.list_records()[0]
    assert current["data_hash"] == first_hash
    assert [row["img_url"] for row in store.list_history(old.record_id)] == [
        None,
        str(observed.img_url),
    ]
    assert len(store.list_records()) == 1
    target = export_records([observed], tmp_path / "record.jsonl", "jsonl")
    assert json.loads(target.read_text(encoding="utf-8"))["img_url"] == str(observed.img_url)
    target = export_records([current], tmp_path / "record.parquet", "parquet")
    table = pq.read_table(target)
    assert pa.types.is_string(table.schema.field("_img_url").type)
    assert table.to_pylist()[0]["_img_url"] == str(observed.img_url)
    target = export_records([observed], tmp_path / "record.csv", "csv")
    with target.open(encoding="utf-8", newline="") as stream:
        assert next(csv.DictReader(stream))["_img_url"] == str(observed.img_url)
    empty = pq.read_table(export_records([], tmp_path / "empty.parquet", "parquet"))
    assert pa.types.is_string(empty.schema.field("_img_url").type)

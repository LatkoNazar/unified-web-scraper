"""Independent regressions for data loss found during final review."""

import asyncio
import json
from pathlib import Path

from unified_web_parser.core.config import JobConfig, LoadedJob
from unified_web_parser.crawling.pages import make_page
from unified_web_parser.pipeline.runner import PipelineRunner
from unified_web_parser.storage import DatasetStore


class SinglePageFetcher:
    def __init__(self, entities):
        self.html = '<script type="application/ld+json">' + json.dumps(entities) + "</script>"

    async def fetch(self, url):
        return make_page(url, self.html, 200)


def loaded_job(tmp_path, *, properties, ai=False):
    return LoadedJob(
        config=JobConfig.model_validate(
            {
                "name": "final-review",
                "sources": [{"url": "https://example.org/careers"}],
                "schema_path": "schema.json",
                "crawl": {"max_depth": 0, "max_pages": 1, "delay": 0},
                "ai": {"enabled": ai, "routing": "rules"},
            }
        ),
        config_path=tmp_path / "job.yaml",
        schema_path=tmp_path / "schema.json",
        extraction_schema={"type": "object", "required": ["title"], "properties": properties},
    )


def test_distinct_identifiers_preserve_multiple_entities_on_one_page(tmp_path):
    job = loaded_job(tmp_path, properties={"title": {"type": "string"}})
    fetcher = SinglePageFetcher(
        [
            {"@type": "JobPosting", "title": "Engineer", "identifier": "ENG1"},
            {"@type": "JobPosting", "title": "Designer", "identifier": "DES2"},
        ]
    )
    report = asyncio.run(PipelineRunner(job, data_dir=tmp_path, fetcher=fetcher).run())
    assert report.status == "success", report.errors
    records = DatasetStore(tmp_path).list_records()
    assert len(records) == 2
    assert {item["data"]["title"] for item in records} == {"Engineer", "Designer"}
    assert len({item["record_id"] for item in records}) == 2


def test_empty_ai_enrichment_retains_explicit_structured_facts(tmp_path):
    class EmptyEnrichment:
        model = "test/fake"

        async def complete_json(self, *_args, **_kwargs):
            return {"records": []}

    job = loaded_job(
        tmp_path,
        properties={
            "title": {"type": "string"},
            "price": {"type": "number"},
            "condition": {"type": ["string", "null"]},
        },
        ai=True,
    )
    fetcher = SinglePageFetcher({"@type": "Product", "name": "Camera", "price": 100})
    report = asyncio.run(
        PipelineRunner(job, data_dir=tmp_path, fetcher=fetcher, llm=EmptyEnrichment()).run()
    )
    records = DatasetStore(tmp_path).list_records()
    assert report.records_written == 1, report.errors
    assert records[0]["data"]["title"] == "Camera"
    assert records[0]["data"]["price"] == 100
    assert Path(report.artifacts["jsonl"]).is_file()


def test_multiple_llm_records_preserve_distinct_entity_urls(tmp_path):
    class TwoOffers:
        model = "test/fake"

        async def complete_json(self, *_args, **_kwargs):
            return {
                "records": [
                    {"title": "Camera A", "url": "https://example.org/products/a"},
                    {"title": "Camera B", "url": "https://example.org/products/b"},
                ]
            }

    job = loaded_job(
        tmp_path,
        properties={
            "title": {"type": "string"},
            "url": {"type": "string"},
            "condition": {"type": ["string", "null"]},
        },
        ai=True,
    )
    fetcher = SinglePageFetcher([])
    fetcher.html = (
        '<main><h1>Camera offers</h1><a href="/products/a">Camera A</a>'
        '<a href="/products/b">Camera B</a></main>'
    )
    report = asyncio.run(
        PipelineRunner(job, data_dir=tmp_path, fetcher=fetcher, llm=TwoOffers()).run()
    )
    assert report.records_written == 2, report.errors
    records = DatasetStore(tmp_path).list_records()
    assert {item["data"]["title"] for item in records} == {"Camera A", "Camera B"}

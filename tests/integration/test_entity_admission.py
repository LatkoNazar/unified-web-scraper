"""Entity admission separates compatibility accessories from requested devices."""

import json
from pathlib import Path

import pytest

from unified_web_parser.core.config import JobConfig, LoadedJob
from unified_web_parser.core.models import Provenance, Record
from unified_web_parser.crawling.pages import make_page
from unified_web_parser.pipeline.checkpoints import CheckpointStore, load_resume_job
from unified_web_parser.pipeline.runner import PipelineRunner
from unified_web_parser.processing.admission import admit_record
from unified_web_parser.storage import DatasetStore

SCHEMA = {
    "title": "Orion device inventory",
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "title": {"type": "string"},
        "url": {"type": "string"},
        "listing_type": {"type": ["string", "null"]},
        "chip": {"type": ["string", "null"]},
        "description": {"type": ["string", "null"]},
    },
    "required": ["title", "url"],
}


def record(title, **data):
    return Record(
        record_id="item",
        job_name="example",
        data={"title": title, **data},
        provenance=Provenance(
            page_id="page",
            source_url="https://example.com/item",
            candidate_ids=("c",),
            extractor="json_ld",
        ),
    )


@pytest.mark.parametrize(
    "title",
    [
        "Чехол-накладка для Orion X5 16/512",
        "Килимок для Orion X5 24GB 1TB",
        "Коврик Orion X5",
        "Protective case for Orion X5",
        "Orion X5 sleeve",
        "Keyboard replacement for Orion X5",
        "Ремонт Orion X5",
    ],
)
def test_primary_non_device_subject_is_rejected_despite_compatibility_specs(title):
    result = admit_record(
        record(title, listing_type="device", chip="X5"),
        SCHEMA,
        "Orion X5",
        assessment_profile="device",
    )
    assert result.status == "rejected" and result.reason == "non_device_subject"


@pytest.mark.parametrize(
    "title",
    [
        "Orion X5 16/512 + case and keyboard",
        "Orion X5 24/1TB with protective case",
        "Orion X5, case included",
        "Orion X5 з чохлом",
        "Orion X5",
    ],
)
def test_actual_device_with_included_accessories_is_kept(title):
    result = admit_record(
        record(
            title, listing_type="device", description="Includes keyboard, mouse, case for Orion X5"
        ),
        SCHEMA,
        "Orion X5",
        assessment_profile="device",
    )
    assert result.status == "accepted"


@pytest.mark.parametrize(
    "title,chip,status",
    [
        ("Orion X4", "X4", "rejected"),
        ("Orion X5", None, "accepted"),
        ("Orion X5Pro", None, "accepted"),
        ("Orion", None, "review"),
        ("Orion X4/X5", None, "accepted"),
        ("Orion X4Х5", None, "accepted"),
    ],
)
def test_model_generation_uses_whole_identity_and_keeps_missing_facts_for_review(
    title, chip, status
):
    result = admit_record(
        record(title, chip=chip, description="Supports Orion X5"),
        SCHEMA,
        "Orion X5",
        assessment_profile="device",
    )
    assert result.status == status


def test_other_explicit_device_family_cannot_match_schema_query_via_description():
    result = admit_record(
        record(
            "Vega Book4 Core7", listing_type="device", description="Our catalog also sells Orion X5"
        ),
        SCHEMA,
        "Orion X5",
        assessment_profile="device",
    )
    assert result.status == "rejected" and result.reason == "explicit_entity_mismatch"
    unknown = admit_record(
        record("Air (SKU944)", listing_type="device"),
        SCHEMA,
        "Orion X5",
        assessment_profile="device",
    )
    assert unknown.status == "review"


def test_generic_profile_and_explicit_accessory_query_are_never_device_filtered():
    source = record("Protective case for Orion X5", listing_type="accessory")
    assert admit_record(source, SCHEMA, "Orion X5").status == "accepted"
    assert (
        admit_record(source, SCHEMA, "case for Orion X5", assessment_profile="device").status
        == "review"
    )
    assert admit_record(record("Unknown"), SCHEMA, "Orion X5").status == "review"


@pytest.mark.asyncio
async def test_rejection_occurs_before_storage_and_cap_with_durable_audit(tmp_path):
    schema = {**SCHEMA, "title": "Orion inventory"}
    job = LoadedJob(
        config_path=tmp_path / "job.yaml",
        extraction_schema=schema,
        config=JobConfig.model_validate(
            {
                "name": "admission",
                "schema_path": "schema.json",
                "search": "Orion X5",
                "assessment_profile": "device",
                "sources": [{"url": "https://example.com/catalog"}],
                "crawl": {"max_pages": 1, "max_records": 1},
                "ai": {"enabled": False},
                "navigation": {"mode": "rules"},
            }
        ),
    )
    entities = [
        {"@type": "Product", "name": "Case for Orion X5", "url": "https://example.com/case"},
        {"@type": "Product", "name": "Orion X5", "url": "https://example.com/device"},
        {"@type": "Product", "name": "Unclear bundle", "url": "https://example.com/unknown"},
    ]

    class Pages:
        async def fetch(self, url):
            return make_page(
                url, '<script type="application/ld+json">' + json.dumps(entities) + "</script>", 200
            )

    events = []
    report = await PipelineRunner(
        job,
        data_dir=tmp_path,
        fetcher=Pages(),
        on_event=lambda kind, data: events.append((kind, data)),
    ).run()
    assert report.records_written == 1
    assert DatasetStore(tmp_path).list_records()[0]["data"]["title"] == "Orion X5"
    assert report.admission == {"version": 1, "accepted": 1, "review": 0, "rejected": 1}
    rejected = [
        json.loads(line)
        for line in Path(report.artifacts["rejected_records"]).read_text().splitlines()
    ]
    assert len(rejected) == 1 and rejected[0]["record"]["data"]["title"] == "Case for Orion X5"
    assert all(
        data["record"]["data"]["title"] != "Case for Orion X5"
        for kind, data in events
        if kind == "record"
    )
    run_dir = Path(report.artifacts["directory"])
    # Raising the cap processes remaining snapshot candidates without re-fetching.
    loaded = load_resume_job(run_dir)
    loaded = loaded.model_copy(
        update={
            "config": loaded.config.model_copy(
                update={
                    "crawl": loaded.config.crawl.model_copy(update={"max_records": 2}),
                }
            )
        }
    )

    class NoFetch:
        async def fetch(self, url):
            pytest.fail("Resume must use its saved catalog snapshot")

    resumed = await PipelineRunner(loaded, data_dir=tmp_path, fetcher=NoFetch()).run(
        resume_dir=run_dir
    )
    assert resumed.records_written == 2
    assert resumed.admission == {"version": 1, "accepted": 1, "review": 1, "rejected": 1}
    assert len(Path(resumed.artifacts["rejected_records"]).read_text().splitlines()) == 1
    assert len(CheckpointStore(run_dir).load()["records"]) == 2


def test_requested_explicit_model_variant_does_not_accept_a_different_observed_variant():
    result = admit_record(record("Orion X5Max"), SCHEMA, "Orion X5Pro", assessment_profile="device")
    assert result.status == "rejected" and result.reason == "explicit_model_mismatch"


@pytest.mark.asyncio
async def test_all_incompatible_entities_is_a_clean_audited_empty_result(tmp_path):
    job = LoadedJob(
        config_path=tmp_path / "job.yaml",
        extraction_schema=SCHEMA,
        config=JobConfig.model_validate(
            {
                "name": "empty",
                "schema_path": "schema.json",
                "search": "Orion X5",
                "assessment_profile": "device",
                "sources": [{"url": "https://example.com/item"}],
                "ai": {"enabled": False},
                "navigation": {"mode": "rules"},
            }
        ),
    )

    class Pages:
        async def fetch(self, url):
            return make_page(
                url,
                '<script type="application/ld+json">'
                + json.dumps(
                    {
                        "@type": "Product",
                        "name": "Case for Orion X5",
                        "url": url,
                    }
                )
                + "</script>",
                200,
            )

    report = await PipelineRunner(job, data_dir=tmp_path, fetcher=Pages()).run()
    assert report.status == "success" and report.errors == []
    assert report.records_written == report.invalid_records == 0
    assert report.admission["rejected"] == 1
    assert any("No matching records" in warning["message"] for warning in report.warnings)
    assert DatasetStore(tmp_path).list_records() == []


def test_explicit_cyrillic_model_is_canonical_only_in_device_profile():
    from unified_web_parser.assessment.rules import assess_record

    item = record('Макбук Apple MacBook Air 15,3" М5 16/512', listing_type="device")
    schema = {**SCHEMA, "title": "MacBook inventory"}
    admission = admit_record(item, schema, "MacBook M5", assessment_profile="device")
    assert admission.status == "accepted"
    assert (
        assess_record(item, "MacBook M5", ["is_relevant"], assessment_profile="device").results[
            "is_relevant"
        ]
        is True
    )
    # The domain-neutral tokenizer does not transliterate arbitrary source text.
    assert assess_record(item, "MacBook M5", ["is_relevant"]).results["is_relevant"] is False


@pytest.mark.parametrize(
    "title",
    [
        "MacBook Air M5 на запчастини",
        "Apple MacBook Pro M5 for parts",
        "Apple MacBook Air M5 розбірка",
        "Orion X5 на деталі",
    ],
)
def test_late_explicit_parts_sale_is_rejected_without_listing_type(title):
    query = "MacBook M5" if "MacBook" in title else "Orion X5"
    item = record(title)
    result = admit_record(item, SCHEMA, query, assessment_profile="device")
    assert result.status == "rejected" and result.reason == "non_device_subject"


@pytest.mark.parametrize(
    "title",
    [
        "Orion X5 with spare parts included",
        "Orion X5 + spare parts",
        "Orion X5 spare parts included",
        "Repaired Orion X5",
        "Orion X5 після ремонту",
        "Без ремонту Orion X5",
        "Orion X5 without disassembly",
        "Orion X5 not for parts",
    ],
)
def test_included_spares_and_repaired_device_are_not_parts_sales(title):
    result = admit_record(
        record(title, description="No repair needed; includes spare parts"),
        SCHEMA,
        "Orion X5",
        assessment_profile="device",
    )
    assert result.status == "accepted"

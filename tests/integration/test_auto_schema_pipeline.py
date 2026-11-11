"""Question-derived schemas are run-local and authoritative during offline replay."""

import json
from pathlib import Path

import pytest

from unified_web_parser.core.config import JobConfig, LoadedJob
from unified_web_parser.crawling.pages import make_page
from unified_web_parser.pipeline.runner import PipelineRunner
from unified_web_parser.pipeline.schema_lifecycle import schema_hash


def field(name, kind):
    return {"name": name, "type": kind, "description": "Explicit source value", "items_type": None}


class SchemaClient:
    model = "schema-test-model"

    def __init__(self, plans=None):
        self.calls = 0
        self.plans = plans or [{"entity_name": "vacancy", "fields": [field("company", "string")]}]

    async def complete_json(self, *_args, **_kwargs):
        result = self.plans[min(self.calls, len(self.plans) - 1)]
        self.calls += 1
        return result


class Pages:
    def __init__(self):
        self.calls = 0

    async def fetch(self, url):
        self.calls += 1
        entity = {
            "@type": "JobPosting",
            "name": "Editor",
            "url": url,
            "hiringOrganization": {"name": "Publishing Lab"},
            "remote": True,
        }
        return make_page(
            url, '<script type="application/ld+json">' + json.dumps(entity) + "</script>", 200
        )


def auto_job(tmp_path):
    return LoadedJob(
        config=JobConfig(
            name="vacancies",
            sources=[{"url": "https://jobs.example/editor"}],
            search="Find remote editor jobs and their company",
            schema_generation={"enabled": True},
            ai={"routing": "rules", "cache": False},
            navigation={"mode": "rules"},
        ),
        config_path=tmp_path / "job.yaml",
        extraction_schema={},
    )


class NoNetwork:
    async def fetch(self, *_args, **_kwargs):
        pytest.fail("Replay fetched a page")

    async def complete_json(self, *_args, **_kwargs):
        pytest.fail("Replay regenerated a schema")


@pytest.mark.asyncio
async def test_generated_schema_is_used_by_extraction_and_saved_before_any_search(tmp_path):
    loaded, schema_client, pages = auto_job(tmp_path), SchemaClient(), Pages()
    report = await PipelineRunner(
        loaded,
        data_dir=tmp_path / "data",
        fetcher=pages,
        schema_client=schema_client,
        llm=NoNetwork(),
    ).run()
    assert report.status == "success", report.errors
    assert pages.calls == 1 and schema_client.calls == 2
    assert report.schema_generation["refinement"]["status"] == "refined"
    assert loaded.extraction_schema == {} and loaded.schema_path is None
    directory = Path(report.artifacts["directory"])
    generated = json.loads((directory / "schema.json").read_text(encoding="utf-8"))
    assert list(generated["properties"]) == ["title", "url", "company"]
    assert json.loads((directory / "job.json").read_text(encoding="utf-8"))["schema"] == generated
    records = [json.loads(line) for line in (directory / "records.jsonl").read_text().splitlines()]
    assert records[0]["data"] == {
        "title": "Editor",
        "url": "https://jobs.example/editor",
        "company": "Publishing Lab",
    }
    assert report.schema_generation["model"] == "schema-test-model"
    assert report.schema_generation["schema_sha256"] == schema_hash(generated)


@pytest.mark.asyncio
async def test_repeated_runs_do_not_reuse_or_mutate_previous_generated_schema(tmp_path):
    loaded = auto_job(tmp_path)
    client = SchemaClient(
        [
            {"entity_name": "vacancy", "fields": [field("company", "string")]},
            {"entity_name": "vacancy", "fields": [field("company", "string")]},
            {"entity_name": "vacancy", "fields": [field("remote", "boolean")]},
            {"entity_name": "vacancy", "fields": [field("remote", "boolean")]},
        ]
    )
    runner = PipelineRunner(
        loaded, data_dir=tmp_path / "data", fetcher=Pages(), schema_client=client, llm=NoNetwork()
    )
    first, second = await runner.run(), await runner.run()
    assert first.status == second.status == "success"
    assert first.schema_generation["schema_sha256"] != second.schema_generation["schema_sha256"]
    assert loaded.extraction_schema == {}


@pytest.mark.asyncio
async def test_replay_restores_schema_over_incompatible_current_manual_defaults_without_ai(
    tmp_path,
):
    loaded = auto_job(tmp_path)
    first = await PipelineRunner(
        loaded,
        data_dir=tmp_path / "data",
        fetcher=Pages(),
        schema_client=SchemaClient(),
        llm=NoNetwork(),
    ).run()
    manual_config = loaded.config.model_copy(
        update={
            "schema_generation": loaded.config.schema_generation.model_copy(
                update={"enabled": False}
            ),
            "schema_path": "manual.json",
            "ai": loaded.config.ai.model_copy(update={"enabled": False}),
        }
    )
    manual = loaded.model_copy(
        update={
            "config": manual_config,
            "extraction_schema": {
                "type": "object",
                "required": ["incompatible"],
                "properties": {"incompatible": {"type": "integer"}},
            },
        }
    )
    replay = await PipelineRunner(
        manual, data_dir=tmp_path / "data", fetcher=NoNetwork(), schema_client=NoNetwork()
    ).run(replay_dir=Path(first.artifacts["directory"]))
    assert replay.status == "success", replay.errors
    assert replay.schema_generation["status"] == "replayed"
    assert replay.schema_generation["network_used"] is False
    assert replay.schema_generation["schema_sha256"] == first.schema_generation["schema_sha256"]
    assert replay.records_written == 1
    assert "schema_generation" not in replay.ai.get("stages", {})


@pytest.mark.asyncio
async def test_failed_generation_never_falls_back_to_old_schema_or_fetches(tmp_path):
    loaded, pages = auto_job(tmp_path), Pages()
    loaded = loaded.model_copy(update={"extraction_schema": {"type": "object"}})
    report = await PipelineRunner(
        loaded,
        data_dir=tmp_path / "data",
        fetcher=pages,
        schema_client=SchemaClient([{"arbitrary": "invalid plan"}]),
        llm=NoNetwork(),
    ).run()
    assert report.status == "failed"
    assert pages.calls == 0
    assert report.schema_generation["status"] == "failed"
    assert any(error["stage"] == "schema_generation" for error in report.errors)


@pytest.mark.asyncio
async def test_corrupted_saved_schema_fails_offline_without_regeneration(tmp_path):
    loaded = auto_job(tmp_path)
    first = await PipelineRunner(
        loaded,
        data_dir=tmp_path / "data",
        fetcher=Pages(),
        schema_client=SchemaClient(),
        llm=NoNetwork(),
    ).run()
    directory = Path(first.artifacts["directory"])
    (directory / "schema.json").write_text('{"$ref":"https://remote.example/schema"}')
    report = await PipelineRunner(
        loaded, data_dir=tmp_path / "data", fetcher=NoNetwork(), schema_client=NoNetwork()
    ).run(replay_dir=directory)
    assert report.status == "failed"
    assert report.schema_generation["status"] == "failed"


@pytest.mark.asyncio
async def test_no_ai_auto_generation_fails_before_fetch_even_with_injected_client(tmp_path):
    loaded, client, pages = auto_job(tmp_path), SchemaClient(), Pages()
    loaded = loaded.model_copy(
        update={
            "config": loaded.config.model_copy(
                update={"ai": loaded.config.ai.model_copy(update={"enabled": False})}
            )
        }
    )
    report = await PipelineRunner(
        loaded, data_dir=tmp_path / "data", fetcher=pages, schema_client=client
    ).run()
    assert report.status == "failed" and client.calls == pages.calls == 0

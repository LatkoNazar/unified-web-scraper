"""Independent checks for run-local schemas, replay, CLI precedence, and real client budgets."""

import hashlib
import json
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from unified_web_parser.core.config import JobConfig, LoadedJob
from unified_web_parser.crawling.pages import make_page
from unified_web_parser.interfaces.cli import _overrides, _parser
from unified_web_parser.pipeline.runner import PipelineRunner
from unified_web_parser.providers.search import SearchBatch, SearchResult
from unified_web_parser.storage import DatasetStore

BOAT_URL = "https://marine.example/boat-one"
PLAN = {
    "entity_name": "Sailing boat",
    "fields": [
        {
            "name": "berth_count",
            "type": "integer",
            "description": "Explicit number of berths",
            "items_type": None,
        },
        {
            "name": "hull_material",
            "type": "string",
            "description": "Stated hull material",
            "items_type": None,
        },
        {
            "name": "maintenance_notes",
            "type": "array",
            "description": "Listed maintenance notes",
            "items_type": "string",
        },
    ],
}
MANUAL = {
    "type": "object",
    "additionalProperties": False,
    "required": ["legacy_field"],
    "properties": {"legacy_field": {"type": "string"}},
}


def complete_wire_plan(value):
    """A provider fixture supplies the metadata required by the new wire contract."""
    value = deepcopy(value)
    value["fields"] = [
        {
            "name": "title",
            "type": "string",
            "description": "Observed title",
            "items_type": None,
            "title": "Title",
            "unit": None,
            "display_role": "text",
            "format": None,
        },
        {
            "name": "url",
            "type": "string",
            "description": "Observed URL",
            "items_type": None,
            "title": "Source URL",
            "unit": None,
            "display_role": "link",
            "format": None,
        },
        *value["fields"],
    ]
    for item in value["fields"]:
        item.setdefault("title", item["name"].replace("_", " ").capitalize())
        for metadata in ("unit", "display_role", "format"):
            item.setdefault(metadata, None)
    value["display_columns"] = [item["name"] for item in value["fields"]][:8]
    return value


def job(tmp_path, *, enabled=True, calls=8, automatic=True, discovery=True):
    return LoadedJob(
        config=JobConfig.model_validate(
            {
                "name": "generated-schema-review",
                "schema_path": "legacy.json",
                "search": "Find sailing boats with hull material, berths and maintenance records",
                "sources": [] if discovery else [{"url": BOAT_URL}],
                "schema_generation": {"enabled": automatic, "max_fields": 5},
                "discovery": {
                    "enabled": discovery,
                    "query_mode": "literal",
                    "search_results": 1,
                    "max_queries": 1,
                    "entrypoint": "result_url",
                },
                "navigation": {"mode": "rules"},
                "ai": {"enabled": enabled, "max_calls": calls, "routing": "rules"},
                "crawl": {"max_pages": 1, "delay": 0},
            }
        ),
        config_path=tmp_path / "job.yaml",
        schema_path=tmp_path / "legacy.json",
        extraction_schema=deepcopy(MANUAL),
    )


class BoatPage:
    async def fetch(self, url):
        return make_page(
            url,
            '<script type="application/ld+json">'
            + json.dumps(
                {
                    "@type": "Product",
                    "name": "Sailing boat",
                    "url": url,
                    "berth_count": 4,
                    "hull_material": "wood",
                    "description": "Maintenance records: hull inspected.",
                }
            )
            + "</script>",
            200,
        )


class NoNetwork:
    async def fetch(self, *_args, **_kwargs):
        pytest.fail("Unexpected fetch")

    async def search(self, *_args, **_kwargs):
        pytest.fail("Unexpected web search")

    async def complete_json(self, *_args, **_kwargs):
        pytest.fail("Unexpected schema/query generation or extraction")


@pytest.mark.asyncio
async def test_one_generated_schema_reaches_every_stage_and_replay_overrides_incompatible_manual(
    tmp_path,
):
    supplied = job(tmp_path)
    config = supplied.config.model_dump(mode="json")
    config["discovery"]["query_mode"] = "ai"
    config["navigation"]["mode"] = "ai"
    config["ai"]["routing"] = "ai"
    supplied = supplied.model_copy(update={"config": JobConfig.model_validate(config)})
    before = deepcopy(supplied.extraction_schema)
    observed = {}
    data_dir = tmp_path / "data"

    class Generator:
        async def complete_json(self, *_args, **_kwargs):
            return deepcopy(PLAN)

    class Query:
        async def complete_json(self, _instruction, payload, _schema, **_kwargs):
            observed["query"] = deepcopy(payload["target_schema"])
            return {"queries": ["Sailing boat"], "site_search": "Sailing boat"}

    class Search:
        async def search(self, query, **_kwargs):
            run = data_dir / "runs" / "generated"
            observed["persisted"] = json.loads((run / "schema.json").read_text())
            assert json.loads((run / "schema-generation.json").read_text())["status"] == "generated"
            assert json.loads((run / "job.json").read_text())["schema"] == observed["persisted"]
            return SearchBatch(
                query=query,
                fetched_at=datetime.now(UTC),
                results=[
                    SearchResult(url=BOAT_URL, citation_order=1),
                ],
            )

    class Navigation:
        async def decide(self, state, questions):
            observed["navigation"] = deepcopy(state["target_schema"])
            return {
                name: {
                    "type": "choice",
                    "choice": "extract"
                    if name == "next_action"
                    else "yes"
                    if name == "extract_current"
                    else "ignore",
                }
                for name in questions
            }

    class Routing:
        model = "review-router"

        async def decide(self, state, questions):
            observed["routing"] = deepcopy(state["target_schema"])
            return {
                name: {
                    "type": "choice",
                    "choice": "json_ld" if "json_ld" in question["criteria"] else "skip",
                }
                for name, question in questions.items()
            }

    class Extraction:
        async def complete_json(self, _instruction, payload, schema, **_kwargs):
            observed["extraction"] = deepcopy(schema)
            observed["extraction_hash"] = payload["schema_sha256"]
            return {"records": [{"maintenance_notes": ["Hull inspected"]}]}

    first = await PipelineRunner(
        supplied,
        data_dir=data_dir,
        fetcher=BoatPage(),
        schema_client=Generator(),
        query_client=Query(),
        search_client=Search(),
        navigation_client=Navigation(),
        decision_client=Routing(),
        llm=Extraction(),
    ).run(run_id="generated")
    assert first.status == "success", first.errors
    effective = observed["persisted"]
    assert set(effective["properties"]) == {
        "title",
        "url",
        "berth_count",
        "hull_material",
        "maintenance_notes",
    }
    assert all(observed[stage] == effective for stage in ("query", "navigation", "routing"))
    patch = observed["extraction"]["properties"]["records"]["items"]
    assert set(patch["properties"]) == {"maintenance_notes"}
    extraction_fingerprint = hashlib.sha256(
        json.dumps(effective, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    assert observed["extraction_hash"] == extraction_fingerprint
    record = DatasetStore(data_dir).list_records()[0]
    assert record["data"] == {
        "title": "Sailing boat",
        "url": BOAT_URL,
        "berth_count": 4,
        "hull_material": "wood",
        "maintenance_notes": ["Hull inspected"],
    }
    assert supplied.extraction_schema == before == MANUAL

    incompatible = job(tmp_path, enabled=False, automatic=False, discovery=False)
    replay = await PipelineRunner(
        incompatible,
        data_dir=data_dir,
        fetcher=NoNetwork(),
        schema_client=NoNetwork(),
        query_client=NoNetwork(),
        search_client=NoNetwork(),
    ).run(replay_dir=Path(first.artifacts["directory"]))
    assert replay.status == "success", replay.errors
    assert replay.schema_generation["status"] == "replayed"
    assert replay.schema_generation["network_used"] is False
    assert json.loads(Path(replay.artifacts["schema"]).read_text()) == effective
    assert incompatible.extraction_schema == MANUAL


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled,calls", [(False, 5), (True, 0)])
async def test_auto_schema_cannot_silently_fallback_or_search_with_ai_disabled_or_no_budget(
    tmp_path,
    enabled,
    calls,
):
    report = await PipelineRunner(
        job(tmp_path, enabled=enabled, calls=calls),
        data_dir=tmp_path / "data",
        fetcher=NoNetwork(),
        schema_client=NoNetwork(),
        search_client=NoNetwork(),
    ).run()
    assert report.status == "failed"
    assert report.pages_attempted == 0
    assert report.schema_generation["status"] == "failed"
    assert any(error["stage"] == "schema_generation" for error in report.errors)


@pytest.mark.parametrize(
    "arguments,automatic",
    [
        (["discover", "Find sailing boats"], True),
        (["discover", "Find sailing boats", "--config", "configs/jobs/web.yaml"], False),
        (["discover", "Find sailing boats", "--config", "custom.yaml", "--auto-schema"], True),
        (["discover", "Find sailing boats", "--fixed-schema"], False),
    ],
)
def test_cli_default_auto_schema_depends_on_explicit_profile_provenance(
    tmp_path, arguments, automatic
):
    supplied = job(tmp_path, automatic=False, discovery=False)
    selected = _overrides(supplied, _parser().parse_args(arguments))
    assert selected.config.schema_generation.enabled is automatic
    assert selected.config.sources == []
    assert supplied.config.schema_generation.enabled is False


@pytest.mark.asyncio
async def test_production_schema_generation_shares_budget_and_warm_schema_cache_is_free(
    tmp_path,
    monkeypatch,
):
    from unified_web_parser.providers import openrouter, search

    requests, budgets = [], []
    data_dir = tmp_path / "data"

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        native_search = bool(body.get("tools"))
        if native_search:
            assert body["tool_choice"] == "required"
            assert (data_dir / "runs" / "warm" / "schema.json").is_file()
            message = {"annotations": [{"type": "url_citation", "url_citation": {"url": BOAT_URL}}]}
        else:
            message = {"content": json.dumps(complete_wire_plan(PLAN))}
        usage = {"prompt_tokens": 10, "completion_tokens": 10, "cost": 0.0001}
        if native_search:
            usage["server_tool_use_details"] = {"web_search_requests": 1}
        return httpx.Response(
            200,
            json={
                "choices": [{"message": message}],
                "usage": usage,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:

        class LocalChat(openrouter.OpenRouterClient):
            def __init__(self, **kwargs):
                budgets.append(kwargs["budget"])
                super().__init__(client=http, **kwargs)

        class LocalSearch(search.OpenRouterSearchClient):
            def __init__(self, **kwargs):
                budgets.append(kwargs["budget"])
                super().__init__(client=http, **kwargs)

        monkeypatch.setattr(openrouter, "OpenRouterClient", LocalChat)
        monkeypatch.setattr(search, "OpenRouterSearchClient", LocalSearch)
        monkeypatch.setenv("OPENROUTER_API_KEY", "schema-review-dummy")
        monkeypatch.setenv("OPENROUTER_MODEL", "qwen/qwen3.7-flash")
        supplied = job(tmp_path, calls=1)
        cold = await PipelineRunner(supplied, data_dir=data_dir, fetcher=BoatPage()).run(
            run_id="cold"
        )
        assert cold.status == "failed"
        assert cold.schema_generation["status"] == "generated"
        assert Path(cold.artifacts["schema"]).is_file()
        assert cold.ai["budget_used"] == cold.ai["calls"] == len(requests) == 1
        assert len(budgets) == 2 and budgets[0] is budgets[1]
        warm = await PipelineRunner(supplied, data_dir=data_dir, fetcher=BoatPage()).run(
            run_id="warm"
        )
    assert warm.status == "partial", warm.errors
    assert warm.records_written == 1
    assert len(requests) == 2
    assert warm.ai["budget_used"] == warm.ai["calls"] == 1
    assert warm.ai["stages"]["schema_generation"]["cache_hits"] == 1
    assert warm.ai["stages"]["schema_generation"]["calls"] == 0
    assert warm.ai["stages"]["schema_generation"]["cost_usd"] == 0
    assert all(budget is budgets[2] for budget in budgets[2:])


@pytest.mark.asyncio
async def test_invalid_paid_field_plan_stops_before_search_and_keeps_attempt_accounting(
    tmp_path,
    monkeypatch,
):
    from unified_web_parser.providers import openrouter

    requests = []

    def respond(request):
        requests.append(request)
        invalid = complete_wire_plan(PLAN)
        invalid["fields"][0]["maximum"] = 5
        return httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(invalid)}}]}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        original = openrouter.OpenRouterClient

        def client(**kwargs):
            return original(client=http, **kwargs)

        monkeypatch.setattr(openrouter, "OpenRouterClient", client)
        monkeypatch.setenv("OPENROUTER_API_KEY", "schema-review-dummy")
        report = await PipelineRunner(
            job(tmp_path, calls=1),
            data_dir=tmp_path / "data",
            fetcher=NoNetwork(),
            search_client=NoNetwork(),
        ).run()
    assert report.status == "failed"
    assert report.pages_attempted == 0
    assert len(requests) == report.ai["budget_used"] == report.ai["calls"] == 1
    assert report.schema_generation["status"] == "failed"
    assert any(error["stage"] == "schema_generation" for error in report.errors)

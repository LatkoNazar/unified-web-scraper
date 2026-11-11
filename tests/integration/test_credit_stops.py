"""Credit stops and user cancellation retain completed records and truthful live usage."""

import asyncio
import json
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from unified_web_parser.core.config import JobConfig, LoadedJob
from unified_web_parser.crawling.pages import make_page
from unified_web_parser.interfaces.api import create_app
from unified_web_parser.interfaces.live import LiveRunManager
from unified_web_parser.pipeline.report import RunReport
from unified_web_parser.pipeline.runner import PipelineRunner
from unified_web_parser.providers.decisions import OpenRouterDecisionsClient
from unified_web_parser.providers.openrouter import OpenRouterClient
from unified_web_parser.providers.search import OpenRouterSearchClient
from unified_web_parser.storage import DatasetStore

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["title", "url", "metric"],
    "properties": {
        "title": {"type": "string"},
        "url": {"type": "string"},
        "metric": {"type": ["number", "null"]},
    },
}


def job(tmp_path, *, stage="extraction"):
    config = {
        "name": "credits",
        "schema_path": "schema.json",
        "sources": [{"url": f"https://science.example/{name}"} for name in ("one", "two", "three")],
        "ai": {"routing": "ai" if stage == "routing" else "rules", "max_calls": 8, "cache": False},
        "navigation": {"mode": "ai" if stage == "navigation" else "rules"},
        "crawl": {"concurrency": 1, "max_pages": 3, "max_records": 3, "delay": 0},
        "assessment": ["is_relevant"],
    }
    if stage in {"schema", "refinement", "delayed_refinement"}:
        config.update(search="Find entities", schema_generation={"enabled": True})
        if stage == "delayed_refinement":
            config["sources"] = config["sources"][:1]
    if stage == "search":
        config.update(
            search="Find entities", sources=[], discovery={"enabled": True, "query_mode": "literal"}
        )
    return LoadedJob(
        config=JobConfig.model_validate(config),
        config_path=tmp_path / "job.yaml",
        extraction_schema=deepcopy(SCHEMA),
    )


class Pages:
    def __init__(self):
        self.urls = []

    async def fetch(self, url):
        self.urls.append(url)
        entity = {
            "@type": "Thing",
            "name": url.rsplit("/", 1)[-1],
            "url": url,
            "description": "An observed scientific entity with a measured attribute.",
        }
        return make_page(
            url, '<script type="application/ld+json">' + json.dumps(entity) + "</script>", 200
        )


def credit_response(*, source="openrouter_key_limit", status=402):
    return httpx.Response(
        status,
        json={
            "error": {
                "code": 402,
                "message": "private-secret-not-for-logs",
                "metadata": {"limit_source": source},
            }
        },
    )


def chat_response(value, cost=0.01):
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": json.dumps(value)}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 4, "cost": cost},
        },
    )


def initial_plan():
    return {
        "entity_name": "Entities",
        "fields": [
            {
                "name": name,
                "type": "string",
                "description": "Observed identity",
                "items_type": None,
                "title": name.capitalize(),
                "unit": None,
                "display_role": None,
                "format": None,
            }
            for name in ("title", "url")
        ],
        "display_columns": ["title", "url"],
    }


@pytest.mark.asyncio
async def test_extraction_credit_stop_saves_prior_records_and_publishes_cost_before_record(
    tmp_path,
):
    sent, events, pages = [], [], Pages()

    def respond(request):
        sent.append(request)
        return chat_response({"records": [{"metric": 5}]}) if len(sent) == 1 else credit_response()

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        model = OpenRouterClient("key", "model", client=http)
        report = await PipelineRunner(
            job(tmp_path),
            data_dir=tmp_path / "data",
            fetcher=pages,
            llm=model,
            on_event=lambda kind, data: events.append((kind, deepcopy(data))),
        ).run()
    assert report.status == "stopped" and report.stop_reason == "credits_exhausted"
    assert report.credit_limit_source == "openrouter_key_limit" and report.errors == []
    assert report.records_written == 1 and len(sent) == len(pages.urls) == 2
    assert report.ai["known_cost_usd"] == 0.01 and not report.ai["cost_complete"]
    assert report.ai["cost_pending_calls"] == 1 and "cost_usd" not in report.ai
    record_index = next(i for i, (kind, _data) in enumerate(events) if kind == "record")
    assert any(
        kind == "usage" and data["ai"].get("known_cost_usd") == 0.01
        for kind, data in events[:record_index]
    )
    assert "private-secret" not in report.model_dump_json()
    rows = DatasetStore(tmp_path / "data").list_records()
    assert len(rows) == 1 and rows[0]["data"]["metric"] == 5
    assert rows[0]["assessments"] and rows[0]["provenance"]["source_url"].endswith("/one")
    directory = Path(report.artifacts["directory"])
    assert len((directory / "records.jsonl").read_text(encoding="utf-8").splitlines()) == 1
    outcomes = report.navigation["source_outcomes"]
    assert outcomes[0]["records_added"] == 1
    assert all(
        source["stop_reason"] == "credits_exhausted" and source["pending_states"] == 1
        for source in outcomes[1:]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stage", ["navigation", "routing", "schema", "refinement", "delayed_refinement", "search"]
)
async def test_credit_signal_escapes_all_fallbacks_without_extra_requests_or_error_storm(
    tmp_path, stage
):
    sent, pages = [], Pages()

    def respond(request):
        sent.append(request)
        if stage in {"refinement", "delayed_refinement"} and len(sent) == 1:
            return chat_response(initial_plan(), 0.001)
        return credit_response(source="openrouter_in_flight_budget", status=200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        model = OpenRouterClient("key", "qwen/qwen3.7-flash", client=http)
        options = {}
        if stage in {"navigation", "routing"}:
            options[{"navigation": "navigation_client", "routing": "decision_client"}[stage]] = (
                OpenRouterDecisionsClient("key", "model-decisions", client=http)
            )
        elif stage in {"schema", "refinement", "delayed_refinement"}:
            options["schema_client"] = model
        else:
            options["search_client"] = OpenRouterSearchClient("key", "model", client=http)
        report = await PipelineRunner(
            job(tmp_path, stage=stage), data_dir=tmp_path / "data", fetcher=pages, **options
        ).run()
    assert report.status == "stopped" and report.stop_reason == "credits_exhausted", report.errors
    assert report.records_written == 0 and report.errors == []
    assert "Тимчасово" in report.stop_message
    assert len(sent) == (2 if stage in {"refinement", "delayed_refinement"} else 1)
    assert len(pages.urls) == (
        0 if stage in {"search", "schema"} else 3 if stage == "refinement" else 1
    )
    assert Path(report.artifacts["jsonl"]).read_text(encoding="utf-8") == ""
    assert "private-secret" not in report.model_dump_json()
    if stage == "search":
        assert report.discovery["query_outcomes"][0]["status"] == "stopped"
    if stage in {"refinement", "delayed_refinement"}:
        assert report.schema_generation["refinement"]["status"] == "stopped"
    if report.navigation:
        assert report.navigation["coverage_status"] == "incomplete"
        assert "credits_exhausted" in report.navigation["incomplete_reasons"]


@pytest.mark.asyncio
async def test_api_sse_and_recovered_snapshot_expose_controlled_credit_stop(tmp_path):
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    (profiles / "schema.json").write_text(json.dumps(SCHEMA), encoding="utf-8")
    (profiles / "custom.yaml").write_text(
        "name: credits\nschema_path: schema.json\n"
        "sources: [{url: https://science.example/one}]\n"
        "ai: {routing: rules, cache: false}\nnavigation: {mode: rules}\n"
        "crawl: {concurrency: 1, delay: 0}\n",
        encoding="utf-8",
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: credit_response())
    ) as provider:

        def factory(loaded, **kwargs):
            return PipelineRunner(
                loaded,
                fetcher=Pages(),
                llm=OpenRouterClient("key", "model", client=provider),
                **kwargs,
            )

        app = create_app(tmp_path / "data", profiles, runner_factory=factory)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as api:
            started = await api.post(
                "/runs",
                json={
                    "query": "Entity",
                    "profile": "custom",
                    "sources": ["https://science.example/one"],
                },
            )
            run_id = started.json()["run_id"]
            await app.state.run_manager.wait(run_id)
            state = (await api.get(f"/runs/{run_id}")).json()
            assert state["status"] == "stopped" and state["stop_reason"] == "credits_exhausted"
            assert state["records_persisted"] == 0 and state["report"]["status"] == "stopped"
            stream = (await api.get(f"/runs/{run_id}/events")).text
            assert (
                '"status": "stopped"' in stream and '"stop_reason": "credits_exhausted"' in stream
            )
        await app.state.run_manager.close()
    recovered = LiveRunManager(tmp_path / "data").snapshot(run_id)
    assert recovered["status"] == "stopped" and recovered["stop_reason"] == "credits_exhausted"


@pytest.mark.asyncio
async def test_user_cancel_during_provider_call_persists_completed_records_without_new_calls(
    tmp_path,
):
    second_started, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def respond(_request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return chat_response({"records": [{"metric": 5}]})
        second_started.set()
        await release.wait()
        return chat_response({"records": [{"metric": 6}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        runner = PipelineRunner(
            job(tmp_path),
            data_dir=tmp_path / "data",
            fetcher=Pages(),
            llm=OpenRouterClient("key", "model", client=http),
        )
        task = asyncio.create_task(runner.run(run_id="cancelled"))
        await second_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    summary = json.loads(
        (tmp_path / "data" / "runs" / "cancelled" / "summary.json").read_text(encoding="utf-8")
    )
    assert summary["status"] == "cancelled" and summary["stop_reason"] == "user_cancelled"
    assert summary["records_written"] == 1 and summary["errors"] == [] and calls == 2
    assert summary["ai"]["known_cost_usd"] == 0.01 and not summary["ai"]["cost_complete"]
    assert summary["ai"]["in_flight_calls"] == 0
    assert len(DatasetStore(tmp_path / "data").list_records()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["success", "stopped", "cancelled"])
async def test_stop_after_task_finishes_cannot_revert_terminal_state(tmp_path, status):
    class InstantRunner:
        def __init__(self, *_args, **_kwargs):
            pass

        async def run(self, *, run_id):
            return RunReport(
                run_id=run_id,
                job_name="instant",
                started_at=datetime.now(UTC),
                status=status,
            )

    manager = LiveRunManager(tmp_path / "data", runner_factory=InstantRunner)
    run_id = manager.submit(job(tmp_path), "Entity")["run_id"]
    # _execute finished, while its scheduled _done callback has not run yet.
    await asyncio.sleep(0)
    assert manager.tasks[run_id].done()
    assert manager.cancel(run_id)["status"] == status
    await asyncio.sleep(0)
    assert manager.snapshot(run_id)["status"] == status
    assert manager.snapshot(run_id)["errors"] == []
    await manager.close()


@pytest.mark.asyncio
async def test_credit_stop_audits_unattempted_search_variants(tmp_path, monkeypatch):
    from unified_web_parser.discovery.search import QueryPlan

    async def planned(*_args, **_kwargs):
        return QueryPlan(queries=("first", "second", "third"), site_search="Entity")

    monkeypatch.setattr("unified_web_parser.discovery.search.plan_queries", planned)
    sent = []

    def respond(request):
        sent.append(request)
        return credit_response()

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        report = await PipelineRunner(
            job(tmp_path, stage="search"),
            data_dir=tmp_path / "data",
            fetcher=Pages(),
            search_client=OpenRouterSearchClient("key", "model", client=http),
        ).run()
    assert report.status == "stopped" and len(sent) == 1
    assert report.discovery["query_outcomes"] == [
        {"query": "first", "status": "stopped", "reason": "credits_exhausted"},
        {"query": "second", "status": "not_attempted", "reason": "credits_exhausted"},
        {"query": "third", "status": "not_attempted", "reason": "credits_exhausted"},
    ]

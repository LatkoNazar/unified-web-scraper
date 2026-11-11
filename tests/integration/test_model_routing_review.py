"""Independent mock-transport checks of stage wiring and the shared AI budget."""

import asyncio
import json

import httpx
import pytest

from unified_web_parser.core.config import JobConfig, LoadedJob
from unified_web_parser.core.exceptions import ProviderError
from unified_web_parser.crawling.pages import make_page
from unified_web_parser.pipeline.runner import PipelineRunner
from unified_web_parser.providers import decisions, openrouter


class ProductFetcher:
    async def fetch(self, url):
        return make_page(
            url,
            '<script type="application/ld+json">'
            '{"@type":"Product","name":"Camera","price":100}</script>',
            200,
        )


def loaded_job(tmp_path, *, max_calls=2):
    return LoadedJob(
        config=JobConfig.model_validate(
            {
                "name": "model-routing-review",
                "sources": [{"url": "https://example.org/products/camera"}],
                "schema_path": "schema.json",
                "ai": {"routing": "ai", "max_calls": max_calls},
                "crawl": {"max_pages": 1, "max_depth": 0, "delay": 0},
            }
        ),
        config_path=tmp_path / "job.yaml",
        schema_path=tmp_path / "schema.json",
        extraction_schema={
            "type": "object",
            "required": ["title"],
            "properties": {
                "title": {"type": "string"},
                "price": {"type": "number"},
                "condition": {"type": ["string", "null"]},
            },
        },
    )


def wire_clients(monkeypatch, http):
    chat_class = openrouter.OpenRouterClient
    decision_class = decisions.OpenRouterDecisionsClient
    created = {}

    def chat(**kwargs):
        created["extraction"] = chat_class(**kwargs, client=http)
        return created["extraction"]

    def router(**kwargs):
        created["routing"] = decision_class(**kwargs, client=http)
        return created["routing"]

    monkeypatch.setattr(openrouter, "OpenRouterClient", chat)
    monkeypatch.setattr(decisions, "OpenRouterDecisionsClient", router)
    monkeypatch.setenv("OPENROUTER_API_KEY", "review-dummy-never-used-outside-mock")
    monkeypatch.delenv("OPENROUTER_MODEL", raising=False)
    monkeypatch.delenv("OPENROUTER_DECISION_MODEL", raising=False)
    return created


def provider_response(request, requests):
    payload = json.loads(request.content)
    requests.append((request.url.path, payload))
    if request.url.path == "/api/alpha/decisions":
        assert set(payload) == {"model", "state", "questions"}
        return httpx.Response(
            200,
            json={
                "model": payload["model"],
                "answers": {
                    name: {
                        "type": "choice",
                        "choice": "json_ld" if "json_ld" in question["criteria"] else "skip",
                        "confidence": 0.95,
                    }
                    for name, question in payload["questions"].items()
                },
                "usage": {"input_tokens": 11, "output_tokens": 3},
            },
        )
    assert request.url.path == "/api/v1/chat/completions"
    assert payload["response_format"] == {"type": "json_object"}
    assert "JSON Schema" in payload["messages"][0]["content"]
    assert '"condition"' in payload["messages"][0]["content"]
    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {"records": [{"title": "Camera", "price": 100, "condition": "used"}]}
                        )
                    }
                }
            ],
            "usage": {"prompt_tokens": 20, "completion_tokens": 5},
        },
    )


@pytest.mark.parametrize("max_calls, expected_status", [(1, "partial"), (2, "success")])
def test_native_routing_and_qwen_extraction_share_attempt_budget(
    tmp_path, monkeypatch, max_calls, expected_status
):
    requests = []

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: provider_response(request, requests))
        ) as http:
            created = wire_clients(monkeypatch, http)
            report = await PipelineRunner(
                loaded_job(tmp_path, max_calls=max_calls),
                data_dir=tmp_path / "data",
                fetcher=ProductFetcher(),
            ).run()
            assert created["routing"].budget is created["extraction"].budget
            assert report.status == expected_status, report.errors
            assert report.records_written == 1
            assert report.ai["calls"] == report.ai["budget_used"] == max_calls
            assert report.ai["stages"]["routing"]["model"] == "openai/gpt-6-luna-decisions"
            assert report.ai["stages"]["extraction"]["model"] == "qwen/qwen3.7-flash"

    asyncio.run(run())
    assert len(requests) == max_calls
    assert requests[0][0] == "/api/alpha/decisions"


def test_second_run_uses_both_stage_caches_without_consuming_budget(tmp_path, monkeypatch):
    requests = []

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: provider_response(request, requests))
        ) as http:
            wire_clients(monkeypatch, http)
            job = loaded_job(tmp_path)
            reports = [
                await PipelineRunner(
                    job, data_dir=tmp_path / "data", fetcher=ProductFetcher()
                ).run()
                for _ in range(2)
            ]
            assert all(report.status == "success" for report in reports)
            assert reports[0].ai["budget_used"] == 2
            assert reports[1].ai["budget_used"] == 0
            assert reports[1].ai["calls"] == 0
            assert reports[1].ai["stages"]["routing"]["cache_hits"] == 1
            assert reports[1].ai["stages"]["extraction"]["cache_hits"] == 1

    asyncio.run(run())
    assert len(requests) == 2


def test_qwen_json_object_response_still_enforces_target_schema():
    async def run():
        transport = httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={"choices": [{"message": {"content": '{"count":"wrong type"}'}}]},
            )
        )
        async with httpx.AsyncClient(transport=transport) as http:
            client = openrouter.OpenRouterClient("review-dummy", "qwen/qwen3.7-flash", client=http)
            with pytest.raises(ProviderError, match="invalid structured JSON"):
                await client.complete_json(
                    "Extract count",
                    {},
                    {
                        "type": "object",
                        "required": ["count"],
                        "properties": {"count": {"type": "integer"}},
                    },
                )

    asyncio.run(run())


def test_injected_legacy_llm_does_not_create_network_clients(tmp_path, monkeypatch):
    class LegacyLLM:
        model = "review/legacy"

        async def complete_json(self, _system, payload, _schema, name):
            if name == "route_candidates":
                return {
                    "targets": [
                        {
                            "candidate_id": payload["candidates"][0]["candidate_id"],
                            "strategy": "json_ld",
                            "reason": "fixture",
                        }
                    ]
                }
            return {"records": [{"title": "Camera", "price": 100, "condition": "used"}]}

    def unexpected_client(**_kwargs):
        raise AssertionError("An injected legacy LLM must not create network clients")

    monkeypatch.setenv("OPENROUTER_API_KEY", "review-dummy")
    monkeypatch.setattr(openrouter, "OpenRouterClient", unexpected_client)
    monkeypatch.setattr(decisions, "OpenRouterDecisionsClient", unexpected_client)
    report = asyncio.run(
        PipelineRunner(
            loaded_job(tmp_path),
            data_dir=tmp_path / "data",
            fetcher=ProductFetcher(),
            llm=LegacyLLM(),
        ).run()
    )
    assert report.status == "success", report.errors
    assert report.records_written == 1

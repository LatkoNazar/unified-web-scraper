"""Independent checks for honest cost accounting and extraction invariants."""

import asyncio
import json

import httpx
import pytest

from unified_web_parser.core.config import JobConfig, LoadedJob
from unified_web_parser.core.models import CandidateBlock, CandidateKind, ExtractionStrategy
from unified_web_parser.crawling.pages import make_page
from unified_web_parser.extraction.schema import SchemaExtractor
from unified_web_parser.pipeline.runner import PipelineRunner
from unified_web_parser.providers.decisions import OpenRouterDecisionsClient
from unified_web_parser.providers.openrouter import OpenRouterClient


def test_native_missing_cost_is_unknown_instead_of_measured_zero():
    async def run():
        transport = httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={
                    "model": "openai/gpt-6-luna-decisions",
                    "answers": {"route": {"type": "choice", "choice": "skip"}},
                    "usage": {"input_tokens": 10, "output_tokens": 1},
                },
            )
        )
        async with httpx.AsyncClient(transport=transport) as http:
            client = OpenRouterDecisionsClient(
                "review-dummy", "openai/gpt-6-luna-decisions", client=http
            )
            await client.decide(
                {},
                {
                    "route": {
                        "type": "choice",
                        "instructions": "Choose route",
                        "criteria": {"skip": "Skip"},
                    }
                },
            )
            assert client.stats["calls"] == 1
            assert "cost_usd" not in client.stats

    asyncio.run(run())


def test_reasoning_disabled_is_explicit_and_usage_remains_reported():
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"value":1}'}}],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 2,
                    "completion_tokens_details": {"reasoning_tokens": 0},
                    "cost": 0.0001,
                },
            },
        )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            client = OpenRouterClient(
                "review-dummy", "qwen/qwen3.7-flash", reasoning=False, client=http
            )
            await client.complete_json(
                "Extract",
                {},
                {"type": "object", "properties": {"value": {"type": "integer"}}},
            )
            assert client.stats["reasoning_tokens"] == 0
            assert client.stats["cost_usd"] == 0.0001

    asyncio.run(run())
    assert requests[0]["reasoning"] == {"enabled": False}
    assert "exclude" not in requests[0]["reasoning"]


def product_schema():
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["title", "price", "currency"],
        "properties": {
            "title": {"type": "string"},
            "price": {"type": "number"},
            "currency": {"type": "string"},
            "description": {"type": "string"},
            "storage_gb": {"type": ["integer", "null"]},
            "mdm_status": {
                "enum": ["claimed_clear", None],
                "description": "No iCloud does not prove absence of MDM; unknown must stay null.",
            },
            "source_conflicts": {"type": "array", "items": {"type": "string"}},
        },
    }


def product_candidate(*, candidate_id="first", description=None):
    entity = {
        "@type": "Product",
        "name": "MacBook Air M4 16 GB 512 SSD",
        "description": description or "SSD 500 GB. No iCloud. Seller states 512 in the title.",
        "offers": {"price": 49000, "priceCurrency": "UAH"},
    }
    return CandidateBlock(
        candidate_id=candidate_id,
        kind=CandidateKind.JSON_LD,
        content=json.dumps(entity, ensure_ascii=False),
    )


def test_sparse_request_retains_conflicting_source_evidence_and_unknown_mdm():
    requests = []

    def respond(request):
        payload = json.loads(request.content)
        requests.append(payload)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "records": [
                                        {
                                            "storage_gb": 512,
                                            "mdm_status": None,
                                            "source_conflicts": [
                                                "SSD: title 512 GB; description 500 GB"
                                            ],
                                        }
                                    ]
                                }
                            )
                        }
                    }
                ]
            },
        )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            client = OpenRouterClient("review-dummy", "qwen/qwen3.7-flash", client=http)
            candidate = product_candidate()
            page = make_page("https://example.org/products/mac", "unrelated rendered HTML", 200)
            record = (
                await SchemaExtractor("review", client, content_profile="commerce").extract(
                    page, candidate, ExtractionStrategy.JSON_LD, product_schema()
                )
            )[0]
            assert record.data["title"] == "MacBook Air M4 16 GB 512 SSD"
            assert record.data["price"] == 49000
            assert record.data["currency"] == "UAH"
            assert record.data["storage_gb"] == 512
            assert record.data["mdm_status"] is None
            assert record.data["source_conflicts"]
            request_data = json.loads(requests[0]["messages"][1]["content"])
            assert request_data["evidence"] == candidate.content
            assert "candidate_id" not in request_data
            assert "deterministic_records" not in request_data
            patch_envelope = json.loads(
                requests[0]["messages"][0]["content"].split("full JSON Schema: ", 1)[1]
            )
            fields = patch_envelope["properties"]["records"]["items"]["properties"]
            assert set(fields) == {"storage_gb", "mdm_status", "source_conflicts"}
            assert "No iCloud" in fields["mdm_status"]["description"]

    asyncio.run(run())


def test_sparse_cache_ignores_page_ids_but_invalidates_source_evidence_schema_and_model(tmp_path):
    calls = []

    def respond(request):
        calls.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": '{"records":[{"storage_gb":512,"mdm_status":null,'
                            '"source_conflicts":[]}]'
                            "}"
                        }
                    }
                ]
            },
        )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            client = OpenRouterClient(
                "review-dummy", "qwen/qwen3.7-flash", client=http, cache_dir=tmp_path
            )
            extractor = SchemaExtractor("review", client, content_profile="commerce")
            first = make_page("https://example.org/products/mac?utm_source=one", "HTML 1", 200)
            second = make_page("https://example.org/products/mac?utm_source=two", "HTML 2", 200)
            schema = product_schema()
            for page, candidate in [
                (first, product_candidate()),
                (second, product_candidate(candidate_id="second-page-candidate")),
            ]:
                await extractor.extract(page, candidate, ExtractionStrategy.JSON_LD, schema)
            assert len(calls) == 1
            assert client.cache_hits == 1
            changed_url = make_page("https://example.org/products/different", "HTML 2", 200)
            await extractor.extract(
                changed_url, product_candidate(), ExtractionStrategy.JSON_LD, schema
            )
            await extractor.extract(
                second,
                product_candidate(description="Changed source description"),
                ExtractionStrategy.JSON_LD,
                schema,
            )
            changed_schema = {**schema, "description": "Changed job meaning"}
            await extractor.extract(
                second, product_candidate(), ExtractionStrategy.JSON_LD, changed_schema
            )
            other_model = OpenRouterClient(
                "review-dummy",
                "example/other-model",
                response_mode="json_object",
                client=http,
                cache_dir=tmp_path,
            )
            await SchemaExtractor("review", other_model, content_profile="commerce").extract(
                second, product_candidate(), ExtractionStrategy.JSON_LD, schema
            )
            assert len(calls) == 5

    asyncio.run(run())


@pytest.mark.parametrize("keyword", ["allOf", "oneOf", "if", "dependentRequired"])
def test_cross_field_schema_retains_full_extraction_path(keyword):
    from unified_web_parser.extraction.enrichment import sparse_schema

    schema = product_schema()
    schema[keyword] = [] if keyword in {"allOf", "oneOf"} else {}
    assert sparse_schema(schema, [{"title": "Known"}]) is None


def test_native_routing_cache_keeps_source_url_isolation(tmp_path):
    requests = []

    class Fetcher:
        count = 0

        async def fetch(self, url):
            self.count += 1
            return make_page(
                url,
                '<script type="application/ld+json">'
                + product_candidate().content
                + f"</script><script>unrelated_dynamic_value={self.count}</script>",
                200,
            )

    class FakeExtraction:
        model = "review/fake"

        async def complete_json(self, *_args, **_kwargs):
            return {"records": [{"storage_gb": 512, "mdm_status": None, "source_conflicts": []}]}

    def respond(request):
        payload = json.loads(request.content)
        requests.append(payload)
        return httpx.Response(
            200,
            json={
                "model": "openai/gpt-6-luna-decisions",
                "answers": {
                    name: {
                        "type": "choice",
                        "choice": "json_ld" if "json_ld" in question["criteria"] else "skip",
                    }
                    for name, question in payload["questions"].items()
                },
                "usage": {"input_tokens": 10, "output_tokens": 1},
            },
        )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            router = OpenRouterDecisionsClient(
                "review-dummy",
                "openai/gpt-6-luna-decisions",
                client=http,
                cache_dir=tmp_path / "cache",
            )
            fetcher = Fetcher()
            for url in [
                "https://example.org/products/first",
                "https://example.org/products/first",
                "https://example.org/products/second",
            ]:
                loaded = LoadedJob(
                    config=JobConfig.model_validate(
                        {
                            "name": "review",
                            "sources": [{"url": url}],
                            "schema_path": "schema.json",
                            "content_profile": "commerce",
                            "assessment_profile": "device",
                            "currency_aliases": {"UAH": "UAH", "грн": "UAH"},
                            "ai": {"routing": "ai"},
                            "crawl": {"max_pages": 1, "max_depth": 0},
                        }
                    ),
                    config_path=tmp_path / "job.yaml",
                    schema_path=tmp_path / "schema.json",
                    extraction_schema=product_schema(),
                )
                report = await PipelineRunner(
                    loaded,
                    data_dir=tmp_path / "data",
                    fetcher=fetcher,
                    llm=FakeExtraction(),
                    decision_client=router,
                ).run()
                assert report.status == "success", report.errors
            assert router.calls == 2
            assert router.cache_hits == 1

    asyncio.run(run())
    assert len(requests) == 2


@pytest.mark.parametrize(
    "variant, expected_native_calls",
    [("coherent", 0), ("missing_url", 1), ("other_url", 1), ("no_description", 1), ("multiple", 1)],
)
def test_hybrid_routes_only_unambiguous_main_entity_without_native_call(
    tmp_path, variant, expected_native_calls
):
    source_url = "https://example.org/products/camera"
    entity = {
        "@type": "Product",
        "name": "Camera",
        "url": source_url,
        "description": "The main camera offered on this page",
        "offers": {"price": 100, "priceCurrency": "UAH"},
    }
    if variant == "missing_url":
        entity.pop("url")
    elif variant == "other_url":
        entity["url"] = "https://example.org/products/other-camera"
    elif variant == "no_description":
        entity.pop("description")
    entities = [entity]
    if variant == "multiple":
        entities.append({**entity, "name": "Other camera", "url": source_url + "-other"})

    class Fetcher:
        async def fetch(self, url):
            return make_page(
                url,
                '<script type="application/ld+json">'
                + json.dumps(entities)
                + "</script><main><article><h1>Camera</h1><p>100 UAH</p>"
                + "<p>The actual camera offered on this detail page.</p></article></main>",
                200,
            )

    class Extraction:
        model = "review/fake"

        async def complete_json(self, *_args, **_kwargs):
            raise AssertionError("All requested facts are present in JSON-LD")

    class Router:
        model = "review/native"
        calls = 0

        async def decide(self, _state, questions):
            self.calls += 1
            return {
                name: {
                    "type": "choice",
                    "choice": "json_ld" if "json_ld" in question["criteria"] else "skip",
                }
                for name, question in questions.items()
            }

    router = Router()
    job = LoadedJob(
        config=JobConfig.model_validate(
            {
                "name": "review",
                "sources": [{"url": source_url}],
                "schema_path": "schema.json",
                "content_profile": "commerce",
                "assessment_profile": "device",
                "currency_aliases": {"UAH": "UAH", "грн": "UAH"},
                "ai": {"routing": "hybrid"},
                "crawl": {"max_pages": 1, "max_depth": 0},
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
    report = asyncio.run(
        PipelineRunner(
            job,
            data_dir=tmp_path / "data",
            fetcher=Fetcher(),
            llm=Extraction(),
            decision_client=router,
        ).run()
    )
    assert report.status == "success", report.errors
    assert router.calls == expected_native_calls

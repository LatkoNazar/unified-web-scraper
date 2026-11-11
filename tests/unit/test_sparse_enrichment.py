import asyncio
import copy
import json
from datetime import UTC, datetime

import httpx
import pytest

from unified_web_parser.core.exceptions import ExtractionError
from unified_web_parser.core.models import CandidateBlock, CandidateKind, ExtractionStrategy, Page
from unified_web_parser.extraction.enrichment import merge_enrichment, sparse_schema
from unified_web_parser.extraction.schema import SchemaExtractor
from unified_web_parser.providers.openrouter import OpenRouterClient

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["title", "price"],
    "properties": {
        "title": {"type": "string"},
        "price": {"type": "number"},
        "description": {"type": "string"},
        "condition": {"type": ["string", "null"], "description": "Seller claim only"},
        "source_conflicts": {"type": "array", "items": {"type": "string"}},
    },
}
ENTITY = {
    "@type": "Product",
    "name": "Camera",
    "description": "Source describes a used camera",
    "price": 100,
}


def source(
    entity=None, *, page_id="snapshot-one", url="https://example.com/item/1", boilerplate=""
):
    content = json.dumps(entity or ENTITY, ensure_ascii=False)
    page = Page(
        page_id=page_id,
        url=url,
        html=boilerplate + '<script type="application/ld+json">' + content + "</script>",
        fetched_at=datetime.now(UTC),
        status_code=200,
    )
    block = CandidateBlock(
        candidate_id=page_id + ":block", kind=CandidateKind.JSON_LD, content=content
    )
    return page, block


class CaptureLLM:
    model = "example/model"

    def __init__(self, patch):
        self.patch = patch
        self.requests = []

    async def complete_json(self, system, payload, schema, name):
        self.requests.append((system, payload, schema))
        return {"records": [self.patch]}


def test_sparse_projection_requests_only_gaps_preserves_evidence_and_validates_full_result():
    client = CaptureLLM({"condition": "used", "source_conflicts": []})
    page, block = source()
    record = asyncio.run(
        SchemaExtractor("camera", client).extract(page, block, ExtractionStrategy.JSON_LD, SCHEMA)
    )[0]
    _, payload, schema = client.requests[0]
    assert set(schema["properties"]["records"]["items"]["properties"]) == {
        "condition",
        "source_conflicts",
    }
    assert schema["properties"]["records"]["items"]["properties"]["condition"]["description"] == (
        "Seller claim only"
    )
    assert set(payload) == {
        "source_url",
        "evidence",
        "schema_sha256",
        "content_profile",
        "currency_aliases",
    }
    assert payload["evidence"] == block.content
    assert record.data == {
        "title": "Camera",
        "price": 100,
        "description": ENTITY["description"],
        "condition": "used",
        "source_conflicts": [],
    }
    assert record.provenance.prompt_version == "extraction.v3"


def test_selected_long_description_is_not_truncated_for_sparse_enrichment():
    entity = {
        **ENTITY,
        "description": "Important evidence " * 2000 + "Final explicit condition: used",
    }
    page, block = source(entity)
    client = CaptureLLM({"condition": "used", "source_conflicts": []})
    asyncio.run(
        SchemaExtractor("camera", client).extract(page, block, ExtractionStrategy.JSON_LD, SCHEMA)
    )
    assert len(client.requests[0][1]["evidence"]) > 16000
    assert client.requests[0][1]["evidence"] == block.content


def test_projection_retains_existing_and_new_conflicts_without_mutating_schema():
    schema = copy.deepcopy(SCHEMA)
    base = {
        "title": "Camera",
        "price": 100,
        "description": "Source",
        "condition": "used",
        "source_conflicts": ["Existing conflict"],
    }
    projected = sparse_schema(schema, [base])
    assert set(projected["properties"]) == {"source_conflicts"}
    merged = merge_enrichment(
        base,
        {
            "title": "Invented",
            "price": 1,
            "source_conflicts": ["Existing conflict", "New conflict"],
        },
    )
    assert merged["source_conflicts"] == ["Existing conflict", "New conflict"]
    assert merged["title"] == "Camera" and merged["price"] == 100
    assert schema == SCHEMA


@pytest.mark.parametrize(
    "extra",
    [
        {"allOf": [{"required": ["title"]}]},
        {"anyOf": [{"required": ["title"]}]},
        {"oneOf": [{"required": ["title"]}]},
        {"if": {"required": ["title"]}, "then": {}},
        {"dependencies": {"title": ["price"]}},
        {"additionalProperties": True},
        {"minProperties": 2},
        {"$defs": {"condition": {"type": "string"}}},
    ],
)
def test_complex_or_open_schemas_conservatively_fall_back_to_full_extraction(extra):
    schema = {**SCHEMA, **extra}
    client = CaptureLLM(
        {
            "title": "Camera",
            "price": 100,
            "description": ENTITY["description"],
            "condition": "used",
            "source_conflicts": [],
        }
    )
    page, block = source()
    asyncio.run(
        SchemaExtractor("camera", client).extract(page, block, ExtractionStrategy.JSON_LD, schema)
    )
    properties = client.requests[0][2]["properties"]["records"]["items"]["properties"]
    assert set(properties) == set(SCHEMA["properties"])


def test_local_refs_use_full_extraction_and_original_reference_validation():
    schema = copy.deepcopy(SCHEMA)
    schema["$defs"] = {"condition": {"enum": ["used", None]}}
    schema["properties"]["condition"] = {"$ref": "#/$defs/condition"}
    client = CaptureLLM({"condition": "used", "source_conflicts": []})
    page, block = source()
    result = asyncio.run(
        SchemaExtractor("camera", client).extract(page, block, ExtractionStrategy.JSON_LD, schema)
    )
    assert result[0].data["condition"] == "used"
    items = client.requests[0][2]["properties"]["records"]["items"]
    assert "title" in items["properties"]
    assert items["properties"]["condition"]["$ref"] == (
        "#/properties/records/items/$defs/condition"
    )


def test_merged_result_must_satisfy_original_full_schema_even_when_patch_is_valid():
    schema = copy.deepcopy(SCHEMA)
    schema["properties"]["price"]["maximum"] = 50
    client = CaptureLLM({"condition": "used", "source_conflicts": []})
    page, block = source()
    with pytest.raises(ExtractionError, match="full job schema"):
        asyncio.run(
            SchemaExtractor("camera", client).extract(
                page, block, ExtractionStrategy.JSON_LD, schema
            )
        )


def test_sparse_projection_preserves_declared_json_schema_dialect():
    schema = copy.deepcopy(SCHEMA)
    schema["$schema"] = "http://json-schema.org/draft-04/schema#"
    schema["properties"]["condition"] = {"type": "number", "minimum": 0, "exclusiveMinimum": True}
    client = CaptureLLM({"condition": 1, "source_conflicts": []})
    page, block = source()
    result = asyncio.run(
        SchemaExtractor("camera", client).extract(page, block, ExtractionStrategy.JSON_LD, schema)
    )
    assert result[0].data["condition"] == 1
    assert client.requests[0][2]["properties"]["records"]["items"]["$schema"] == schema["$schema"]


def test_baseline_flag_keeps_full_output_schema_with_same_facts_and_identity():
    page, block = source()
    sparse = CaptureLLM({"condition": "used", "source_conflicts": []})
    full = CaptureLLM(
        {
            "title": "Camera",
            "price": 100,
            "description": ENTITY["description"],
            "condition": "used",
            "source_conflicts": [],
        }
    )
    first = asyncio.run(
        SchemaExtractor("camera", sparse).extract(page, block, ExtractionStrategy.JSON_LD, SCHEMA)
    )[0]
    second = asyncio.run(
        SchemaExtractor("camera", full, sparse_enrichment=False).extract(
            page, block, ExtractionStrategy.JSON_LD, SCHEMA
        )
    )[0]
    assert first.data == second.data and first.record_id == second.record_id
    assert "title" in full.requests[0][2]["properties"]["records"]["items"]["properties"]


def test_cache_survives_snapshot_boilerplate_but_not_source_evidence_schema_or_model_change(
    tmp_path,
):
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        patch = {"records": [{"condition": "used", "source_conflicts": []}]}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(patch)}}]})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            client = OpenRouterClient(
                "secret", "qwen/qwen3.7-flash", client=http, cache_dir=tmp_path
            )
            extractor = SchemaExtractor("camera", client)
            p1, c1 = source()
            first = (await extractor.extract(p1, c1, ExtractionStrategy.JSON_LD, SCHEMA))[0]
            p2, c2 = source(
                page_id="snapshot-two",
                boilerplate="<div>Changed navigation</div>",
                url="https://example.com/item/1?utm_source=tracking",
            )
            second = (await extractor.extract(p2, c2, ExtractionStrategy.JSON_LD, SCHEMA))[0]
            assert client.calls == 1 and client.cache_hits == 1
            assert first.record_id == second.record_id
            assert second.provenance.page_id == "snapshot-two"
            assert second.provenance.candidate_ids == (c2.candidate_id,)
            p3, c3 = source({**ENTITY, "description": "Changed evidence"})
            await extractor.extract(p3, c3, ExtractionStrategy.JSON_LD, SCHEMA)
            changed_schema = copy.deepcopy(SCHEMA)
            changed_schema["properties"]["title"]["description"] = "New field meaning"
            await extractor.extract(p1, c1, ExtractionStrategy.JSON_LD, changed_schema)
            alternate = OpenRouterClient(
                "secret",
                "alternate/model",
                response_mode="json_object",
                client=http,
                cache_dir=tmp_path,
            )
            await SchemaExtractor("camera", alternate).extract(
                p1, c1, ExtractionStrategy.JSON_LD, SCHEMA
            )
            p4, c4 = source(url="https://different.example/item/1")
            other = (await extractor.extract(p4, c4, ExtractionStrategy.JSON_LD, SCHEMA))[0]
            assert other.record_id != first.record_id
            assert len(requests) == 5

    asyncio.run(run())

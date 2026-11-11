import hashlib
import json
from copy import deepcopy

import httpx
import pytest
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError

from unified_web_parser.core.exceptions import ProviderError
from unified_web_parser.providers.openrouter import OpenRouterClient, RequestBudget
from unified_web_parser.schema_generation import (
    PROMPT_VERSION,
    compile_schema,
    generate_schema,
)


def field(name="price", kind="number", *, items_type=None, description="Observed amount"):
    return {"name": name, "type": kind, "items_type": items_type, "description": description}


def plan(*fields, entity_name="Observed entity"):
    return {"entity_name": entity_name, "fields": list(fields)}


def complete_wire_plan(value):
    value = deepcopy(value)
    names = {item["name"] for item in value["fields"]}
    identities = [
        field(name, "string", description="Observed identity")
        for name in ("title", "url")
        if name not in names
    ]
    value["fields"] = identities + value["fields"]
    for item in value["fields"]:
        item.setdefault("title", item["name"].replace("_", " ").capitalize())
        for metadata in ("unit", "display_role", "format"):
            item.setdefault(metadata, None)
    value.setdefault("display_columns", [item["name"] for item in value["fields"]][:8])
    return value


class PlanClient:
    def __init__(self, response):
        self.response = response
        self.requests = []

    async def complete_json(self, *args, **kwargs):
        self.requests.append((args, kwargs))
        return deepcopy(self.response)


def validator(schema):
    return Draft202012Validator(schema, format_checker=FormatChecker())


async def test_question_generates_closed_nullable_schema_without_budget_or_model_filters():
    question = "Find a used acoustic guitar under 5000 UAH with condition and listed accessories"
    client = PlanClient(
        plan(
            field("price"),
            field("currency", "string"),
            field("condition", "string"),
            field("accessories", "array", items_type="string", description="Listed accessories"),
        )
    )
    generated = await generate_schema(question, client=client, max_fields=6)
    schema = generated.extraction_schema
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert not schema["additionalProperties"]
    assert schema["required"] == list(schema["properties"])
    assert set(schema["properties"]) == {
        "title",
        "url",
        "price",
        "currency",
        "condition",
        "accessories",
    }
    assert schema["properties"]["price"]["type"] == ["number", "null"]
    assert "maximum" not in json.dumps(schema) and "enum" not in json.dumps(schema)
    record = {
        "title": "Different guitar",
        "url": "https://shop.example/items/one",
        "price": 9000,
        "currency": "UAH",
        "condition": None,
        "accessories": [],
    }
    # The request's budget is not a validation rule; a contrary observed fact survives.
    validator(schema).validate(record)
    record["price"] = -1
    validator(schema).validate(record)  # The compiler does not guess numeric semantics.
    record.update(price=None, currency=None, accessories=None)
    validator(schema).validate(record)
    instruction, payload, response_schema = client.requests[0][0]
    assert payload == {"question": question, "max_fields": 6}
    assert "primitive arrays only" in instruction
    assert "Do not turn requested subjects" in instruction
    assert "MacBook" not in instruction
    assert response_schema["additionalProperties"] is False
    assert generated.prompt_version == PROMPT_VERSION


async def test_new_wire_requires_labels_identity_and_bounded_columns_with_optional_url():
    response = complete_wire_plan(
        plan({**field("dimension"), "title": "Розмір", "unit": "cm", "display_role": "metric"})
    )
    response["fields"][0]["title"] = "Назва"
    response["fields"][1]["title"] = "Джерело"
    response["display_columns"] = ["title", "dimension"]
    client = PlanClient(response)
    generated = await generate_schema("Знайди предмети за їхніми характеристиками", client=client)
    wire = client.requests[0][0][2]
    required = set(wire["properties"]["fields"]["items"]["required"])
    assert {"title", "unit", "display_role", "format"} <= required
    Draft202012Validator(wire).validate(response)
    for broken in (
        {**response, "fields": response["fields"][1:]},
        {**response, "display_columns": ["dimension", "url"]},
        {**response, "display_columns": ["title", *[f"attribute_{i}" for i in range(8)]]},
    ):
        with pytest.raises(ValidationError):
            Draft202012Validator(wire).validate(broken)
    missing_unit = deepcopy(response)
    del missing_unit["fields"][-1]["unit"]
    with pytest.raises(ValidationError):
        Draft202012Validator(wire).validate(missing_unit)
    assert generated.extraction_schema["properties"]["title"]["title"] == "Назва"
    assert generated.extraction_schema["properties"]["dimension"]["x-unit"] == "cm"
    assert generated.extraction_schema["x-display"]["columns"] == ["title", "dimension"]
    assert compile_schema(plan(field())).extraction_schema["properties"]["price"]["type"] == [
        "number",
        "null",
    ]


@pytest.mark.parametrize(
    "mutation",
    [
        {"title": None},
        {"title": ""},
        {"title": " "},
        {"url": None},
        {"url": "/relative"},
        {"url": "not a URL"},
        {"url": "file:///tmp/x"},
        {"price": "100"},
        {"unexpected": True},
    ],
)
def test_generated_schema_enforces_record_identity_types_and_closed_properties(mutation):
    schema = compile_schema(plan(field())).extraction_schema
    record = {"title": "A real title", "url": "https://catalog.example/item", "price": None}
    record.update(mutation)
    with pytest.raises(ValidationError):
        validator(schema).validate(record)


def test_all_nullable_fields_are_required_for_stable_columns():
    schema = compile_schema(plan(field())).extraction_schema
    with pytest.raises(ValidationError, match="required property"):
        validator(schema).validate({"title": "Title", "url": "https://catalog.example/item"})


def test_primitive_arrays_are_bounded_and_reject_nested_or_null_elements():
    schema = compile_schema(plan(field("tags", "array", items_type="string"))).extraction_schema
    base = {"title": "Title", "url": "https://catalog.example/item"}
    for value in (None, [], ["tag"]):
        validator(schema).validate({**base, "tags": value})
    for value in ([None], [["tag"]], [{"tag": "value"}], ["tag"] * 101):
        with pytest.raises(ValidationError):
            validator(schema).validate({**base, "tags": value})


@pytest.mark.parametrize(
    "bad",
    [
        plan(field(), field()),
        plan(field("price USD")),
        plan(field("price\n")),
        plan(field("price\x00")),
        plan(field("Price")),
        plan(field("_provenance")),
        plan(field("record_id")),
        plan(field("job_name")),
        plan(field("data")),
        plan(field("source_url")),
        plan(field("title", "integer")),
        plan(field("url", "array", items_type="string")),
        plan(field("tags", "array")),
        plan(field("tags", "array", items_type="object")),
        plan(field("details", "object")),
        plan(field("amount", "null")),
        plan(field(description=" ")),
        plan(field(description="invalid\x00description")),
        plan(entity_name=" "),
        plan(entity_name="invalid\x00entity"),
        {"entity_name": "Entity", "fields": [], "$ref": "https://example.org/schema"},
        plan({**field(), "$ref": "https://example.org/schema"}),
        plan({**field(), "enum": [5000]}),
        plan({**field(), "maximum": 5000}),
        plan({**field(), "pattern": ".*"}),
        plan({**field(), "default": 10}),
    ],
)
async def test_invalid_or_arbitrary_schema_keyword_plan_fails_even_from_injected_client(bad):
    client = PlanClient(bad)
    with pytest.raises(ProviderError, match="Invalid generated field plan"):
        await generate_schema("Find entities", client=client)
    assert len(client.requests) == 1


def test_mandatory_fields_are_locally_fixed_and_count_against_limit():
    empty = compile_schema(plan(), max_fields=2)
    explicit = compile_schema(
        plan(
            field("title", "string", description="Must have a different rule"),
            field("url", "string", description="Anything is a URL"),
        ),
        max_fields=2,
    )
    assert empty.extraction_schema == explicit.extraction_schema
    assert empty.schema_sha256 == explicit.schema_sha256
    with pytest.raises(ProviderError, match="max_fields"):
        compile_schema(plan(field()), max_fields=2)
    assert len(compile_schema(plan(field()), max_fields=3).extraction_schema["properties"]) == 3


def test_plan_size_and_unicode_schema_byte_size_are_bounded():
    with pytest.raises(ProviderError):
        compile_schema(plan(*(field(f"field_{index}") for index in range(31))), max_fields=30)
    with pytest.raises(ProviderError, match="32 KiB"):
        compile_schema(
            plan(*(field(f"field_{index}", description="🙂" * 500) for index in range(28))),
            max_fields=30,
        )


def test_compilation_hash_and_roundtrip_plan_are_deterministic_and_offline():
    generated = compile_schema(plan(field("salary"), field("company", "string")))
    rebuilt = compile_schema(json.loads(json.dumps(generated.plan)), max_fields=30)
    assert rebuilt == generated
    serialized = json.dumps(
        generated.extraction_schema,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    assert generated.schema_sha256 == hashlib.sha256(serialized).hexdigest()
    assert "$ref" not in json.dumps(generated.extraction_schema)


def test_optional_presentation_and_date_format_metadata_are_locally_compiled():
    generated = compile_schema(
        plan(
            {
                **field("published_at", "string"),
                "title": "Published",
                "format": "date-time",
                "display_role": "text",
            }
        )
    )
    definition = generated.extraction_schema["properties"]["published_at"]
    assert definition["title"] == "Published" and definition["format"] == "date-time"
    assert generated.extraction_schema["x-display"]["columns"] == ["title", "url", "published_at"]
    validator(generated.extraction_schema).validate(
        {
            "title": "Entity",
            "url": "https://example.org/item",
            "published_at": "2026-10-23T10:00:00Z",
        }
    )
    with pytest.raises(ValidationError):
        validator(generated.extraction_schema).validate(
            {"title": "Entity", "url": "https://example.org/item", "published_at": "not a date"}
        )


@pytest.mark.parametrize(
    "bad",
    [
        {**field("count", "integer"), "format": "date"},
        {**field("published", "string"), "format": "email"},
        {**field("title", "string"), "format": "date"},
        {**field("metric"), "title": "bad\x00label"},
        {**field("metric"), "unit": "bad\x00unit"},
    ],
)
def test_invalid_presentation_or_date_format_cannot_change_fixed_contract(bad):
    with pytest.raises(ProviderError):
        compile_schema(plan(bad))


async def test_client_cache_and_shared_budget_apply_without_new_transport(tmp_path):
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": json.dumps(complete_wire_plan(plan(field())))}}
                ],
                "usage": {"prompt_tokens": 50, "completion_tokens": 20, "cost": 0.0001},
            },
        )

    budget = RequestBudget(1)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = OpenRouterClient(
            "dummy-secret",
            "qwen/qwen3.7-flash",
            client=http,
            budget=budget,
            cache_dir=tmp_path,
            reasoning=False,
            max_output_tokens=4096,
        )
        first = await generate_schema("Find prices", client=client)
        second = await generate_schema("Find prices", client=client)
        assert first == second
        assert budget.used == client.calls == 1 and client.cache_hits == 1
        assert client.stats["cost_usd"] == 0.0001
        with pytest.raises(ProviderError, match="budget exhausted"):
            await generate_schema("A different question", client=client)
    assert requests[0]["reasoning"] == {"enabled": False}
    assert requests[0]["max_completion_tokens"] == 4096
    assert "max_tokens" not in requests[0]
    assert all("dummy-secret" not in file.read_text() for file in tmp_path.iterdir())


async def test_malformed_paid_response_consumes_budget_and_does_not_fallback():
    budget = RequestBudget(1)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})
        )
    ) as http:
        client = OpenRouterClient("dummy", "model", client=http, budget=budget)
        with pytest.raises(ProviderError):
            await generate_schema("Find prices", client=client)
        assert client.calls == budget.used == 1


@pytest.mark.parametrize(
    "question,max_fields",
    [
        ("", 20),
        (" ", 20),
        ("q" * 4001, 20),
        ("bad\x00question", 20),
        ("q", 1),
        ("q", 31),
        ("q", True),
    ],
)
async def test_invalid_user_options_fail_before_model(question, max_fields):
    client = PlanClient(plan())
    with pytest.raises(ValueError):
        await generate_schema(question, client=client, max_fields=max_fields)
    assert not client.requests

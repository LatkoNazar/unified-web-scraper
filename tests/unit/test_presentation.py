"""Dataset facets retain observed types, missingness and schema display intent."""

from unified_web_parser.interfaces.presentation import build_presentation


def by_key(presentation):
    return {field["key"]: field for field in presentation["fields"]}


def test_schema_metadata_and_factual_facets_preserve_zero_false_and_null():
    schema = {
        "properties": {
            "title": {"type": "string"},
            "reading": {"type": ["number", "null"], "title": "Observed reading", "x-unit": "km"},
            "verified": {"type": ["boolean", "null"]},
            "location": {"type": "string"},
            "details": {"type": "object"},
        }
    }
    records = [
        {
            "data": {
                "title": "A",
                "reading": 0,
                "verified": False,
                "location": "North",
                "details": {"x": 1},
            }
        },
        {"data": {"title": "B", "reading": 2.5, "verified": True, "location": "North"}},
        {"data": {"title": "C", "reading": None, "verified": None, "location": "South"}},
    ]
    presentation = build_presentation(schema, records, question="reading")
    fields = by_key(presentation)
    assert presentation["status"] == "provisional" and presentation["sample_size"] == 3
    assert fields["reading"]["label"] == "Observed reading"
    assert fields["reading"]["unit"] == "km"
    assert fields["reading"]["filter"] == "range"
    assert fields["reading"]["stats"] == {
        "count": 2,
        "missing": 1,
        "distinct_count": 2,
        "values": [{"value": 0, "count": 1}, {"value": 2.5, "count": 1}],
        "truncated": False,
        "min": 0,
        "max": 2.5,
    }
    assert fields["verified"]["type"] == "boolean"
    assert fields["verified"]["stats"]["values"] == [
        {"value": False, "count": 1},
        {"value": True, "count": 1},
    ]
    assert fields["location"]["stats"]["values"][0] == {"value": "North", "count": 2}
    assert fields["details"]["role"] == "detail" and fields["details"]["filter"] == "none"
    assert fields["details"]["stats"]["values"] == []
    assert presentation["columns"][:2] == ["title", "reading"]


def test_mixed_types_remain_distinct_and_arrays_have_whole_value_text_facets():
    records = [{"data": {"value": value, "tags": ["a", "b"]}} for value in [0, False, "0", None]]
    fields = by_key(build_presentation({}, records, ready=True))
    assert fields["value"]["type"] == "mixed" and fields["value"]["filter"] == "text"
    assert fields["value"]["stats"]["distinct_count"] == 3
    assert fields["value"]["stats"]["missing"] == 1
    assert fields["tags"]["type"] == "array" and fields["tags"]["filter"] == "text"
    assert fields["tags"]["stats"]["values"] == [{"value": ["a", "b"], "count": 4}]


def test_high_cardinality_is_bounded_and_late_fields_are_discovered_without_schema():
    records = [{"data": {"identifier": f"item-{i}"}} for i in range(70)]
    records[-1]["data"]["later"] = "only last row"
    presentation = build_presentation({}, records, ready=True)
    fields = by_key(presentation)
    assert presentation["status"] == "ready"
    assert fields["identifier"]["filter"] == "text"
    assert fields["identifier"]["stats"]["distinct_count"] == 70
    assert len(fields["identifier"]["stats"]["values"]) == 40
    assert fields["identifier"]["stats"]["truncated"] is True
    assert fields["later"]["stats"]["count"] == 1 and fields["later"]["stats"]["missing"] == 69


def test_explicit_columns_roles_and_iso_date_ranges_are_schema_driven():
    schema = {
        "x-display": {"columns": ["url", "occurred", "title", "url", "absent"]},
        "properties": {
            "title": {"type": "string"},
            "occurred": {"type": ["string", "null"], "format": "date-time"},
            "photo": {"type": "string", "x-display-role": "image"},
            "url": {"type": "string", "format": "uri"},
        },
    }
    records = [
        {"data": {"occurred": value}}
        for value in ["2026-10-02T12:00:00+02:00", "2026-10-02T09:00:00Z", "not a date", None]
    ]
    result = build_presentation(schema, records)
    field = by_key(result)["occurred"]
    assert result["columns"] == ["url", "occurred", "title"]
    assert field["type"] == "string" and field["filter"] == "range"
    assert field["stats"]["min"] == "2026-10-02T09:00:00Z"
    assert field["stats"]["max"] == "2026-10-02T12:00:00+02:00"
    assert field["stats"]["count"] == 3
    assert by_key(result)["photo"]["role"] == "image"


def test_columns_are_generic_bounded_and_relevance_precedes_coverage():
    properties = {
        "title": {"type": "string"},
        "description": {"type": "string"},
        "url": {"type": "string"},
        "external_id": {"type": "string"},
        **{f"field_{i}": {"type": "integer"} for i in range(12)},
        "event_topic": {"type": "string", "title": "Event topic"},
    }
    records = [
        {
            "source_url": "https://example.org/a",
            "img_url": "https://example.org/a.jpg",
            "data": {"title": "A", **{f"field_{i}": i for i in range(12)}, "event_topic": "Space"},
        }
    ]
    result = build_presentation({"properties": properties}, records, question="event topic")
    assert result["columns"][:2] == ["title", "event_topic"]
    assert len(result["columns"]) == 8
    assert not {"url", "img_url", "description", "external_id"} & set(result["columns"])
    assert by_key(result)["img_url"]["role"] == "image"


def test_empty_schema_fields_remain_typed_before_first_record():
    result = build_presentation({"properties": {"distance": {"type": ["integer", "null"]}}}, [])
    field = result["fields"][0]
    assert result["status"] == "provisional" and result["sample_size"] == 0
    assert field["type"] == "integer" and field["nullable"] is True
    assert field["stats"]["count"] == 0 and "min" not in field["stats"]


def test_generated_schema_display_roles_are_normalized_to_public_presentation_contract():
    from unified_web_parser.schema_generation import compile_schema

    generated = compile_schema(
        {
            "entity_name": "Observation",
            "fields": [
                {
                    "name": "distance",
                    "type": "number",
                    "description": "Observed distance",
                    "items_type": None,
                    "title": "Distance",
                    "unit": "km",
                    "display_role": "metric",
                },
                {
                    "name": "summary",
                    "type": "string",
                    "description": "Source details",
                    "items_type": None,
                    "display_role": "description",
                },
                {
                    "name": "state",
                    "type": "string",
                    "description": "Observed state",
                    "items_type": None,
                    "display_role": "status",
                },
            ],
            "display_columns": ["title", "distance", "state", "url"],
        }
    )
    result = build_presentation(
        generated.extraction_schema,
        [
            {
                "data": {
                    "title": "A",
                    "url": "https://example.org/a",
                    "distance": 0,
                    "summary": "Details",
                    "state": "observed",
                }
            }
        ],
    )
    fields = by_key(result)
    assert fields["title"]["role"] == "title"
    assert fields["url"]["role"] == "link"
    assert fields["distance"]["role"] == "attribute" and fields["distance"]["unit"] == "km"
    assert fields["state"]["role"] == "attribute"
    assert fields["summary"]["role"] == "detail"
    assert result["columns"] == ["title", "distance", "state", "url"]
    fallback = build_presentation(
        {key: value for key, value in generated.extraction_schema.items() if key != "x-display"},
        [{"data": {"summary": "Details"}}],
    )
    assert "summary" not in fallback["columns"]

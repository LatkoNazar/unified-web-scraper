"""Schema-backed display metadata and bounded facets over observed dataset values."""

import json
import math
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime
from typing import Any

_VALUE_LIMIT = 40
_TYPES = {"string", "integer", "number", "boolean", "array", "object"}
_ROLES = {"title", "link", "image", "attribute", "detail"}
_ROLE_ALIASES = {
    "description": "detail",
    "text": "attribute",
    "price": "attribute",
    "status": "attribute",
    "metric": "attribute",
}


def _type(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


def _schema_types(spec: Mapping[str, Any]) -> set[str]:
    declared = spec.get("type", [])
    kinds = {declared} if isinstance(declared, str) else set(declared or [])
    for branch in spec.get("anyOf", []) + spec.get("oneOf", []):
        if isinstance(branch, dict):
            kinds.update(_schema_types(branch))
    return kinds


def _resolved_type(kinds: set[str]) -> str:
    kinds = kinds & _TYPES
    if kinds and kinds <= {"integer", "number"}:
        return "number" if "number" in kinds else "integer"
    return next(iter(kinds)) if len(kinds) == 1 else "mixed"


def _identity(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _date_key(value: Any, format_name: str) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        if format_name == "date":
            return float(date.fromisoformat(value).toordinal())
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=parsed.tzinfo or UTC).timestamp()
    except (ValueError, OverflowError):
        return None


def _field_stats(values: list[Any], size: int, kind: str, format_name: str | None) -> dict:
    frequencies = Counter(_identity(value) for value in values)
    ordered = sorted(frequencies, key=lambda key: (-frequencies[key], key))
    stats = {
        "count": len(values),
        "missing": size - len(values),
        "distinct_count": len(frequencies),
        "values": [
            {"value": json.loads(key), "count": frequencies[key]} for key in ordered[:_VALUE_LIMIT]
        ],
        "truncated": len(frequencies) > _VALUE_LIMIT,
    }
    if kind in {"integer", "number"}:
        numbers = [
            value
            for value in values
            if isinstance(value, (int, float))
            and not isinstance(value, bool)
            and (not isinstance(value, float) or math.isfinite(value))
        ]
        if numbers:
            stats.update(min=min(numbers), max=max(numbers))
    elif kind == "string" and format_name in {"date", "date-time"}:
        dates = [
            (key, value) for value in values if (key := _date_key(value, format_name)) is not None
        ]
        if dates:
            stats.update(min=min(dates)[1], max=max(dates)[1])
    if kind == "object":
        stats["values"] = []
    return stats


def build_presentation(
    schema: Mapping[str, Any] | None,
    records: Iterable[Mapping[str, Any]],
    *,
    question: str = "",
    ready: bool = False,
) -> dict[str, Any]:
    """Describe schema fields without coercing or inferring unobserved facts.

    Array facets describe whole array values; they deliberately use text filtering,
    not scalar membership counts. Missing includes absent fields and explicit null.
    """
    schema = schema or {}
    properties = schema.get("properties", {})
    properties = properties if isinstance(properties, dict) else {}
    rows = []
    for record in records:
        data = record.get("data", {})
        row = dict(data) if isinstance(data, dict) else {}
        if "url" not in row and record.get("source_url") is not None:
            row["url"] = str(record["source_url"])
        if "img_url" not in row and record.get("img_url") is not None:
            row["img_url"] = str(record["img_url"])
        rows.append(row)
    keys = dict.fromkeys([*properties, *(key for row in rows for key in row)])
    fields = []
    for key in keys:
        spec = properties.get(key, {})
        spec = spec if isinstance(spec, dict) else {}
        values = [row[key] for row in rows if row.get(key) is not None]
        kinds = _schema_types(spec)
        kind = _resolved_type(kinds | {_type(value) for value in values})
        format_name = spec.get("format") if isinstance(spec.get("format"), str) else None
        role = spec.get("x-display-role")
        role = role if isinstance(role, str) else None
        if role not in _ROLES:
            identity_role = {
                "title": "title",
                "url": "link",
                "img_url": "image",
                "description": "detail",
                "external_id": "detail",
            }.get(key)
            role = (
                "detail"
                if role == "description" or kind == "object"
                else identity_role or _ROLE_ALIASES.get(role, "attribute")
            )
        stats = _field_stats(values, len(rows), kind, format_name)
        if kind in {"number", "integer"} or (
            kind == "string" and format_name in {"date", "date-time"}
        ):
            filter_name = "range"
        elif kind == "boolean":
            filter_name = "boolean"
        elif kind == "object" or role == "image":
            filter_name = "none"
        elif kind == "string" and stats["distinct_count"] <= _VALUE_LIMIT and role == "attribute":
            filter_name = "select"
        else:
            filter_name = "text"
        fields.append(
            {
                "key": key,
                "label": spec.get("title")
                if isinstance(spec.get("title"), str) and spec["title"].strip()
                else key.replace("_", " ").strip().capitalize(),
                "type": kind,
                "nullable": "null" in kinds or stats["missing"] > 0,
                "format": format_name,
                "unit": spec.get("x-unit") if isinstance(spec.get("x-unit"), str) else None,
                "role": role,
                "filter": filter_name,
                "stats": stats,
            }
        )
    display = schema.get("x-display", {})
    requested = display.get("columns") if isinstance(display, dict) else None
    if isinstance(requested, list) and requested:
        columns = list(
            dict.fromkeys(key for key in requested if isinstance(key, str) and key in keys)
        )[:8]
    else:
        tokens = set(re.findall(r"\w+", question.casefold()))
        positions = {field["key"]: index for index, field in enumerate(fields)}

        def rank(field: dict) -> tuple:
            spec = properties.get(field["key"], {})
            description = spec.get("description", "") if isinstance(spec, dict) else ""
            field_tokens = set(
                re.findall(
                    r"\w+",
                    f"{field['key']} {field['label']} {description}".casefold().replace("_", " "),
                )
            )
            return (-len(tokens & field_tokens), -field["stats"]["count"], positions[field["key"]])

        titles = [field["key"] for field in fields if field["role"] == "title"]
        attributes = sorted((field for field in fields if field["role"] == "attribute"), key=rank)
        columns = [*titles[:1], *(field["key"] for field in attributes)][:8]
    return {
        "version": 1,
        "status": "ready" if ready else "provisional",
        "fields": fields,
        "columns": columns,
        "sample_size": len(rows),
    }

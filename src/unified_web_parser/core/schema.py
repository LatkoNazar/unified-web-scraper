"""Offline JSON Schema validation, shared by configuration and extraction."""

from typing import Any

from jsonschema.validators import validator_for

from unified_web_parser.core.exceptions import ConfigurationError


def check_local_references(schema: Any) -> None:
    """Inspect standard subschemas, leaving examples, defaults, and field names alone."""
    if not isinstance(schema, dict):
        return
    for keyword in ("$ref", "$dynamicRef", "$recursiveRef"):
        if keyword in schema:
            reference = schema[keyword]
            if not isinstance(reference, str) or not reference.startswith("#"):
                raise ConfigurationError(
                    f"Only in-document schema references are allowed: {reference!r}"
                )
    for keyword in (
        "$defs",
        "definitions",
        "properties",
        "patternProperties",
        "dependentSchemas",
        "dependencies",
    ):
        children = schema.get(keyword)
        if isinstance(children, dict):
            for child in children.values():
                check_local_references(child)
    for keyword in (
        "additionalProperties",
        "additionalItems",
        "unevaluatedProperties",
        "unevaluatedItems",
        "propertyNames",
        "contains",
        "not",
        "if",
        "then",
        "else",
        "contentSchema",
        "items",
    ):
        child = schema.get(keyword)
        if isinstance(child, list):
            for item in child:
                check_local_references(item)
        else:
            check_local_references(child)
    for keyword in ("allOf", "anyOf", "oneOf", "prefixItems"):
        children = schema.get(keyword)
        if isinstance(children, list):
            for child in children:
                check_local_references(child)


def validate_schema(schema: Any) -> dict[str, Any]:
    """Validate a local schema without fetching any referenced resources."""
    if not isinstance(schema, dict):
        raise ConfigurationError("Extraction schema must be a JSON object")
    if "x-entity-types" in schema:
        entity_types = schema["x-entity-types"]
        if (
            not isinstance(entity_types, list)
            or not 1 <= len(entity_types) <= 50
            or any(
                not isinstance(value, str) or not value.strip() or len(value) > 500
                for value in entity_types
            )
            or len(set(entity_types)) != len(entity_types)
        ):
            raise ConfigurationError(
                "x-entity-types must contain 1–50 distinct nonblank type strings"
            )
    check_local_references(schema)
    if "$schema" in schema and not isinstance(schema["$schema"], str):
        raise ConfigurationError("JSON Schema dialect must be a string")
    validator_class = validator_for(schema, default=None)
    if "$schema" in schema and validator_class is None:
        raise ConfigurationError(f"Unsupported JSON Schema dialect: {schema['$schema']!r}")
    (validator_class or validator_for({})).check_schema(schema)
    return schema

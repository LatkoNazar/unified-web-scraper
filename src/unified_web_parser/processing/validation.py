"""Offline record validation and deterministic duplicate suppression."""

import json
from collections.abc import Sequence
from typing import Any

from jsonschema import FormatChecker
from jsonschema.validators import validator_for

from unified_web_parser.core.models import Record
from unified_web_parser.core.schema import check_local_references


def validate_records(
    records: Sequence[Record], schema: dict[str, Any]
) -> tuple[
    list[Record],
    list[dict[str, Any]],
]:
    check_local_references(schema)
    validator_class = validator_for(schema)
    validator_class.check_schema(schema)
    validator = validator_class(schema, format_checker=FormatChecker())
    valid = []
    errors = []
    seen: set[str] = set()
    for record in records:
        failures = list(validator.iter_errors(record.data))
        if failures:
            errors.extend(
                {
                    "record_id": record.record_id,
                    "path": list(error.absolute_path),
                    "message": error.message,
                }
                for error in failures
            )
            continue
        fingerprint = json.dumps([str(record.provenance.source_url), record.data], sort_keys=True)
        if fingerprint not in seen:
            seen.add(fingerprint)
            valid.append(record)
    return valid, errors

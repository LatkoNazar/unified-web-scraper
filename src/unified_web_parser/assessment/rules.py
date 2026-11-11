"""Domain-neutral lexical assessment with explicit optional device rules."""

from collections.abc import Sequence
from typing import Any

from unified_web_parser.assessment.tokens import _tokens
from unified_web_parser.core.models import Assessment, Record


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for child in value.values() for text in _strings(child)]
    if isinstance(value, list):
        return [text for child in value for text in _strings(child)]
    return []


def assess_record(
    record: Record,
    search: str | None,
    criteria: Sequence[str],
    *,
    assessment_profile: str = "generic",
    relevance_fields: Sequence[str] | None = None,
) -> Assessment:
    """Generic relevance uses observed text only; unsupported decisions stay unknown."""
    if assessment_profile == "device":
        from unified_web_parser.assessment.device import assess_device_record

        return assess_device_record(record, search, criteria)
    if assessment_profile != "generic":
        raise ValueError("Unsupported assessment profile")
    values = (
        record.data.values()
        if relevance_fields is None
        else (record.data[field] for field in relevance_fields if field in record.data)
    )
    observed = _tokens(" ".join(text for value in values for text in _strings(value)))
    query = _tokens(search) if search else set()
    results = {
        criterion: (query <= observed if query and observed else None)
        if criterion == "is_relevant"
        else None
        for criterion in criteria
    }
    return Assessment(record_id=record.record_id, assessor="rules", results=results)

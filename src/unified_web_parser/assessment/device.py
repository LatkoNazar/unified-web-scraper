"""Optional assessments after extraction; routing remains a separate concern."""

from collections.abc import Sequence
from typing import Any

from unified_web_parser.assessment.tokens import _tokens
from unified_web_parser.core.models import Assessment, Record
from unified_web_parser.profiles.device import device_identity, model_groups

_SEMANTIC_FIELDS = {
    "title",
    "name",
    "brand",
    "model",
    "chip",
    "series",
    "category",
    "product_type",
    "company",
    "author",
    "role",
    "location",
    "city",
    "country",
    "employment_type",
}


def _relevance_tokens(data: dict[str, Any]) -> set[str]:
    # Description, prices, identifiers and field names are not product identity evidence.
    values = [
        str(value)
        for key, value in data.items()
        if key in _SEMANTIC_FIELDS and isinstance(value, str)
    ]
    for field in ("ram_gb", "storage_gb", "screen_inches"):
        value = data.get(field)
        if isinstance(value, int | float) and not isinstance(value, bool):
            values.append(str(value))
            if field.endswith("_gb"):
                values.append("GB")
            elif 0 < value < 100:
                # Display families use nominal inches, e.g. a 15.3-inch panel is "15".
                values.append(str(int(value)))
    # Only an explicit alphanumeric model receives canonical lookalike symbols.
    # Other source language/text remains untouched, and generic tokenization is
    # unchanged. This makes observed Cyrillic "М5" usable for an ASCII M5 query.
    identity = " ".join(values)
    values.extend(
        family + variant
        for family, variants in model_groups(identity).items()
        for variant in variants
    )
    return _tokens(" ".join(values))


def assess_device_record(record: Record, search: str | None, criteria: Sequence[str]) -> Assessment:
    """Three-valued rules: False means no rule flag, never independently verified safety.

    MDM rules activate only for schemas that expose ``mdm_status``. An explicit
    seller claim of no MDM does not establish a negative ``has_mdm_risk`` verdict.
    """
    data = record.data
    admission, reason, _evidence = device_identity(data, search)
    non_device = admission == "rejected"
    results = {}
    for criterion in criteria:
        if criterion == "is_relevant":
            query_tokens = _tokens(search) if search else set()
            results[criterion] = (
                False
                if non_device
                else query_tokens <= _relevance_tokens(data)
                if query_tokens
                else None
            )
        elif criterion == "has_mdm_risk":
            results[criterion] = (
                True
                if data.get("mdm_status")
                in {
                    "claimed_managed",
                    "claimed_bypass",
                }
                else None
            )
        elif criterion == "requires_manual_review":
            if (
                non_device
                or reason == "conflicting_device_identity"
                or bool(search and not _tokens(search) <= _relevance_tokens(data))
                or bool(data.get("source_conflicts"))
                or ("mdm_status" in data and data["mdm_status"] != "claimed_clear")
            ):
                results[criterion] = True
            elif any(key in data for key in ("mdm_status", "source_conflicts", "listing_type")):
                results[criterion] = False
            else:
                results[criterion] = None
        else:
            # Unsupported interpretations stay explicitly unknown, never "safe" by default.
            results[criterion] = None
    return Assessment(record_id=record.record_id, assessor="rules", results=results)

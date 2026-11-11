"""Auditable record admission after extraction and before durable dataset writes."""

import re
from collections.abc import Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from unified_web_parser.assessment.tokens import _tokens
from unified_web_parser.core.models import Record


class Admission(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    status: Literal["accepted", "review", "rejected"]
    reason: str
    evidence: tuple[str, ...] = ()


def admit_record(
    record: Record,
    schema: dict[str, Any],
    search: str | None,
    *,
    assessment_profile: str = "generic",
    relevance_fields: Sequence[str] | None = None,
) -> Admission:
    """Schema/query identity is three-valued; domain exclusions require an explicit profile."""
    data = record.data
    if assessment_profile == "device":
        from unified_web_parser.assessment.device import _relevance_tokens
        from unified_web_parser.profiles.device import device_identity, model_groups, models_overlap

        status, reason, evidence = device_identity(data, search)
        if status == "rejected" or reason in {
            "conflicting_device_identity",
            "query_requests_non_device",
        }:
            return Admission(status=status, reason=reason, evidence=evidence)
        identity = " ".join(
            str(data.get(key) or "")
            for key in ("title", "name", "headline", "model", "chip", "series")
        )
        descriptive = {
            "device",
            "devices",
            "laptop",
            "laptops",
            "notebook",
            "phone",
            "tablet",
            "listing",
            "listings",
            "market",
            "inventory",
            "offer",
            "offers",
        }
        anchors = {
            word
            for word in _tokens(str(schema.get("title", ""))) & _tokens(search or "")
            if len(word) >= 3 and word.isalpha() and word not in descriptive
        }
        actual = _tokens(identity)
        requested_models = model_groups(search or "")
        observed_models = model_groups(identity)
        has_requested_model = any(
            models_overlap(wanted, observed_models.get(family, set()))
            for family, wanted in requested_models.items()
        )
        # Product-family tokens are schema/query bound. Other concrete model
        # names prove mismatch; opaque all-caps SKUs and localized/abbreviated
        # names do not, and therefore remain reviewable.
        plain_title = re.sub(r"\([^)]*\)", "", str(data.get("title") or ""))
        other_models = [
            prefix + number
            for prefix, number in re.findall(r"\b([A-Za-z]{1,16})(\d+)\b", plain_title)
            if len(prefix) <= 2 or not prefix.isupper()
        ]
        if anchors and not anchors & actual and not has_requested_model and other_models:
            return Admission(
                status="rejected",
                reason="explicit_entity_mismatch",
                evidence=(str(data.get("title") or ""), "requested=" + ",".join(sorted(anchors))),
            )
        observed = _relevance_tokens(data)
    elif assessment_profile == "generic":
        # Only requested textual identity fields contribute. Missing facts or a
        # natural-language query that cannot be proven lexically stay for review.
        properties = schema.get("properties", {})
        fields = (
            relevance_fields
            if relevance_fields is not None
            else [
                key
                for key, definition in properties.items()
                if key in {"title", "name", "headline"}
                or isinstance(definition, dict)
                and definition.get("x-display-role") == "title"
            ]
        )
        observed = _tokens(" ".join(data[key] for key in fields if isinstance(data.get(key), str)))
        evidence = tuple(data[key] for key in fields if isinstance(data.get(key), str))
    else:
        raise ValueError("Unsupported assessment profile")
    query = _tokens(search) if search else set()
    if not query:
        return Admission(status="accepted", reason="no_query_constraint")
    if query <= observed:
        return Admission(status="accepted", reason="identity_matches_query", evidence=evidence)
    return Admission(status="review", reason="query_not_fully_confirmed", evidence=evidence)

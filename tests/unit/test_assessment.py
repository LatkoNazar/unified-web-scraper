import pytest

from unified_web_parser.assessment.rules import assess_record
from unified_web_parser.core.models import Provenance, Record


def record(data):
    return Record(
        record_id="item",
        job_name="example",
        data=data,
        provenance=Provenance(
            page_id="p",
            source_url="https://example.com/item",
            candidate_ids=("c",),
            extractor="json_ld",
        ),
    )


@pytest.mark.parametrize(
    "data, expected",
    [
        ({"title": "MacBook Air 15 M4 16/512"}, True),
        ({"title": "MacBook Air 13.6 M4 16/512"}, False),
        ({"title": "MacBook Air M4", "price": 15, "storage_gb": 512}, False),
        ({"title": "MacBook Air 15 M40"}, False),
        ({"title": "MacBook Air M4", "screen_inches": 15.3}, True),
        ({"title": "MacBook Air 15 M4", "listing_type": "parts"}, False),
        ({"title": "MacBook Air 15 M4 case", "listing_type": "accessory"}, False),
        ({"title": "MacBook Air 15 M4 repair", "listing_type": "service"}, False),
        ({"title": "MacBook Air 13 M4", "description": "Supports MacBook Air 15 M4"}, False),
    ],
)
def test_relevance_uses_identity_tokens_and_listing_type(data, expected):
    result = assess_record(
        record(data), "MacBook Air 15 M4", ["is_relevant"], assessment_profile="device"
    )
    assert result.results["is_relevant"] is expected


def test_numeric_tokens_require_whole_decimal_match():
    item = record({"title": "Widget 13.6 inch"})
    assert assess_record(item, "Widget 13.6", ["is_relevant"]).results["is_relevant"] is True
    assert assess_record(item, "Widget 13", ["is_relevant"]).results["is_relevant"] is False


@pytest.mark.parametrize(
    "status, risk, review",
    [
        (None, None, True),
        ("claimed_clear", None, False),
        ("claimed_managed", True, True),
        ("claimed_bypass", True, True),
    ],
)
def test_mdm_source_claim_is_not_verified_safety(status, risk, review):
    result = assess_record(
        record({"title": "Device", "mdm_status": status}),
        None,
        ["has_mdm_risk", "requires_manual_review"],
        assessment_profile="device",
    )
    assert result.results == {"has_mdm_risk": risk, "requires_manual_review": review}


def test_generic_job_does_not_require_review_because_mdm_field_is_absent():
    result = assess_record(
        record({"title": "Python engineer", "company": "Acme"}),
        "Python Acme",
        ["is_relevant", "has_mdm_risk", "requires_manual_review", "unconfigured"],
    )
    assert result.results == {
        "is_relevant": True,
        "has_mdm_risk": None,
        "requires_manual_review": None,
        "unconfigured": None,
    }


def test_source_conflicts_require_manual_review():
    result = assess_record(
        record(
            {
                "title": "Device",
                "mdm_status": "claimed_clear",
                "source_conflicts": ["Title says512; description says500"],
            }
        ),
        None,
        ["requires_manual_review"],
        assessment_profile="device",
    )
    assert result.results["requires_manual_review"] is True

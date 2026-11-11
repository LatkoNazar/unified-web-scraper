import pytest
from pydantic import ValidationError

from unified_web_parser.core.exceptions import RoutingError
from unified_web_parser.core.models import (
    CandidateBlock,
    RoutingContext,
    RoutingPlan,
    RoutingTarget,
)


@pytest.fixture
def context():
    return RoutingContext(
        page_id="snapshot-1",
        extraction_schema={"type": "object", "properties": {"employer": {"type": "string"}}},
        candidates=(
            CandidateBlock(candidate_id="jsonld-0", kind="json_ld", content='{"employer":"ACME"}'),
            CandidateBlock(candidate_id="dom-1", kind="dom", content="Employer: ACME"),
        ),
    )


def test_decision_can_select_known_blocks_and_strategy(context):
    plan = RoutingPlan(
        page_id=context.page_id,
        targets=(
            RoutingTarget(candidate_id="jsonld-0", strategy="json_ld"),
            RoutingTarget(candidate_id="dom-1", strategy="llm"),
        ),
    )
    assert plan.validate_against(context) is plan


def test_empty_plan_and_explicit_skip_are_valid(context):
    RoutingPlan(page_id=context.page_id, targets=()).validate_against(context)
    RoutingPlan(
        page_id=context.page_id,
        targets=(RoutingTarget(candidate_id="dom-1", strategy="skip"),),
    ).validate_against(context)


@pytest.mark.parametrize(
    ("page_id", "ids"),
    [
        ("snapshot-1", ["invented-selector"]),
        ("snapshot-1", ["dom-1", "dom-1"]),
        ("another-snapshot", ["dom-1"]),
    ],
)
def test_invalid_routing_rejected(context, page_id, ids):
    plan = RoutingPlan(
        page_id=page_id,
        targets=tuple(RoutingTarget(candidate_id=item, strategy="llm") for item in ids),
    )
    with pytest.raises(RoutingError):
        plan.validate_against(context)


def test_duplicate_candidate_inventory_rejected(context):
    with pytest.raises(ValidationError, match="unique"):
        RoutingContext(
            page_id=context.page_id,
            extraction_schema=context.extraction_schema,
            candidates=(context.candidates[0], context.candidates[0]),
        )


def test_unknown_strategy_rejected():
    with pytest.raises(ValidationError):
        RoutingTarget(candidate_id="dom-1", strategy="execute_code")

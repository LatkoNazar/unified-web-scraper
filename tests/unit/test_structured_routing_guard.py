import json

import pytest

from unified_web_parser.core.models import CandidateBlock, RoutingContext
from unified_web_parser.routing.engines import can_route_structured


def context(entity, *, source_url="https://example.org/products/camera", extra=()):
    content = entity if isinstance(entity, str) else json.dumps(entity)
    return RoutingContext(
        page_id="snapshot",
        source_url=source_url,
        extraction_schema={},
        candidates=(
            CandidateBlock(candidate_id="structured", kind="json_ld", content=content),
            *extra,
        ),
    )


@pytest.mark.parametrize(
    "identity",
    [
        {"url": "https://example.org/products/camera"},
        {"@id": "/products/camera"},
        {"url": "https://example.org/products/camera?utm_source=ad#details"},
        {"url": {"@id": "other"}, "@id": "/products/camera"},
    ],
)
def test_single_described_entity_matching_page_can_use_rules(identity):
    assert can_route_structured(context({**identity, "description": "Explicit product details"}))


@pytest.mark.parametrize(
    "entity",
    [
        {"description": "Details without explicit URL"},
        {"url": "https://example.org/products/another", "description": "Details"},
        {"url": "/products/another", "@id": "/products/camera", "description": "Details"},
        {"url": "/products/camera", "description": "   "},
        {"url": "/products/camera", "description": {"text": "nested"}},
        {"url": {"@id": "/products/camera"}, "description": "Details"},
        {"url": "", "description": "Details"},
        [{"url": "/products/camera", "description": "Details"}],
        "{broken",
    ],
)
def test_ambiguous_or_missing_evidence_uses_native_fallback(entity):
    assert not can_route_structured(context(entity))


def test_missing_source_multiple_entities_and_no_structured_candidate_fall_back():
    entity = {"url": "/products/camera", "description": "Explicit details"}
    assert not can_route_structured(context(entity, source_url=None))
    second = CandidateBlock(candidate_id="second", kind="json_ld", content=json.dumps(entity))
    assert not can_route_structured(context(entity, extra=(second,)))
    assert not can_route_structured(
        RoutingContext(
            page_id="snapshot",
            source_url="https://example.org/products/camera",
            extraction_schema={},
            candidates=(),
        )
    )


def test_dom_candidates_do_not_make_one_structured_entity_ambiguous():
    dom = CandidateBlock(candidate_id="dom", kind="dom", content="Duplicate visible facts")
    assert can_route_structured(
        context({"url": "/products/camera", "description": "Details"}, extra=(dom,))
    )

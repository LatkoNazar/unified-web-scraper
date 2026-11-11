"""Generic pipeline messages; domain fields belong in each job's JSON Schema."""

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    StringConstraints,
    field_validator,
    model_validator,
)

from unified_web_parser.core.exceptions import RoutingError

Identifier = Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1)]


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Page(Message):
    page_id: Identifier
    url: HttpUrl
    fetched_at: datetime
    html: str
    status_code: Annotated[int, Field(ge=100, le=599)]
    snapshot_ref: str | None = None


class CandidateKind(StrEnum):
    JSON_LD = "json_ld"
    DOM = "dom"
    TEXT = "text"


class ExtractionStrategy(StrEnum):
    JSON_LD = "json_ld"
    DOM = "dom"
    LLM = "llm"
    SKIP = "skip"


class CandidateBlock(Message):
    """A deterministic candidate ID is scoped to one immutable page snapshot."""

    candidate_id: Identifier
    kind: CandidateKind
    content: str
    locator: str | None = None


class RoutingContext(Message):
    page_id: Identifier
    source_url: HttpUrl | None = None
    extraction_schema: dict[str, Any]
    candidates: tuple[CandidateBlock, ...]

    @model_validator(mode="after")
    def unique_candidate_ids(self) -> "RoutingContext":
        ids = [candidate.candidate_id for candidate in self.candidates]
        if len(ids) != len(set(ids)):
            raise ValueError("Candidate IDs must be unique within a page snapshot")
        return self


class RoutingTarget(Message):
    candidate_id: Identifier
    strategy: ExtractionStrategy
    reason: str | None = None
    confidence: Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)] | None = None
    probabilities: dict[str, Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]] | None = None


class RoutingPlan(Message):
    page_id: Identifier
    targets: tuple[RoutingTarget, ...]

    def validate_against(self, context: RoutingContext) -> "RoutingPlan":
        """Reject unknown IDs, repeated targets, and plans for another snapshot."""
        if self.page_id != context.page_id:
            raise RoutingError("Routing plan belongs to another page snapshot")
        known = {candidate.candidate_id for candidate in context.candidates}
        seen: set[str] = set()
        for target in self.targets:
            if target.candidate_id not in known:
                raise RoutingError(f"Unknown candidate ID: {target.candidate_id}")
            if target.candidate_id in seen:
                raise RoutingError(f"Repeated candidate ID: {target.candidate_id}")
            seen.add(target.candidate_id)
        return self


class Provenance(Message):
    page_id: Identifier
    source_url: HttpUrl
    candidate_ids: tuple[Identifier, ...]
    extractor: Identifier
    snapshot_ref: str | None = None
    model: str | None = None
    prompt_version: str | None = None
    identity_basis: str | None = None


class Record(Message):
    record_id: Identifier
    job_name: Identifier
    data: dict[str, Any]
    img_url: HttpUrl | None = None
    provenance: Provenance

    @field_validator("img_url")
    @classmethod
    def image_has_no_credentials(cls, value: HttpUrl | None) -> HttpUrl | None:
        if value is not None and (value.username or value.password):
            raise ValueError("Image URL cannot contain credentials")
        return value


class Assessment(Message):
    """Optional interpretation of an extracted record, separate from routing."""

    record_id: Identifier
    assessor: Identifier
    results: dict[str, Any]

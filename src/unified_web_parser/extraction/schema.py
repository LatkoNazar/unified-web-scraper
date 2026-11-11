"""Public schema-driven extraction coordinator, independent of crawling and storage."""

import json
from collections.abc import Mapping, Sequence
from typing import Any

from unified_web_parser.core.exceptions import ExtractionError
from unified_web_parser.core.models import CandidateBlock, ExtractionStrategy, Page, Record
from unified_web_parser.extraction.enrichment import enrich_records
from unified_web_parser.extraction.identity import build_records
from unified_web_parser.extraction.images import observed_images
from unified_web_parser.extraction.projection import dom_entity, map_entity
from unified_web_parser.providers.openrouter import OpenRouterClient
from unified_web_parser.routing.candidates import is_candidate_entity, structured_entities


class SchemaExtractor:
    def __init__(
        self,
        job_name: str,
        llm: OpenRouterClient | None = None,
        *,
        sparse_enrichment: bool = True,
        content_profile: str = "generic",
        currency_aliases: Mapping[str, str] | None = None,
    ) -> None:
        self.job_name = job_name
        self.llm = llm
        self.sparse_enrichment = sparse_enrichment
        if content_profile not in {"generic", "commerce"}:
            raise ValueError("Unsupported content profile")
        self.content_profile = content_profile
        self.currency_aliases = dict(currency_aliases or {})

    async def extract(
        self,
        page: Page,
        candidate: CandidateBlock,
        strategy: ExtractionStrategy,
        schema: dict[str, Any],
    ) -> Sequence[Record]:
        if strategy == ExtractionStrategy.SKIP:
            return []
        if strategy == ExtractionStrategy.JSON_LD:
            try:
                entities = list(
                    structured_entities(
                        json.loads(candidate.content),
                        content_profile=self.content_profile,
                        schema=schema,
                    )
                )
                if schema.get("x-entity-types"):
                    entities = [
                        entity
                        for entity in entities
                        if is_candidate_entity(entity, schema, content_profile=self.content_profile)
                    ]
            except (ValueError, TypeError) as exc:
                raise ExtractionError("Selected JSON-LD is malformed") from exc
        else:
            entities = [
                dom_entity(
                    candidate.content,
                    str(page.url),
                    card=bool(candidate.locator and candidate.locator.startswith("card:")),
                    content_profile=self.content_profile,
                    currency_aliases=self.currency_aliases,
                )
            ]
        images = observed_images(
            page,
            candidate,
            entities,
            schema=schema,
            content_profile=self.content_profile,
            currency_aliases=self.currency_aliases,
        )
        extracted = [
            map_entity(
                entity,
                schema,
                str(page.url),
                content_profile=self.content_profile,
                observed_img_url=images[index],
            )
            for index, entity in enumerate(entities)
        ]
        used_llm = False
        needs_fill = any(
            any(
                name not in data or data[name] is None
                for name in schema.get("properties", {})
                if name != "img_url"
            )
            for data in extracted
        )
        if self.llm is not None and (strategy == ExtractionStrategy.LLM or needs_fill):
            extracted = await enrich_records(
                self.llm,
                page,
                candidate,
                strategy,
                schema,
                extracted,
                sparse_enrichment=self.sparse_enrichment,
                content_profile=self.content_profile,
                currency_aliases=self.currency_aliases,
            )
            used_llm = True
        if "img_url" in schema.get("properties", {}):
            for index, data in enumerate(extracted):
                data["img_url"] = images[index] if len(extracted) == len(images) else None
        return build_records(
            self.job_name,
            page,
            candidate,
            strategy,
            schema,
            entities,
            extracted,
            content_profile=self.content_profile,
            llm=self.llm,
            used_llm=used_llm,
            images=images,
        )

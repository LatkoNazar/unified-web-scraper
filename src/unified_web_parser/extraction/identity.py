"""Source-derived record identifiers and auditable extraction provenance."""

import hashlib
import json
from collections.abc import Sequence
from typing import Any
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from unified_web_parser.core.models import (
    CandidateBlock,
    ExtractionStrategy,
    Page,
    Provenance,
    Record,
)
from unified_web_parser.extraction.projection import plain_value
from unified_web_parser.routing.candidates import is_candidate_entity, structured_entities
from unified_web_parser.sources.navigation import canonical_url


def _page_entities(
    page: Page, schema: dict[str, Any], *, content_profile: str = "generic"
) -> list[dict[str, Any]]:
    """Identity needs page-level context even after routing splits JSON-LD candidates."""
    soup = BeautifulSoup(page.html, "html.parser")
    entities = []
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            document = json.loads(script.string or script.get_text())
        except (ValueError, TypeError):
            continue
        for entity in structured_entities(document, content_profile=content_profile, schema=schema):
            if is_candidate_entity(entity, schema, content_profile=content_profile):
                entities.append(entity)
    return entities


def _identifier(entity: dict[str, Any]) -> str | None:
    for field in ("identifier", "productID", "sku"):
        value = entity.get(field)
        if isinstance(value, dict):
            value = value.get("value") or value.get("@value") or value.get("@id")
        if isinstance(value, str | int) and not isinstance(value, bool) and str(value).strip():
            return str(value)
    return None


def _entity_identity(
    page: Page, entity: dict[str, Any], pool: Sequence[dict[str, Any]]
) -> tuple[str, str]:
    source_url = canonical_url(str(page.url))

    def entity_url(item: dict[str, Any]) -> str | None:
        value = plain_value(item.get("url") or item.get("@id"))
        return canonical_url(urljoin(source_url, str(value))) if value else None

    url = entity_url(entity)
    multiple = len(pool) > 1
    if url and (
        not multiple or (url != source_url and sum(entity_url(item) == url for item in pool) <= 1)
    ):
        return url, "entity_url"
    if multiple:
        source_id = _identifier(entity)
        if source_id is not None:
            return source_url + "#identifier:" + source_id, "source_identifier"
        # Without source identity, content is the only reproducible distinguishing evidence.
        digest = hashlib.sha256(json.dumps(entity, sort_keys=True, ensure_ascii=False).encode())
        return source_url + "#content:" + digest.hexdigest(), "content_fingerprint"
    return source_url, "page_url"


def _evidence_urls(content: str, page_url: str) -> set[str]:
    soup = BeautifulSoup(content, "html.parser")
    urls = {canonical_url(page_url)}
    for tag in soup.find_all("a", href=True):
        url = urljoin(page_url, str(tag["href"]))
        if urlsplit(url).scheme in {"http", "https"}:
            urls.add(canonical_url(url))
    return urls


def build_records(
    job_name: str,
    page: Page,
    candidate: CandidateBlock,
    strategy: ExtractionStrategy,
    schema: dict[str, Any],
    entities: list[dict[str, Any]],
    extracted: list[dict[str, Any]],
    *,
    content_profile: str,
    llm: Any,
    used_llm: bool,
    images: Sequence[str | None] = (),
) -> list[Record]:
    records = []
    identity_pool = _page_entities(page, schema, content_profile=content_profile) or entities
    if len(entities) > len(identity_pool):
        identity_pool = entities
    evidence_urls = _evidence_urls(candidate.content, str(page.url))
    for index, data in enumerate(extracted):
        if not data or not any(value is not None for value in data.values()):
            continue
        source_url = canonical_url(str(page.url))
        # Identity comes from source evidence even when a job's projection omits URL.
        entity = entities[index] if len(entities) == len(extracted) else {}
        multiple_llm = len(extracted) > len(entities)
        is_card = bool(candidate.locator and candidate.locator.startswith("card:"))
        if strategy != ExtractionStrategy.JSON_LD and (multiple_llm or is_card):
            supplied_url = data.get("url") or data.get("source_url")
            url = canonical_url(urljoin(source_url, str(supplied_url))) if supplied_url else None
            if url and url != source_url and url in evidence_urls:
                identity, identity_basis = url, "evidence_url"
            elif entity.get("url") and canonical_url(str(entity["url"])) != source_url:
                identity, identity_basis = canonical_url(str(entity["url"])), "entity_url"
            else:
                fingerprint = {"record": data, "candidate": candidate.content}
                digest = hashlib.sha256(
                    json.dumps(
                        fingerprint,
                        sort_keys=True,
                        ensure_ascii=False,
                    ).encode()
                ).hexdigest()
                identity, identity_basis = (
                    source_url + "#content:" + digest,
                    "content_fingerprint",
                )
        else:
            identity, identity_basis = _entity_identity(page, entity, identity_pool)
        digest = hashlib.sha256(
            json.dumps(
                [job_name, urlsplit(source_url).netloc, identity],
                sort_keys=True,
                ensure_ascii=False,
            ).encode()
        ).hexdigest()[:24]
        records.append(
            Record(
                record_id=digest,
                job_name=job_name,
                data=data,
                img_url=images[index] if len(images) == len(extracted) else None,
                provenance=Provenance(
                    page_id=page.page_id,
                    source_url=page.url,
                    candidate_ids=(candidate.candidate_id,),
                    extractor=(strategy.value + ("+openrouter" if used_llm else "")),
                    snapshot_ref=page.snapshot_ref,
                    model=getattr(llm, "model", None) if used_llm else None,
                    prompt_version="extraction.v3" if used_llm else None,
                    identity_basis=identity_basis,
                ),
            )
        )
    return records

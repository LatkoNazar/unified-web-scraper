"""Generate or restore one run-local schema before discovery and extraction."""

import asyncio
import hashlib
import json
from contextlib import AsyncExitStack
from copy import deepcopy
from pathlib import Path
from typing import Any

from unified_web_parser.core.config import JobConfig
from unified_web_parser.core.exceptions import CreditsExhausted, DurabilityError, PipelineError
from unified_web_parser.core.models import Page
from unified_web_parser.core.schema import validate_schema
from unified_web_parser.pipeline.artifacts import atomic_text
from unified_web_parser.pipeline.report import RunReport
from unified_web_parser.providers.openrouter import RequestBudget


async def prepare_schema_client(
    config: JobConfig,
    *,
    data_dir: Path,
    injected_llm: Any,
    stack: AsyncExitStack,
    key: str | None,
    model: str,
    budget: RequestBudget,
    clients: dict[str, Any],
) -> None:
    """Reopen the same client for unfinished generation/refinement, without calling it."""
    if clients.get("schema_generation") is not None:
        return
    if not key or injected_llm is not None:
        raise PipelineError("Schema generation requires a schema_client or OpenRouter key")
    from unified_web_parser.providers.openrouter import OpenRouterClient

    clients["schema_generation"] = await stack.enter_async_context(
        OpenRouterClient(
            api_key=key,
            model=model,
            max_calls=max(1, config.ai.max_calls),
            timeout=config.ai.timeout,
            budget=budget,
            cache_dir=data_dir / "ai_cache" if config.ai.cache else None,
            reasoning=False,
            max_output_tokens=4096,
            stage="schema_generation",
        )
    )


async def prepare_schema(
    config: JobConfig,
    *,
    data_dir: Path,
    injected_llm: Any,
    replay_dir: Path | None,
    stack: AsyncExitStack,
    key: str | None,
    model: str,
    budget: RequestBudget,
    clients: dict[str, Any],
    report: RunReport,
    run_dir: Path,
) -> dict[str, Any]:
    """Resolve one run-local schema before any search or page processing."""
    from unified_web_parser.schema_generation import compile_schema, generate_schema

    audit_path = run_dir / "schema-generation.json"
    schema_path = run_dir / "schema.json"
    report.artifacts.update(schema=str(schema_path), schema_generation=str(audit_path))
    report.schema_generation = {
        "status": "running",
        "question": config.search,
        "max_fields": config.schema_generation.max_fields,
        "model": None if replay_dir is not None else model,
        "network_used": False,
    }
    baseline = None
    try:
        if replay_dir is not None:
            previous = Path(replay_dir)
            saved_schema_path = previous / "schema.json"
            saved_job_path = previous / "job.json"
            saved_job = (
                json.loads(saved_job_path.read_text(encoding="utf-8"))
                if saved_job_path.is_file()
                else {}
            )
            schema = (
                json.loads(saved_schema_path.read_text(encoding="utf-8"))
                if saved_schema_path.is_file()
                else saved_job.get("schema")
            )
            validate_schema(schema)
            if (
                not schema.get("properties")
                or schema.get("additionalProperties") is not False
                or not {"title", "url"} <= set(schema.get("required", []))
            ):
                raise PipelineError("Saved generated schema is missing its closed record contract")
            if "schema" in saved_job and saved_job["schema"] != schema:
                raise PipelineError("Saved generated schema disagrees with job.json")
            previous_audit = previous / "schema-generation.json"
            if previous_audit.is_file():
                metadata = json.loads(previous_audit.read_text(encoding="utf-8"))
                if metadata.get("status") not in {"generated", "replayed"}:
                    raise PipelineError("Saved schema generation did not complete successfully")
                if metadata.get("plan"):
                    compiled = compile_schema(metadata["plan"], max_fields=30)
                    expected_schema = compiled.extraction_schema
                    if "x-display" not in schema:
                        # Replay schemas produced by the original v2 compiler verbatim.
                        expected_schema = deepcopy(expected_schema)
                        expected_schema.pop("x-display", None)
                        for definition in expected_schema.get("properties", {}).values():
                            for annotation in ("title", "x-unit", "x-display-role"):
                                definition.pop(annotation, None)
                    if expected_schema != schema:
                        raise PipelineError("Saved schema disagrees with its generation plan")
                expected_hash = metadata.get("schema_sha256")
                if expected_hash and expected_hash != schema_hash(schema):
                    raise PipelineError("Saved generated schema hash does not match")
                report.schema_generation.update(metadata)
            report.schema_generation.update(
                status="replayed",
                regenerated=False,
                from_run=str(previous),
                network_used=False,
                schema_sha256=schema_hash(schema),
            )
        else:
            if not config.ai.enabled or (
                config.ai.max_calls < 1
                and not (budget.journal and budget.journal.summary()["calls"])
            ):
                raise PipelineError("Schema generation requires AI enabled and ai.max_calls >= 1")
            await prepare_schema_client(
                config,
                data_dir=data_dir,
                injected_llm=injected_llm,
                stack=stack,
                key=key,
                model=model,
                budget=budget,
                clients=clients,
            )
            client = clients["schema_generation"]
            baseline = getattr(client, "calls", None)
            generated = await generate_schema(
                config.search, client=client, max_fields=config.schema_generation.max_fields
            )
            schema = validate_schema(deepcopy(generated.extraction_schema))
            report.schema_generation.update(
                status="generated",
                model=getattr(client, "model", model),
                plan=generated.plan,
                prompt_version=generated.prompt_version,
                schema_sha256=schema_hash(schema),
            )
        atomic_text(schema_path, json.dumps(schema, ensure_ascii=False, indent=2))
        return schema
    except DurabilityError:
        report.schema_generation.update(status="failed", stop_reason="durability_failure")
        raise
    except CreditsExhausted:
        report.schema_generation.update(status="stopped", stop_reason="credits_exhausted")
        raise
    except Exception as exc:
        report.schema_generation.update(
            status="failed", error={"type": type(exc).__name__, "message": str(exc)}
        )
        report.errors.append(
            {"stage": "schema_generation", "type": type(exc).__name__, "message": str(exc)}
        )
        raise PipelineError(
            "Schema generation/restoration failed; inspect schema-generation.json"
        ) from exc
    finally:
        current = getattr(clients.get("schema_generation"), "calls", None)
        if isinstance(baseline, int) and isinstance(current, int) and current > baseline:
            report.schema_generation["network_used"] = True
        atomic_text(audit_path, json.dumps(report.schema_generation, ensure_ascii=False, indent=2))


def schema_hash(schema: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            schema, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    ).hexdigest()


def contextual_sample(
    pages: list[Page], config: JobConfig, schema: dict[str, Any]
) -> list[dict[str, Any]]:
    """Reuse at most three selected pages, excluding generic page-shell candidates.

    Snapshot IDs and HTML boilerplate do not enter provider cache keys. Evidence is
    bounded to 8,000 characters per page and labelled when a block is shortened.
    """
    from bs4 import BeautifulSoup

    from unified_web_parser.routing.candidates import CandidateBuilder
    from unified_web_parser.sources.navigation import canonical_url

    builder = CandidateBuilder(
        max_candidates=6,
        max_chars=16000,
        schema=schema,
        content_profile=config.content_profile,
        currency_aliases=config.currency_aliases,
    )
    observed = []
    for page in pages:
        blocks = []
        remaining = 8000
        for candidate in builder.build(page):
            if candidate.kind.value != "json_ld":
                visible = BeautifulSoup(candidate.content, "html.parser").get_text(" ", strip=True)
                if len(visible) < 20:
                    continue
                if candidate.locator == "main" and not BeautifulSoup(
                    candidate.content, "html.parser"
                ).find(["article", "table", "dl"]):
                    continue
            content = candidate.content[:remaining]
            if len(content) < 20:
                continue
            blocks.append(
                {
                    "kind": candidate.kind.value,
                    "content": content,
                    "truncated": len(content) < len(candidate.content),
                }
            )
            remaining -= len(content)
            if remaining < 100 or len(blocks) >= 3:
                break
        if blocks:
            observed.append({"source_url": canonical_url(str(page.url)), "blocks": blocks})
            if len(observed) == 3:
                break
    return observed


async def refine_schema(
    config: JobConfig,
    provisional_schema: dict[str, Any],
    pages: list[Page],
    *,
    clients: dict[str, Any],
    report: RunReport,
    run_dir: Path,
) -> dict[str, Any]:
    """Attempt one content-based revision before any record has been extracted.

    Failure preserves the valid provisional contract, with an explicit warning;
    it never retries, fetches pages, or changes a schema after records exist.
    """
    from unified_web_parser.schema_generation import generate_schema

    metadata = report.schema_generation
    metadata["provisional"] = {
        key: deepcopy(metadata.get(key)) for key in ("plan", "schema_sha256", "prompt_version")
    }
    sample = contextual_sample(pages, config, provisional_schema)
    refinement: dict[str, Any] = {
        "status": "no_evidence" if not sample else "running",
        "sample_pages": len(sample),
        "sample_sha256": schema_hash({"pages": sample}),
        "network_used": False,
    }
    metadata["refinement"] = refinement
    evidence_path = run_dir / "schema-evidence.json"
    atomic_text(evidence_path, json.dumps({"pages": sample}, ensure_ascii=False, indent=2))
    report.artifacts["schema_evidence"] = str(evidence_path)
    client = clients.get("schema_generation")
    baseline = getattr(client, "calls", None)
    final_schema = provisional_schema
    try:
        if sample:
            generated = await generate_schema(
                config.search,
                client=client,
                max_fields=config.schema_generation.max_fields,
                observations=sample,
                provisional_plan=metadata.get("plan"),
            )
            final_schema = validate_schema(deepcopy(generated.extraction_schema))
            metadata.update(
                plan=generated.plan,
                prompt_version=generated.prompt_version,
                schema_sha256=schema_hash(final_schema),
            )
            refinement.update(status="refined", model=getattr(client, "model", None))
    except asyncio.CancelledError:
        refinement.update(status="cancelled")
        raise
    except DurabilityError:
        refinement.update(status="failed", stop_reason="durability_failure")
        raise
    except CreditsExhausted:
        refinement.update(status="stopped", stop_reason="credits_exhausted")
        raise
    except Exception as exc:
        refinement.update(status="failed", error={"type": type(exc).__name__, "message": str(exc)})
        report.warnings.append(
            {
                "stage": "schema_refinement",
                "message": "Content-based refinement failed; using the valid provisional schema",
                "type": type(exc).__name__,
                "detail": str(exc),
            }
        )
    finally:
        current = getattr(client, "calls", None)
        if isinstance(baseline, int) and isinstance(current, int) and current > baseline:
            refinement["network_used"] = True
            metadata["network_used"] = True
        atomic_text(run_dir / "schema.json", json.dumps(final_schema, ensure_ascii=False, indent=2))
        atomic_text(
            run_dir / "schema-generation.json", json.dumps(metadata, ensure_ascii=False, indent=2)
        )
    return final_schema

"""Execute bounded query variants and build an auditable effective source set."""

import json
from collections.abc import Callable
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from unified_web_parser.core.config import JobConfig, SourceConfig
from unified_web_parser.core.exceptions import (
    CreditsExhausted,
    DurabilityError,
    PipelineError,
    ProviderError,
)
from unified_web_parser.pipeline.artifacts import atomic_text
from unified_web_parser.pipeline.report import RunReport
from unified_web_parser.providers.openrouter import RequestBudget


async def discover_sources(
    config: JobConfig,
    schema: dict[str, Any],
    *,
    data_dir: Path,
    injected_llm: Any,
    progress: Callable[[str], None],
    stack: AsyncExitStack,
    key: str | None,
    model: str,
    budget: RequestBudget,
    clients: dict[str, Any],
    report: RunReport,
    run_dir: Path,
) -> JobConfig:
    """Resolve web citations without changing the job or silently using fallback roots."""
    from unified_web_parser.discovery.search import plan_queries
    from unified_web_parser.discovery.sites import SiteLedger
    from unified_web_parser.providers.openrouter import OpenRouterClient
    from unified_web_parser.providers.search import OpenRouterSearchClient

    options = config.discovery
    mode = options.query_mode if config.ai.enabled else "literal"
    report.discovery = {
        "status": "running",
        "question": config.search,
        "query_mode": options.query_mode,
        "effective_query_mode": mode,
        "queries": [],
        "site_search": None,
        "requested_results": options.search_results,
        "results_per_query": options.search_results,
        "requested_query_count": options.max_queries,
        "max_sites": options.max_sites,
        "entrypoint": options.entrypoint,
        "site_identity": "registrable_domain_including_private_suffixes",
        "engine": options.engine,
        "mode": options.effective_mode,
        "search_context_size": options.search_context_size,
        "search_contract": "server-tool.v1",
        "executed_query_text": "not_exposed_by_chat_api",
        "results": [],
        "resolved_sources": [],
        "pages_per_site": options.pages_per_site,
        "network_used": False,
        "search_network_used": False,
        "query_outcomes": [],
        "ordering": "citation_encounter_order",
    }
    path = run_dir / "discovery.json"
    report.artifacts["discovery"] = str(path)
    call_baselines = {}
    usage_baselines = {}
    try:
        if config.ai.max_calls < 1 and not (budget.journal and budget.journal.summary()["calls"]):
            raise PipelineError(
                "Web discovery requires ai.max_calls >= 1, including literal search"
            )
        if mode == "literal" and options.query_mode == "ai":
            report.warnings.append(
                {"stage": "discovery", "message": "AI disabled; using literal web-search query"}
            )
        if mode == "ai" and clients.get("query_planning") is None:
            if not key or injected_llm is not None:
                raise PipelineError("AI discovery requires a query_client or OpenRouter key")
            clients["query_planning"] = await stack.enter_async_context(
                OpenRouterClient(
                    api_key=key,
                    model=model,
                    max_calls=max(1, config.ai.max_calls),
                    timeout=config.ai.timeout,
                    cache_dir=data_dir / "ai_cache" if config.ai.cache else None,
                    budget=budget,
                    reasoning=False,
                    max_output_tokens=1024,
                    stage="query_planning",
                )
            )
        if clients.get("web_search") is None:
            if not key or injected_llm is not None:
                raise PipelineError("Web discovery requires a search_client or OpenRouter key")
            clients["web_search"] = await stack.enter_async_context(
                OpenRouterSearchClient(
                    api_key=key,
                    model=model,
                    max_calls=max(1, config.ai.max_calls),
                    timeout=config.ai.timeout,
                    cache_dir=data_dir / "search_cache" if config.ai.cache else None,
                    budget=budget,
                    reasoning=False,
                    max_output_tokens=max(1024, options.search_results * 160),
                    stage="web_search",
                )
            )
        call_baselines = {
            stage: getattr(client, "calls", None)
            for stage, client in clients.items()
            if client is not None
        }
        initial_usage = getattr(clients.get("web_search"), "stats", {})
        usage_baselines = dict(initial_usage) if isinstance(initial_usage, dict) else {}
        try:
            plan = await plan_queries(
                config.search,
                schema,
                client=clients.get("query_planning"),
                max_queries=options.max_queries,
                mode=mode,
            )
        except (CreditsExhausted, DurabilityError):
            raise
        except ProviderError as exc:
            if mode != "ai":
                raise
            report.discovery.update(
                effective_query_mode="literal_fallback",
                planning_error={"type": type(exc).__name__, "message": str(exc)},
            )
            report.warnings.append(
                {
                    "stage": "query_planning",
                    "message": "AI query planning failed; using the original literal question",
                    "detail": str(exc),
                }
            )
            plan = await plan_queries(config.search, schema, mode="literal")
        report.discovery.update(
            queries=list(plan.queries),
            planned_query_count=len(plan.queries),
            site_search=plan.site_search,
            assessment_search=plan.site_search,
        )
        ledger = SiteLedger(max_sites=options.max_sites, entrypoint=options.entrypoint)
        for source in config.sources:
            ledger.add(str(source.url), kind=source.kind, configured=True)
        report.discovery.update(ledger.report())
        for query_index, query in enumerate(plan.queries):
            progress(f"Web search: {query}")
            try:
                batch = await clients["web_search"].search(
                    query,
                    limit=options.search_results,
                    engine=options.engine,
                    mode=options.effective_mode,
                    search_context_size=options.search_context_size,
                    cache_ttl_seconds=options.cache_ttl_seconds,
                )
            except (CreditsExhausted, DurabilityError):
                report.discovery["query_outcomes"].append(
                    {"query": query, "status": "stopped", "reason": "credits_exhausted"}
                )
                report.discovery["query_outcomes"].extend(
                    {"query": remaining, "status": "not_attempted", "reason": "credits_exhausted"}
                    for remaining in plan.queries[query_index + 1 :]
                )
                report.discovery.update(ledger.report())
                raise
            except Exception as exc:
                outcome = {
                    "query": query,
                    "status": "failed",
                    "error": {"type": type(exc).__name__, "message": str(exc)},
                }
                report.discovery["query_outcomes"].append(outcome)
                report.errors.append({"stage": "discovery", "query": query, **outcome["error"]})
                report.discovery.update(ledger.report())
                exhausted = isinstance(exc, ProviderError) and (
                    budget.remaining <= 0 or "request budget exhausted" in str(exc).lower()
                )
                if exhausted:
                    report.discovery["query_outcomes"].extend(
                        {
                            "query": skipped,
                            "status": "not_attempted",
                            "reason": "request_budget_exhausted",
                        }
                        for skipped in plan.queries[query_index + 1 :]
                    )
                    break
                continue  # A failed variant must not discard or suppress other variants.
            if not batch.cache_hit:
                report.discovery["network_used"] = True
                report.discovery["search_network_used"] = True
            report.discovery["query_outcomes"].append(
                {
                    "query": query,
                    "status": "completed",
                    "returned_results": len(batch.results),
                    "cache_hit": batch.cache_hit,
                    "web_search_requests": batch.web_search_requests,
                    "original_web_search_requests": batch.original_web_search_requests,
                    "search_contract": batch.search_contract,
                }
            )
            for result in batch.results[: options.search_results]:
                source = SourceConfig(url=result.url)
                ledger.add(
                    str(source.url),
                    citation={
                        **result.model_dump(mode="json"),
                        "query": query,
                        "provider": batch.provider,
                        "cache_hit": batch.cache_hit,
                        "fetched_at": batch.fetched_at.isoformat(),
                    },
                )
            report.discovery.update(ledger.report())
        sources = [SourceConfig(url=url, kind=kind) for url, kind in ledger.sources]
        completed = any(
            item["status"] == "completed" for item in report.discovery["query_outcomes"]
        )
        if not completed:
            raise PipelineError(
                "Every web-search variant failed; configured sources were not used as fallback"
            )
        if not ledger.results:
            if not sources:
                raise PipelineError("Web search returned no usable source URLs")
            report.warnings.append(
                {
                    "stage": "discovery",
                    "message": (
                        "Web search returned no URLs; "
                        "only explicitly configured sources will be crawled"
                    ),
                }
            )
        report.discovery["status"] = (
            "partial"
            if any(item["status"] != "completed" for item in report.discovery["query_outcomes"])
            else "completed"
        )
        return config.model_copy(update={"sources": sources, "search": plan.site_search})
    except (CreditsExhausted, DurabilityError):
        raise
    except Exception as exc:
        report.discovery.update(
            status="failed", error={"type": type(exc).__name__, "message": str(exc)}
        )
        report.errors.append(
            {"stage": "discovery", "type": type(exc).__name__, "message": str(exc)}
        )
        raise PipelineError("Web discovery failed; inspect discovery.json") from exc
    finally:
        final_usage = getattr(clients.get("web_search"), "stats", {})
        if isinstance(final_usage, dict):
            report.discovery["web_search_usage"] = {
                name: final_usage[name] - usage_baselines.get(name, 0)
                for name in (
                    "web_search_requests",
                    "reported_web_search_requests",
                    "cached_web_search_requests",
                    "search_usage_violations",
                )
                if type(final_usage.get(name)) is int and type(usage_baselines.get(name, 0)) is int
            }
        for stage, baseline in call_baselines.items():
            current = getattr(clients.get(stage), "calls", None)
            if isinstance(baseline, int) and isinstance(current, int) and current > baseline:
                report.discovery["network_used"] = True
                if stage == "web_search":
                    report.discovery["search_network_used"] = True
        atomic_text(path, json.dumps(report.discovery, ensure_ascii=False, indent=2))

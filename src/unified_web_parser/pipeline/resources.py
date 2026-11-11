"""Composition helpers for independently accounted clients sharing one request budget."""

import os
from collections.abc import MutableMapping
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from unified_web_parser.core.config import AIConfig, JobConfig
from unified_web_parser.core.exceptions import PipelineError
from unified_web_parser.pipeline.report import RunReport
from unified_web_parser.providers.http import HttpFetcher
from unified_web_parser.providers.openrouter import RequestBudget


async def openrouter_client(
    stack: AsyncExitStack,
    *,
    api_key: str,
    model: str,
    ai: AIConfig,
    budget: RequestBudget,
    cache_dir: Path | None,
    extraction: bool = False,
    stage: str | None = None,
) -> Any:
    from unified_web_parser.providers.openrouter import OpenRouterClient

    client_class = OpenRouterClient
    options = {"response_mode": "auto"}
    if model.endswith("-decisions"):
        from unified_web_parser.providers.decisions import OpenRouterDecisionsClient

        client_class = OpenRouterDecisionsClient
        options = {}
    elif extraction:
        options = {
            "response_mode": ai.response_mode,
            "reasoning": ai.reasoning,
            "max_output_tokens": ai.max_output_tokens,
        }
    return await stack.enter_async_context(
        client_class(
            api_key=api_key,
            model=model,
            max_calls=max(1, ai.max_calls),
            timeout=ai.timeout,
            cache_dir=cache_dir,
            budget=budget,
            stage=stage,
            **options,
        )
    )


def resolve_models(
    config: JobConfig,
    *,
    model_override: str | None = None,
    decision_model_override: str | None = None,
) -> tuple[str, str]:
    extraction = model_override or os.getenv("OPENROUTER_MODEL") or config.ai.model
    decision = (
        decision_model_override
        or os.getenv("OPENROUTER_DECISION_MODEL")
        or config.ai.decision_model
    )
    if any(
        "online" in (part.casefold() for part in model.split(":")[1:])
        for model in (extraction, decision)
    ):
        raise PipelineError("The deprecated :online modifier causes implicit web search")
    return extraction, decision


async def prepare_page_clients(
    clients: MutableMapping[str, Any],
    config: JobConfig,
    *,
    stack: AsyncExitStack,
    key: str | None,
    extraction_model: str,
    decision_model: str,
    budget: RequestBudget,
    cache_dir: Path | None,
    injected_llm: Any,
    replay: bool,
    report: RunReport,
) -> None:
    """Register clients immediately so partial setup remains visible to accounting."""
    recoverable = bool(budget.journal and budget.journal.summary()["calls"])
    available = config.ai.max_calls > 0 or recoverable
    if clients["extraction"] is None and config.ai.enabled and available:
        if key:
            clients["extraction"] = await openrouter_client(
                stack,
                api_key=key,
                model=extraction_model,
                ai=config.ai,
                budget=budget,
                cache_dir=cache_dir,
                extraction=True,
                stage="extraction",
            )
        else:
            report.warnings.append(
                {
                    "stage": "ai",
                    "message": "No OpenRouter key; using deterministic extraction",
                }
            )
    if (
        clients["routing"] is None
        and injected_llm is None
        and config.ai.enabled
        and available
        and key
        and config.ai.routing != "rules"
    ):
        clients["routing"] = await openrouter_client(
            stack,
            api_key=key,
            model=decision_model,
            ai=config.ai,
            budget=budget,
            cache_dir=cache_dir,
            stage="routing",
        )
    if (
        clients["navigation"] is None
        and not replay
        and injected_llm is None
        and config.ai.enabled
        and available
        and key
        and config.navigation.mode != "rules"
    ):
        clients["navigation"] = await openrouter_client(
            stack,
            api_key=key,
            model=decision_model,
            ai=config.ai,
            budget=budget,
            cache_dir=cache_dir,
            stage="navigation",
        )


async def prepare_fetcher(
    fetcher: Any, config: JobConfig, schema: dict[str, Any], *, stack: AsyncExitStack, replay: bool
) -> Any:
    """Open the selected provider only for online runs, preserving injected instances."""
    if fetcher is None and not replay:
        fetcher_class = HttpFetcher
        if config.crawl.backend == "browser":
            from unified_web_parser.providers.crawl4ai import Crawl4AIFetcher

            fetcher_class = Crawl4AIFetcher
        fetch_options = {
            "timeout": config.crawl.timeout,
            "retries": config.crawl.retries,
            "delay": config.crawl.delay,
            "max_bytes": config.crawl.max_bytes,
        }
        if config.crawl.backend == "browser":
            fetch_options["render_delay"] = config.crawl.render_delay
            fetch_options["allow_filters"] = config.navigation.allow_filters
            fetch_options["schema"] = schema
            fetch_options["content_profile"] = config.content_profile
            fetch_options["currency_aliases"] = config.currency_aliases
        fetcher = await stack.enter_async_context(fetcher_class(**fetch_options))
    return fetcher

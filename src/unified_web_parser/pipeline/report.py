"""Persistent, machine-readable run outcomes, including partial failures."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from unified_web_parser.core.config import AIConfig
    from unified_web_parser.providers.openrouter import RequestBudget


class RunReport(BaseModel):
    run_id: str
    job_name: str
    status: Literal["running", "success", "partial", "failed", "stopped", "cancelled"] = "running"
    stop_reason: str | None = None
    stop_message: str | None = None
    credit_limit_source: str | None = None
    started_at: datetime
    finished_at: datetime | None = None
    pages_attempted: int = 0
    pages_fetched: int = 0
    records_written: int = 0
    invalid_records: int = 0
    admission: dict[str, Any] = Field(default_factory=dict)
    incremental: dict[str, Any] = Field(default_factory=dict)
    errors: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[dict[str, Any]] = Field(default_factory=list)
    artifacts: dict[str, str] = Field(default_factory=dict)
    ai: dict[str, Any] = Field(default_factory=dict)
    navigation: dict[str, Any] = Field(default_factory=dict)
    discovery: dict[str, Any] = Field(default_factory=dict)
    schema_generation: dict[str, Any] = Field(default_factory=dict)


def collect_ai_usage(
    clients: Mapping[str, Any], *, ai: AIConfig, budget: RequestBudget | None
) -> dict[str, Any]:
    """Account shared clients once and expose cost only with measured metadata."""
    journal = getattr(budget, "journal", None)
    if journal is not None and (
        journal.summary()["calls"]
        or any(getattr(client, "budget", None) is budget for client in clients.values() if client)
    ):
        usage = journal.summary()
        usage.update(
            max_calls=ai.max_calls,
            budget_used=budget.used,
            model=getattr(clients.get("extraction"), "model", ai.model),
            decision_model=getattr(clients.get("routing"), "model", None),
        )
        for stage, client in clients.items():
            if client is None:
                continue
            stats = getattr(client, "stats", {})
            group = usage["stages"].setdefault(
                stage,
                {
                    "calls": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "known_cost_usd": 0.0,
                    "cost_usd": 0.0,
                    "cost_complete": True,
                    "cost_pending_calls": 0,
                    "model": getattr(client, "model", None),
                },
            )
            group["cache_hits"] = stats.get("cache_hits", 0)
        # Durable attempts are cumulative. Optional cache reads never add paid
        # attempts, while current process cache hits remain useful diagnostics.
        usage["cache_hits"] = sum(
            getattr(client, "stats", {}).get("cache_hits", 0)
            for client in {id(value): value for value in clients.values() if value}.values()
        )
        if budget.stop_reason:
            usage.update(
                stop_reason=budget.stop_reason, credit_limit_source=budget.credit_limit_source
            )
        return usage
    usage: dict[str, Any] = {}
    stages = {}
    accounted_clients = {}
    for stage, client in clients.items():
        if client is not None:
            if id(client) in accounted_clients:
                stages[stage] = {
                    "model": getattr(client, "model", None),
                    "shared_with": accounted_clients[id(client)],
                }
                continue
            accounted_clients[id(client)] = stage
            stats = getattr(client, "stats", {})
            stages[stage] = {
                "model": getattr(client, "model", None),
                **(dict(stats) if isinstance(stats, dict) else {}),
            }
    if stages:
        usage = {
            name: sum(stats.get(name, 0) for stats in stages.values())
            for name in ("calls", "cache_hits", "input_tokens", "output_tokens")
        }
        usage.update(
            {
                "model": getattr(clients.get("extraction"), "model", ai.model),
                "decision_model": getattr(clients.get("routing"), "model", None),
                "stages": stages,
                "max_calls": ai.max_calls,
            }
        )
        paid_stages = [stage for stage in stages.values() if stage.get("calls", 0)]
        measured_stages = [stage for stage in stages.values() if "shared_with" not in stage]
        known = [
            stage.get("known_cost_usd", stage.get("cost_usd"))
            for stage in measured_stages
            if isinstance(stage.get("known_cost_usd", stage.get("cost_usd")), int | float)
        ]
        trackable = all("calls" in stage for stage in measured_stages)
        usage["cost_complete"] = trackable and all(
            stage.get("cost_complete", "cost_usd" in stage or not stage.get("calls", 0))
            for stage in measured_stages
        )
        usage["cost_pending_calls"] = sum(
            stage.get("cost_pending_calls", 0 if "cost_usd" in stage else stage.get("calls", 0))
            for stage in measured_stages
        )
        usage["in_flight_calls"] = sum(stage.get("in_flight_calls", 0) for stage in measured_stages)
        if known or trackable and not usage["calls"]:
            usage["known_cost_usd"] = sum(known)
        if paid_stages and all("cost_usd" in stage for stage in paid_stages):
            usage["cost_usd"] = sum(stage["cost_usd"] for stage in paid_stages)
        elif not paid_stages and usage["cache_hits"]:
            usage["cost_usd"] = 0.0
        if "web_search" in stages:
            usage["cost_scope"] = (
                "Reported provider usage; no independent billing total is inferred"
            )
        search_stats = stages.get("web_search", {})
        if type(search_stats.get("web_search_requests")) is int:
            usage["web_search_requests"] = search_stats["web_search_requests"]
        if budget is not None:
            usage["budget_used"] = budget.used
            if budget.stop_reason:
                usage.update(
                    stop_reason=budget.stop_reason, credit_limit_source=budget.credit_limit_source
                )
    return usage

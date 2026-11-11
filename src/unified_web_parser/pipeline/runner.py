"""Bounded schema-driven pipeline, shared by CLI, API and offline reprocessing."""

import asyncio
import json
import os
from collections.abc import Callable
from contextlib import AsyncExitStack
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from unified_web_parser.core.config import JobConfig, LoadedJob
from unified_web_parser.core.exceptions import CreditsExhausted, DurabilityError, PipelineError
from unified_web_parser.core.models import Record
from unified_web_parser.pipeline.artifacts import (
    SnapshotWriter,
    atomic_text,
    load_replay,
    read_manifest,
    restore_discovery,
    write_job_snapshot,
)
from unified_web_parser.pipeline.checkpoints import CheckpointError, CheckpointStore, has_checkpoint
from unified_web_parser.pipeline.crawl import CrawlEngine, refresh_coverage
from unified_web_parser.pipeline.discovery import discover_sources
from unified_web_parser.pipeline.events import EventCallback, emit_event
from unified_web_parser.pipeline.incremental import IncrementalSession, _saved_job
from unified_web_parser.pipeline.persistence import persist_completed_records
from unified_web_parser.pipeline.report import RunReport, collect_ai_usage
from unified_web_parser.pipeline.resources import (
    prepare_fetcher,
    prepare_page_clients,
    resolve_models,
)
from unified_web_parser.pipeline.schema_lifecycle import prepare_schema, prepare_schema_client
from unified_web_parser.pipeline.schema_session import SchemaSession
from unified_web_parser.storage import DatasetStore
from unified_web_parser.storage.run_lock import RunLease


class PipelineRunner:
    def __init__(
        self,
        job: LoadedJob,
        *,
        data_dir: Path = Path("data"),
        fetcher: Any = None,
        llm: Any = None,
        store: Any = None,
        progress: Callable[[str], None] | None = None,
        model_override: str | None = None,
        decision_model_override: str | None = None,
        decision_client: Any = None,
        navigation_client: Any = None,
        query_client: Any = None,
        search_client: Any = None,
        schema_client: Any = None,
        restore_generated_schema: bool = True,
        on_event: EventCallback | None = None,
    ):
        self.job = job
        self.data_dir = Path(data_dir).resolve()
        self.fetcher = fetcher
        self.llm = llm
        self.store = store
        self.on_event = on_event

        def publish_progress(message: str) -> None:
            if progress is not None:
                progress(message)
            emit_event(self.on_event, "log", {"level": "info", "message": message})

        self.progress = publish_progress
        self.model_override = model_override
        self.decision_model_override = decision_model_override
        self.decision_client = decision_client
        self.navigation_client = navigation_client
        self.query_client = query_client
        self.search_client = search_client
        self.schema_client = schema_client
        self.restore_generated_schema = restore_generated_schema

    async def run(
        self,
        *,
        replay_dir: Path | None = None,
        run_id: str | None = None,
        resume_dir: Path | None = None,
        lease: RunLease | None = None,
    ) -> RunReport:
        """Execute under one OS lease; resume restores this run's durable frontier."""
        if resume_dir is not None and replay_dir is not None:
            raise PipelineError("Resume and snapshot reprocessing are separate operations")
        run_id = run_id or (Path(resume_dir).name if resume_dir else uuid4().hex)
        run_dir = (Path(resume_dir) if resume_dir else self.data_dir / "runs" / run_id).resolve()
        if run_dir.name != run_id or run_dir.parent != self.data_dir / "runs":
            raise PipelineError("Run directory must match the run ID inside this data directory")
        if resume_dir is None:
            run_dir.mkdir(parents=True, exist_ok=False)
        elif not has_checkpoint(run_dir):
            raise PipelineError("Run has no durable checkpoint to resume")
        owned_lease = lease or RunLease(run_dir)
        if owned_lease.run_dir != run_dir:
            raise PipelineError("Run lease belongs to another directory")
        if not owned_lease.held:
            owned_lease.acquire()
        try:
            checkpoint = CheckpointStore(run_dir)
            saved = checkpoint.load() if resume_dir else {}
            if saved.get("completed"):
                raise PipelineError(
                    "Run has already completed; there is no unfinished work to resume"
                )
            return await self._execute(
                run_id=run_id,
                run_dir=run_dir,
                replay_dir=replay_dir,
                checkpoint=checkpoint,
                saved=saved,
            )
        finally:
            owned_lease.release()

    async def _execute(
        self,
        *,
        run_id: str,
        run_dir: Path,
        replay_dir: Path | None,
        checkpoint: CheckpointStore,
        saved: dict[str, Any],
    ) -> RunReport:
        config = self.job.config
        if saved:
            previous = JobConfig.model_validate(saved["config"]).model_dump(mode="json")
            current = config.model_dump(mode="json")
            for section, names in {
                "crawl": ("max_pages", "max_records", "timeout", "retries"),
                "ai": ("enabled", "max_calls", "timeout"),
            }.items():
                for name in names:
                    previous[section][name] = current[section][name]
            if previous != current:
                raise PipelineError(
                    "Resume preserves the saved sources, schema and provider settings; "
                    "only limits, timeouts, retries and disabling AI may change"
                )
            replay_dir = Path(saved["replay_dir"]) if saved.get("replay_dir") else None
        snapshots = self.data_dir / "snapshots"
        snapshots.mkdir(parents=True, exist_ok=True)
        report = RunReport(
            run_id=run_id,
            job_name=config.name,
            started_at=datetime.now(UTC),
            artifacts={"directory": str(run_dir), "summary": str(run_dir / "summary.json")},
        )
        if saved:
            previous_summary = run_dir / "summary.json"
            if previous_summary.is_file():
                history = run_dir / "resume-history" / f"{saved.get('resume_count', 0) + 1}.json"
                atomic_text(history, previous_summary.read_text(encoding="utf-8"))
            report = RunReport.model_validate(saved["report"])
            if previous_summary.is_file():
                report.artifacts["resume_history"] = str(run_dir / "resume-history")
            report.status = "running"
            report.finished_at = None
            report.stop_reason = report.stop_message = report.credit_limit_source = None
        store = None
        pg_store = None
        store_started = pg_started = False
        configuration = config.model_dump(mode="json")
        write_job_snapshot(configuration, saved.get("schema", self.job.extraction_schema), run_dir)
        records_by_id: dict[str, Record] = {
            value["record_id"]: Record.model_validate(value) for value in saved.get("records", [])
        }
        page_clients = {
            "extraction": self.llm,
            "routing": self.decision_client,
            "navigation": None if replay_dir is not None else self.navigation_client,
        }
        discovery_clients = (
            {}
            if replay_dir is not None
            else {
                "query_planning": self.query_client,
                "web_search": self.search_client,
            }
        )
        schema_clients = (
            {"schema_generation": self.schema_client}
            if config.schema_generation.enabled and replay_dir is None
            else {}
        )
        effective_schema = deepcopy(saved.get("schema", self.job.extraction_schema))
        effective_config = (
            JobConfig.model_validate(saved["effective_config"])
            if saved.get("effective_config")
            else config
        )
        if saved.get("effective_config"):
            effective_config = effective_config.model_copy(
                update={"crawl": config.crawl, "ai": config.ai}
            )
        update_from = self.job.update_from_run or (
            Path(saved["update_from_run"]) if saved.get("update_from_run") else None
        )
        if update_from is not None and not saved:
            parent = _saved_job(update_from)
            site_search = parent.get("effective_config", {}).get("search") or parent["report"].get(
                "discovery", {}
            ).get("site_search")
            if isinstance(site_search, str):
                effective_config = config.model_copy(update={"search": site_search})
        budget = None
        incremental = None
        engine = None
        pages = []
        durability_failed = False
        checkpoint.update(
            config=configuration,
            schema=effective_schema,
            report=report.model_dump(mode="json"),
            completed=False,
            phase=saved.get("phase", "preparing"),
            resume_count=saved.get("resume_count", 0) + bool(saved),
            replay_dir=str(replay_dir) if replay_dir else None,
            update_from_run=str(update_from) if update_from else None,
            incremental_completion_protocol=1,
        )
        manifest = run_dir / "pages.jsonl"
        if manifest.is_file() and read_manifest(manifest, repair_tail=True)[1]:
            report.warnings.append(
                {
                    "stage": "recovery",
                    "message": "Quarantined a torn trailing snapshot-manifest append",
                }
            )

        def publish(kind: str, data: dict[str, Any]) -> None:
            # Event observers remain optional. Critical record/frontier writes use
            # their own synchronous transaction boundaries below.
            if self.on_event is not None:
                emit_event(
                    self.on_event,
                    kind,
                    {
                        **data,
                        "counters": {
                            "pages_attempted": report.pages_attempted,
                            "pages_fetched": report.pages_fetched,
                            "records_found": len(records_by_id),
                            "invalid_records": report.invalid_records,
                        },
                        "errors": report.errors,
                        "warnings": report.warnings,
                        "records_persisted": report.records_written,
                        "stop_reason": report.stop_reason,
                        "stop_message": report.stop_message,
                        "credit_limit_source": report.credit_limit_source,
                        "ai": collect_ai_usage(
                            {**page_clients, **discovery_clients, **schema_clients},
                            ai=config.ai,
                            budget=budget,
                        ),
                    },
                )

        def commit_record(record: Record) -> None:
            try:
                # Per-run authority is written first, so later same-job runs can
                # never replace this run's recoverable evidence envelope.
                checkpoint.save_record(record)
                store.save_records(run_id, [record], [])
                if pg_store is not None:
                    pg_store.save_records(run_id, [record], [])
                report.records_written = len(set(records_by_id) | {record.record_id})
                checkpoint.update(report=report.model_dump(mode="json"))
            except DurabilityError:
                raise
            except Exception as exc:
                raise CheckpointError("Unable to durably commit a validated record") from exc

        def save_completed() -> list[Record]:
            if engine is not None:
                engine.reconcile_processing()
            return persist_completed_records(
                config,
                effective_config,
                records_by_id,
                run_id=run_id,
                run_dir=run_dir,
                store=store,
                pg_store=pg_store,
                report=report,
                publish=publish,
            )

        publish("stage", {"stage": "preparing", "status": "running"})
        try:
            store = self.store or DatasetStore(self.data_dir)
            store.start_run(run_id, config.name, configuration)
            store_started = True
            if "postgres" in config.outputs:
                database_url = os.getenv("DATABASE_URL")
                if not database_url:
                    raise PipelineError(
                        "postgres output requires DATABASE_URL in environment or .env"
                    )
                pg_store = DatasetStore(self.data_dir, database_url=database_url)
                pg_store.start_run(run_id, config.name, configuration)
                pg_started = True
            if records_by_id:
                try:
                    restored_records = list(records_by_id.values())
                    store.save_records(run_id, restored_records, [])
                    if pg_store is not None:
                        pg_store.save_records(run_id, restored_records, [])
                    report.records_written = len(restored_records)
                    checkpoint.update(report=report.model_dump(mode="json"))
                except DurabilityError:
                    raise
                except Exception as exc:
                    raise CheckpointError(
                        "Unable to reconcile recovered records with storage"
                    ) from exc
            async with AsyncExitStack() as stack:
                from unified_web_parser.providers.openrouter import RequestBudget
                from unified_web_parser.providers.usage_journal import UsageJournal

                journal = UsageJournal(run_dir / "usage.jsonl")
                if saved:
                    journal.begin_resume()
                budget = RequestBudget(config.ai.max_calls, journal=journal)
                observed_budgets = {id(budget): budget}
                for client in {**page_clients, **discovery_clients, **schema_clients}.values():
                    client_budget = getattr(client, "budget", None)
                    if isinstance(client_budget, RequestBudget):
                        observed_budgets[id(client_budget)] = client_budget
                for observed_budget in observed_budgets.values():
                    stack.callback(observed_budget.observe(lambda: publish("usage", {})))
                key = os.getenv("OPENROUTER_API_KEY")
                if saved.get("resolved_models"):
                    extraction_model = saved["resolved_models"]["extraction"]
                    decision_model = saved["resolved_models"]["decision"]
                    if (
                        self.model_override is not None
                        and self.model_override != extraction_model
                        or self.decision_model_override is not None
                        and self.decision_model_override != decision_model
                    ):
                        raise PipelineError(
                            "Resume cannot change the saved resolved provider models"
                        )
                else:
                    extraction_model, decision_model = resolve_models(
                        config,
                        model_override=self.model_override,
                        decision_model_override=self.decision_model_override,
                    )
                checkpoint.update(
                    resolved_models={
                        "extraction": extraction_model,
                        "decision": decision_model,
                    }
                )
                cache_dir = self.data_dir / "ai_cache" if config.ai.cache else None
                restore_auto = (
                    replay_dir is not None
                    and self.restore_generated_schema
                    and (Path(replay_dir) / "schema-generation.json").is_file()
                )
                if (config.schema_generation.enabled or restore_auto) and not saved.get(
                    "schema_prepared"
                ):
                    publish("stage", {"stage": "schema_generation"})
                    effective_schema = await prepare_schema(
                        config,
                        data_dir=self.data_dir,
                        injected_llm=self.llm,
                        replay_dir=replay_dir,
                        stack=stack,
                        key=key,
                        model=extraction_model,
                        budget=budget,
                        clients=schema_clients,
                        report=report,
                        run_dir=run_dir,
                    )
                    write_job_snapshot(configuration, effective_schema, run_dir)
                if (
                    saved.get("schema_prepared")
                    and not saved.get("schema_frozen")
                    and config.schema_generation.enabled
                    and config.ai.enabled
                    and replay_dir is None
                ):
                    await prepare_schema_client(
                        config,
                        data_dir=self.data_dir,
                        injected_llm=self.llm,
                        stack=stack,
                        key=key,
                        model=extraction_model,
                        budget=budget,
                        clients=schema_clients,
                    )
                checkpoint.update(
                    schema=effective_schema,
                    schema_prepared=True,
                    report=report.model_dump(mode="json"),
                    phase="discovery",
                )
                publish("schema", {"schema": deepcopy(effective_schema)})
                if saved.get("discovery_prepared"):
                    pass
                elif replay_dir is not None:
                    effective_config = restore_discovery(config, Path(replay_dir), report)
                elif config.discovery.enabled:
                    publish("stage", {"stage": "discovery"})
                    effective_config = await discover_sources(
                        config,
                        effective_schema,
                        data_dir=self.data_dir,
                        injected_llm=self.llm,
                        progress=self.progress,
                        stack=stack,
                        key=key,
                        model=extraction_model,
                        budget=budget,
                        clients=discovery_clients,
                        report=report,
                        run_dir=run_dir,
                    )
                checkpoint.update(
                    effective_config=effective_config.model_dump(mode="json"),
                    discovery_prepared=True,
                    report=report.model_dump(mode="json"),
                    phase="crawling",
                )
                await prepare_page_clients(
                    page_clients,
                    config,
                    stack=stack,
                    key=key,
                    extraction_model=extraction_model,
                    decision_model=decision_model,
                    budget=budget,
                    cache_dir=cache_dir,
                    injected_llm=self.llm,
                    replay=replay_dir is not None,
                    report=report,
                )
                fetcher = await prepare_fetcher(
                    self.fetcher,
                    config,
                    effective_schema,
                    stack=stack,
                    replay=replay_dir is not None,
                )
                if config.incremental.enabled and replay_dir is None:
                    incremental = IncrementalSession(
                        config=config,
                        schema=effective_schema,
                        data_dir=self.data_dir,
                        run_dir=run_dir,
                        checkpoint=checkpoint,
                        report=report,
                        resolved_models={
                            "extraction": extraction_model,
                            "decision": decision_model,
                        },
                        parent=update_from,
                    )
                    stack.callback(incremental.close)
                session = SchemaSession(
                    config,
                    effective_schema,
                    contextual=config.schema_generation.enabled
                    and config.ai.enabled
                    and replay_dir is None
                    and not saved.get("schema_frozen"),
                    llm=page_clients["extraction"],
                    decision_client=page_clients["routing"],
                    schema_clients=schema_clients,
                    records=records_by_id,
                    report=report,
                    run_dir=run_dir,
                    progress=self.progress,
                    on_event=publish if self.on_event is not None else None,
                    checkpoint=checkpoint,
                    commit_record=commit_record,
                    incremental=incremental,
                )
                await session.start()
                save = SnapshotWriter(run_dir, snapshots)
                if replay_dir:
                    pages = load_replay(
                        Path(replay_dir),
                        schema=effective_schema,
                        content_profile=config.content_profile,
                        currency_aliases=config.currency_aliases,
                    )
                    replay_started = set(saved.get("replay_started", []))
                    for page in pages[: config.crawl.max_pages]:
                        if checkpoint.page_done(page.page_id):
                            continue
                        if page.page_id not in replay_started:
                            report.pages_attempted += 1
                            report.pages_fetched += 1
                            replay_started.add(page.page_id)
                            checkpoint.update(replay_started=sorted(replay_started))
                        publish("counters", {"stage": "replay"})
                        page = save(page)
                        checkpoint.update(report=report.model_dump(mode="json"))
                        await session.process(page)
                        if len(records_by_id) >= config.crawl.max_records:
                            break
                else:
                    engine = CrawlEngine(
                        effective_config,
                        effective_schema,
                        fetcher=fetcher,
                        report=report,
                        run_dir=run_dir,
                        save_page=save,
                        extract_page=session.process,
                        record_count=lambda: len(records_by_id),
                        record_pages=lambda: (
                            record.provenance.page_id for record in records_by_id.values()
                        ),
                        navigation_client=page_clients["navigation"],
                        progress=self.progress,
                        on_event=publish if self.on_event is not None else None,
                        checkpoint=checkpoint,
                        incremental=incremental,
                    )
                    await engine.run()
                    await session.finalize()
                    engine.reconcile_processing()
            records = save_completed()
            rejected = report.admission.get("rejected", 0)
            reused = report.incremental.get("skipped_detail_urls", 0) + report.incremental.get(
                "skipped_records", 0
            )
            clean_empty = (
                not records and (rejected > 0 or reused > 0) and not report.invalid_records
            )
            report.status = (
                ("partial" if report.errors else "success") if records or clean_empty else "failed"
            )
            if clean_empty:
                report.warnings.append(
                    {
                        "stage": "admission",
                        "message": (
                            "No new records; completed compatible detail entities were reused"
                            if reused
                            else (
                                f"No matching records; admission rejected {rejected} "
                                "incompatible entities"
                            )
                        ),
                    }
                )
            elif not records and not report.errors:
                report.errors.append(
                    {
                        "stage": "extraction",
                        "message": "No valid records; inspect snapshots and routing plans",
                    }
                )
        except CreditsExhausted as exc:
            report.status = "stopped"
            report.stop_reason = exc.stop_reason
            report.stop_message = exc.stop_message
            report.credit_limit_source = exc.limit_source
            if budget is not None:
                budget.stop_credits(exc.limit_source)
            if report.navigation:
                report.navigation["stop_reason"] = exc.stop_reason
            if report.discovery:
                report.discovery.update(status="stopped", stop_reason=exc.stop_reason)
            publish("stage", {"stage": "stopping"})
            try:
                save_completed()
            except Exception as error:
                report.status = "failed"
                report.errors.append(
                    {"stage": "storage", "type": type(error).__name__, "message": str(error)}
                )
        except asyncio.CancelledError:
            report.status = "cancelled"
            report.stop_reason = "user_cancelled"
            report.stop_message = "Збір зупинено користувачем."
            if report.navigation:
                report.navigation["stop_reason"] = report.stop_reason
            try:
                save_completed()
            except Exception as error:
                report.errors.append(
                    {"stage": "storage", "type": type(error).__name__, "message": str(error)}
                )
            raise
        except DurabilityError as exc:
            durability_failed = True
            report.status = "failed"
            report.errors.append(
                {"stage": "durability", "type": type(exc).__name__, "message": str(exc)}
            )
        except Exception as exc:
            report.status = "failed"
            try:
                if store_started:
                    save_completed()
            except Exception as error:
                report.errors.append(
                    {"stage": "storage", "type": type(error).__name__, "message": str(error)}
                )
            report.errors.append({"stage": "run", "type": type(exc).__name__, "message": str(exc)})
        finally:
            report.ai = collect_ai_usage(
                {**page_clients, **discovery_clients, **schema_clients},
                ai=config.ai,
                budget=budget,
            )
            report.finished_at = datetime.now(UTC)
            if report.discovery:
                report.discovery["crawl_status"] = report.status
                discovery_path = run_dir / "discovery.json"
                report.artifacts["discovery"] = str(discovery_path)
                atomic_text(
                    discovery_path, json.dumps(report.discovery, ensure_ascii=False, indent=2)
                )
            if report.navigation:
                refresh_coverage(report.navigation, report.errors)
            for target, started in ((pg_store, pg_started), (store, store_started)):
                if started:
                    try:
                        target.finish_run(run_id, report.model_dump(mode="json"))
                    except Exception as exc:
                        report.status = "failed"
                        report.errors.append(
                            {"stage": "storage", "message": str(exc), "type": type(exc).__name__}
                        )
            if report.navigation:
                refresh_coverage(report.navigation, report.errors)
            summary = report.model_dump(mode="json")
            atomic_text(run_dir / "summary.json", json.dumps(summary, ensure_ascii=False, indent=2))
            if not durability_failed:
                state = checkpoint.load()
                frontier = state.get("crawl", {})
                replay_unfinished = bool(replay_dir) and any(
                    not checkpoint.page_done(page.page_id) for page in pages
                )
                completed = (
                    not replay_unfinished
                    and report.status in {"success", "partial"}
                    and not frontier.get("queue")
                    and not frontier.get("inflight")
                    and not state.get("schema_pending")
                )
                checkpoint.update(
                    report=summary,
                    schema=effective_schema,
                    completed=completed,
                    phase="finished" if completed else "interrupted",
                )
            publish("finished", {"report": summary, "stage": "finished"})
        return report

"""Incremental run composition: frozen job updates and audited detail reuse."""

import asyncio
import json
import re
from pathlib import Path
from threading import Event
from typing import Any
from urllib.parse import urljoin, urlsplit

from unified_web_parser.core.config import JobConfig, LoadedJob
from unified_web_parser.core.exceptions import PipelineError
from unified_web_parser.core.models import CandidateBlock, CandidateKind, Page
from unified_web_parser.core.schema import validate_schema
from unified_web_parser.extraction.projection import dom_entity
from unified_web_parser.navigation import InventoryBuilder
from unified_web_parser.pipeline.artifacts import read_manifest
from unified_web_parser.pipeline.checkpoints import CheckpointStore, has_checkpoint
from unified_web_parser.pipeline.report import RunReport
from unified_web_parser.sources.navigation import canonical_url
from unified_web_parser.storage import DatasetStore
from unified_web_parser.storage.incremental import IncrementalLedger, processing_signature
from unified_web_parser.storage.run_lock import RunLease


def _saved_job(run_dir: Path) -> dict[str, Any]:
    if has_checkpoint(run_dir):
        return CheckpointStore(run_dir).load()
    job = json.loads((run_dir / "job.json").read_text(encoding="utf-8"))
    report = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    return {"config": job["config"], "schema": job["schema"], "report": report}


def load_update_job(run_dir: Path, *, lease: RunLease | None = None) -> LoadedJob:
    """Fork a finished run's exact schema/question into a new incremental job run."""
    run_dir = Path(run_dir).resolve()
    if lease is None:
        with RunLease(run_dir) as owned:
            return load_update_job(run_dir, lease=owned)
    if not lease.held or lease.run_dir != run_dir:
        raise PipelineError("Update source requires its held run lease")
    saved = _saved_job(run_dir)
    if has_checkpoint(run_dir) and not saved.get("completed"):
        raise PipelineError(
            "This run has unfinished checkpoint work; resume it before creating an update"
        )
    report = RunReport.model_validate(saved["report"])
    if report.status not in {"success", "partial"} or report.finished_at is None:
        raise PipelineError("Update requires a finished successful or partial collection")
    schema = validate_schema(saved["schema"])
    config = dict(saved["config"])
    effective = saved.get("effective_config", {})
    sources = effective.get("sources") or config.get("sources", [])
    if not sources:
        sources = [{"url": url} for url in report.discovery.get("resolved_sources", [])]
    assembly_path = run_dir / "assembly.json"
    if assembly_path.is_file():
        assembly = json.loads(assembly_path.read_text(encoding="utf-8"))
        for reference in assembly.get("sources", []):
            identifier = reference.get("run_id") if isinstance(reference, dict) else None
            if not isinstance(identifier, str) or not re.fullmatch(
                r"[A-Za-z0-9_-]{1,128}", identifier
            ):
                raise PipelineError("Assembly source has an unsafe run identifier")
            source_run = (run_dir.parent / identifier).resolve()
            if source_run.parent != run_dir.parent:
                raise PipelineError("Assembly source must stay inside the same runs directory")
            source_job = source_run / "job.json"
            if not source_job.is_file():
                raise PipelineError(f"Declared assembly source job is missing: {identifier}")
            original = json.loads(source_job.read_text(encoding="utf-8"))
            declared = JobConfig.model_validate(original["config"])
            sources = [*sources, *[source.model_dump(mode="json") for source in declared.sources]]
    # Explicit legacy assemblies may combine observed catalogs beyond the final
    # producer's configuration. Recover only catalog URLs proven by their HTML.
    manifest = run_dir / "pages.jsonl"
    if (
        not has_checkpoint(run_dir) or (run_dir / "assembly.json").is_file()
    ) and manifest.is_file():
        entries, torn = read_manifest(manifest)
        if torn:
            raise PipelineError("Update source has a torn snapshot manifest")
        rows_path = run_dir / "records.jsonl"
        groups: dict[str, set[str]] = {}
        if rows_path.is_file():
            for line in rows_path.read_text(encoding="utf-8").splitlines():
                row = json.loads(line)
                source = canonical_url(row["provenance"]["source_url"])
                value = row.get("data", {}).get("url")
                if isinstance(value, str):
                    groups.setdefault(source, set()).add(canonical_url(value))
        builder = InventoryBuilder(
            schema=schema,
            content_profile=config.get("content_profile", "generic"),
            currency_aliases=config.get("currency_aliases", {}),
            max_actions=5,
            max_summary_chars=1000,
            allow_scroll=False,
        )
        for entry in {row["page_id"]: row for row in entries}.values():
            source = canonical_url(entry["url"])
            if len(groups.get(source, set())) < 2 or source in groups[source]:
                continue
            snapshot = Path(entry["snapshot_ref"])
            snapshot = snapshot if snapshot.is_absolute() else run_dir / snapshot
            message = {key: value for key, value in entry.items() if key in Page.model_fields}
            message["html"] = snapshot.read_text(encoding="utf-8")
            inventory = builder.build(Page.model_validate(message), query=config.get("search"))
            if inventory.has_results and not inventory.has_entity:
                sources = [*sources, {"url": entry["url"], "kind": "url"}]
    sources = list({canonical_url(str(item["url"])): item for item in sources}.values())
    config.update(sources=sources, incremental={"enabled": True})
    config["schema_path"] = config.get("schema_path") or str(run_dir / "schema.json")
    config["schema_generation"] = {**config.get("schema_generation", {}), "enabled": False}
    config["discovery"] = {**config.get("discovery", {}), "enabled": False}
    models = saved.get("resolved_models", {})
    config["ai"] = {
        **config.get("ai", {}),
        **{
            key: models[field]
            for key, field in (("model", "extraction"), ("decision_model", "decision"))
            if field in models
        },
    }
    return LoadedJob(
        config=JobConfig.model_validate(config),
        extraction_schema=schema,
        config_path=run_dir / "job.json",
        update_from_run=run_dir,
    )


class IncrementalSession:
    """Own one processing scope lease and skip only its proven complete details."""

    def __init__(
        self,
        *,
        config: JobConfig,
        schema: dict[str, Any],
        data_dir: Path,
        run_dir: Path,
        checkpoint: CheckpointStore,
        report: RunReport,
        resolved_models: dict[str, str],
        parent: Path | None = None,
    ):
        self.config, self.schema, self.data_dir = config, schema, data_dir
        self.run_dir, self.checkpoint, self.report = run_dir, checkpoint, report
        self.models, self.parent = resolved_models, parent
        self.ledger: IncrementalLedger | None = None
        self.lease = None
        self.skipped_urls: set[str] = set()
        self.skipped_record_urls: set[str] = set()
        state = checkpoint.load().get("incremental_state", {})
        self.skipped_urls = set(state.get("skipped_urls", []))
        self.skipped_record_urls = set(state.get("skipped_record_urls", []))
        self.marked: set[str] = set(state.get("marked", []))
        self._schema_frozen = False
        self.active_ids: set[str] = set()
        self.cancel_seed = Event()
        report.incremental = report.incremental or {
            "enabled": True,
            "parent_run_id": parent.name if parent else None,
            "dataset_scope": "new_records_only",
            "skipped_detail_urls": 0,
            "skipped_records": 0,
            "completed_details": 0,
        }

    def ready(self) -> bool:
        if not self._schema_frozen:
            self._schema_frozen = bool(self.checkpoint.load().get("schema_frozen"))
            if not self._schema_frozen:
                return False
        if self.ledger is None:
            signature = processing_signature(self.config, self.schema, resolved_models=self.models)
            ledger = IncrementalLedger(self.data_dir, self.config.name, signature)
            lease = ledger.scope_lease().acquire()
            self.ledger, self.lease = ledger, lease
            self.active_ids = DatasetStore(self.data_dir).active_record_ids(self.config.name)
            self.report.incremental["signature"] = signature
            if self.parent is not None:
                from unified_web_parser.storage.incremental import seed_from_run

                self.report.incremental["seed"] = seed_from_run(
                    self.parent,
                    ledger,
                    allowed_record_ids=self.active_ids,
                    cancelled=self.cancel_seed.is_set,
                )
        return True

    async def start(self) -> None:
        """Inspect legacy evidence off the API loop while retaining exclusive ownership."""
        if self.ledger is not None:
            return
        task = asyncio.create_task(asyncio.to_thread(self.ready))
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
                self.cancel_seed.set()
        task.result()
        if cancelled:
            raise asyncio.CancelledError

    def _save_state(self) -> None:
        self.checkpoint.update(
            incremental_state={
                "skipped_urls": sorted(self.skipped_urls),
                "skipped_record_urls": sorted(self.skipped_record_urls),
                "marked": sorted(self.marked),
            },
            report=self.report.model_dump(mode="json"),
        )

    def close(self) -> None:
        if self.lease:
            self.lease.release()

    def known_detail(self, url: str, *, record: bool = False) -> bool:
        if not self.ready():
            return False
        completion = self.ledger.lookup(url)
        if completion is None:
            return False
        if completion.outcome == "processed" and not set(completion.record_ids) <= self.active_ids:
            self.ledger.invalidate(url)
            return False
        values = self.skipped_record_urls if record else self.skipped_urls
        identity = canonical_url(url)
        if identity in values:
            return True
        values.add(identity)
        self.checkpoint.save_plan(
            ("incremental-record:" if record else "incremental-detail:") + identity,
            {
                "url": identity,
                "completed_run_id": completion.last_run_id,
                "outcome": completion.outcome,
                "record_ids": list(completion.record_ids),
            },
        )
        self.report.incremental["skipped_records" if record else "skipped_detail_urls"] = len(
            values
        )
        self._save_state()
        return True

    def candidate_known(self, page: Page, candidate: CandidateBlock) -> bool:
        if not self.ready():
            return False
        if candidate.kind == CandidateKind.JSON_LD:
            try:
                entity = json.loads(candidate.content)
            except (ValueError, TypeError):
                return False
            url = entity.get("url", entity.get("@id")) if isinstance(entity, dict) else None
        elif candidate.kind == CandidateKind.DOM:
            if candidate.locator != "entity" and not (candidate.locator or "").startswith("card:"):
                return False
            if (candidate.locator or "").startswith("card:"):
                from bs4 import BeautifulSoup

                try:
                    links = {
                        canonical_url(urljoin(str(page.url), anchor["href"]))
                        for anchor in BeautifulSoup(candidate.content, "html.parser").find_all(
                            "a", href=True
                        )
                        if urlsplit(urljoin(str(page.url), anchor["href"])).scheme
                        in {"http", "https"}
                    }
                except ValueError:
                    return False
                if len(links) != 1:
                    return False
            entity = dom_entity(
                candidate.content,
                str(page.url),
                card=candidate.locator != "entity",
                content_profile=self.config.content_profile,
                currency_aliases=self.config.currency_aliases,
            )
            url = entity.get("url")
        else:
            return False
        if not isinstance(url, str):
            return False
        try:
            resolved = urljoin(str(page.url), url)
            if urlsplit(resolved).scheme not in {"http", "https"}:
                return False
            return self.known_detail(resolved, record=True)
        except ValueError:
            # Bad source metadata is not proof that an entity was already processed.
            return False

    def complete_page(self, page: Page) -> None:
        if not self.ready():
            return
        detail = self.checkpoint.plan("detail:" + page.page_id) or {}
        result = self.checkpoint.plan("page-result:" + page.page_id) or {}
        if not detail.get("confirmed") or not result.get("successful"):
            return
        if not self.checkpoint.page_done(page.page_id):
            return
        ids = result.get("own_record_ids", [])
        rejected = result.get("own_rejections", 0)
        if not ids and not rejected:
            return
        url = str(page.url)
        self.ledger.complete_detail(
            url,
            run_id=self.report.run_id,
            record_ids=ids,
            outcome="processed" if ids else "rejected",
            detail_confirmed=True,
            persisted=True,
        )
        self.active_ids.update(ids)
        self.marked.add(canonical_url(url))
        self.report.incremental["completed_details"] = len(self.marked)
        self._save_state()

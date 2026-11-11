"""Freeze a run's contextual schema before processing its already-fetched pages."""

from collections.abc import Callable, MutableMapping
from copy import deepcopy
from pathlib import Path
from typing import Any

from unified_web_parser.core.config import JobConfig
from unified_web_parser.core.models import Page, Record
from unified_web_parser.pipeline.artifacts import write_job_snapshot
from unified_web_parser.pipeline.checkpoints import CheckpointStore
from unified_web_parser.pipeline.events import EventCallback, emit_event
from unified_web_parser.pipeline.pages import PageProcessor
from unified_web_parser.pipeline.report import RunReport
from unified_web_parser.pipeline.schema_lifecycle import contextual_sample, refine_schema


class SchemaSession:
    """Own the bounded sample and one page processor for the effective run schema.

    Navigation and providers keep the same schema dictionary throughout the run.
    A contextual session updates that dictionary in place, then constructs its
    processor and drains saved pages. Manual and replay sessions start directly.
    """

    def __init__(
        self,
        config: JobConfig,
        schema: dict[str, Any],
        *,
        contextual: bool,
        llm: Any,
        decision_client: Any,
        schema_clients: dict[str, Any],
        records: MutableMapping[str, Record],
        report: RunReport,
        run_dir: Path,
        progress: Callable[[str], None],
        on_event: EventCallback | None = None,
        checkpoint: CheckpointStore | None = None,
        commit_record: Callable[[Record], None] | None = None,
        incremental: Any = None,
    ) -> None:
        self.config = config
        self.schema = schema
        self.contextual = contextual
        self.llm = llm
        self.decision_client = decision_client
        self.schema_clients = schema_clients
        self.records = records
        self.report = report
        self.run_dir = run_dir
        self.progress = progress
        self.on_event = on_event
        self.checkpoint = checkpoint
        self.commit_record = commit_record
        self.incremental = incremental
        self._pending_pages: list[Page] = []
        self._representative_pages: list[Page] = []
        self._processor: PageProcessor | None = None

    async def start(self) -> None:
        if self.checkpoint:
            saved = self.checkpoint.load()
            self._pending_pages = [
                Page.model_validate(page) for page in saved.get("schema_pending", [])
            ]
            for page in self._pending_pages:
                restored = self._restore_page(page)
                if contextual_sample([restored], self.config, self.schema):
                    self._representative_pages.append(restored)
            if saved.get("schema_frozen"):
                self.contextual = False
        if not self.contextual:
            await self.finalize()

    @staticmethod
    def _restore_page(page: Page) -> Page:
        if not page.html and page.snapshot_ref:
            return page.model_copy(
                update={"html": Path(page.snapshot_ref).read_text(encoding="utf-8")}
            )
        return page

    async def process(self, page: Page) -> None:
        if self.checkpoint and self.checkpoint.page_done(page.page_id):
            if self.incremental:
                self.incremental.complete_page(page)
            return
        if self._processor is not None:
            await self._processor.process(page)
            return
        if any(pending.page_id == page.page_id for pending in self._pending_pages):
            return
        if contextual_sample([page], self.config, self.schema):
            self._representative_pages.append(page)
            self._pending_pages.append(page)
        else:
            # Saved shells wait on disk, not in an unbounded HTML buffer.
            self._pending_pages.append(
                page.model_copy(update={"html": ""}) if page.snapshot_ref else page
            )
        if self.checkpoint:
            self.checkpoint.update(
                schema_pending=[
                    page.model_dump(mode="json", exclude={"html"}) | {"html": ""}
                    if page.snapshot_ref
                    else page.model_dump(mode="json")
                    for page in self._pending_pages
                ]
            )
        if len(self._representative_pages) >= 3:
            await self.finalize()

    async def finalize(self) -> None:
        """Freeze once and flush a short sample when the frontier ends."""
        if self._processor is not None:
            return
        if self.contextual:
            emit_event(self.on_event, "stage", {"stage": "schema_refinement"})
            refined = await refine_schema(
                self.config,
                self.schema,
                self._representative_pages,
                clients=self.schema_clients,
                report=self.report,
                run_dir=self.run_dir,
            )
            final_contract = deepcopy(refined)
            self.schema.clear()
            self.schema.update(final_contract)
            write_job_snapshot(self.config.model_dump(mode="json"), self.schema, self.run_dir)
            emit_event(self.on_event, "schema", {"schema": deepcopy(self.schema)})
        if self.checkpoint:
            self.checkpoint.update(
                schema=self.schema, schema_frozen=True, report=self.report.model_dump(mode="json")
            )
        if self.incremental:
            await self.incremental.start()
        self._processor = PageProcessor(
            self.config,
            self.schema,
            llm=self.llm,
            decision_client=self.decision_client,
            records=self.records,
            report=self.report,
            run_dir=self.run_dir,
            progress=self.progress,
            on_event=self.on_event,
            checkpoint=self.checkpoint,
            commit_record=self.commit_record,
            incremental=self.incremental,
        )
        buffered = list(self._pending_pages)
        self._pending_pages.clear()
        for page in buffered:
            await self._processor.process(self._restore_page(page))
            if len(self.records) >= self.config.crawl.max_records:
                break

        if self.checkpoint:
            self.checkpoint.update(
                schema_pending=[
                    page.model_dump(mode="json", exclude={"html"}) | {"html": ""}
                    if page.snapshot_ref
                    else page.model_dump(mode="json")
                    for page in buffered
                    if not self.checkpoint.page_done(page.page_id)
                ]
            )

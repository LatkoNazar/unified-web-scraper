"""Route and validate page evidence while preserving factual precedence and bounds."""

import json
import os
from collections.abc import Callable, MutableMapping
from pathlib import Path
from typing import Any

from unified_web_parser.core.config import JobConfig
from unified_web_parser.core.exceptions import CreditsExhausted, DurabilityError
from unified_web_parser.core.models import Page, Record, RoutingContext, RoutingPlan
from unified_web_parser.extraction.schema import SchemaExtractor
from unified_web_parser.pipeline.artifacts import read_manifest
from unified_web_parser.pipeline.checkpoints import CheckpointStore
from unified_web_parser.pipeline.events import EventCallback, emit_event
from unified_web_parser.pipeline.report import RunReport
from unified_web_parser.processing.admission import admit_record
from unified_web_parser.processing.validation import validate_records
from unified_web_parser.routing.candidates import CandidateBuilder
from unified_web_parser.routing.engines import (
    LunaDecisionEngine,
    OpenRouterDecisionEngine,
    RuleDecisionEngine,
    can_route_structured,
)
from unified_web_parser.sources.navigation import canonical_url


class PageProcessor:
    def __init__(
        self,
        config: JobConfig,
        schema: dict[str, Any],
        *,
        llm: Any,
        decision_client: Any,
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
        self.records = records
        self.report = report
        self.run_dir = run_dir
        self.progress = progress
        self.on_event = on_event
        self.checkpoint = checkpoint
        self.commit_record = commit_record
        self.incremental = incremental
        self._admissions = checkpoint.plans("admission:") if checkpoint else {}
        self._admission_counts()
        for filename in ("admission.jsonl", "rejected-records.jsonl"):
            path = run_dir / filename
            if path.exists() and read_manifest(path, repair_tail=True)[1]:
                report.warnings.append(
                    {"stage": "admission", "message": f"Repaired torn {filename} tail"}
                )
        self.builder = CandidateBuilder(
            max_candidates=config.ai.max_candidates,
            max_chars=config.ai.max_candidate_chars,
            schema=schema,
            content_profile=config.content_profile,
            currency_aliases=config.currency_aliases,
            skip_url=(lambda url: incremental.known_detail(url, record=True))
            if incremental
            else None,
        )
        self.extractor = SchemaExtractor(
            job_name=config.name,
            llm=llm,
            sparse_enrichment=config.ai.sparse_enrichment,
            content_profile=config.content_profile,
            currency_aliases=config.currency_aliases,
        )
        self.rule_engine = RuleDecisionEngine()
        self.ai_engine = None
        if decision_client:
            self.ai_engine = (
                LunaDecisionEngine(decision_client)
                if hasattr(decision_client, "decide")
                else OpenRouterDecisionEngine(decision_client)
            )
        elif llm:
            self.ai_engine = OpenRouterDecisionEngine(llm)

    def _admission_counts(self) -> None:
        self.report.admission = {
            "version": 1,
            **{
                status: sum(value["status"] == status for value in self._admissions.values())
                for status in ("accepted", "review", "rejected")
            },
        }

    def _admit(self, record: Record) -> bool:
        decision = admit_record(
            record,
            self.schema,
            self.config.search,
            assessment_profile=self.config.assessment_profile,
            relevance_fields=self.config.relevance_fields,
        )
        value = decision.model_dump(mode="json")
        previous = self._admissions.get(record.record_id)
        if previous != value:
            audit = {
                "record_id": record.record_id,
                "source_url": str(record.provenance.source_url),
                **value,
            }
            try:
                path = self.run_dir / "admission.jsonl"
                with path.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(audit, ensure_ascii=False) + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                self.report.artifacts["admission"] = str(path)
                if decision.status == "rejected":
                    rejected = self.run_dir / "rejected-records.jsonl"
                    with rejected.open("a", encoding="utf-8") as stream:
                        stream.write(
                            json.dumps(
                                {"record": record.model_dump(mode="json"), "admission": value},
                                ensure_ascii=False,
                            )
                            + "\n"
                        )
                        stream.flush()
                        os.fsync(stream.fileno())
                    self.report.artifacts["rejected_records"] = str(rejected)
            except OSError as exc:
                raise DurabilityError("Unable to persist record admission evidence") from exc
            if self.checkpoint:
                self.checkpoint.save_plan("admission:" + record.record_id, value)
            self._admissions[record.record_id] = value
            if previous is not None:
                self.report.admission[previous["status"]] -= 1
            self.report.admission[decision.status] += 1
        return decision.status != "rejected"

    async def process(self, page: Page) -> None:
        builder, extractor = self.builder, self.extractor
        rule_engine, ai_engine = self.rule_engine, self.ai_engine
        records, report, run_dir = self.records, self.report, self.run_dir
        schema = self.schema
        config = self.config
        if self.checkpoint and self.checkpoint.page_done(page.page_id):
            if self.incremental:
                self.incremental.complete_page(page)
            return
        emit_event(self.on_event, "stage", {"stage": "routing", "url": str(page.url)})
        errors_before, invalid_before = len(report.errors), report.invalid_records
        warnings_before = len(report.warnings)
        prior_outcome = (
            self.checkpoint.plan("page-result:" + page.page_id) if self.checkpoint else None
        )
        own_ids: set[str] = set((prior_outcome or {}).get("own_record_ids", []))
        own_rejections = (prior_outcome or {}).get("own_rejections", 0)
        try:
            candidates = tuple(builder.build(page))
            existing_plan = (
                self.checkpoint.plan("extraction:" + page.page_id) if self.checkpoint else None
            )
            if self.incremental and existing_plan is None:
                candidates = tuple(
                    candidate
                    for candidate in candidates
                    if not self.incremental.candidate_known(page, candidate)
                )
            if not candidates:
                report.warnings.append(
                    {
                        "stage": "candidates",
                        "url": str(page.url),
                        "message": "Page has no usable extraction candidates",
                    }
                )
            context = RoutingContext(
                page_id=page.page_id,
                source_url=page.url,
                extraction_schema=schema,
                candidates=candidates,
            )
            engine = ai_engine if ai_engine and config.ai.routing == "ai" else rule_engine
            if config.ai.routing == "hybrid" and ai_engine:
                # Skip AI routing only for one described entity bound to this page.
                engine = rule_engine if can_route_structured(context) else ai_engine
            saved_plan = (
                self.checkpoint.plan("extraction:" + page.page_id) if self.checkpoint else None
            )
            try:
                plan = (
                    RoutingPlan.model_validate(saved_plan)
                    if saved_plan is not None
                    else await engine.select(context)
                ).validate_against(context)
            except (CreditsExhausted, DurabilityError):
                raise
            except Exception as exc:
                if saved_plan is not None or engine is rule_engine:
                    raise
                report.warnings.append(
                    {
                        "stage": "routing",
                        "url": str(page.url),
                        "message": str(exc),
                        "fallback": "rules",
                    }
                )
                plan = (await rule_engine.select(context)).validate_against(context)
            if self.checkpoint:
                self.checkpoint.save_plan(
                    "extraction:" + page.page_id, plan.model_dump(mode="json")
                )
            with (run_dir / "routing.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(plan.model_dump(mode="json"), ensure_ascii=False) + "\n")
            if not any(target.strategy.value != "skip" for target in plan.targets):
                report.warnings.append(
                    {
                        "stage": "routing",
                        "url": str(page.url),
                        "message": "No relevant page blocks selected",
                    }
                )
            by_id = {candidate.candidate_id: candidate for candidate in candidates}
            for target in plan.targets:
                if target.strategy.value == "skip":
                    continue
                if self.checkpoint and self.checkpoint.candidate_done(
                    page.page_id, target.candidate_id
                ):
                    continue
                candidate = by_id[target.candidate_id]
                if self.incremental and self.incremental.candidate_known(page, candidate):
                    if self.checkpoint:
                        self.checkpoint.complete_candidate(page.page_id, target.candidate_id)
                    continue
                emit_event(self.on_event, "stage", {"stage": "extraction", "url": str(page.url)})
                try:
                    extracted = await extractor.extract(
                        page,
                        candidate,
                        target.strategy,
                        schema,
                    )
                except (CreditsExhausted, DurabilityError):
                    raise
                except Exception as exc:
                    report.errors.append(
                        {
                            "stage": "extraction",
                            "url": str(page.url),
                            "candidate_id": candidate.candidate_id,
                            "type": type(exc).__name__,
                            "message": str(exc),
                        }
                    )
                    # Provider failure must not discard explicit structured/DOM facts.
                    if extractor.llm is None:
                        continue
                    extracted = await SchemaExtractor(
                        job_name=config.name,
                        content_profile=config.content_profile,
                        currency_aliases=config.currency_aliases,
                    ).extract(
                        page,
                        candidate,
                        target.strategy,
                        schema,
                    )
                valid, errors = validate_records(extracted, schema)
                report.invalid_records += len(errors)
                for error in errors:
                    report.warnings.append({"stage": "validation", "url": str(page.url), **error})
                truncated = False
                for record in valid:
                    previous = records.get(record.record_id)
                    if previous:
                        # Earlier explicit facts take precedence; later blocks fill gaps only.
                        merged = {
                            **record.data,
                            **{
                                key: value
                                for key, value in previous.data.items()
                                if value is not None
                            },
                        }
                        provenance = previous.provenance.model_copy(
                            update={
                                "candidate_ids": tuple(
                                    dict.fromkeys(
                                        previous.provenance.candidate_ids
                                        + record.provenance.candidate_ids
                                    )
                                ),
                            }
                        )
                        merged_record = previous.model_copy(
                            update={
                                "data": merged,
                                "provenance": provenance,
                                "img_url": previous.img_url or record.img_url,
                            }
                        )
                        merged_valid, merge_errors = validate_records(
                            [merged_record],
                            schema,
                        )
                        if not merged_valid:
                            report.warnings.append(
                                {
                                    "stage": "merge",
                                    "url": str(page.url),
                                    "message": "Conflicting evidence kept separate",
                                    "errors": merge_errors,
                                }
                            )
                            continue
                        record = merged_valid[0]
                    elif len(records) >= config.crawl.max_records:
                        truncated = True
                        break
                    record_url = record.data.get("url")
                    own = isinstance(record_url, str) and canonical_url(
                        record_url
                    ) == canonical_url(str(page.url))
                    if not self._admit(record):
                        own_rejections += own
                        continue
                    if self.commit_record is not None:
                        self.commit_record(record)
                    records[record.record_id] = record
                    if own:
                        own_ids.add(record.record_id)
                    if self.on_event is not None:
                        emit_event(
                            self.on_event, "record", {"record": record.model_dump(mode="json")}
                        )
                    self.progress(f"Record: {record.data.get('title', record.record_id)}")
                if self.checkpoint and self.incremental:
                    prior = self.checkpoint.plan("page-result:" + page.page_id) or {}
                    self.checkpoint.save_plan(
                        "page-result:" + page.page_id,
                        {
                            "successful": prior.get("successful", True)
                            and len(report.errors) == errors_before
                            and report.invalid_records == invalid_before
                            and len(report.warnings) == warnings_before,
                            "own_record_ids": sorted(
                                own_ids | set(prior.get("own_record_ids", []))
                            ),
                            "own_rejections": own_rejections,
                        },
                    )
                if self.checkpoint and not truncated:
                    self.checkpoint.complete_candidate(page.page_id, target.candidate_id)
                if len(records) >= config.crawl.max_records:
                    break
            if self.checkpoint and all(
                target.strategy.value == "skip"
                or self.checkpoint.candidate_done(page.page_id, target.candidate_id)
                for target in plan.targets
            ):
                previous_result = self.checkpoint.plan("page-result:" + page.page_id) or {}
                self.checkpoint.save_plan(
                    "page-result:" + page.page_id,
                    {
                        "successful": previous_result.get("successful", True)
                        and len(report.errors) == errors_before
                        and report.invalid_records == invalid_before
                        and len(report.warnings) == warnings_before,
                        "own_record_ids": sorted(
                            own_ids | set(previous_result.get("own_record_ids", []))
                        ),
                        "own_rejections": own_rejections,
                    },
                )
                self.checkpoint.complete_page(page.page_id)
                if self.incremental:
                    self.incremental.complete_page(page)
        except (CreditsExhausted, DurabilityError):
            raise
        except Exception as exc:
            report.errors.append(
                {
                    "stage": "page",
                    "url": str(page.url),
                    "type": type(exc).__name__,
                    "message": str(exc),
                }
            )
        finally:
            emit_event(self.on_event, "counters", {"stage": "extraction"})

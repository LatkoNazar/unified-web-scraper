"""Durable cross-run detail completion, separate from ephemeral crawl frontiers.

Catalog traversal is deliberately not persisted here. A completion means that a
confirmed detail's processing and its records (or explicit rejection) committed.
"""

import hashlib
import json
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from unified_web_parser.core.config import AIConfig
from unified_web_parser.core.exceptions import DurabilityError
from unified_web_parser.sources.navigation import canonical_url
from unified_web_parser.storage.run_lock import RunLease

PROCESSING_VERSION = "incremental-processing.v1/extraction.v3"


class IncrementalError(DurabilityError):
    """A required detail completion could not be read or durably committed."""


def _json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def processing_signature(
    config: Any,
    schema: Mapping[str, Any],
    *,
    resolved_models: Mapping[str, Any] | None = None,
    processing_version: str = PROCESSING_VERSION,
) -> str:
    """Fingerprint processing semantics, never source lists, run IDs or safety caps.

    Call only after freezing the effective schema and resolving model overrides.
    Explicit model overrides use ``extraction`` and ``decision`` keys, matching
    the recovery checkpoint. No credentials or complete configuration are stored.
    """
    raw = config.model_dump(mode="json") if hasattr(config, "model_dump") else dict(config)
    ai = {**AIConfig().model_dump(mode="json"), **raw.get("ai", {})}
    models = dict(resolved_models or {})
    ai["model"] = models.get("extraction", ai["model"])
    ai["decision_model"] = models.get("decision", ai["decision_model"])
    context = {
        "version": processing_version,
        "schema": dict(schema),
        "query": raw.get("search"),
        "content_profile": raw.get("content_profile", "generic"),
        "assessment_profile": raw.get("assessment_profile", "generic"),
        "currency_aliases": raw.get("currency_aliases", {}),
        "relevance_fields": raw.get("relevance_fields"),
        "assessment": raw.get("assessment", []),
        "ai": {
            key: ai[key]
            for key in (
                "enabled",
                "model",
                "decision_model",
                "response_mode",
                "reasoning",
                "max_output_tokens",
                "sparse_enrichment",
                "routing",
                "max_candidates",
                "max_candidate_chars",
            )
        },
    }
    return hashlib.sha256(_json(context).encode("utf-8")).hexdigest()


def _url(url: str) -> str:
    try:
        value = canonical_url(url.strip())
        parsed = urlsplit(value)
        _ = parsed.port
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError
        if parsed.username or parsed.password or any(char.isspace() for char in parsed.netloc):
            raise ValueError
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("Detail completion requires a valid HTTP(S) URL") from exc
    return value


@dataclass(frozen=True)
class Completion:
    job_name: str
    signature: str
    canonical_url: str
    last_run_id: str
    outcome: Literal["processed", "rejected"]
    record_ids: tuple[str, ...]
    completed_at: str


class IncrementalLedger:
    """SQLite FULL-sync authority keyed by job, processing context and detail URL.

    A crash before ``complete_detail`` causes safe reprocessing; it cannot create
    a false completion. Callers must commit all records/admission decisions first.
    Explicitly invalidate entries when a later correction quarantines their rows.
    """

    def __init__(self, data_dir: Path, job_name: str, signature: str) -> None:
        if not job_name or not signature:
            raise ValueError("job_name and processing signature must be nonempty")
        self.path = Path(data_dir) / "incremental.sqlite"
        self.job_name = job_name
        self.signature = signature
        scope = hashlib.sha256(_json([job_name, signature]).encode("utf-8")).hexdigest()
        self.scope_dir = self.path.parent / "incremental-locks" / scope
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._connection() as connection:
                connection.execute("""CREATE TABLE IF NOT EXISTS detail_completions (
                    job_name TEXT NOT NULL, signature TEXT NOT NULL, url TEXT NOT NULL,
                    last_run_id TEXT NOT NULL, outcome TEXT NOT NULL
                        CHECK(outcome IN ('processed','rejected')),
                    record_ids TEXT NOT NULL, completed_at TEXT NOT NULL,
                    PRIMARY KEY(job_name,signature,url))""")
        except OSError as exc:
            raise IncrementalError("Unable to initialize incremental completion ledger") from exc

    def scope_lease(self) -> RunLease:
        """Hold once around the run to prevent concurrent misses and duplicate billing."""
        return RunLease(self.scope_dir)

    @contextmanager
    def _connection(self):
        connection = None
        try:
            connection = sqlite3.connect(self.path, timeout=30)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA synchronous=FULL")
            with connection:
                yield connection
        except (sqlite3.Error, OSError) as exc:
            raise IncrementalError(
                "Unable to read or commit incremental completion ledger"
            ) from exc
        finally:
            if connection is not None:
                connection.close()

    def lookup(self, url: str) -> Completion | None:
        identity = _url(url)
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM detail_completions WHERE job_name=? AND signature=? AND url=?",
                (self.job_name, self.signature, identity),
            ).fetchone()
        if row is None:
            return None
        try:
            identifiers = json.loads(row["record_ids"])
            if (
                not isinstance(identifiers, list)
                or any(not isinstance(item, str) or not item.strip() for item in identifiers)
                or row["outcome"] not in {"processed", "rejected"}
                or (row["outcome"] == "processed" and not identifiers)
                or not isinstance(row["last_run_id"], str)
                or not row["last_run_id"].strip()
                or not isinstance(row["completed_at"], str)
                or not row["completed_at"].strip()
            ):
                raise ValueError("Invalid completion payload")
            return Completion(
                self.job_name,
                self.signature,
                row["url"],
                row["last_run_id"],
                row["outcome"],
                tuple(identifiers),
                row["completed_at"],
            )
        except (TypeError, ValueError) as exc:
            raise IncrementalError("Invalid incremental completion metadata") from exc

    def known_detail(self, url: str) -> bool:
        return self.lookup(url) is not None

    def complete_detail(
        self,
        url: str,
        *,
        run_id: str,
        record_ids: Sequence[str] = (),
        outcome: Literal["processed", "rejected"] = "processed",
        detail_confirmed: bool,
        persisted: bool,
    ) -> Completion:
        """Mark only after confirmed detail processing and durable result admission.

        No-record outcomes require an explicit rejection; empty extraction,
        validation failures, HTTP errors and interrupted work are not completion.
        """
        if detail_confirmed is not True or persisted is not True:
            raise ValueError("Detail evidence and durable processing are both required")
        if outcome not in {"processed", "rejected"}:
            raise ValueError("Completion outcome must be processed or explicitly rejected")
        if (
            not isinstance(run_id, str)
            or not run_id.strip()
            or isinstance(record_ids, str)
            or any(not isinstance(value, str) or not value.strip() for value in record_ids)
        ):
            raise ValueError("Completion requires a run ID and valid record IDs")
        ids = tuple(sorted(set(record_ids)))
        if outcome == "processed" and not ids:
            raise ValueError("Processed detail completion requires committed records")
        completion = Completion(
            self.job_name,
            self.signature,
            _url(url),
            run_id,
            outcome,
            ids,
            datetime.now(UTC).isoformat(),
        )
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO detail_completions VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(job_name,signature,url) DO UPDATE SET "
                "last_run_id=excluded.last_run_id,outcome=excluded.outcome,"
                "record_ids=excluded.record_ids,completed_at=excluded.completed_at",
                (
                    self.job_name,
                    self.signature,
                    completion.canonical_url,
                    run_id,
                    outcome,
                    _json(ids),
                    completion.completed_at,
                ),
            )
        return completion

    mark_completed = complete_detail

    def invalidate(self, url: str) -> bool:
        """Make a corrected/quarantined detail eligible again in this context."""
        with self._connection() as connection:
            cursor = connection.execute(
                "DELETE FROM detail_completions WHERE job_name=? AND signature=? AND url=?",
                (self.job_name, self.signature, _url(url)),
            )
            return bool(cursor.rowcount)

    def seed_finished_run(
        self,
        run_dir: Path,
        *,
        allowed_record_ids: set[str] | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> dict[str, int]:
        """Import proven own-detail records from an immutable terminal legacy run.

        Catalog-only rows remain eligible for actual detail enrichment. No legacy
        zero-record rejection is inferred. Callers can restrict IDs to currently
        admitted dataset rows, excluding later quarantine/corrections.
        """
        directory = Path(run_dir)
        summary = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
        job = json.loads((directory / "job.json").read_text(encoding="utf-8"))
        if summary.get("status") not in {"success", "partial"} or not summary.get("finished_at"):
            return {"seeded": 0, "skipped": 0}
        models = None
        page_results: dict[str, dict[str, Any]] = {}
        completed_pages: set[str] = set()
        completion_protocol = False
        checkpoint = directory / "checkpoint.sqlite"
        if checkpoint.exists():
            with sqlite3.connect(checkpoint.resolve().as_uri() + "?mode=ro", uri=True) as con:
                row = con.execute(
                    "SELECT value FROM metadata WHERE key='resolved_models'"
                ).fetchone()
                if row:
                    models = json.loads(row[0])
                row = con.execute(
                    "SELECT value FROM metadata WHERE key='incremental_completion_protocol'"
                ).fetchone()
                completion_protocol = bool(row and json.loads(row[0]))
                tables = {
                    value[0]
                    for value in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
                }
                if "plans" in tables:
                    page_results = {
                        key.removeprefix("page-result:"): json.loads(payload)
                        for key, payload in con.execute(
                            "SELECT page_id,payload FROM plans WHERE page_id LIKE 'page-result:%'"
                        )
                    }
                    completion_protocol = completion_protocol or bool(page_results)
                if completion_protocol and "candidates" in tables:
                    completed_pages = {
                        value[0]
                        for value in con.execute(
                            "SELECT page_id FROM candidates WHERE candidate_id='__page_complete__'"
                        )
                    }
        if (
            job["config"].get("name") != self.job_name
            or processing_signature(job["config"], job["schema"], resolved_models=models)
            != self.signature
        ):
            return {"seeded": 0, "skipped": 0}
        failed_urls, failed_pages = set(), set()
        unresolved = list(summary.get("errors", [])) + [
            warning
            for warning in summary.get("warnings", [])
            if warning.get("stage") in {"routing", "extraction", "validation"}
        ]
        if any(
            not item.get("url") and not item.get("source_url") and not item.get("page_id")
            for item in unresolved
        ):
            return {"seeded": 0, "skipped": 0}
        for error in unresolved:
            for key in ("url", "source_url"):
                if error.get(key):
                    try:
                        failed_urls.add(_url(error[key]))
                    except ValueError:
                        pass
            if error.get("page_id"):
                failed_pages.add(error["page_id"])
        grouped: dict[str, list[str]] = {}
        skipped = 0
        # Migration proves page roles from saved evidence; a source's own catalog
        # heading/URL is not a successfully processed detail.
        from unified_web_parser.core.models import Record
        from unified_web_parser.crawling.pages import make_page
        from unified_web_parser.navigation.inventory import InventoryBuilder
        from unified_web_parser.processing.admission import admit_record
        from unified_web_parser.processing.validation import validate_records

        config = job["config"]
        builder = InventoryBuilder(
            schema=job["schema"],
            content_profile=config.get("content_profile", "generic"),
            currency_aliases=config.get("currency_aliases", {}),
        )
        detail_pages: dict[tuple[str, str], bool] = {}
        for line in (directory / "records.jsonl").read_text(encoding="utf-8").splitlines():
            if cancelled is not None and cancelled():
                return {"seeded": 0, "skipped": skipped, "cancelled": True}
            if not line.strip():
                continue
            record = json.loads(line)
            provenance, data = record.get("provenance", {}), record.get("data", {})
            identifier = record.get("record_id")
            try:
                url = _url(data.get("url"))
                source = _url(provenance.get("source_url"))
            except ValueError:
                skipped += 1
                continue
            if (
                not identifier
                or (allowed_record_ids is not None and identifier not in allowed_record_ids)
                or record.get("excluded_at")
                or url != source
                or url in failed_urls
                or provenance.get("page_id") in failed_pages
                or not provenance.get("snapshot_ref")
                or not Path(provenance["snapshot_ref"]).is_file()
            ):
                skipped += 1
                continue
            page_id = provenance.get("page_id")
            if completion_protocol:
                result = page_results.get(page_id, {})
                own_ids = result.get("own_record_ids") if isinstance(result, dict) else None
                if (
                    page_id not in completed_pages
                    or not isinstance(result, dict)
                    or result.get("successful") is not True
                    or not isinstance(own_ids, list)
                    or identifier not in own_ids
                ):
                    skipped += 1
                    continue
            try:
                typed_record = Record.model_validate(
                    {
                        key: record[key]
                        for key in ("record_id", "job_name", "data", "provenance", "img_url")
                        if key in record
                    }
                )
                valid, errors = validate_records([typed_record], job["schema"])
                admission = admit_record(
                    typed_record,
                    job["schema"],
                    config.get("search"),
                    assessment_profile=config.get("assessment_profile", "generic"),
                    relevance_fields=config.get("relevance_fields"),
                )
                if errors or not valid or admission.status == "rejected":
                    skipped += 1
                    continue
                page_key = (source, provenance["snapshot_ref"])
                if page_key not in detail_pages:
                    page = make_page(
                        source, Path(provenance["snapshot_ref"]).read_text(encoding="utf-8"), 200
                    )
                    inventory = builder.build(page, config.get("search"))
                    detail_pages[page_key] = inventory.has_entity and not inventory.has_results
                if not detail_pages[page_key]:
                    skipped += 1
                    continue
            except (ValueError, TypeError, OSError):
                skipped += 1
                continue
            grouped.setdefault(url, []).append(identifier)
        seeded = 0
        for url, identifiers in grouped.items():
            if cancelled is not None and cancelled():
                return {"seeded": seeded, "skipped": skipped, "cancelled": True}
            self.complete_detail(
                url,
                run_id=summary["run_id"],
                record_ids=identifiers,
                detail_confirmed=True,
                persisted=True,
            )
            seeded += 1
        return {"seeded": seeded, "skipped": skipped}


def seed_from_run(
    run_dir: Path,
    ledger: IncrementalLedger,
    *,
    allowed_record_ids: set[str] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> dict[str, int]:
    """Compatibility entry point for conservative terminal-run migration."""
    return ledger.seed_finished_run(
        run_dir, allowed_record_ids=allowed_record_ids, cancelled=cancelled
    )

"""Local snapshot persistence and offline replay metadata, without provider calls."""

import json
import os
from contextlib import suppress
from pathlib import Path
from typing import Any
from uuid import uuid4

from unified_web_parser.core.config import JobConfig
from unified_web_parser.core.exceptions import DurabilityError, PipelineError
from unified_web_parser.core.models import Page
from unified_web_parser.navigation import InventoryBuilder
from unified_web_parser.pipeline.report import RunReport


def atomic_text(path: Path, text: str) -> None:
    """Flush an immutable artifact before publishing its path in a checkpoint."""
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        raise DurabilityError(f"Unable to persist artifact: {path.name}") from exc
    finally:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)


def read_manifest(path: Path, *, repair_tail: bool = False) -> tuple[list[dict[str, Any]], bool]:
    """Accept only a torn final append; malformed interior evidence is an error."""
    raw = path.read_bytes()
    lines = raw.splitlines(keepends=True)
    messages = []
    repaired = False
    offset = 0
    for index, line in enumerate(lines):
        try:
            message = json.loads(line)
            if not isinstance(message, dict):
                raise ValueError("Manifest entry must be an object")
        except (ValueError, UnicodeError) as exc:
            if index != len(lines) - 1 or line.endswith(b"\n"):
                raise PipelineError(f"Invalid manifest entry {index + 1}: {path}") from exc
            if repair_tail:
                quarantine = path.with_name(path.name + f".torn-{uuid4().hex}")
                quarantine.write_bytes(raw[offset:])
                with path.open("r+b") as stream:
                    stream.truncate(offset)
                    stream.flush()
                    os.fsync(stream.fileno())
            repaired = True
            break
        messages.append(message)
        if index == len(lines) - 1 and not line.endswith(b"\n") and repair_tail:
            with path.open("ab") as stream:
                stream.write(b"\n")
                stream.flush()
                os.fsync(stream.fileno())
        offset += len(line)
    return messages, repaired


class SnapshotWriter:
    """Append snapshot roles without rescanning a growing manifest per page."""

    def __init__(self, run_dir: Path, snapshots: Path):
        self.run_dir = run_dir
        self.snapshots = snapshots
        self.manifest = run_dir / "pages.jsonl"
        self.latest = (
            {message["page_id"]: message for message in read_manifest(self.manifest)[0]}
            if self.manifest.exists()
            else {}
        )

    def __call__(self, page: Page, *, extractable: bool = True) -> Page:
        path = self.snapshots / f"{page.page_id}.html"
        if not path.is_file():
            atomic_text(path, page.html)
        message = page.model_dump(mode="json", exclude={"html"})
        message["snapshot_ref"] = str(path)
        message["extractable"] = extractable
        if self.latest.get(page.page_id) != message:
            try:
                with self.manifest.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(message, ensure_ascii=False) + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
            except OSError as exc:
                raise DurabilityError("Unable to persist the snapshot manifest") from exc
            self.latest[page.page_id] = message
        return page.model_copy(update={"snapshot_ref": str(path)})


def save_snapshot(
    page: Page,
    run_dir: Path,
    snapshots: Path,
    *,
    extractable: bool = True,
) -> Page:
    return SnapshotWriter(run_dir, snapshots)(page, extractable=extractable)


def load_replay(
    replay_dir: Path,
    *,
    schema: dict[str, Any] | None = None,
    content_profile: str = "generic",
    currency_aliases: dict[str, str] | None = None,
) -> list[Page]:
    manifest = replay_dir / "pages.jsonl"
    if not manifest.is_file():
        raise PipelineError(f"Replay manifest not found: {manifest}")
    pages = []
    messages, torn = read_manifest(manifest)
    if torn:
        raise PipelineError(
            "Replay manifest has a torn tail; resume repairs it under its run lease"
        )
    latest = {message["page_id"]: message for message in messages}
    for message in latest.values():
        message = dict(message)
        extractable = message.pop("extractable", None)
        snapshot = Path(message["snapshot_ref"])
        if not snapshot.is_absolute():
            snapshot = replay_dir / snapshot
        message["html"] = snapshot.read_text(encoding="utf-8")
        page = Page.model_validate(message)
        # Backward compatibility for manifests created before explicit roles.
        if extractable is None:
            inventory = InventoryBuilder(
                schema=schema,
                content_profile=content_profile,
                currency_aliases=currency_aliases,
            ).build(page)
            extractable = inventory.has_entity and not inventory.has_results
        if extractable:
            pages.append(page)
    return pages


def restore_discovery(config: JobConfig, replay_dir: Path, report: RunReport) -> JobConfig:
    """Restore the exact saved site-search phrase without planning or network I/O."""
    previous_discovery = replay_dir / "discovery.json"
    if previous_discovery.is_file():
        report.discovery = json.loads(previous_discovery.read_text(encoding="utf-8"))
        report.discovery.update(
            status="replayed",
            from_run=str(replay_dir),
            network_used=False,
            search_network_used=False,
        )
        site_search = report.discovery.get("site_search")
        if isinstance(site_search, str):
            return config.model_copy(update={"search": site_search})
    elif config.discovery.enabled:
        report.discovery = {
            "status": "replayed",
            "question": config.search,
            "network_used": False,
            "resolved_sources": [],
        }
    return config


def write_job_snapshot(config: dict[str, Any], schema: dict[str, Any], run_dir: Path) -> None:
    atomic_text(
        run_dir / "job.json",
        json.dumps({"config": config, "schema": schema}, ensure_ascii=False, indent=2),
    )

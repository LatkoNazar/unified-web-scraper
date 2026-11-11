"""Cross-run completion reuses proven details without persisting catalog frontiers."""

import copy
import json
import os
import sqlite3
import subprocess
import sys

import pytest

from unified_web_parser.core.exceptions import DurabilityError
from unified_web_parser.storage.incremental import (
    IncrementalLedger,
    processing_signature,
    seed_from_run,
)
from unified_web_parser.storage.run_lock import RunLockedError

URL = "https://source.example/item?ref=one"
SCHEMA = {
    "type": "object",
    "required": ["title", "url"],
    "properties": {"title": {"type": "string"}, "url": {"type": "string"}},
}
CONFIG = {"name": "example", "search": "Actual", "content_profile": "commerce"}


def ledger(tmp_path, *, config=None):
    return IncrementalLedger(tmp_path, "example", processing_signature(config or CONFIG, SCHEMA))


def complete(store, url=URL, **kwargs):
    return store.complete_detail(
        url, run_id="one", record_ids=["record"], detail_confirmed=True, persisted=True, **kwargs
    )


def test_context_ignores_source_expansion_run_identity_and_safety_limits():
    expanded = {
        **CONFIG,
        "sources": [{"url": "https://another.example/"}],
        "outputs": ["parquet"],
        "crawl": {"max_pages": 999, "max_records": 3000, "max_depth": 8},
        "navigation": {"max_actions": 190},
        "ai": {"max_calls": 999, "cache": False},
    }
    assert processing_signature(CONFIG, SCHEMA) == processing_signature(expanded, SCHEMA)


@pytest.mark.parametrize(
    "change",
    [
        {"search": "Other"},
        {"content_profile": "generic"},
        {"assessment_profile": "device"},
        {"currency_aliases": {"грн": "UAH"}},
        {"relevance_fields": ["title"]},
        {"assessment": ["is_relevant"]},
        {"ai": {"enabled": False}},
        {"ai": {"model": "different/model"}},
        {"ai": {"routing": "rules"}},
        {"ai": {"sparse_enrichment": False}},
        {"ai": {"max_candidate_chars": 32000}},
        {"ai": {"max_candidates": 100}},
    ],
)
def test_processing_changes_require_new_completion_scope(change):
    assert processing_signature(CONFIG, SCHEMA) != processing_signature(
        {**CONFIG, **change}, SCHEMA
    )


def test_schema_version_and_resolved_model_changes_invalidate_scope():
    original = processing_signature(CONFIG, SCHEMA)
    changed = copy.deepcopy(SCHEMA)
    changed["properties"]["extra"] = {"type": "number"}
    assert original != processing_signature(CONFIG, changed)
    assert original != processing_signature(CONFIG, SCHEMA, processing_version="next")
    assert original != processing_signature(CONFIG, SCHEMA, resolved_models={"extraction": "other"})


def test_durable_identity_keeps_meaningful_queries_and_removes_only_tracking(tmp_path):
    store = ledger(tmp_path)
    result = complete(store, URL + "&utm_source=test#gallery")
    assert result.canonical_url == URL
    reopened = ledger(tmp_path)
    assert reopened.lookup(URL).last_run_id == "one"
    assert reopened.lookup(URL).record_ids == ("record",)
    assert not reopened.known_detail("https://source.example/item?ref=two")
    assert not ledger(tmp_path, config={**CONFIG, "search": "Other"}).known_detail(URL)
    assert not IncrementalLedger(tmp_path, "another-job", store.signature).known_detail(URL)


@pytest.mark.parametrize(
    "detail,persisted,ids,outcome",
    [
        (False, True, ["record"], "processed"),
        (True, False, ["record"], "processed"),
        (True, True, [], "processed"),
        (True, True, ["record"], "http403"),
    ],
)
def test_unproven_or_uncommitted_work_never_marks_completion(
    tmp_path, detail, persisted, ids, outcome
):
    store = ledger(tmp_path)
    with pytest.raises(ValueError):
        store.complete_detail(
            URL,
            run_id="one",
            record_ids=ids,
            outcome=outcome,
            detail_confirmed=detail,
            persisted=persisted,
        )
    assert store.lookup(URL) is None


def test_explicit_durable_rejection_and_quarantine_invalidation(tmp_path):
    store = ledger(tmp_path)
    result = store.complete_detail(
        URL, run_id="one", outcome="rejected", detail_confirmed=True, persisted=True
    )
    assert result.outcome == "rejected" and result.record_ids == ()
    assert store.known_detail(URL)
    assert store.invalidate(URL)
    assert not store.known_detail(URL)
    assert not store.invalidate(URL)


def test_scope_lease_blocks_duplicate_workers_but_not_other_context(tmp_path):
    store = ledger(tmp_path)
    with store.scope_lease():
        with pytest.raises(RunLockedError):
            ledger(tmp_path).scope_lease().acquire()
        with ledger(tmp_path, config={**CONFIG, "search": "Other"}).scope_lease():
            pass
    with ledger(tmp_path).scope_lease():
        pass


def test_subprocess_hard_exit_retains_commit_and_rolls_back_uncommitted_work(tmp_path):
    store = ledger(tmp_path)
    code = (
        "import os; from pathlib import Path; "
        "from unified_web_parser.storage.incremental import IncrementalLedger; "
        f"s=IncrementalLedger(Path({str(tmp_path)!r}),'example',{store.signature!r}); "
        f"s.complete_detail({URL!r},run_id='child',record_ids=['record'],"
        "detail_confirmed=True,persisted=True); os._exit(0)"
    )
    subprocess.run([sys.executable, "-c", code], check=True, env=os.environ.copy())
    assert ledger(tmp_path).lookup(URL).last_run_id == "child"
    code = (
        "import os,sqlite3; "
        f"c=sqlite3.connect({str(store.path)!r}); c.execute('BEGIN IMMEDIATE'); "
        "c.execute('DELETE FROM detail_completions'); os._exit(0)"
    )
    subprocess.run([sys.executable, "-c", code], check=True, env=os.environ.copy())
    assert ledger(tmp_path).known_detail(URL)


def test_sqlite_failure_is_not_silently_treated_as_unseen(tmp_path, monkeypatch):
    store = ledger(tmp_path)

    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("unavailable")

    monkeypatch.setattr(sqlite3, "connect", fail)
    with pytest.raises(DurabilityError):
        store.lookup(URL)


@pytest.mark.parametrize("payload", ['"record"', '{"record":true}', "[]", "[null]", '[" "]'])
def test_semantically_corrupt_completion_never_suppresses_fetch(tmp_path, payload):
    store = ledger(tmp_path)
    complete(store)
    with sqlite3.connect(store.path) as con:
        con.execute("UPDATE detail_completions SET record_ids=?", (payload,))
    with pytest.raises(DurabilityError, match="Invalid incremental completion"):
        store.known_detail(URL)


def legacy_run(tmp_path, *, catalog=False):
    directory = tmp_path / "legacy"
    directory.mkdir()
    url = "https://source.example/catalog" if catalog else URL
    snapshot = directory / "page.html"
    entity = {"@type": "Product", "name": "Actual", "url": url}
    if catalog:
        entity = {
            "@type": "ItemList",
            "itemListElement": [
                {"@type": "Product", "name": "One", "url": "https://source.example/one"},
                {"@type": "Product", "name": "Two", "url": "https://source.example/two"},
            ],
        }
    else:
        entity["offers"] = {"@type": "Offer", "price": 12, "priceCurrency": "USD"}
    snapshot.write_text(
        '<script type="application/ld+json">' + json.dumps(entity) + "</script>", encoding="utf-8"
    )
    summary = {"run_id": "legacy", "status": "partial", "finished_at": "2026-10-24T01:00:00Z"}
    record = {
        "record_id": "record",
        "job_name": "example",
        "data": {"title": "Actual", "url": url},
        "provenance": {
            "source_url": url,
            "page_id": "page",
            "candidate_ids": ["candidate"],
            "extractor": "json_ld",
            "snapshot_ref": str(snapshot),
        },
    }
    (directory / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    (directory / "job.json").write_text(
        json.dumps({"config": CONFIG, "schema": SCHEMA}), encoding="utf-8"
    )
    (directory / "records.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
    return directory, summary, record


def test_finished_legacy_own_detail_is_seeded_but_catalog_heading_is_not(tmp_path):
    store = ledger(tmp_path)
    directory, _, _ = legacy_run(tmp_path)
    assert seed_from_run(directory, store) == {"seeded": 1, "skipped": 0}
    assert store.known_detail(URL)
    other = tmp_path / "other"
    other.mkdir()
    catalog, _, _ = legacy_run(other, catalog=True)
    assert seed_from_run(catalog, store) == {"seeded": 0, "skipped": 1}
    assert not store.known_detail("https://source.example/catalog")


@pytest.mark.parametrize(
    "case",
    [
        "catalog-provenance",
        "excluded",
        "unfinished",
        "error",
        "fallback",
        "invalid",
        "not-admitted",
    ],
)
def test_legacy_import_never_promotes_incomplete_or_quarantined_observations(tmp_path, case):
    store = ledger(tmp_path)
    directory, summary, record = legacy_run(tmp_path)
    allowed = None
    if case == "catalog-provenance":
        record["provenance"]["source_url"] = "https://source.example/catalog"
    elif case == "excluded":
        record["excluded_at"] = "2026-10-24"
    elif case == "unfinished":
        summary["finished_at"] = None
    elif case == "error":
        summary["errors"] = [{"stage": "extraction", "url": URL}]
    elif case == "fallback":
        summary["warnings"] = [{"stage": "routing", "url": URL, "fallback": "rules"}]
    elif case == "invalid":
        record["data"].pop("title")
    elif case == "not-admitted":
        allowed = set()
    (directory / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    (directory / "records.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
    assert seed_from_run(directory, store, allowed_record_ids=allowed)["seeded"] == 0
    assert not store.known_detail(URL)


@pytest.mark.parametrize(
    "case,expected",
    [
        ("legacy", 1),
        ("marker-only", 0),
        ("other-result", 0),
        ("no-sentinel", 0),
        ("unsuccessful", 0),
        ("wrong-record", 0),
        ("complete", 1),
    ],
)
def test_new_checkpoint_protocol_requires_success_and_page_sentinel(tmp_path, case, expected):
    store = ledger(tmp_path)
    directory, _, _ = legacy_run(tmp_path)
    with sqlite3.connect(directory / "checkpoint.sqlite") as con:
        con.executescript("""
            CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);
            CREATE TABLE plans(page_id TEXT PRIMARY KEY,payload TEXT);
            CREATE TABLE candidates(page_id TEXT,candidate_id TEXT);
        """)
        con.execute("INSERT INTO metadata VALUES('completed','true')")
        if case != "legacy":
            con.execute("INSERT INTO metadata VALUES('incremental_completion_protocol','1')")
        if case not in {"legacy", "marker-only"}:
            page = "other" if case == "other-result" else "page"
            payload = {
                "successful": case != "unsuccessful",
                "own_record_ids": ["different" if case == "wrong-record" else "record"],
            }
            con.execute(
                "INSERT INTO plans VALUES(?,?)", ("page-result:" + page, json.dumps(payload))
            )
            if case != "no-sentinel":
                con.execute("INSERT INTO candidates VALUES(?, '__page_complete__')", (page,))
    assert seed_from_run(directory, store)["seeded"] == expected
    assert store.known_detail(URL) is bool(expected)


@pytest.mark.parametrize("stop_at", [1, 2])
def test_cancelled_migration_never_commits_after_cancellation(tmp_path, stop_at):
    store = ledger(tmp_path)
    directory, _, _ = legacy_run(tmp_path)
    calls = 0

    def cancelled():
        nonlocal calls
        calls += 1
        return calls >= stop_at

    assert seed_from_run(directory, store, cancelled=cancelled) == {
        "seeded": 0,
        "skipped": 0,
        "cancelled": True,
    }
    assert not store.known_detail(URL)

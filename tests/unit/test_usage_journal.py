"""Paid attempt intent, outputs and costs survive process death without resending."""

import asyncio
import json
import os
import subprocess
import sys

import httpx
import pytest

from unified_web_parser.core.exceptions import CreditsExhausted, ProviderError
from unified_web_parser.providers.decisions import OpenRouterDecisionsClient
from unified_web_parser.providers.openrouter import OpenRouterClient, RequestBudget
from unified_web_parser.providers.search import OpenRouterSearchClient
from unified_web_parser.providers.usage_journal import (
    RequestOutcomeUnknown,
    UsageJournal,
    UsageJournalError,
)

SCHEMA = {"type": "object", "properties": {"answer": {"type": "integer"}}, "required": ["answer"]}
QUESTIONS = {"route": {"type": "choice", "instructions": "Choose", "criteria": {"yes": "Yes"}}}
RESULT = {
    "id": "gen-response-1",
    "choices": [{"message": {"content": '{"answer":7}'}}],
    "usage": {"prompt_tokens": 12, "completion_tokens": 3, "cost": 0.004},
}


@pytest.mark.asyncio
async def test_intent_precedes_http_and_completed_output_replays_without_optional_cache(tmp_path):
    path = tmp_path / "usage.jsonl"
    journal = UsageJournal(path)

    def respond(_request):
        events = [json.loads(line) for line in path.read_text().splitlines()]
        assert len(events) == 1 and events[0]["event"] == "started"
        assert journal.summary()["in_flight_calls"] == 1
        return httpx.Response(200, json=RESULT)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = OpenRouterClient(
            "private-key",
            "model",
            client=http,
            budget=RequestBudget(1, journal),
            stage="extraction",
        )
        assert await client.complete_json("private-system", {"private-prompt": 1}, SCHEMA) == {
            "answer": 7
        }
    ledger = path.read_text()
    assert all(
        word not in ledger for word in ("private-key", "private-system", "private-prompt", "answer")
    )
    restored = UsageJournal(path)
    budget = RequestBudget(1, restored)
    assert budget.used == 1 and budget.remaining == 0
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _r: pytest.fail("paid resend"))
    ) as http:
        client = OpenRouterClient(
            "private-key", "model", client=http, budget=budget, stage="extraction"
        )
        assert await client.complete_json("private-system", {"private-prompt": 1}, SCHEMA) == {
            "answer": 7
        }
        assert client.calls == 0 and client.cache_hits == 1
    summary = restored.summary()
    assert summary["calls"] == 1 and summary["cost_usd"] == 0.004
    assert summary["input_tokens"] == 12 and summary["stages"]["extraction"]["calls"] == 1


CHILD = r"""
import asyncio,json,os,sys
from pathlib import Path
import httpx
from unified_web_parser.providers.openrouter import OpenRouterClient,RequestBudget
from unified_web_parser.providers.usage_journal import UsageJournal
path=Path(sys.argv[1]);mode=sys.argv[2]
journal=UsageJournal(path)
if mode=="after_spool":
    original=journal._append
    def append(event):
        if event["event"]=="response":os._exit(72)
        original(event)
    journal._append=append
def respond(request):
    if mode=="inflight":os._exit(71)
    envelope={"id":"gen-child", "choices":[{"message":{"content":'{"answer":7}'}}],
              "usage":{"prompt_tokens":12,"completion_tokens":3,"cost":.004}}
    return httpx.Response(200,json=envelope)
async def main():
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client=OpenRouterClient("private-key","model",client=http,budget=RequestBudget(4,journal))
        schema={"type":"object","properties":{"answer":{"type":"integer"}},
                "required":["answer"]}
        await client.complete_json("private-system",{"private-prompt":1},schema)
    os._exit(73)
asyncio.run(main())
"""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode,exitcode", [("inflight", 71), ("after_spool", 72), ("completed", 73)]
)
async def test_real_subprocess_hard_exit_preserves_unknown_or_completed_response(
    tmp_path, mode, exitcode
):
    path = tmp_path / "usage.jsonl"
    result = subprocess.run(
        [sys.executable, "-c", CHILD, str(path), mode], capture_output=True, timeout=20
    )
    assert result.returncode == exitcode, result.stderr.decode(errors="replace")
    journal = UsageJournal(path)
    budget = RequestBudget(4, journal)
    assert budget.used == 1
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _r: pytest.fail("paid resend"))
    ) as http:
        client = OpenRouterClient("private-key", "model", client=http, budget=budget)
        if mode == "inflight":
            with pytest.raises(RequestOutcomeUnknown):
                await client.complete_json("private-system", {"private-prompt": 1}, SCHEMA)
            assert journal.summary()["cost_pending_calls"] == 1
            assert not journal.summary()["cost_complete"]
            assert "cost_usd" not in journal.summary()
        else:
            assert await client.complete_json("private-system", {"private-prompt": 1}, SCHEMA) == {
                "answer": 7
            }
            assert journal.summary()["cost_usd"] == 0.004
        assert client.calls == 0 and journal.summary()["calls"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", [httpx.ReadTimeout, httpx.WriteTimeout, httpx.RemoteProtocolError]
)
async def test_ambiguous_transport_failure_is_not_blindly_retried(tmp_path, failure):
    journal = UsageJournal(tmp_path / "usage.jsonl")
    calls = []

    def respond(request):
        calls.append(request)
        raise failure("private request content", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = OpenRouterClient(
            "private-key", "model", client=http, budget=RequestBudget(5, journal)
        )
        for _ in range(2):
            with pytest.raises(RequestOutcomeUnknown):
                await client.complete_json("system", {}, SCHEMA)
    assert len(calls) == 1 and journal.summary()["cost_pending_calls"] == 1
    assert "private request content" not in journal.path.read_text()


@pytest.mark.asyncio
async def test_proven_connection_failure_can_retry_and_cost_is_not_double_counted(
    tmp_path, monkeypatch
):
    async def no_wait(_delay):
        return None

    monkeypatch.setattr(asyncio, "sleep", no_wait)
    journal = UsageJournal(tmp_path / "usage.jsonl")
    calls = []

    def respond(request):
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ConnectError("not connected", request=request)
        return httpx.Response(200, json=RESULT)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = OpenRouterClient("key", "model", client=http, budget=RequestBudget(4, journal))
        await client.complete_json("system", {}, SCHEMA)
    assert len(calls) == 2 and journal.summary()["calls"] == 2
    assert journal.summary()["cost_usd"] == 0.004
    assert journal.summary()["request_outcomes"] == {"not_sent": 1, "response": 1}


@pytest.mark.asyncio
async def test_durable_write_failure_prevents_dispatch_and_does_not_retry_paid_response(
    tmp_path, monkeypatch
):
    journal = UsageJournal(tmp_path / "usage.jsonl")
    calls = []
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: calls.append(r) or httpx.Response(200, json=RESULT))
    ) as http:
        client = OpenRouterClient("key", "model", client=http, budget=RequestBudget(4, journal))
        original = os.fsync
        monkeypatch.setattr(os, "fsync", lambda _fd: (_ for _ in ()).throw(OSError("disk fault")))
        with pytest.raises(UsageJournalError):
            await client.complete_json("system", {}, SCHEMA)
        assert calls == []
        monkeypatch.setattr(os, "fsync", original)
    journal = UsageJournal(tmp_path / "second.jsonl")
    monkeypatch.setattr(os, "replace", lambda *_a: (_ for _ in ()).throw(OSError("spool fault")))
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: calls.append(r) or httpx.Response(200, json=RESULT))
    ) as http:
        client = OpenRouterClient("key", "model", client=http, budget=RequestBudget(4, journal))
        with pytest.raises(UsageJournalError):
            await client.complete_json("system", {}, SCHEMA)
        assert len(calls) == 1 and client.stats["cost_usd"] == 0.004


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["chat", "native", "search"])
async def test_billed_semantic_errors_replay_without_another_request(tmp_path, kind):
    journal = UsageJournal(tmp_path / "usage.jsonl")
    calls = []
    model = "model-decisions" if kind == "native" else "model"
    envelope = {
        "model": model,
        "answers": {},
        "usage": {"input_tokens": 4, "output_tokens": 0, "cost": 0.002},
    }
    if kind == "chat":
        envelope = {"choices": [{"message": {"content": "invalid-json"}}], "usage": {"cost": 0.002}}
    elif kind == "search":
        envelope = {
            "choices": [{"message": {"content": "", "annotations": []}}],
            "usage": {"cost": 0.002, "server_tool_use": {"web_search_requests": 2}},
        }

    def respond(request):
        calls.append(request)
        return httpx.Response(200, json=envelope)

    classes = {
        "chat": OpenRouterClient,
        "native": OpenRouterDecisionsClient,
        "search": OpenRouterSearchClient,
    }
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        for _ in range(2):
            client = classes[kind](
                "key",
                model,
                client=http,
                budget=RequestBudget(5, UsageJournal(journal.path)),
                stage=kind,
            )
            with pytest.raises(ProviderError):
                if kind == "chat":
                    await client.complete_json("system", {}, SCHEMA)
                elif kind == "native":
                    await client.decide({}, QUESTIONS)
                else:
                    await client.search("query", limit=1)
    assert len(calls) == 1
    assert UsageJournal(journal.path).summary()["cost_usd"] == 0.002


@pytest.mark.asyncio
async def test_credit_latch_restores_and_readonly_reader_never_repairs_active_tail(tmp_path):
    path = tmp_path / "usage.jsonl"
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _r: httpx.Response(
                200,
                json={
                    "error": {
                        "code": 402,
                        "message": "private-key",
                        "metadata": {"limit_source": "openrouter_key_limit"},
                    }
                },
            )
        )
    ) as http:
        client = OpenRouterClient(
            "private-key", "model", client=http, budget=RequestBudget(5, UsageJournal(path))
        )
        with pytest.raises(CreditsExhausted):
            await client.complete_json("system", {}, SCHEMA)
    restored = RequestBudget(5, UsageJournal(path))
    with pytest.raises(CreditsExhausted):
        restored.check_stop()
    assert restored.used == 1 and restored.credit_limit_source == "openrouter_key_limit"
    assert "private-key" not in "".join(
        p.read_text() for p in path.parent.rglob("*") if p.is_file()
    )
    with path.open("ab") as stream:
        stream.write(b'{"partial":')
    before = path.read_bytes()
    assert UsageJournal(path, read_only=True).summary()["calls"] == 1
    assert path.read_bytes() == before
    UsageJournal(path)
    assert not path.read_bytes().endswith(b'{"partial":')


@pytest.mark.asyncio
async def test_durable_search_replay_retains_citations_without_rebilling_tools(tmp_path):
    path = tmp_path / "usage.jsonl"
    sent = []
    response = {
        "id": "gen-search",
        "choices": [
            {
                "message": {
                    "content": "Result",
                    "annotations": [
                        {
                            "type": "url_citation",
                            "url_citation": {"url": "https://public.example/item"},
                        }
                    ],
                }
            }
        ],
        "usage": {"cost": 0.005, "server_tool_use_details": {"web_search_requests": 1}},
    }

    def respond(request):
        sent.append(request)
        return httpx.Response(200, json=response)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        original = OpenRouterSearchClient(
            "key", "model", client=http, budget=RequestBudget(1, UsageJournal(path))
        )
        first = await original.search("query", limit=1)
        restored_journal = UsageJournal(path)
        resumed = OpenRouterSearchClient(
            "key", "model", client=http, budget=RequestBudget(1, restored_journal)
        )
        second = await resumed.search("query", limit=1)
    assert len(sent) == 1 and second.results == first.results
    assert second.cache_hit and second.web_search_requests == 0
    assert second.fetched_at <= first.fetched_at
    assert resumed.stats["reported_web_search_requests"] == 0
    assert resumed.stats["cached_web_search_requests"] == 1
    assert restored_journal.summary()["web_search_requests"] == 1
    assert restored_journal.summary()["cost_usd"] == 0.005


def test_read_only_response_api_cannot_create_or_replace_spool(tmp_path):
    path = tmp_path / "usage.jsonl"
    journal = UsageJournal(path)
    attempt = journal.begin(model="model", endpoint="chat", fingerprint="a" * 64)
    reader = UsageJournal(path, read_only=True)
    with pytest.raises(UsageJournalError):
        reader.response(attempt, status_code=200, envelope=RESULT)
    assert not reader.responses_dir.exists()


def test_recovered_envelope_cannot_be_mutated_through_caller_owned_objects(tmp_path):
    journal = UsageJournal(tmp_path / "usage.jsonl")
    fingerprint = "b" * 64
    attempt = journal.begin(model="model", endpoint="chat", fingerprint=fingerprint)
    envelope = {"usage": {"cost": 0.003}, "result": {"title": "Original"}}
    journal.response(attempt, status_code=200, envelope=envelope)
    envelope["usage"]["cost"] = 1000
    first = journal.lookup(fingerprint)[1]
    first["result"]["title"] = "Changed"
    assert journal.lookup(fingerprint)[1]["result"]["title"] == "Original"
    assert journal.summary()["cost_usd"] == 0.003


@pytest.mark.asyncio
async def test_normal_cache_hit_creates_no_new_durable_attempt(tmp_path):
    journal = UsageJournal(tmp_path / "usage.jsonl")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _r: httpx.Response(200, json=RESULT))
    ) as http:
        client = OpenRouterClient(
            "key",
            "model",
            client=http,
            cache_dir=tmp_path / "cache",
            budget=RequestBudget(5, journal),
        )
        await client.complete_json("system", {}, SCHEMA)
        await client.complete_json("system", {}, SCHEMA)
    assert journal.summary()["calls"] == 1 and client.cache_hits == 1


@pytest.mark.asyncio
async def test_explicit_resume_can_retry_known_credit_rejection_without_erasing_history(tmp_path):
    path = tmp_path / "usage.jsonl"
    responses = [
        httpx.Response(402, json={"error": {"code": 402}}),
        httpx.Response(200, json=RESULT),
        httpx.Response(402, json={"error": {"code": 402}}),
    ]
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _r: responses.pop(0))
    ) as http:
        original = OpenRouterClient(
            "key", "model", client=http, budget=RequestBudget(5, UsageJournal(path))
        )
        with pytest.raises(CreditsExhausted):
            await original.complete_json("system", {}, SCHEMA)
        restored = UsageJournal(path)
        with pytest.raises(CreditsExhausted):
            RequestBudget(5, restored).check_stop()
        restored.begin_resume()
        budget = RequestBudget(5, restored)
        budget.check_stop()
        assert budget.used == 1
        resumed = OpenRouterClient("key", "model", client=http, budget=budget)
        assert await resumed.complete_json("system", {}, SCHEMA) == {"answer": 7}
        assert budget.used == 2 and restored.summary()["calls"] == 2
        assert restored.summary()["known_cost_usd"] == 0.004
        assert restored.summary()["cost_pending_calls"] == 1
        assert not restored.summary()["cost_complete"]
        with pytest.raises(CreditsExhausted):
            await resumed.complete_json("another", {}, SCHEMA)
        with pytest.raises(CreditsExhausted):
            RequestBudget(5, UsageJournal(path)).check_stop()
    assert responses == []


def test_resume_boundary_never_unlocks_an_orphan_paid_attempt(tmp_path):
    journal = UsageJournal(tmp_path / "usage.jsonl")
    fingerprint = "c" * 64
    journal.begin(model="model", endpoint="chat", fingerprint=fingerprint)
    restored = UsageJournal(journal.path)
    restored.begin_resume()
    with pytest.raises(RequestOutcomeUnknown):
        restored.lookup(fingerprint)
    assert restored.summary()["calls"] == 1 and restored.summary()["resume_boundaries"] == 1


def test_torn_usage_tail_is_durably_quarantined_before_repair_without_free_orphan(
    tmp_path, monkeypatch
):
    path = tmp_path / "usage.jsonl"
    journal = UsageJournal(path)
    journal.begin(model="model", endpoint="chat", fingerprint="d" * 64)
    valid = path.read_bytes()
    torn = b'{"event":"response","usage":'
    with path.open("ab") as stream:
        stream.write(torn)
    original = path.read_bytes()
    reader = UsageJournal(path, read_only=True)
    assert path.read_bytes() == original and list(tmp_path.glob("usage.jsonl.torn-*")) == []
    summary = reader.summary()
    assert summary["calls"] == 1 and summary["cost_pending_calls"] == 1
    assert summary["request_outcomes"] == {"unknown": 1}
    assert not summary["cost_complete"] and "cost_usd" not in summary
    fsync = os.fsync
    synced = []

    def check_order(fd):
        quarantines = list(tmp_path.glob("usage.jsonl.torn-*"))
        assert len(quarantines) == 1 and quarantines[0].read_bytes() == torn
        if not synced:
            assert path.read_bytes() == original
        else:
            assert path.read_bytes() == valid
        fsync(fd)
        synced.append(fd)

    monkeypatch.setattr(os, "fsync", check_order)
    repaired = UsageJournal(path)
    assert len(synced) == 2 and path.read_bytes() == valid
    assert repaired.quarantined_tail.read_bytes() == torn
    assert repaired.summary()["cost_pending_calls"] == 1


def test_quarantine_sync_failure_never_truncates_the_original_usage_log(tmp_path, monkeypatch):
    path = tmp_path / "usage.jsonl"
    original = b'{"unfinished":'
    path.write_bytes(original)
    monkeypatch.setattr(os, "fsync", lambda _fd: (_ for _ in ()).throw(OSError("disk fault")))
    with pytest.raises(UsageJournalError):
        UsageJournal(path)
    assert path.read_bytes() == original


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 404])
async def test_zero_call_resume_replays_completed_success_or_failure_without_rebilling(
    tmp_path, status
):
    path = tmp_path / "usage.jsonl"
    sent = []

    def respond(request):
        sent.append(request)
        return httpx.Response(status, json=RESULT)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        original = OpenRouterClient(
            "key", "model", client=http, budget=RequestBudget(2, UsageJournal(path))
        )
        if status == 200:
            assert await original.complete_json("system", {}, SCHEMA) == {"answer": 7}
        else:
            with pytest.raises(ProviderError, match="HTTP 404"):
                await original.complete_json("system", {}, SCHEMA)
        journal = UsageJournal(path)
        resumed = OpenRouterClient(
            "key", "model", client=http, max_calls=1, budget=RequestBudget(0, journal)
        )
        if status == 200:
            assert await resumed.complete_json("system", {}, SCHEMA) == {"answer": 7}
        else:
            with pytest.raises(ProviderError, match="HTTP 404"):
                await resumed.complete_json("system", {}, SCHEMA)
        with pytest.raises(ProviderError, match="budget exhausted"):
            await resumed.complete_json("changed request", {}, SCHEMA)
    assert len(sent) == 1 and journal.summary()["calls"] == 1
    assert journal.summary()["cost_usd"] == 0.004

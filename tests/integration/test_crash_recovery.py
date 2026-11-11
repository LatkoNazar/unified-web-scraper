"""Real process death across the record and fetched-page durability boundaries."""

import asyncio
import json
import os
import site
import subprocess
import sys
import time
from pathlib import Path

import pytest

from unified_web_parser.core.config import JobConfig, LoadedJob
from unified_web_parser.core.exceptions import DurabilityError, PipelineError
from unified_web_parser.crawling.pages import make_page
from unified_web_parser.pipeline.artifacts import read_manifest
from unified_web_parser.pipeline.checkpoints import (
    CheckpointStore,
    load_resume_job,
    recovery_snapshot,
)
from unified_web_parser.pipeline.runner import PipelineRunner
from unified_web_parser.storage import DatasetStore
from unified_web_parser.storage.run_lock import RunLease, RunLockedError, is_run_active

WORKER = """
import asyncio,json,sys,time,os
from pathlib import Path
from unified_web_parser.core.config import JobConfig,LoadedJob
from unified_web_parser.crawling.pages import make_page
from unified_web_parser.pipeline.runner import PipelineRunner
root=Path(sys.argv[1]); mode=sys.argv[2]
urls=['https://offline.example/one'] if mode=='snapshot' else ['https://offline.example/one','https://offline.example/two']
job=LoadedJob(config_path=root/'job.yaml',config=JobConfig.model_validate({
'name':'crash-example','schema_path':'unused.json','sources':[{'url':u} for u in urls],
'crawl':{'max_pages':8,'concurrency':1,'delay':0},'ai':{'enabled':False},'navigation':{'mode':'rules'}
}),extraction_schema={'type':'object','required':['title','url'], 'properties':{
 'title':{'type':'string'},'url':{'type':'string'},
 'description':{'type':['string','null']}},'additionalProperties':False})
class Pages:
 async def fetch(self,url):
  with (root/'fetches.jsonl').open('a') as f:f.write(json.dumps(url)+'\\n')
  if mode=='records' and url.endswith('/two'):
   (root/'ready').write_text(str(os.getpid()))
   await asyncio.Event().wait()
  entity={'@type':'Article','headline':'Article '+url.rsplit('/',1)[-1],
          'url':url,'description':'Observed fact'}
  return make_page(url,'<script type="application/ld+json">'+json.dumps(entity)+'</script>',200)
def event(kind,data):
 if mode=='snapshot' and kind=='stage' and data.get('stage')=='routing':
  (root/'ready').write_text(str(os.getpid()))
  while True:time.sleep(.05)
asyncio.run(PipelineRunner(job,data_dir=root,fetcher=Pages(),on_event=event).run(run_id='durable'))
"""


def wait_for_file(path: Path, process: subprocess.Popen) -> None:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if path.exists():
            return
        if process.poll() is not None:
            pytest.fail(f"Worker exited before checkpoint: {process.returncode}")
        time.sleep(0.03)
    pytest.fail("Worker did not reach the controlled durability boundary")


@pytest.mark.parametrize("mode", ["records", "snapshot"])
def test_hard_kill_restores_exact_run_and_continues_without_refetching_completed_page(
    tmp_path, mode
):
    script = tmp_path / "worker.py"
    script.write_text(WORKER, encoding="utf-8")
    # Windows venv python.exe is a redirector: killing it may leave the real
    # worker alive. Launch the base interpreter directly with this env's deps.
    interpreter = getattr(sys, "_base_executable", sys.executable)
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(
            [
                str(Path("src").resolve()),
                *site.getsitepackages(),
            ]
        ),
    }
    log = (tmp_path / "worker.log").open("w")
    process = subprocess.Popen(
        [interpreter, str(script), str(tmp_path), mode],
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    try:
        wait_for_file(tmp_path / "ready", process)
        assert int((tmp_path / "ready").read_text()) == process.pid
        run_dir = tmp_path / "runs" / "durable"
        assert is_run_active(run_dir)
        with pytest.raises(RunLockedError), RunLease(run_dir):
            pytest.fail("Concurrent lease must never be admitted")
        if mode == "records":
            live = recovery_snapshot(tmp_path, "durable")
            assert live["records_persisted"] == 1
            assert live["records"][0]["data"]["title"] == "Article one"
            assert DatasetStore(tmp_path).list_records()[0]["data"]["title"] == "Article one"
        process.kill()  # actual forced termination: no Python finally/exit hooks
        process.wait(timeout=10)
        assert not is_run_active(run_dir)
        recovered = recovery_snapshot(tmp_path, "durable")
        assert recovered["status"] == "interrupted" and recovered["resume_available"]
        saved = CheckpointStore(run_dir).load()
        assert saved["crawl"]["inflight"]
        if mode == "snapshot":
            result = subprocess.run(
                [
                    interpreter,
                    "-m",
                    "unified_web_parser",
                    "resume",
                    "durable",
                    "--data-dir",
                    str(tmp_path),
                    "--no-ai",
                ],
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert result.returncode == 0, result.stderr + result.stdout
        else:

            class Pages:
                async def fetch(self, url):
                    with (tmp_path / "fetches.jsonl").open("a") as stream:
                        stream.write(json.dumps(url) + "\n")
                    return make_page(
                        url,
                        '<script type="application/ld+json">'
                        + json.dumps(
                            {
                                "@type": "Article",
                                "headline": "Article two",
                                "url": url,
                                "description": "Observed fact",
                            }
                        )
                        + "</script>",
                        200,
                    )

            report = asyncio.run(
                PipelineRunner(load_resume_job(run_dir), data_dir=tmp_path, fetcher=Pages()).run(
                    resume_dir=run_dir
                )
            )
            assert report.status == "success", report.errors
        final = CheckpointStore(run_dir).load()
        assert final["completed"]
        expected = 2 if mode == "records" else 1
        assert len(final["records"]) == expected
        exports = [
            json.loads(line) for line in (run_dir / "records.jsonl").read_text().splitlines()
        ]
        assert len(exports) == expected == len({record["record_id"] for record in exports})
        fetches = [
            json.loads(line) for line in (tmp_path / "fetches.jsonl").read_text().splitlines()
        ]
        assert fetches.count("https://offline.example/one") == 1
        assert CheckpointStore(run_dir).load()["report"]["run_id"] == "durable"
        assert not recovery_snapshot(tmp_path, "durable")["resume_available"]
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        log.close()


def article_job(tmp_path):
    return LoadedJob(
        config_path=tmp_path / "job.yaml",
        config=JobConfig.model_validate(
            {
                "name": "immutable",
                "sources": [{"url": "https://offline.example/one"}],
                "schema_path": "unused.json",
                "ai": {"enabled": False},
                "navigation": {"mode": "rules"},
            }
        ),
        extraction_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "url": {"type": "string"},
            },
            "required": ["title", "url"],
            "additionalProperties": False,
        },
    )


@pytest.mark.asyncio
async def test_same_job_newer_run_cannot_overwrite_exact_historical_checkpoint(tmp_path):
    class Pages:
        def __init__(self, title):
            self.title = title

        async def fetch(self, url):
            return make_page(
                url,
                '<script type="application/ld+json">'
                + json.dumps(
                    {
                        "@type": "Article",
                        "headline": self.title,
                        "url": url,
                    }
                )
                + "</script>",
                200,
            )

    job = article_job(tmp_path)
    first = await PipelineRunner(job, data_dir=tmp_path, fetcher=Pages("Earlier")).run()
    await PipelineRunner(job, data_dir=tmp_path, fetcher=Pages("Later")).run()
    assert DatasetStore(tmp_path).list_records()[0]["data"]["title"] == "Later"
    historical = recovery_snapshot(tmp_path, first.run_id)
    assert historical["records"][0]["data"]["title"] == "Earlier"
    assert historical["status"] == "success"


def test_manifest_torn_tail_is_quarantined_but_invalid_interior_is_not_ignored(tmp_path):
    path = tmp_path / "pages.jsonl"
    path.write_bytes(b'{"page_id":"first"}\n{"page_')
    entries, torn = read_manifest(path, repair_tail=True)
    assert torn and entries == [{"page_id": "first"}]
    assert path.read_bytes() == b'{"page_id":"first"}\n'
    assert list(tmp_path.glob("pages.jsonl.torn-*"))[0].read_bytes() == b'{"page_'
    path.write_bytes(b'{"page_id":"first"}\ninvalid\n{"page_id":"last"}\n')
    with pytest.raises(PipelineError, match="entry 2"):
        read_manifest(path, repair_tail=True)


@pytest.mark.asyncio
async def test_record_checkpoint_failure_stops_before_next_fetch_or_record_event(
    tmp_path, monkeypatch
):
    fetched, events = [], []

    class Pages:
        async def fetch(self, url):
            fetched.append(url)
            return make_page(
                url,
                '<script type="application/ld+json">'
                + json.dumps(
                    {
                        "@type": "Article",
                        "headline": "Observed",
                        "url": url,
                    }
                )
                + "</script>",
                200,
            )

    def broken(self, record):
        raise DurabilityError("Disk failed")

    monkeypatch.setattr(CheckpointStore, "save_record", broken)
    job = article_job(tmp_path)
    config = job.config.model_copy(
        update={
            "sources": JobConfig.model_validate(
                {
                    **job.config.model_dump(mode="json"),
                    "sources": [
                        {"url": "https://offline.example/one"},
                        {"url": "https://offline.example/two"},
                    ],
                    "crawl": {"concurrency": 1},
                }
            ).sources
        }
    )
    job = job.model_copy(
        update={
            "config": config.model_copy(
                update={"crawl": config.crawl.model_copy(update={"concurrency": 1})}
            )
        }
    )
    report = await PipelineRunner(
        job, data_dir=tmp_path, fetcher=Pages(), on_event=lambda kind, data: events.append(kind)
    ).run()
    assert report.status == "failed"
    assert fetched == ["https://offline.example/one"]
    assert "record" not in events
    saved = CheckpointStore(tmp_path / "runs" / report.run_id).load()
    assert not saved["completed"] and not saved["records"]


PAID_WORKER = """
import asyncio,json,os,sys
from pathlib import Path
import httpx
from unified_web_parser.core.config import JobConfig,LoadedJob
from unified_web_parser.crawling.pages import make_page
from unified_web_parser.pipeline.runner import PipelineRunner
from unified_web_parser.providers import openrouter
from unified_web_parser.providers.usage_journal import UsageJournal
root=Path(sys.argv[1])
os.environ['OPENROUTER_API_KEY']='local-fake-key'
os.environ.pop('OPENROUTER_MODEL',None)
job=LoadedJob(config_path=root/'job.yaml', config=JobConfig.model_validate({
 'name':'paid-recovery','schema_path':'unused.json',
 'sources':[{'url':'https://offline.example/one'}],
 'ai':{'model':'test/model','routing':'rules','max_calls':1,'cache':False},
 'navigation':{'mode':'rules'},'crawl':{'concurrency':1,'delay':0}}),
 extraction_schema={'type':'object','required':['title','url','metric'],
 'additionalProperties':False,'properties':{'title':{'type':'string'},
 'url':{'type':'string'},'metric':{'type':['integer','null']}}})
class Pages:
 async def fetch(self,url):
  entity={'@type':'Thing','name':'Observed','url':url,'description':'Observed fact'}
  return make_page(url,'<script type="application/ld+json">'+json.dumps(entity)+'</script>',200)
def respond(request):
 return httpx.Response(200,json={'id':'paid-fixture','usage':{
  'prompt_tokens':10,'completion_tokens':5,'cost':.005},'choices':[{'message':{
  'content':json.dumps({'records':[{'metric':5}]})}}]})
base=openrouter.OpenRouterClient
original=UsageJournal.response
def crash_after_paid_response(self,*args,**kwargs):
 original(self,*args,**kwargs)
 os._exit(64)
UsageJournal.response=crash_after_paid_response
async def main():
 async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
  class LocalClient(base):
   def __init__(self,**kwargs):super().__init__(client=http,**kwargs)
  openrouter.OpenRouterClient=LocalClient
  await PipelineRunner(job,data_dir=root,fetcher=Pages()).run(run_id='paid')
asyncio.run(main())
"""


@pytest.mark.asyncio
async def test_paid_response_recovered_at_zero_budget_with_cache_disabled(tmp_path, monkeypatch):
    import httpx

    from unified_web_parser.providers import openrouter

    script = tmp_path / "paid_worker.py"
    script.write_text(PAID_WORKER, encoding="utf-8")
    interpreter = getattr(sys, "_base_executable", sys.executable)
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(
            [
                str(Path("src").resolve()),
                *site.getsitepackages(),
            ]
        ),
    }
    result = subprocess.run(
        [interpreter, str(script), str(tmp_path)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 64, result.stderr
    run_dir = tmp_path / "runs" / "paid"
    before = recovery_snapshot(tmp_path, "paid")
    assert before["status"] == "interrupted" and before["records"] == []
    assert before["ai"]["calls"] == 1 and before["ai"]["known_cost_usd"] == 0.005
    assert not (tmp_path / "ai_cache").exists()
    requests = []

    def forbidden(request):
        requests.append(request)
        raise AssertionError("Durable paid output must not trigger another HTTP call")

    class NoFetch:
        async def fetch(self, url):
            pytest.fail("Saved fetched evidence must not be refetched")

    base = openrouter.OpenRouterClient
    monkeypatch.setenv("OPENROUTER_API_KEY", "local-fake-key")
    monkeypatch.setenv("OPENROUTER_MODEL", "changed/model-must-not-run")
    monkeypatch.setenv("OPENROUTER_DECISION_MODEL", "changed/decision-must-not-run")
    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as http:

        class LocalClient(base):
            def __init__(self, **kwargs):
                super().__init__(client=http, **kwargs)

        monkeypatch.setattr(openrouter, "OpenRouterClient", LocalClient)
        job = load_resume_job(run_dir)
        job = job.model_copy(
            update={
                "config": job.config.model_copy(
                    update={
                        "ai": job.config.ai.model_copy(update={"max_calls": 0}),
                    }
                )
            }
        )
        report = await PipelineRunner(job, data_dir=tmp_path, fetcher=NoFetch()).run(
            resume_dir=run_dir,
        )
    assert report.status == "success", report.errors
    assert not requests
    assert report.records_written == 1
    assert report.ai["calls"] == 1 and report.ai["known_cost_usd"] == 0.005
    assert report.ai["cache_hits"] == 1
    assert report.ai["model"] == "test/model"
    assert CheckpointStore(run_dir).load()["records"][0]["data"]["metric"] == 5


@pytest.mark.asyncio
async def test_checkpoint_record_reconciles_after_global_store_commit_failed(tmp_path):
    class FirstCommitFails(DatasetStore):
        def save_records(self, *args, **kwargs):
            raise OSError("Global dataset unavailable")

    class Pages:
        async def fetch(self, url):
            return make_page(
                url,
                '<script type="application/ld+json">'
                + json.dumps(
                    {
                        "@type": "Article",
                        "headline": "Observed",
                        "url": url,
                    }
                )
                + "</script>",
                200,
            )

    events = []
    report = await PipelineRunner(
        article_job(tmp_path),
        data_dir=tmp_path,
        fetcher=Pages(),
        store=FirstCommitFails(tmp_path),
        on_event=lambda kind, data: events.append(kind),
    ).run()
    run_dir = tmp_path / "runs" / report.run_id
    assert report.status == "failed" and "record" not in events
    assert len(CheckpointStore(run_dir).load()["records"]) == 1
    assert DatasetStore(tmp_path).list_records() == []

    class NoFetch:
        async def fetch(self, url):
            pytest.fail("Already-fetched interrupted page must be reused")

    resumed = await PipelineRunner(
        load_resume_job(run_dir), data_dir=tmp_path, fetcher=NoFetch()
    ).run(resume_dir=run_dir)
    # The recovered execution succeeds; the previous failed outcome remains immutable.
    assert resumed.status == "success"
    previous = json.loads((run_dir / "resume-history" / "1.json").read_text())
    assert previous["status"] == "failed" and previous["errors"][0]["stage"] == "durability"
    assert resumed.records_written == 1
    assert DatasetStore(tmp_path).list_records()[0]["data"]["title"] == "Observed"
    assert (
        len(
            DatasetStore(tmp_path).list_history(
                CheckpointStore(run_dir).load()["records"][0]["record_id"], "immutable"
            )
        )
        == 1
    )

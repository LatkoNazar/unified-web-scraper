"""Cross-run increments revisit discovery surfaces but never redo completed details."""

import asyncio
import json
from pathlib import Path

import pytest

from unified_web_parser.core.config import JobConfig, LoadedJob
from unified_web_parser.core.exceptions import ConfigurationError, PipelineError
from unified_web_parser.crawling.errors import FetchError
from unified_web_parser.crawling.pages import make_page
from unified_web_parser.interfaces.cli.arguments import build_parser
from unified_web_parser.interfaces.cli.jobs import load_requested_job
from unified_web_parser.pipeline.checkpoints import CheckpointStore
from unified_web_parser.pipeline.incremental import load_update_job
from unified_web_parser.pipeline.runner import PipelineRunner
from unified_web_parser.storage import DatasetStore
from unified_web_parser.storage.run_lock import RunLease

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["title", "url", "metric"],
    "properties": {
        "title": {"type": "string"},
        "url": {"type": "string"},
        "metric": {"type": ["integer", "null"]},
    },
}


def job(tmp_path, sources=None, **overrides):
    config = {
        "name": "incremental-articles",
        "schema_path": "schema.json",
        "search": "Article",
        "sources": [{"url": url} for url in (sources or ["https://example.com/catalog"])],
        "incremental": {"enabled": True},
        "navigation": {"mode": "rules"},
        "ai": {"enabled": True, "routing": "rules", "cache": False},
        "crawl": {"max_pages": 10, "max_records": 20, "concurrency": 1, "delay": 0, "max_depth": 3},
        **overrides,
    }
    return LoadedJob(
        config_path=tmp_path / "job.yaml",
        config=JobConfig.model_validate(config),
        extraction_schema=json.loads(json.dumps(SCHEMA)),
    )


def entity(name):
    return {
        "@type": "Article",
        "headline": f"Article {name}",
        "url": f"https://example.com/{name}",
        "description": "Observed article body",
    }


def detail(name):
    return '<script type="application/ld+json">' + json.dumps(entity(name)) + "</script>"


def catalog(names, *, next_page=False):
    cards = "".join(
        f'<article><h2><a href="/{name}">Article {name}</a></h2><p>Observed article</p></article>'
        for name in names
    )
    return (
        '<h1>Article catalog</h1><script type="application/ld+json">'
        + json.dumps([entity(name) for name in names])
        + "</script>"
        + cards
        + ('<a rel="next" href="/catalog?page=2">Next</a>' if next_page else "")
    )


class Pages:
    def __init__(self, mapping):
        self.mapping = mapping
        self.calls = []

    async def fetch(self, url):
        self.calls.append(url)
        value = self.mapping[url]
        if isinstance(value, Exception):
            raise value
        return make_page(url, value, 200)


class Model:
    model = "qwen/qwen3.7-flash"

    def __init__(self, *, fail=False):
        self.payloads = []
        self.fail = fail

    async def complete_json(self, system, payload, schema, **kwargs):
        self.payloads.append(payload)
        if self.fail:
            raise RuntimeError("Provider did not complete")
        return {"records": [{"metric": 7}]}


@pytest.mark.asyncio
async def test_new_run_revisits_catalog_and_pagination_fetches_only_new_detail(tmp_path):
    supplied = job(tmp_path)
    first_pages = Pages(
        {
            "https://example.com/catalog": catalog(["a"], next_page=True),
            "https://example.com/catalog?page=2": catalog(["b"]),
            "https://example.com/a": detail("a"),
            "https://example.com/b": detail("b"),
        }
    )
    first_model = Model()
    first = await PipelineRunner(
        supplied, data_dir=tmp_path, fetcher=first_pages, llm=first_model
    ).run()
    assert first.status == "success", first.errors
    assert first.records_written == 2 and first.incremental["completed_details"] == 2
    first_ids = {row["record_id"] for row in DatasetStore(tmp_path).list_records()}
    updated = load_update_job(Path(first.artifacts["directory"]))
    second_pages = Pages(
        {
            "https://example.com/catalog": catalog(["a"], next_page=True),
            "https://example.com/catalog?page=2": catalog(["b", "c"]),
            "https://example.com/c": detail("c"),
        }
    )
    second_model = Model()
    second = await PipelineRunner(
        updated, data_dir=tmp_path, fetcher=second_pages, llm=second_model
    ).run()
    assert second.run_id != first.run_id and second.status == "success", second.errors
    assert second.records_written == 1
    assert set(second_pages.calls) == {
        "https://example.com/catalog",
        "https://example.com/catalog?page=2",
        "https://example.com/c",
    }
    assert len(second_model.payloads) == 1
    assert second.incremental["skipped_detail_urls"] == 2
    assert second.incremental["parent_run_id"] == first.run_id
    rows = DatasetStore(tmp_path).list_records()
    assert len(rows) == 3 and first_ids <= {row["record_id"] for row in rows}
    delta = [json.loads(line) for line in Path(second.artifacts["jsonl"]).read_text().splitlines()]
    assert [row["data"]["url"] for row in delta] == ["https://example.com/c"]


@pytest.mark.asyncio
async def test_known_catalog_candidate_is_removed_before_routing_and_enrichment(tmp_path):
    first = await PipelineRunner(
        job(tmp_path, ["https://example.com/a"]),
        data_dir=tmp_path,
        fetcher=Pages({"https://example.com/a": detail("a")}),
        llm=Model(),
    ).run()
    assert first.incremental["completed_details"] == 1
    supplied = job(tmp_path, ["https://example.com/catalog?q=Article"]).model_copy(
        update={
            "config": job(tmp_path, ["https://example.com/catalog?q=Article"]).config.model_copy(
                update={
                    "crawl": job(tmp_path).config.crawl.model_copy(update={"max_depth": 0}),
                }
            )
        }
    )
    model = Model()
    report = await PipelineRunner(
        supplied,
        data_dir=tmp_path,
        fetcher=Pages(
            {
                "https://example.com/catalog?q=Article": (
                    '<form action="/catalog"><input name="q" value="Article">'
                    "<button>Search</button></form>" + catalog(["a", "b"])
                )
            }
        ),
        llm=model,
    ).run()
    assert report.status == "success", report.errors
    assert report.records_written == 1 and report.incremental["skipped_records"] == 1
    assert len(model.payloads) == 1
    assert "Article a" not in json.dumps(model.payloads)
    assert DatasetStore(tmp_path).list_records()[0]["data"]["url"] == "https://example.com/b"
    # A catalog extraction does not mark its own URL, nor an unvisited B detail complete.
    assert report.incremental["completed_details"] == 0


@pytest.mark.asyncio
async def test_http_failures_and_enrichment_failure_remain_eligible_next_run(tmp_path):
    supplied = job(tmp_path, ["https://example.com/a", "https://example.com/b"])
    first = await PipelineRunner(
        supplied,
        data_dir=tmp_path,
        fetcher=Pages(
            {
                "https://example.com/a": detail("a"),
                "https://example.com/b": FetchError(
                    "https://example.com/b", "Denied", status_code=403
                ),
            }
        ),
        llm=Model(fail=True),
    ).run()
    assert first.status == "partial" and first.incremental["completed_details"] == 0
    pages = Pages({"https://example.com/a": detail("a"), "https://example.com/b": detail("b")})
    second = await PipelineRunner(supplied, data_dir=tmp_path, fetcher=pages, llm=Model()).run()
    assert second.records_written == 2 and len(pages.calls) == 2
    assert second.incremental["completed_details"] == 2


@pytest.mark.asyncio
async def test_changed_schema_or_query_reprocesses_while_sources_can_expand(tmp_path):
    supplied = job(tmp_path, ["https://example.com/a"])
    await PipelineRunner(
        supplied,
        data_dir=tmp_path,
        fetcher=Pages({"https://example.com/a": detail("a")}),
        llm=Model(),
    ).run()
    expanded = job(tmp_path, ["https://example.com/a", "https://example.com/b"])
    pages = Pages({"https://example.com/b": detail("b")})
    report = await PipelineRunner(expanded, data_dir=tmp_path, fetcher=pages, llm=Model()).run()
    assert pages.calls == ["https://example.com/b"] and report.records_written == 1
    for changed in [
        supplied.model_copy(
            update={"config": supplied.config.model_copy(update={"search": "Article a"})}
        ),
        supplied.model_copy(
            update={"extraction_schema": {**SCHEMA, "title": "Different extraction schema"}}
        ),
    ]:
        fresh = Pages({"https://example.com/a": detail("a")})
        await PipelineRunner(changed, data_dir=tmp_path, fetcher=fresh, llm=Model()).run()
        assert fresh.calls == ["https://example.com/a"]


@pytest.mark.asyncio
async def test_catalog_only_and_cancelled_detail_are_not_permanently_complete(tmp_path):
    supplied = job(tmp_path, ["https://example.com/a"])

    def cancel_before_processing(kind, data):
        if kind == "stage" and data.get("stage") == "routing":
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await PipelineRunner(
            supplied,
            data_dir=tmp_path,
            fetcher=Pages({"https://example.com/a": detail("a")}),
            llm=Model(),
            on_event=cancel_before_processing,
        ).run()
    pages = Pages({"https://example.com/a": detail("a")})
    report = await PipelineRunner(supplied, data_dir=tmp_path, fetcher=pages, llm=Model()).run()
    assert len(pages.calls) == 1 and report.incremental["completed_details"] == 1


@pytest.mark.asyncio
async def test_unchanged_run_is_clean_empty_delta_and_cli_forks_exact_schema(tmp_path):
    supplied = job(tmp_path, ["https://example.com/a"])
    first = await PipelineRunner(
        supplied,
        data_dir=tmp_path,
        fetcher=Pages({"https://example.com/a": detail("a")}),
        llm=Model(),
    ).run()
    run_dir = Path(first.artifacts["directory"])
    with RunLease(run_dir) as lease:
        updated = load_update_job(run_dir, lease=lease)
    assert updated.extraction_schema == supplied.extraction_schema
    assert not updated.config.schema_generation.enabled and updated.config.incremental.enabled
    args = build_parser().parse_args(
        ["update", first.run_id, "--data-dir", str(tmp_path), "--max-pages", "5"]
    )
    parsed = load_requested_job(args)
    assert parsed.update_from_run == run_dir and parsed.config.crawl.max_pages == 5
    for option in ("--auto-schema", "--no-incremental"):
        unsafe = build_parser().parse_args(
            ["update", first.run_id, "--data-dir", str(tmp_path), option]
        )
        with pytest.raises(ConfigurationError, match="Update requires"):
            load_requested_job(unsafe)
    pages, model = Pages({}), Model()
    second = await PipelineRunner(parsed, data_dir=tmp_path, fetcher=pages, llm=model).run()
    assert second.status == "success" and second.records_written == 0
    assert pages.calls == model.payloads == []
    assert second.incremental["skipped_detail_urls"] == 1
    assert len(DatasetStore(tmp_path).list_records()) == 1
    assert CheckpointStore(Path(second.artifacts["directory"])).load()["completed"]
    assert build_parser().parse_args(["run", "profile.yaml", "--incremental"]).incremental is True
    assert (
        build_parser().parse_args(["run", "profile.yaml", "--no-incremental"]).incremental is False
    )


@pytest.mark.asyncio
async def test_update_rejects_unfinished_checkpoint_instead_of_losing_frontier(tmp_path):
    supplied = job(tmp_path, ["https://example.com/a", "https://example.com/b"])
    supplied = supplied.model_copy(
        update={
            "config": supplied.config.model_copy(
                update={
                    "crawl": supplied.config.crawl.model_copy(update={"max_pages": 1}),
                }
            )
        }
    )
    report = await PipelineRunner(
        supplied,
        data_dir=tmp_path,
        fetcher=Pages({"https://example.com/a": detail("a")}),
        llm=Model(),
    ).run()
    with pytest.raises(PipelineError, match="unfinished"):
        load_update_job(Path(report.artifacts["directory"]))


@pytest.mark.asyncio
@pytest.mark.parametrize("depth", [0, 3])
async def test_known_items_do_not_starve_new_candidates_or_pagination_at_small_caps(
    tmp_path, depth
):
    first_job = job(
        tmp_path,
        ["https://example.com/a", "https://example.com/b"],
        ai={"enabled": True, "routing": "rules", "cache": False, "max_candidates": 2},
        navigation={"mode": "rules", "max_actions": 2},
    )
    first = await PipelineRunner(
        first_job,
        data_dir=tmp_path,
        fetcher=Pages({"https://example.com/a": detail("a"), "https://example.com/b": detail("b")}),
        llm=Model(),
    ).run()
    assert first.incremental["completed_details"] == 2
    source = (
        "https://example.com/catalog?q=Article" if depth == 0 else "https://example.com/catalog"
    )
    next_job = first_job.model_copy(
        update={
            "config": first_job.config.model_copy(
                update={
                    "sources": [type(first_job.config.sources[0])(url=source)],
                    "crawl": first_job.config.crawl.model_copy(update={"max_depth": depth}),
                    "navigation": first_job.config.navigation.model_copy(
                        update={"max_actions": 3 if depth == 0 else 2}
                    ),
                }
            )
        }
    )
    html = catalog(["a", "b", "c"], next_page=True)
    if depth == 0:
        html = (
            '<form action="/catalog"><input name="q" value="Article">'
            "<button>Search</button></form>" + html
        )
    pages = Pages(
        {
            source: html,
            "https://example.com/catalog?page=2": catalog(["a", "b", "d"]),
            "https://example.com/c": detail("c"),
            "https://example.com/d": detail("d"),
        }
    )
    model = Model()
    report = await PipelineRunner(next_job, data_dir=tmp_path, fetcher=pages, llm=model).run()
    assert report.status == "success", report.errors
    expected = {"https://example.com/c", "https://example.com/d"}
    rows = [json.loads(line) for line in Path(report.artifacts["jsonl"]).read_text().splitlines()]
    assert {row["data"]["url"] for row in rows} == expected
    assert report.records_written == len(expected) == len(model.payloads)
    assert not {"https://example.com/a", "https://example.com/b"}.intersection(pages.calls)
    if depth:
        assert "https://example.com/catalog?page=2" in pages.calls
    else:
        assert not expected.intersection(pages.calls)


@pytest.mark.asyncio
async def test_legacy_seeding_does_not_block_event_loop_and_cancel_waits_for_worker(
    tmp_path, monkeypatch
):
    import threading
    import time

    import unified_web_parser.storage.incremental as storage_incremental

    supplied = job(tmp_path, ["https://example.com/a"])
    first = await PipelineRunner(
        supplied,
        data_dir=tmp_path,
        fetcher=Pages({"https://example.com/a": detail("a")}),
        llm=Model(),
    ).run()
    updated = load_update_job(Path(first.artifacts["directory"]))
    entered, exited = threading.Event(), threading.Event()

    def seed(*args, cancelled=None, **kwargs):
        entered.set()
        while not cancelled():
            time.sleep(0.002)
        exited.set()
        return {"seeded": [], "skipped": [], "cancelled": True}

    monkeypatch.setattr(storage_incremental, "seed_from_run", seed)
    task = asyncio.create_task(
        PipelineRunner(updated, data_dir=tmp_path, fetcher=Pages({}), llm=Model()).run()
    )
    async with asyncio.timeout(5):
        while not entered.is_set():
            await asyncio.sleep(0.001)
        assert not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert exited.is_set()

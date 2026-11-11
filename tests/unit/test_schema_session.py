"""The schema-session boundary preserves shared state and saved-page processing order."""

import json
from datetime import UTC, datetime

import pytest

from unified_web_parser.core.config import JobConfig
from unified_web_parser.crawling.pages import make_page
from unified_web_parser.pipeline import schema_session
from unified_web_parser.pipeline.report import RunReport


@pytest.mark.asyncio
async def test_freeze_keeps_shared_schema_and_restores_shell_before_draining_buffer(
    tmp_path, monkeypatch
):
    config = JobConfig(
        name="session",
        search="Find entities",
        schema_generation={"enabled": True},
        sources=[{"url": "https://example.org/"}],
    )
    shared_schema = {"type": "object", "properties": {"title": {"type": "string"}}}
    refined_schema = {
        "type": "object",
        "properties": {"title": {"type": "string"}, "duration": {"type": "number"}},
    }
    report = RunReport(run_id="session", job_name="session", started_at=datetime.now(UTC))
    constructed, processed, samples, events = [], [], [], []

    class Processor:
        def __init__(self, _config, schema, **_kwargs):
            assert schema is shared_schema
            assert schema == refined_schema
            constructed.append(self)

        async def process(self, page):
            saved_job = json.loads((tmp_path / "job.json").read_text(encoding="utf-8"))
            assert saved_job["schema"] == shared_schema
            processed.append((str(page.url), page.html))

    async def refine(_config, schema, pages, **_kwargs):
        assert schema is shared_schema
        samples.append([str(page.url) for page in pages])
        return refined_schema

    monkeypatch.setattr(schema_session, "PageProcessor", Processor)
    monkeypatch.setattr(schema_session, "refine_schema", refine)
    session = schema_session.SchemaSession(
        config,
        shared_schema,
        contextual=True,
        llm=None,
        decision_client=None,
        schema_clients={},
        records={},
        report=report,
        run_dir=tmp_path,
        progress=lambda _message: None,
        on_event=lambda kind, data: events.append((kind, data)),
    )
    await session.start()
    shell_html = "<h1>Welcome</h1><p>Please browse our website.</p>"
    snapshot = tmp_path / "shell.html"
    snapshot.write_text(shell_html, encoding="utf-8")
    shell = make_page("https://example.org/", shell_html, 200).model_copy(
        update={"snapshot_ref": str(snapshot)}
    )
    await session.process(shell)
    for index in range(2):
        await session.process(
            make_page(
                f"https://example.org/{index}",
                '<script type="application/ld+json">'
                + json.dumps({"@type": "Thing", "name": f"Entity {index}"})
                + "</script>",
                200,
            )
        )
    assert not constructed and not processed and not samples
    third = make_page(
        "https://example.org/2",
        '<script type="application/ld+json">'
        + json.dumps({"@type": "Thing", "name": "Entity 2"})
        + "</script>",
        200,
    )
    await session.process(third)
    await session.finalize()
    assert len(constructed) == len(samples) == 1
    assert samples[0] == [f"https://example.org/{index}" for index in range(3)]
    assert [url for url, _html in processed] == ["https://example.org/", *samples[0]]
    assert processed[0][1] == shell_html
    assert [kind for kind, _data in events] == ["stage", "schema"]
    assert shared_schema == refined_schema

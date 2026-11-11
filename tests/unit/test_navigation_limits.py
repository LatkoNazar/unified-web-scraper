"""Navigation safety/performance settings remain explicit and backend-specific."""

from contextlib import AsyncExitStack

import pytest
from pydantic import ValidationError

from unified_web_parser.core.config import CrawlConfig, JobConfig
from unified_web_parser.pipeline.resources import prepare_fetcher


@pytest.mark.parametrize("value", [-0.1, 10.1])
def test_render_delay_is_bounded(value):
    with pytest.raises(ValidationError):
        CrawlConfig(render_delay=value)
    assert CrawlConfig().render_delay == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["http", "browser"])
async def test_only_browser_receives_configured_render_delay(monkeypatch, backend):
    observed = {}

    class Fetcher:
        def __init__(self, **kwargs):
            observed.update(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

    monkeypatch.setattr("unified_web_parser.pipeline.resources.HttpFetcher", Fetcher)
    monkeypatch.setattr("unified_web_parser.providers.crawl4ai.Crawl4AIFetcher", Fetcher)
    config = JobConfig(
        name="delay",
        sources=[{"url": "https://example.org"}],
        schema_path="schema.json",
        crawl={"backend": backend, "render_delay": 0.5},
    )
    async with AsyncExitStack() as stack:
        await prepare_fetcher(None, config, {}, stack=stack, replay=False)
    if backend == "browser":
        assert observed["render_delay"] == 0.5
    else:
        assert "render_delay" not in observed

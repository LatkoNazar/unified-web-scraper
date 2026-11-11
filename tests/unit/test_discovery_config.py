"""Discovery options fail locally before any provider request."""

import pytest
from pydantic import ValidationError

from unified_web_parser.core.config import DiscoveryConfig, JobConfig, LoadedJob, load_job
from unified_web_parser.interfaces.cli import _overrides, _parser


@pytest.mark.parametrize(
    "options",
    [
        {"search_results": 0},
        {"search_results": 26},
        {"search_results": True},
        {"max_sites": 0},
        {"max_sites": 101},
        {"max_queries": 4},
        {"pages_per_site": 0},
        {"pages_per_site": 1001},
        {"cache_ttl_seconds": 604801},
        {"engine": "parallel", "mode": "auto"},
        {"engine": "exa", "mode": "basic"},
    ],
)
def test_discovery_rejects_invalid_provider_and_budget_options(options):
    with pytest.raises(ValidationError):
        DiscoveryConfig.model_validate(options)


def test_engine_default_mode_and_maximum_values():
    assert DiscoveryConfig().effective_mode == "basic"
    assert DiscoveryConfig(engine="exa").effective_mode == "auto"
    assert DiscoveryConfig(search_results=25, max_queries=3, pages_per_site=1000)
    assert DiscoveryConfig(cache_ttl_seconds=0).cache_ttl_seconds == 0


@pytest.mark.parametrize("path", ["configs/jobs/web.yaml", "configs/jobs/web-auto.yaml"])
def test_generic_profiles_allow_site_navigation_after_all_roots(path):
    config = load_job(path).config
    assert config.discovery.max_queries == 3
    assert config.discovery.search_results == config.discovery.max_sites == 25
    assert config.discovery.pages_per_site == 5
    assert config.discovery.entrypoint == "site_root"
    assert config.crawl.max_pages == 125
    assert config.crawl.max_records == config.ai.max_calls == 100
    assert config.navigation.allow_filters is False


def test_cli_site_limits_and_filter_opt_in_are_independent(tmp_path):
    args = _parser().parse_args(
        [
            "discover",
            "books",
            "--search-results",
            "25",
            "--max-sites",
            "7",
            "--entrypoint",
            "result_url",
            "--allow-filters",
        ]
    )
    config = _overrides(profile(tmp_path), args).config
    assert config.discovery.search_results == 25
    assert config.discovery.max_sites == 7
    assert config.discovery.entrypoint == "result_url"
    assert config.navigation.allow_filters is True


@pytest.mark.parametrize(
    "updates",
    [
        {"discovery": {"enabled": False}},
        {"search": None},
        {"search": " "},
        {"search": "x" * 4001},
    ],
)
def test_source_free_jobs_require_an_enabled_discovery_question(updates):
    values = {
        "name": "web",
        "schema_path": "schema.json",
        "search": "books",
        "sources": [],
        "discovery": {"enabled": True},
        **updates,
    }
    with pytest.raises(ValidationError):
        JobConfig.model_validate(values)


def profile(tmp_path):
    return LoadedJob(
        config=JobConfig(
            name="web",
            schema_path="schema.json",
            search="old question",
            discovery=DiscoveryConfig(enabled=True, mode="basic"),
        ),
        config_path=tmp_path / "job.yaml",
        schema_path=tmp_path / "schema.json",
        extraction_schema={"type": "object"},
    )


def test_cli_engine_override_selects_compatible_default_mode(tmp_path):
    args = _parser().parse_args(["discover", "books", "--search-engine", "exa"])
    job = _overrides(profile(tmp_path), args)
    assert job.config.discovery.engine == "exa"
    assert job.config.discovery.effective_mode == "auto"
    assert job.config.sources == []


@pytest.mark.parametrize(
    "arguments",
    [
        ["search", "books", "--source", "https://books.example/"],
        ["extract", "https://books.example/item", "--config", "unused.yaml"],
    ],
)
def test_explicit_source_commands_disable_web_discovery(tmp_path, arguments):
    job = _overrides(profile(tmp_path), _parser().parse_args(arguments))
    assert job.config.discovery.enabled is False
    assert len(job.config.sources) == 1
    if arguments[0] == "extract":
        assert job.config.search is None
        assert job.config.crawl.max_depth == 0

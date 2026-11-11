"""Manual schema loading remains explicit; automatic generation is deferred."""

import pytest
from pydantic import ValidationError

from unified_web_parser.core.config import JobConfig, load_job
from unified_web_parser.core.exceptions import ConfigurationError
from unified_web_parser.interfaces.cli import _overrides, _parser


@pytest.mark.parametrize("max_fields", [1, 31, True])
def test_invalid_field_limits_fail_before_generation(max_fields):
    with pytest.raises(ValidationError):
        JobConfig(
            name="x",
            search="jobs",
            discovery={"enabled": True},
            schema_generation={"enabled": True, "max_fields": max_fields},
        )


def test_missing_manual_schema_is_allowed_only_when_generation_enabled(tmp_path):
    path = tmp_path / "job.yaml"
    path.write_text(
        "name: jobs\nsources: [{url: https://jobs.example/}]\nschema_path: missing.json\n"
    )
    with pytest.raises(ConfigurationError):
        load_job(path)
    deferred = load_job(path, auto_schema=True, question="Find editor vacancies")
    assert deferred.schema_path is None and deferred.extraction_schema == {}
    assert deferred.config.search == "Find editor vacancies"
    with pytest.raises(ConfigurationError):
        load_job(path, auto_schema=True)


def test_discover_defaults_to_auto_but_explicit_profile_and_fixed_flag_keep_manual_schema():
    job = load_job("configs/jobs/web.yaml")
    implicit = _overrides(job, _parser().parse_args(["discover", "Find editors"]))
    assert implicit.config.schema_generation.enabled is True
    explicit = _overrides(
        job, _parser().parse_args(["discover", "Find editors", "--config", "configs/jobs/web.yaml"])
    )
    assert explicit.config.schema_generation.enabled is False
    fixed = _overrides(job, _parser().parse_args(["discover", "Find editors", "--fixed-schema"]))
    assert fixed.config.schema_generation.enabled is False
    forced = _overrides(
        job,
        _parser().parse_args(
            [
                "discover",
                "Find editors",
                "--config",
                "configs/jobs/web.yaml",
                "--auto-schema",
                "--schema-max-fields",
                "5",
            ]
        ),
    )
    assert (
        forced.config.schema_generation.enabled and forced.config.schema_generation.max_fields == 5
    )


def test_auto_and_fixed_flags_are_mutually_exclusive():
    with pytest.raises(SystemExit):
        _parser().parse_args(["discover", "editors", "--auto-schema", "--fixed-schema"])

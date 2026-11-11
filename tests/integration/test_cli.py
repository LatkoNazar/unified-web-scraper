import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from unified_web_parser.core.config import load_job
from unified_web_parser.interfaces.cli import _overrides, _parser, main

ROOT = Path(__file__).resolve().parents[2]


def test_module_entry_point_validates_example():
    result = subprocess.run(
        [sys.executable, "-m", "unified_web_parser", "validate", "configs/jobs/macbooks.yaml"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "Valid job: macbook-monitor" in result.stdout


def test_invalid_config_returns_nonzero_without_traceback(tmp_path: Path, capsys):
    invalid = tmp_path / "invalid.yaml"
    invalid.write_text("name: [bad\n", encoding="utf-8")
    assert main(["validate", str(invalid)]) == 1
    output = capsys.readouterr()
    assert "Configuration error:" in output.err
    assert "Traceback" not in output.err


def test_missing_file_returns_nonzero(capsys):
    assert main(["validate", "missing-config.yaml"]) == 1
    assert "Configuration error:" in capsys.readouterr().err


def test_unknown_command_fails(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["unknown-command", "configs/jobs/macbooks.yaml"])
    assert exc.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_search_navigation_uses_supplied_roots_and_independent_policy():
    args = _parser().parse_args(
        [
            "search",
            "Двокімнатна квартира Львів",
            "--source",
            "https://housing.example/",
            "--source",
            "https://another.example/catalog",
            "--navigation",
            "rules",
            "--routing",
            "hybrid",
            "--no-ai",
        ]
    )
    job = _overrides(load_job(ROOT / "configs/jobs/macbooks.yaml"), args)
    assert [str(source.url) for source in job.config.sources] == [
        "https://housing.example/",
        "https://another.example/catalog",
    ]
    assert job.config.search == "Двокімнатна квартира Львів"
    assert job.config.navigation.mode == "rules"
    assert job.config.ai.routing == "hybrid"
    assert job.config.ai.enabled is False


@pytest.mark.parametrize(
    "extra,generated,profile",
    [
        ([], True, "generic"),
        (["--fixed-schema", "--no-ai"], False, "generic"),
        (["--config", "configs/jobs/macbooks.yaml"], False, "commerce"),
    ],
)
def test_search_default_is_generic_auto_schema_without_web_discovery(
    extra, generated, profile, monkeypatch, capsys
):
    from unified_web_parser.pipeline.report import RunReport

    captured = []

    class LocalRunner:
        def __init__(self, job, **kwargs):
            captured.append(job)

        async def run(self, **kwargs):
            return RunReport(
                run_id="cli", job_name="cli", status="success", started_at=datetime.now(UTC)
            )

    monkeypatch.setattr("unified_web_parser.pipeline.runner.PipelineRunner", LocalRunner)
    assert (
        main(["search", "Atmospheric observations", "--source", "https://science.example/", *extra])
        == 0
    )
    job = captured[0]
    assert job.config.schema_generation.enabled is generated
    assert job.config.content_profile == profile
    assert not job.config.discovery.enabled
    assert job.config.search == "Atmospheric observations"
    assert [str(source.url) for source in job.config.sources] == ["https://science.example/"]
    assert "success" in capsys.readouterr().out

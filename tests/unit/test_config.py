import json
from pathlib import Path

import pytest

from unified_web_parser.core.config import load_job
from unified_web_parser.core.exceptions import ConfigurationError


@pytest.fixture
def job_files(tmp_path: Path) -> Path:
    jobs = tmp_path / "jobs"
    schemas = tmp_path / "schemas"
    jobs.mkdir()
    schemas.mkdir()
    (schemas / "vacancy.json").write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "properties": {"employer": {"type": "string"}, "salary": {"type": "number"}},
                "required": ["employer"],
            }
        ),
        encoding="utf-8",
    )
    path = jobs / "vacancies.yaml"
    path.write_text(
        "name: engineering-jobs\nsources:\n  - url: https://example.org/jobs\n"
        "schema_path: ../schemas/vacancy.json\n",
        encoding="utf-8",
    )
    return path


def test_arbitrary_schema_and_relative_path(job_files: Path, tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path.parent)
    job = load_job(job_files)
    assert job.config.name == "engineering-jobs"
    assert job.schema_path == (tmp_path / "schemas" / "vacancy.json").resolve()
    assert "employer" in job.extraction_schema["properties"]
    assert job.config.outputs == []


def test_checked_in_macbook_example():
    path = Path(__file__).resolve().parents[2] / "configs/jobs/macbooks.yaml"
    assert load_job(path).config.outputs == ["jsonl", "parquet"]


@pytest.mark.parametrize(
    "setting",
    [
        "mode: browser-agent",
        "max_actions: 0",
        "max_actions: 191",
        "max_actions: true",
        "max_summary_chars: 99",
        "max_interactions_per_url: 0",
        "unknown_setting: true",
    ],
)
def test_navigation_policy_rejects_unsupported_or_unbounded_settings(job_files, setting):
    with job_files.open("a", encoding="utf-8") as stream:
        stream.write(f"navigation:\n  {setting}\n")
    with pytest.raises(ConfigurationError):
        load_job(job_files)


@pytest.mark.parametrize("addition", ["unknown: value\n", "name: duplicate\n", "outputs: [xml]\n"])
def test_invalid_or_duplicate_options_rejected(job_files: Path, addition: str):
    with job_files.open("a", encoding="utf-8") as stream:
        stream.write(addition)
    with pytest.raises(ConfigurationError):
        load_job(job_files)


@pytest.mark.parametrize(
    "document",
    [
        "name: [broken\n",
        "- not-a-job\n",
        "",
        "name: 123\nsources: []\nschema_path: none.json\n",
        "name: x\nsources: [{url: ftp://example.org}]\nschema_path: x.json\n",
        "name: x\nsources: [{url: https://example.org, typo: true}]\nschema_path: x.json\n",
    ],
)
def test_malformed_jobs_rejected(job_files: Path, document: str):
    job_files.write_text(document, encoding="utf-8")
    with pytest.raises(ConfigurationError):
        load_job(job_files)


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "not-a-type"},
        {"$ref": "https://example.org/schema.json"},
        {"properties": {"name": {"$ref": "other.json#/$defs/name"}}},
        {"$defs": {"name": {"$dynamicRef": "https://example.org#name"}}},
        {"$schema": "https://example.org/custom-dialect"},
        {"$schema": []},
        [],
    ],
)
def test_invalid_or_external_schemas_rejected(job_files: Path, schema):
    schema_path = job_files.parent.parent / "schemas/vacancy.json"
    schema_path.write_text(json.dumps(schema), encoding="utf-8")
    with pytest.raises(ConfigurationError):
        load_job(job_files)


def test_internal_refs_and_literal_ref_fields_are_supported(job_files: Path):
    schema = {
        "$defs": {"label": {"type": "string"}},
        "type": "object",
        "properties": {"$ref": {"$ref": "#/$defs/label"}},
        "examples": [{"$ref": "https://example.org/data"}],
    }
    schema_path = job_files.parent.parent / "schemas/vacancy.json"
    schema_path.write_text(json.dumps(schema), encoding="utf-8")
    assert load_job(job_files).extraction_schema == schema


@pytest.mark.parametrize("contents", ["{broken", "\udc80"])
def test_unreadable_json_schema_rejected(job_files: Path, contents: str):
    schema_path = job_files.parent.parent / "schemas/vacancy.json"
    schema_path.write_bytes(contents.encode("utf-8", errors="surrogatepass"))
    with pytest.raises(ConfigurationError):
        load_job(job_files)


def test_missing_config_and_schema_errors(job_files: Path):
    with pytest.raises(ConfigurationError):
        load_job(job_files.parent / "missing.yaml")
    (job_files.parent.parent / "schemas/vacancy.json").unlink()
    with pytest.raises(ConfigurationError):
        load_job(job_files)

"""Validated HTTP run requests and profile-to-job translation."""

from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, StringConstraints

from unified_web_parser.core.config import JobConfig, LoadedJob, load_job
from unified_web_parser.core.exceptions import ConfigurationError


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: Annotated[
        str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=4000)
    ]
    profile: (
        Annotated[str, StringConstraints(strict=True, pattern=r"^[A-Za-z0-9_-]{1,100}$")] | None
    ) = None
    sources: list[HttpUrl] = Field(default_factory=list, max_length=25)
    backend: Literal["http", "browser"] = "http"
    ai: bool = Field(default=True, strict=True)
    incremental: bool = Field(default=False, strict=True)
    max_pages: Annotated[int, Field(strict=True, ge=1, le=500)] | None = None
    max_records: Annotated[int, Field(strict=True, ge=1, le=1000)] | None = None
    max_calls: Annotated[int, Field(strict=True, ge=0, le=500)] | None = None


def requested_job(request: RunRequest, config_dir: Path) -> LoadedJob:
    """Use a known profile, with the same generic schema defaults as the CLI."""
    root = config_dir.resolve()
    path = (root / f"{request.profile or 'web'}.yaml").resolve()
    if path.parent != root or not path.is_file():
        raise ConfigurationError("Unknown job profile")
    auto_schema = (
        True if request.profile is None and request.ai else False if not request.ai else None
    )
    job = load_job(path, auto_schema=auto_schema, question=request.query)
    config = job.config.model_dump(mode="json")
    config["search"] = request.query
    config["sources"] = [{"url": str(url)} for url in request.sources]
    config["discovery"]["enabled"] = not bool(request.sources)
    config["crawl"]["backend"] = request.backend
    config["ai"]["enabled"] = request.ai
    config["incremental"]["enabled"] = request.incremental
    for name, cap in (("max_pages", 500), ("max_records", 1000)):
        config["crawl"][name] = getattr(request, name) or min(config["crawl"][name], cap)
    config["ai"]["max_calls"] = (
        request.max_calls if request.max_calls is not None else min(config["ai"]["max_calls"], 500)
    )
    return job.model_copy(update={"config": JobConfig.model_validate(config)})

"""Load strict job YAML and an offline, local JSON Schema document."""

import json
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from jsonschema.exceptions import SchemaError
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    StringConstraints,
    ValidationError,
    model_validator,
)
from yaml.constructor import ConstructorError

from unified_web_parser.core.exceptions import ConfigurationError
from unified_web_parser.core.schema import validate_schema as validate_schema

NonEmptyString = Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1)]


class StrictConfig(BaseModel):
    """Reject misspelled and unsupported configuration options."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class SourceConfig(StrictConfig):
    url: HttpUrl
    kind: Literal["url", "sitemap"] = "url"


class CrawlConfig(StrictConfig):
    backend: Literal["http", "browser"] = "http"
    max_pages: Annotated[int, Field(strict=True, ge=1, le=10000)] = 20
    max_records: Annotated[int, Field(strict=True, ge=1, le=100000)] = 100
    max_depth: Annotated[int, Field(strict=True, ge=0, le=10)] = 2
    concurrency: Annotated[int, Field(strict=True, ge=1, le=20)] = 2
    timeout: Annotated[float, Field(gt=0, le=300)] = 30
    delay: Annotated[float, Field(ge=0, le=60)] = 1
    render_delay: Annotated[float, Field(ge=0, le=10)] = 3
    retries: Annotated[int, Field(strict=True, ge=0, le=5)] = 2
    max_bytes: Annotated[int, Field(strict=True, ge=1024, le=50000000)] = 5000000


class IncrementalConfig(StrictConfig):
    """Reuse only completed detail entities across compatible runs of this job."""

    enabled: bool = False


class AIConfig(StrictConfig):
    enabled: bool = True
    model: NonEmptyString = "qwen/qwen3.7-flash"
    decision_model: NonEmptyString = "openai/gpt-6-luna-decisions"
    response_mode: Literal["auto", "json_schema", "json_object"] = "auto"
    reasoning: bool | None = None
    max_output_tokens: Annotated[int, Field(strict=True, ge=256, le=65536)] = 6000
    sparse_enrichment: bool = True
    routing: Literal["rules", "ai", "hybrid"] = "hybrid"
    max_calls: Annotated[int, Field(strict=True, ge=0, le=10000)] = 30
    max_candidates: Annotated[int, Field(strict=True, ge=1, le=200)] = 30
    max_candidate_chars: Annotated[int, Field(strict=True, ge=100, le=100000)] = 16000
    timeout: Annotated[float, Field(gt=0, le=300)] = 60
    cache: bool = True

    @model_validator(mode="after")
    def no_implicit_web_search(self) -> "AIConfig":
        for model in (self.model, self.decision_model):
            if "online" in (part.casefold() for part in model.split(":")[1:]):
                raise ValueError("The deprecated :online modifier causes implicit web search")
        return self


class NavigationConfig(StrictConfig):
    """Bounded, domain-independent discovery and browser interaction policy."""

    mode: Literal["rules", "ai", "hybrid"] = "hybrid"
    max_actions: Annotated[int, Field(strict=True, ge=1, le=190)] = 80
    max_summary_chars: Annotated[int, Field(strict=True, ge=100, le=16000)] = 4000
    max_interactions_per_url: Annotated[int, Field(strict=True, ge=1, le=100)] = 8
    allow_scroll: bool = True
    allow_filters: bool = False


class DiscoveryConfig(StrictConfig):
    """Web search selects source URLs before the normal bounded crawler starts."""

    enabled: bool = False
    query_mode: Literal["ai", "literal"] = "ai"
    max_queries: Annotated[int, Field(strict=True, ge=1, le=3)] = 3
    search_results: Annotated[int, Field(strict=True, ge=1, le=25)] = 25
    max_sites: Annotated[int, Field(strict=True, ge=1, le=100)] = 25
    entrypoint: Literal["site_root", "result_url"] = "site_root"
    search_context_size: Literal["low", "medium", "high"] = "low"
    pages_per_site: Annotated[int, Field(strict=True, ge=1, le=1000)] = 10
    engine: Literal["parallel", "exa"] = "parallel"
    mode: (
        Literal[
            "basic",
            "fast",
            "turbo",
            "advanced",
            "auto",
            "instant",
            "deep-lite",
            "deep",
            "deep-reasoning",
        ]
        | None
    ) = None
    cache_ttl_seconds: Annotated[int, Field(strict=True, ge=0, le=604800)] = 3600

    @model_validator(mode="after")
    def engine_mode(self) -> "DiscoveryConfig":
        allowed = (
            {"basic", "fast", "turbo", "advanced"}
            if self.engine == "parallel"
            else {"auto", "instant", "fast", "deep-lite", "deep", "deep-reasoning"}
        )
        if self.mode is not None and self.mode not in allowed:
            raise ValueError(
                f"Discovery mode {self.mode!r} is unsupported by engine {self.engine!r}"
            )
        return self

    @property
    def effective_mode(self) -> str:
        return self.mode or ("basic" if self.engine == "parallel" else "auto")


class SchemaGenerationConfig(StrictConfig):
    enabled: bool = False
    max_fields: Annotated[int, Field(strict=True, ge=2, le=30)] = 20


class JobConfig(StrictConfig):
    name: NonEmptyString
    content_profile: Literal["generic", "commerce"] = "generic"
    assessment_profile: Literal["generic", "device"] = "generic"
    currency_aliases: dict[NonEmptyString, NonEmptyString] = Field(
        default_factory=dict, max_length=100
    )
    relevance_fields: list[NonEmptyString] | None = Field(default=None, max_length=100)
    sources: list[SourceConfig] = Field(default_factory=list)
    schema_path: NonEmptyString | None = None
    search: NonEmptyString | None = None
    outputs: list[Literal["postgres", "parquet", "jsonl", "csv"]] = Field(default_factory=list)
    crawl: CrawlConfig = Field(default_factory=CrawlConfig)
    incremental: IncrementalConfig = Field(default_factory=IncrementalConfig)
    ai: AIConfig = Field(default_factory=AIConfig)
    navigation: NavigationConfig = Field(default_factory=NavigationConfig)
    discovery: DiscoveryConfig = Field(default_factory=DiscoveryConfig)
    schema_generation: SchemaGenerationConfig = Field(default_factory=SchemaGenerationConfig)
    assessment: list[NonEmptyString] = Field(default_factory=list)

    @model_validator(mode="after")
    def source_requirements(self) -> "JobConfig":
        if not self.sources and not self.discovery.enabled:
            raise ValueError("At least one source is required unless web discovery is enabled")
        if self.discovery.enabled:
            if self.search is None:
                raise ValueError("Web discovery requires a search question")
            if len(self.search) > 4000:
                raise ValueError("Discovery questions must not exceed 4000 characters")
        if not self.schema_generation.enabled and self.schema_path is None:
            raise ValueError("schema_path is required unless schema generation is enabled")
        if self.schema_generation.enabled and (self.search is None or len(self.search) > 4000):
            raise ValueError("Schema generation requires a question of 1 to 4000 characters")
        return self


class LoadedJob(StrictConfig):
    """A validated configuration plus its resolved schema and source paths."""

    config: JobConfig
    config_path: Path
    schema_path: Path | None = None
    extraction_schema: dict[str, Any]
    update_from_run: Path | None = None


class _UniqueKeyLoader(yaml.SafeLoader):
    """Avoid silently accepting overwritten YAML options."""


def _mapping(loader: _UniqueKeyLoader, node: yaml.MappingNode) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        try:
            duplicate = key in result
        except TypeError as exc:
            raise ConstructorError(
                None, None, "unhashable mapping key", key_node.start_mark
            ) from exc
        if duplicate:
            raise ConstructorError(None, None, f"duplicate key: {key!r}", key_node.start_mark)
        result[key] = loader.construct_object(value_node, deep=True)
    return result


_UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def load_job(
    path: str | Path, *, auto_schema: bool | None = None, question: str | None = None
) -> LoadedJob:
    """Read YAML, resolve ``schema_path`` relative to it, and validate the schema."""
    config_path = Path(path).expanduser().resolve()
    try:
        document = yaml.load(config_path.read_text(encoding="utf-8"), Loader=_UniqueKeyLoader)
        if isinstance(document, dict):
            if auto_schema is not None:
                if not isinstance(document.get("schema_generation", {}), dict):
                    raise ConfigurationError("schema_generation must be a mapping")
                document["schema_generation"] = {
                    **document.get("schema_generation", {}),
                    "enabled": auto_schema,
                }
            if question is not None:
                document["search"] = question
        config = JobConfig.model_validate(document)
        schema_path = None
        schema = {}
        if not config.schema_generation.enabled:
            schema_path = (config_path.parent / config.schema_path).resolve()
            schema = validate_schema(json.loads(schema_path.read_text(encoding="utf-8")))
    except (
        OSError,
        UnicodeError,
        yaml.YAMLError,
        ValidationError,
        json.JSONDecodeError,
        SchemaError,
    ) as exc:
        raise ConfigurationError(f"Cannot load job {config_path}: {exc}") from exc
    return LoadedJob(
        config=config,
        config_path=config_path,
        schema_path=schema_path,
        extraction_schema=schema,
    )

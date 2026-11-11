"""Argument definitions for the command-line interface."""

import argparse
from pathlib import Path


def _runtime_options(parser: argparse.ArgumentParser) -> None:
    schema = parser.add_mutually_exclusive_group()
    schema.add_argument(
        "--auto-schema",
        dest="auto_schema",
        action="store_const",
        const=True,
        default=None,
        help="Generate the extraction schema from the question",
    )
    schema.add_argument(
        "--fixed-schema",
        dest="auto_schema",
        action="store_const",
        const=False,
        help="Use the configured manual schema",
    )
    parser.add_argument(
        "--schema-max-fields", type=int, help="Generated fields including title/url (2–30)"
    )
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--backend", choices=["http", "browser"])
    parser.add_argument("--content-profile", choices=["generic", "commerce"])
    parser.add_argument("--assessment-profile", choices=["generic", "device"])
    parser.add_argument("--timeout", type=float, help="Page request timeout in seconds (0–300)")
    parser.add_argument("--retries", type=int, help="Page request retries (0–5)")
    parser.add_argument("--ai-timeout", type=float, help="Provider request timeout (0–300 seconds)")
    incremental = parser.add_mutually_exclusive_group()
    incremental.add_argument(
        "--incremental",
        dest="incremental",
        action="store_const",
        const=True,
        default=None,
        help="Skip compatible completed details across job runs",
    )
    incremental.add_argument(
        "--no-incremental", dest="incremental", action="store_const", const=False
    )
    parser.add_argument("--max-pages", type=int)
    parser.add_argument("--max-records", type=int)
    parser.add_argument("--max-calls", type=int)
    parser.add_argument("--model")
    parser.add_argument(
        "--decision-model", help="Navigation and block selection model, independent of extraction"
    )
    parser.add_argument("--response-mode", choices=["auto", "json_schema", "json_object"])
    parser.add_argument("--reasoning", choices=["auto", "on", "off"])
    parser.add_argument("--max-output-tokens", type=int)
    parser.add_argument(
        "--full-enrichment",
        action="store_true",
        help="Request full records instead of sparse JSON-LD enrichment",
    )
    parser.add_argument("--routing", choices=["rules", "ai", "hybrid"])
    parser.add_argument("--navigation", choices=["rules", "ai", "hybrid"])
    filters = parser.add_mutually_exclusive_group()
    filters.add_argument(
        "--allow-filters",
        dest="allow_filters",
        action="store_const",
        const=True,
        default=None,
        help="Allow observed filter/sort controls",
    )
    filters.add_argument("--no-filters", dest="allow_filters", action="store_const", const=False)
    parser.add_argument(
        "--no-ai",
        action="store_true",
        help="Disable inference; discovery still performs budgeted literal web search",
    )
    parser.add_argument("--outputs", nargs="+", choices=["jsonl", "parquet", "csv", "postgres"])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="unified-parser")
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path(".env"),
        help="Local settings file; existing environment takes precedence",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate", help="Validate a job YAML and local JSON Schema")
    validate.add_argument("config")
    run = commands.add_parser("run", help="Collect a schema-driven dataset")
    run.add_argument("config")
    _runtime_options(run)
    search = commands.add_parser("search", help="Search sources using a job schema")
    search.add_argument("query")
    search.add_argument(
        "--source",
        action="append",
        required=True,
        help="Absolute source URL; repeatable",
    )
    search.add_argument(
        "--config", default=None, help="Explicit profiles keep their schema by default"
    )
    _runtime_options(search)
    discover = commands.add_parser(
        "discover", help="Search the web, then crawl selected result sites"
    )
    discover.add_argument("query", help="Question or search intent")
    discover.add_argument(
        "--config", default=None, help="Explicit profiles keep their schema by default"
    )
    discover.add_argument("--search-results", type=int, help="Citation limit per query (1–25)")
    discover.add_argument(
        "--max-sites", type=int, help="Distinct site cap across all queries (1–100)"
    )
    discover.add_argument("--entrypoint", choices=["site_root", "result_url"])
    discover.add_argument("--pages-per-site", type=int)
    discover.add_argument("--query-mode", choices=["ai", "literal"])
    discover.add_argument("--max-queries", type=int)
    discover.add_argument("--search-engine", choices=["parallel", "exa"])
    discover.add_argument("--search-context-size", choices=["low", "medium", "high"])
    discover.add_argument(
        "--search-mode",
        choices=[
            "basic",
            "fast",
            "turbo",
            "advanced",
            "auto",
            "instant",
            "deep-lite",
            "deep",
            "deep-reasoning",
        ],
    )
    _runtime_options(discover)
    extract = commands.add_parser("extract", help="Extract a direct URL using a job schema")
    extract.add_argument("url")
    extract.add_argument("--config", required=True)
    _runtime_options(extract)
    replay = commands.add_parser("reprocess", help="Process saved snapshots without crawling")
    replay.add_argument("config")
    replay.add_argument("--from-run", type=Path, required=True)
    _runtime_options(replay)
    resume = commands.add_parser(
        "resume", help="Continue an interrupted run from its durable checkpoint"
    )
    resume.add_argument("run_id")
    _runtime_options(resume)
    update = commands.add_parser("update", help="Start a NEW incremental run from a finished run")
    update.add_argument("run_id")
    _runtime_options(update)
    export = commands.add_parser("export", help="Export the current stored job dataset")
    export.add_argument("job_name")
    export.add_argument("--format", choices=["parquet", "jsonl", "csv"], default="parquet")
    export.add_argument("--output", type=Path, required=True)
    export.add_argument("--data-dir", type=Path, default=Path("data"))
    for name in ("runs", "records"):
        command = commands.add_parser(name, help=f"Inspect stored {name}")
        command.add_argument("--data-dir", type=Path, default=Path("data"))
        command.add_argument("--limit", type=int, default=20)
        if name == "records":
            command.add_argument("--job")
    serve = commands.add_parser("serve", help="Serve the local job and dataset API")
    serve.add_argument("--data-dir", type=Path, default=Path("data"))
    serve.add_argument("--config-dir", type=Path, default=Path("configs/jobs"))
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    return parser

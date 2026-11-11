"""Resolve job configuration and apply explicit CLI options."""

import argparse

from unified_web_parser.core.config import JobConfig, LoadedJob, load_job


def schema_mode(args: argparse.Namespace) -> bool | None:
    mode = getattr(args, "auto_schema", None)
    if mode is None and args.command in {"search", "discover"} and args.config is None:
        return True
    return mode


def apply_overrides(job: LoadedJob, args: argparse.Namespace) -> LoadedJob:
    config = job.config.model_dump(mode="json")
    if getattr(args, "incremental", None) is not None:
        config["incremental"]["enabled"] = args.incremental
    for option in ("content_profile", "assessment_profile"):
        value = getattr(args, option, None)
        if value is not None:
            config[option] = value
    auto_schema = schema_mode(args)
    if auto_schema is not None:
        config["schema_generation"]["enabled"] = auto_schema
    if getattr(args, "schema_max_fields", None) is not None:
        config["schema_generation"]["max_fields"] = args.schema_max_fields
    for option in ("backend", "max_pages", "max_records", "timeout", "retries"):
        value = getattr(args, option, None)
        if value is not None:
            config["crawl"][option] = value
    for option in (
        "max_calls",
        "model",
        "decision_model",
        "response_mode",
        "routing",
        "max_output_tokens",
    ):
        value = getattr(args, option, None)
        if value is not None:
            config["ai"][option] = value
    if getattr(args, "ai_timeout", None) is not None:
        config["ai"]["timeout"] = args.ai_timeout
    if args.reasoning is not None:
        config["ai"]["reasoning"] = {"auto": None, "on": True, "off": False}[args.reasoning]
    if args.full_enrichment:
        config["ai"]["sparse_enrichment"] = False
    if args.no_ai:
        config["ai"]["enabled"] = False
    if args.navigation is not None:
        config["navigation"]["mode"] = args.navigation
    if getattr(args, "allow_filters", None) is not None:
        config["navigation"]["allow_filters"] = args.allow_filters
    if args.outputs is not None:
        config["outputs"] = args.outputs
    if args.command == "search":
        config["discovery"]["enabled"] = False
        config["search"] = args.query
        config["sources"] = [{"url": source} for source in args.source]
    elif args.command == "discover":
        config["search"] = args.query
        config["sources"] = []
        config["discovery"]["enabled"] = True
        for option in (
            "search_results",
            "pages_per_site",
            "query_mode",
            "max_queries",
            "max_sites",
            "entrypoint",
            "search_context_size",
        ):
            value = getattr(args, option, None)
            if value is not None:
                config["discovery"][option] = value
        if args.search_engine is not None:
            config["discovery"]["engine"] = args.search_engine
            if args.search_mode is None:
                config["discovery"]["mode"] = None
        if args.search_mode is not None:
            config["discovery"]["mode"] = args.search_mode
    elif args.command == "extract":
        config["discovery"]["enabled"] = False
        config["search"] = None
        config["sources"] = [{"url": args.url}]
        config["crawl"]["max_depth"] = 0
    return job.model_copy(update={"config": JobConfig.model_validate(config)})


def load_requested_job(args: argparse.Namespace) -> LoadedJob:
    if args.command == "update":
        from unified_web_parser.core.exceptions import ConfigurationError
        from unified_web_parser.pipeline.incremental import load_update_job

        if (
            getattr(args, "auto_schema", None) is True
            or getattr(args, "incremental", None) is False
        ):
            raise ConfigurationError(
                "Update requires the saved schema and incremental collection; "
                "use a new run to regenerate the schema or collect everything"
            )
        return apply_overrides(load_update_job(args.data_dir / "runs" / args.run_id), args)
    if args.command == "resume":
        from unified_web_parser.core.exceptions import ConfigurationError
        from unified_web_parser.pipeline.checkpoints import load_resume_job

        unsafe = (
            "backend",
            "content_profile",
            "assessment_profile",
            "auto_schema",
            "schema_max_fields",
            "model",
            "decision_model",
            "response_mode",
            "reasoning",
            "max_output_tokens",
            "routing",
            "navigation",
            "allow_filters",
            "outputs",
            "incremental",
        )
        if any(getattr(args, name, None) is not None for name in unsafe) or args.full_enrichment:
            raise ConfigurationError(
                "Resume permits only limits, timeouts, retries and --no-ai; "
                "saved schema and provider settings remain fixed"
            )
        return apply_overrides(load_resume_job(args.data_dir / "runs" / args.run_id), args)
    return apply_overrides(
        load_job(
            args.config or "configs/jobs/web.yaml",
            auto_schema=schema_mode(args),
            question=args.query if args.command in {"search", "discover"} else None,
        ),
        args,
    )

"""Execute parsed CLI commands against the shared application components."""

import argparse
import asyncio
import json
import sys

from unified_web_parser.core.config import load_job
from unified_web_parser.interfaces.cli.jobs import load_requested_job


def execute(args: argparse.Namespace) -> int:
    if args.command == "validate":
        job = load_job(args.config)
        print(f"Valid job: {job.config.name}")
        print(f"Schema: {job.schema_path or 'generated at run time'}")
        print(f"Sources: {len(job.config.sources)}")
        return 0
    if args.command in {"run", "search", "discover", "extract", "reprocess", "resume", "update"}:
        from unified_web_parser.pipeline.runner import PipelineRunner

        job = load_requested_job(args)
        runner = PipelineRunner(
            job,
            data_dir=args.data_dir,
            progress=lambda text: print(text, file=sys.stderr),
            model_override=args.model,
            decision_model_override=args.decision_model,
            restore_generated_schema=args.auto_schema is not False,
        )
        report = asyncio.run(
            runner.run(
                replay_dir=getattr(args, "from_run", None),
                resume_dir=args.data_dir / "runs" / args.run_id
                if args.command == "resume"
                else None,
                run_id=args.run_id if args.command == "resume" else None,
            )
        )
        print(report.model_dump_json(indent=2))
        return {"success": 0, "stopped": 0, "cancelled": 130, "partial": 2}.get(report.status, 1)
    if args.command == "serve":
        import uvicorn

        from unified_web_parser.interfaces.api import create_app

        uvicorn.run(create_app(args.data_dir, args.config_dir), host=args.host, port=args.port)
        return 0
    from unified_web_parser.storage import DatasetStore, export_records

    store = DatasetStore(args.data_dir)
    if args.command == "export":
        records = store.list_records(job_name=args.job_name, limit=100000)
        path = export_records(records, args.output, args.format)
        print(f"Exported {len(records)} records to {path}")
    elif args.command == "runs":
        print(json.dumps(store.list_runs(limit=args.limit), ensure_ascii=False, indent=2))
    elif args.command == "records":
        print(
            json.dumps(
                store.list_records(job_name=args.job, limit=args.limit),
                ensure_ascii=False,
                indent=2,
            )
        )
    return 0

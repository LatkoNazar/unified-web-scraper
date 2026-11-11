"""CLI composition root; stable entry point for the console script and Python callers."""

import sys
from collections.abc import Sequence

from dotenv import load_dotenv
from pydantic import ValidationError

from unified_web_parser.core.exceptions import ConfigurationError, PipelineError
from unified_web_parser.interfaces.cli.arguments import build_parser
from unified_web_parser.interfaces.cli.commands import execute
from unified_web_parser.interfaces.cli.jobs import apply_overrides

# Preserve existing callers while the implementation lives in cohesive modules.
_parser = build_parser
_overrides = apply_overrides

__all__ = ["apply_overrides", "build_parser", "main"]


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    load_dotenv(args.env_file, override=False)
    try:
        return execute(args)
    except (
        ConfigurationError,
        PipelineError,
        ValidationError,
        OSError,
        ValueError,
        RuntimeError,
    ) as exc:
        print(
            f"Configuration error: {exc}"
            if isinstance(exc, ConfigurationError)
            else f"Error: {exc}",
            file=sys.stderr,
        )
        return 1
    except KeyboardInterrupt:
        print("Run interrupted; inspect the run summary for saved progress.", file=sys.stderr)
        return 130

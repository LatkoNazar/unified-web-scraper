"""Question-driven, locally compiled extraction schemas."""

from unified_web_parser.schema_generation.generator import (
    PROMPT_VERSION,
    GeneratedSchema,
    compile_schema,
    generate_schema,
)

__all__ = ["PROMPT_VERSION", "GeneratedSchema", "compile_schema", "generate_schema"]

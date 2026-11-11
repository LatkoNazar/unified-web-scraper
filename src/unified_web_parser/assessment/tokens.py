"""Unicode lexical tokenization shared by optional assessment profiles."""

import re
import unicodedata
from contextlib import suppress
from decimal import Decimal, InvalidOperation


def _tokens(text: str) -> set[str]:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    tokens = set()
    for token in re.findall(r"\d+(?:[.,]\d+)?|[^\W\d_]+", normalized):
        if token[0].isdecimal():
            with suppress(InvalidOperation):
                token = format(Decimal(token.replace(",", ".")).normalize(), "f")
        tokens.add(token)
    return tokens

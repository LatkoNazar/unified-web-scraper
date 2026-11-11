"""Opt-in monetary evidence, with currency meanings supplied by the job.

Symbols establish monetary evidence without establishing a currency code. Text
aliases (including ISO codes) are recognized only when configured; structured
priceCurrency values do not need this helper and are preserved as source facts.
"""

import re
import unicodedata
from collections.abc import Mapping
from functools import lru_cache

_AMOUNT = r"(?<![\d.,])(?:\d{1,3}(?:[ \xa0]\d{3})+|\d+)(?:[.,]\d{1,2})?(?![\d.,])"


@lru_cache(maxsize=128)
def _currency_pattern(alias_names: tuple[str, ...]) -> str:
    # Unicode's symbol class covers currencies without a product/country list.
    symbols = _symbols()
    parts = [re.escape(key) for key in sorted(alias_names, key=len, reverse=True) if key]
    parts.append("[" + re.escape(symbols) + "]")
    return "(?:" + "|".join(parts) + ")"


@lru_cache(maxsize=1)
def _symbols() -> str:
    return "".join(chr(code) for code in range(0x110000) if unicodedata.category(chr(code)) == "Sc")


def _patterns(aliases: Mapping[str, str]) -> tuple[re.Pattern[str], re.Pattern[str]]:
    return _compiled_patterns(tuple(sorted(aliases)))


@lru_cache(maxsize=128)
def _compiled_patterns(alias_names: tuple[str, ...]) -> tuple[re.Pattern[str], re.Pattern[str]]:
    currency = _currency_pattern(alias_names)
    price = re.compile(
        rf"(?P<prefix>{currency})\s*(?P<before>{_AMOUNT})|"
        rf"(?P<after>{_AMOUNT})\s*(?P<suffix>{currency})(?!\w)",
        re.IGNORECASE,
    )
    interval = re.compile(
        rf"(?:{currency}\s*{_AMOUNT}\s*(?:[-–—]|to|до)\s*(?:{currency}\s*)?{_AMOUNT}"
        rf"|{_AMOUNT}\s*(?:{currency}\s*)?(?:[-–—]|to|до)\s*{_AMOUNT}\s*{currency})",
        re.IGNORECASE,
    )
    return price, interval


def has_price_range(text: str, *, currency_aliases: Mapping[str, str] | None = None) -> bool:
    """Whether a monetary span states a range rather than one observed amount."""
    return _patterns(currency_aliases or {})[1].search(text) is not None


def has_money_evidence(text: str, *, currency_aliases: Mapping[str, str] | None = None) -> bool:
    return _patterns(currency_aliases or {})[0].search(text) is not None


def read_price(
    text: str, *, currency_aliases: Mapping[str, str] | None = None
) -> tuple[str, str | None] | None:
    """Read one amount without converting it or guessing an ambiguous currency."""
    aliases = currency_aliases or {}
    price, interval = _patterns(aliases)
    if interval.search(text):
        return None
    match = price.search(text)
    if match is None:
        return None
    symbol = match.group("prefix") or match.group("suffix")
    code = next(
        (value for key, value in aliases.items() if key.casefold() == symbol.casefold()), None
    )
    amount = match.group("before") or match.group("after")
    return amount, code

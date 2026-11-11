"""Explicit device-profile identity rules, separate from generic extraction."""

import re
import unicodedata
from collections.abc import Mapping
from typing import Any

# These words identify what is sold only in its primary title/category. They
# never inspect descriptions, where a genuine device may list included extras.
_SUBJECTS = {
    "accessory": re.compile(
        r"\b(?:чох\w*|чех\w*|накладк\w*|сумк\w*|килим\w*|коврик\w*|"
        r"mouse\s*pad\w*|desk\s*mat\w*|sleeve\w*|case\w*|cover\w*|"
        r"кабел\w*|cable\w*|charger\w*|зарядн\w*|адаптер\w*|adapter\w*|"
        r"keyboard\w*|клавіатур\w*|клавиатур\w*|миша|мышь|mouse|"
        r"плівк\w*|пленк\w*|screen\s+protector\w*)\b",
        re.I,
    ),
    "parts": re.compile(r"\b(?:запчаст\w*|розбір\w*|разбор\w*|parts?|spares?)\b", re.I),
    "service": re.compile(
        r"\b(?:ремонт(?:у|ом|ів|ы|а|е)?|repairs?|repairing|сервіс\w*|сервис\w*|заміна|замена)\b",
        re.I,
    ),
}
_PARTS_SALE = re.compile(
    r"\b(?:for\s+(?:parts|spares)|на\s+(?:запчаст\w*|детал\w*|розбір\w*|разбор\w*)|"
    r"розбірка|разборка|disassembly)\b",
    re.I,
)
_NOT_SALE = re.compile(r"\b(?:без|не|no|not|without|after|після|после)\s+(?:\w+\s+)?$", re.I)
_COMPATIBILITY = re.compile(r"\b(?:для|for|під|совместим\w*|сумісн\w*|compatible)\b", re.I)
_BUNDLE = re.compile(r"\+|\b(?:with|includes?|included|bundled|разом|комплект\w*)\b", re.I)
_INCLUDED = re.compile(
    r"\+|\b(?:with|includes?|included|bundled|разом|подарунок|подарок|комплект\w*)\b|"
    r"\b(?:з|із|с)\s+(?:чох\w*|чех\w*|клав\w*|миш\w*|мыш\w*)",
    re.I,
)
_CONTAINER = re.compile(
    r"\b(?:чох\w*|чех\w*|накладк\w*|сумк\w*|килим\w*|коврик\w*|sleeve\w*|case\w*|cover\w*)\b", re.I
)
_MODEL = re.compile(r"(?<!\w)([^\W\d_]{1,16})-?(\d{1,4})([^\W\d_]*)(?!\w)", re.I)
_NON_DEVICE = {"accessory", "accessories", "part", "parts", "service", "services"}


def _text(value: Any) -> str:
    return unicodedata.normalize("NFKC", value).casefold() if isinstance(value, str) else ""


def subject_kind(title: str) -> str | None:
    """Compatibility targets and bundled extras do not become the sale subject."""
    text = _text(title)
    if any(not _NOT_SALE.search(text[: match.start()]) for match in _PARTS_SALE.finditer(text)):
        return "parts"
    compatibility = _COMPATIBILITY.search(text)
    head = text[: compatibility.start()] if compatibility else text
    bundle = _BUNDLE.search(head)
    if bundle:
        head = head[: bundle.start()]
    for kind, pattern in _SUBJECTS.items():
        found = pattern.search(head)
        if not found or _NOT_SALE.search(head[: found.start()]):
            continue
        if kind == "parts" and _INCLUDED.search(text):
            continue
        if kind == "accessory" and found.start() > 0 and _INCLUDED.search(text):
            continue
        # A leading noun or a subject before 'for' is strong evidence. A later
        # keyboard/layout mention in a device title remains ambiguous.
        if compatibility or len(re.findall(r"\w+", head[: found.start()])) <= 2:
            return kind
    return None


def model_groups(text: str) -> dict[str, set[str]]:
    groups: dict[str, set[str]] = {}
    normalized = re.sub(r"(?<=\d)(?=[^\W\d_]\d)", "/", _text(text))
    # Marketplace titles sometimes mix Cyrillic lookalikes into an alphanumeric
    # model. Normalize only model symbols, never arbitrary user field text.
    symbols = str.maketrans("мхасртк", "mxacptk")
    for prefix, number, suffix in _MODEL.findall(normalized):
        prefix = prefix.translate(symbols)
        groups.setdefault(prefix, set()).add(number + suffix.translate(symbols))
    return groups


def models_overlap(requested: set[str], observed: set[str]) -> bool:
    """A base generation includes suffixed variants; omitted variants are unknown."""
    for wanted in requested:
        wanted_number, wanted_suffix = re.fullmatch(r"(\d+)(.*)", wanted).groups()
        for actual in observed:
            actual_number, actual_suffix = re.fullmatch(r"(\d+)(.*)", actual).groups()
            if wanted_number == actual_number and (
                not wanted_suffix or not actual_suffix or wanted_suffix == actual_suffix
            ):
                return True
    return False


def device_identity(
    data: Mapping[str, Any], search: str | None
) -> tuple[str, str, tuple[str, ...]]:
    """Reject only explicit incompatible subjects/models; keep missing facts for review."""
    title = next(
        (data[key] for key in ("title", "name", "headline") if isinstance(data.get(key), str)), ""
    )
    requested_kind = subject_kind(search or "")
    title_kind = subject_kind(title)
    listed = _text(data.get("listing_type"))
    listed = {"accessories": "accessory", "part": "parts", "services": "service"}.get(
        listed, listed
    )
    if requested_kind is not None:
        # A generic reusable device profile must not reject a request explicitly
        # asking for an accessory/repair. It still does not prove its relevance.
        return "review", "query_requests_non_device", (search or "",)
    if title_kind is not None:
        return "rejected", "non_device_subject", (title, title_kind)
    if listed in _NON_DEVICE:
        if _INCLUDED.search(title):
            return "review", "conflicting_device_identity", (title, f"listing_type={listed}")
        return "rejected", "non_device_listing_type", (f"listing_type={listed}", title)
    # Only identity fields can establish a model. A compatibility paragraph may
    # mention the requested generation while the sold device is another model.
    observed = " ".join(
        str(data.get(key) or "") for key in ("title", "name", "headline", "model", "chip", "series")
    )
    requested = model_groups(search or "")
    actual = model_groups(observed)
    for family, wanted in requested.items():
        if family in actual and not models_overlap(wanted, actual[family]):
            return (
                "rejected",
                "explicit_model_mismatch",
                (
                    f"requested={family}:{','.join(sorted(wanted))}",
                    f"observed={family}:{','.join(sorted(actual[family]))}",
                    title,
                ),
            )
    return "review", "identity_not_fully_confirmed", (title,) if title else ()

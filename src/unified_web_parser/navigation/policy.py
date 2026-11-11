"""Public-search and optional-filter policy applied to observed HTML controls."""

import re
from urllib.parse import unquote

from bs4 import BeautifulSoup, Tag

from unified_web_parser.navigation.html import (
    control_identity,
    is_visible,
    observed_label,
    selector_for,
    ui_words,
)

SEARCH = re.compile(r"search|\bfind\b|шук|знай|поиск|искать|buscar|suche|recherch|chercher", re.I)
NEXT = re.compile(
    r"^(?:next(?:\s+page)?|далі|наступна(?:\s+сторінка)?|наступний|"
    r"далее|вперед|вперёд|forward|следующая(?:\s+страница)?|›|»|→)$",
    re.I,
)
MORE = re.compile(
    r"load\s*more|show\s*more|показати\s*(?:ще|більше)|завантажити\s*ще|показать\s*ещ[её]", re.I
)
UNSAFE = re.compile(
    r"checkout|add\s*to\s*(?:cart|basket)|\bbuy\b|\bpurchase\b|\bdelete\b|"
    r"sign\s*(?:in|up)|log\s*in|\bpay\b|купити|придбати|в\s*кошик|"
    r"в\s*корзину|купить|заказать|оплатить|замовити|сплатити|видалити|увійти|зареєструватись",
    re.I,
)
QUERY_NAMES = {"q", "query", "search", "s", "search_term", "text", "keyword", "keywords"}
SENSITIVE = re.compile(r"password|passwd|csrf|token|secret|authorization|session|api[_-]?key", re.I)
FILTER = re.compile(
    r"(?:^|[^a-z])(?:filters?|sorting|sort|price|budget|location|city|region|distance|"
    r"condition|brand|attributes?|minprice|maxprice)(?:[^a-z]|$)|"
    r"фільтр|фильтр|сорту|сортир|цін|цен|локац|міст|город|регіон|регион",
    re.I,
)
CONSENT = re.compile(
    r"consent|agree|terms|privacy|policy|cookie|captcha|security|згод|умов|політик", re.I
)
PAGE = re.compile(r"^(?:page|pageno|page_number|pageindex)$|pagination|go to page", re.I)
FILTER_CONTEXT = re.compile(
    r"(?:^|[^a-z])(?:filters?|sorting|sort|facets?)(?:[^a-z]|$)|фільтр|фильтр|сорту|сортир", re.I
)
FILTER_COMMAND = re.compile(r"\b(?:filters?|sorting|sort)\b|фільтр|фильтр|сорту|сортир", re.I)


def is_pagination_control(tag: Tag) -> bool:
    return str(tag.get("name", "")).lower() in {
        "page",
        "p",
        "pageno",
        "page_number",
        "pageindex",
    } or bool(PAGE.search(observed_label(tag)))


def is_operational_control(tag: Tag) -> bool:
    return str(tag.get("name", "")).lower() in {
        "locale",
        "language",
        "lang",
        "ui_language",
        "interface_language",
        "charset",
        "encoding",
        "method",
        "mode",
        "route",
        "routing",
    } or bool(re.search(r"language|locale|interface|мова|язык", control_identity(tag), re.I))


def has_filter_context(tag: Tag) -> bool:
    for ancestor in [tag, *list(tag.parents)[:3]]:
        if isinstance(ancestor, Tag) and (
            FILTER_CONTEXT.search(
                ui_words(
                    " ".join(
                        str(ancestor.get(key, "")) for key in ("class", "id", "aria-label", "role")
                    )
                )
            )
            or ancestor.has_attr("data-filter")
            or ancestor.has_attr("data-sort")
        ):
            return True
    return False


def is_filter_control(tag: Tag) -> bool:
    identity = control_identity(tag)
    if (
        SENSITIVE.search(identity)
        or CONSENT.search(identity + " " + observed_label(tag))
        or is_operational_control(tag)
    ):
        return False
    if str(tag.get("name", "")).lower() in QUERY_NAMES or is_pagination_control(tag):
        return False
    semantic_filter = bool(FILTER.search(identity) or has_filter_context(tag))
    if tag.name == "select":
        return semantic_filter
    if tag.name != "input" or tag.get("type", "text") in {"submit", "button", "password"}:
        return False
    if tag.get("type", "text") in {"number", "range", "checkbox", "radio"}:
        return semantic_filter
    if tag.get("type") == "hidden":
        return bool(FILTER.search(identity))
    return semantic_filter


def is_unsafe_control(tag: Tag) -> bool:
    """Exclude explicit mutation/auth intent in observed labels and bindings."""
    values = [observed_label(tag)]
    for attribute in ("href", "action", "formaction", "id", "name", "data-action", "onclick"):
        value = str(tag.get(attribute, ""))
        values.append(re.sub(r"([a-z])([A-Z])", r"\1 \2", unquote(value)).replace("_", " "))
    context = " ".join(values).replace("-", " ")
    form = tag.find_parent("form")
    return bool(UNSAFE.search(context)) or bool(
        form is not None and (form.find("input", type="password") or is_unsafe_control(form))
    )


def is_search_input(tag: Tag) -> bool:
    identity = " ".join(str(tag.get(key, "")) for key in ("id", "name"))
    if SENSITIVE.search(identity):
        return False
    associated_labels = []
    if tag.get("id"):
        root = tag
        while isinstance(root.parent, Tag):
            root = root.parent
        associated_labels = root.find_all("label", attrs={"for": tag["id"]})
    wrapping_label = tag.find_parent("label")
    cues = " ".join(
        [identity, observed_label(tag), *(observed_label(label) for label in associated_labels)]
        + ([observed_label(wrapping_label)] if wrapping_label else [])
    )
    return (
        tag.name == "input"
        and tag.get("type", "text").lower()
        not in {
            "password",
            "hidden",
            "email",
            "checkbox",
            "radio",
            "file",
        }
        and (
            tag.get("type") == "search"
            or tag.get("role") == "searchbox"
            or str(tag.get("name", "")).lower() in QUERY_NAMES
            or bool(SEARCH.search(cues))
        )
    )


def collect_search_filters(
    soup: BeautifulSoup, *, allow_filters: bool
) -> tuple[set[int], tuple[str, ...], dict]:
    filters = [tag for tag in soup.select("input, select") if is_filter_control(tag)]
    filter_ids = {id(tag) for tag in filters}
    # Other visible fields in the same keyword-search form are optional constraints.
    # Hidden routing/security controls remain untouched unless explicitly identified.
    for form in soup.find_all("form"):
        query_fields = [
            tag
            for tag in form.find_all("input")
            if is_visible(tag) and is_search_input(tag) and not is_filter_control(tag)
        ]
        query_fields.sort(key=lambda tag: str(tag.get("name", "")).lower() not in QUERY_NAMES)
        if not query_fields:
            continue
        for tag in form.select("input, select"):
            identity = control_identity(tag) + " " + observed_label(tag)
            if (
                tag is query_fields[0]
                or id(tag) in filter_ids
                or not is_visible(tag)
                or tag.get("type") in {"hidden", "submit", "button", "password"}
                or SENSITIVE.search(identity)
                or CONSENT.search(identity)
                or is_operational_control(tag)
                or is_pagination_control(tag)
            ):
                continue
            filters.append(tag)
            filter_ids.add(id(tag))
    filter_names = tuple(dict.fromkeys(str(tag["name"]) for tag in filters if tag.get("name")))
    filter_bindings = (
        {
            "filter_selectors": tuple(selector_for(tag) for tag in filters),
            "filter_query_names": filter_names,
        }
        if not allow_filters
        else {"uses_filters": bool(filters)}
    )
    return filter_ids, filter_names, filter_bindings

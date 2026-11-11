"""Resolve the user's query only through source-observed URL and form bindings."""

from urllib.parse import quote, urlencode, urljoin, urlsplit, urlunsplit

from unified_web_parser.navigation.models import NavigationAction
from unified_web_parser.sources.navigation import without_query_keys

_ASCII_WHITESPACE = " \t\n\r\f\v"


def observed_url(base: str, value: str) -> str | None:
    try:
        base = base.strip(_ASCII_WHITESPACE)
        value = value.strip(_ASCII_WHITESPACE)
        result = urlsplit(urljoin(base, value))
        origin = urlsplit(base)
        if (
            result.scheme not in {"http", "https"}
            or not result.hostname
            or result.hostname != origin.hostname
            or result.port != origin.port
            or result.username
            or result.password
        ):
            return None
    except ValueError:
        return None
    return result.geturl()


def resolve_action_url(action: NavigationAction, query: str | None = None) -> str | None:
    """Resolve only observed URLs; otherwise a browser must execute the bound control."""
    if action.kind == "open":
        return action.url.strip(_ASCII_WHITESPACE) if action.url is not None else None
    if action.kind != "search" or not query:
        return None
    if action.url_template:
        token = action.query_name
        if not token or "{" + token + "}" not in action.url_template:
            return None
        return without_query_keys(
            action.url_template.replace("{" + token + "}", quote(query, safe="")),
            action.filter_query_names,
        )
    if action.form_method == "get" and action.url and action.query_name:
        parts = urlsplit(
            without_query_keys(
                action.url,
                (
                    *action.filter_query_names,
                    action.query_name,
                    *(name for name, _ in action.form_values),
                ),
            )
        )
        values = [
            (key, value)
            for key, value in action.form_values
            if key != action.query_name and key not in action.filter_query_names
        ]
        values.append((action.query_name, query))
        return urlunsplit(
            (
                parts.scheme,
                parts.netloc,
                parts.path,
                "&".join(part for part in (parts.query, urlencode(values)) if part),
                parts.fragment,
            )
        )
    return None

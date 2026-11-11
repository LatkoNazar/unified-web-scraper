"""Build observed form, input, button and select capabilities in source order."""

from collections.abc import Callable

from bs4 import BeautifulSoup, Tag

from unified_web_parser.navigation.html import is_visible, observed_label, selector_for
from unified_web_parser.navigation.policy import (
    FILTER_COMMAND,
    MORE,
    NEXT,
    QUERY_NAMES,
    SEARCH,
    SENSITIVE,
    has_filter_context,
    is_filter_control,
    is_pagination_control,
    is_search_input,
    is_unsafe_control,
)
from unified_web_parser.navigation.urls import observed_url


def observe_controls(
    soup: BeautifulSoup,
    origin: str,
    add: Callable[..., None],
    *,
    allow_filters: bool,
    filter_ids: set[int],
    filter_names: tuple[str, ...],
    filter_bindings: dict,
) -> None:
    bound_inputs: set[str] = set()
    bound_buttons: set[str] = set()
    for form in soup.find_all("form"):
        if not is_visible(form) or form.find("input", type="password") or is_unsafe_control(form):
            continue
        inputs = [tag for tag in form.find_all("input") if is_visible(tag) and is_search_input(tag)]
        inputs = [tag for tag in inputs if id(tag) not in filter_ids]
        if not inputs:
            continue
        inputs.sort(key=lambda tag: str(tag.get("name", "")).lower() not in QUERY_NAMES)
        field = inputs[0]
        input_selector = selector_for(field)
        bound_inputs.add(input_selector)
        button = form.find("button", type="submit") or form.find("button")
        submit_selector = selector_for(button) if button and is_visible(button) else None
        if submit_selector:
            bound_buttons.add(submit_selector)
        method = str(form.get("method", "get")).lower()
        action = str(form.get("action", ""))
        action_url = observed_url(origin, action) if action else None
        if action and action_url is None:
            continue
        values = []
        sensitive_hidden = False
        for tag in form.select("input, select"):
            name = tag.get("name")
            if not name or tag is field or tag.has_attr("disabled"):
                continue
            if SENSITIVE.search(str(name)):
                sensitive_hidden |= tag.get("type") == "hidden"
                continue
            if not allow_filters and (id(tag) in filter_ids or str(name) in filter_names):
                continue
            if tag.name == "select":
                selected = tag.find_all("option", selected=True)
                if not selected and not tag.has_attr("multiple"):
                    selected = tag.find_all("option")[:1]
                values.extend(
                    (str(name), str(option.get("value", option.text)))
                    for option in selected
                    if not option.has_attr("disabled")
                )
            elif tag.get("type", "text") not in {
                "submit",
                "button",
                "checkbox",
                "radio",
            } or tag.has_attr("checked"):
                default = "on" if tag.get("type") in {"checkbox", "radio"} else ""
                values.append((str(name), str(tag.get("value", default))))
        add(
            "search",
            observed_label(field) or "Search form",
            url=action_url if method == "get" and not sensitive_hidden else None,
            selector=selector_for(form),
            input_selector=input_selector,
            submit_selector=submit_selector,
            form_method=method,
            query_name=str(field.get("name", "")) or None,
            form_values=tuple(values),
            hint="search",
            **filter_bindings,
        )
    for field in soup.find_all("input"):
        if field.find_parent("form") is not None:
            continue
        selector = selector_for(field)
        if (
            is_visible(field)
            and is_search_input(field)
            and not is_filter_control(field)
            and selector not in bound_inputs
        ):
            submit = None
            for ancestor in list(field.parents)[:3]:
                if not isinstance(ancestor, Tag):
                    continue
                submit = next(
                    (
                        button
                        for button in ancestor.find_all("button")
                        if is_visible(button)
                        and SEARCH.search(observed_label(button))
                        and not is_unsafe_control(button)
                    ),
                    None,
                )
                if submit:
                    break
            submit_selector = selector_for(submit) if submit else None
            if submit_selector:
                bound_buttons.add(submit_selector)
            add(
                "search",
                observed_label(field) or "Search input",
                input_selector=selector,
                submit_selector=submit_selector,
                hint="search",
                **filter_bindings,
            )

    buttons = []
    for tag in soup.select('button, [role="button"]'):
        label = observed_label(tag)
        selector = selector_for(tag)
        if not is_visible(tag) or selector in bound_buttons or not label or is_unsafe_control(tag):
            continue
        filter_button = bool(
            FILTER_COMMAND.search(label)
            or (
                has_filter_context(tag)
                and not (SEARCH.search(label) or MORE.search(label) or NEXT.search(label))
            )
        )
        if not allow_filters and filter_button:
            continue
        hint = (
            "filter"
            if filter_button
            else "search"
            if SEARCH.search(label)
            else "load_more"
            if MORE.search(label)
            else "pagination"
            if NEXT.search(label)
            else "expand"
        )
        buttons.append((hint == "expand", tag, label, selector, hint))
    for _, _, label, selector, hint in sorted(buttons, key=lambda value: value[0])[:16]:
        add("click", label, selector=selector, hint=hint)

    for field in soup.find_all("select"):
        pagination = is_pagination_control(field)
        if (
            not is_visible(field)
            or is_unsafe_control(field)
            or (not allow_filters and not pagination)
        ):
            continue
        for option in field.find_all("option")[:8]:
            if option.has_attr("disabled") or option.has_attr("selected"):
                continue
            add(
                "select",
                (observed_label(field)[:100] + ": " + observed_label(option))[:300],
                selector=selector_for(field),
                option_value=str(option.get("value", option.text)),
                hint="pagination" if pagination else "filter",
            )

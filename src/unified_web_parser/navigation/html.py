"""Read labels, visibility and exact element bindings from an HTML snapshot."""

import re

from bs4 import Tag


def ui_words(value: str) -> str:
    return re.sub(r"([a-z])([A-Z])", r"\1 \2", value)


def observed_label(tag: Tag) -> str:
    pieces = [
        tag.get("aria-label"),
        tag.get("title"),
        tag.get("placeholder"),
        tag.get_text(" ", strip=True),
    ]
    return " ".join(str(value) for value in pieces if value).strip()[:300]


def is_visible(tag: Tag) -> bool:
    for current in [tag, *tag.parents]:
        if not isinstance(current, Tag):
            continue
        style = str(current.get("style", "")).replace(" ", "").lower()
        if (
            current.has_attr("hidden")
            or current.has_attr("disabled")
            or current.get("aria-hidden") == "true"
            or "display:none" in style
            or "visibility:hidden" in style
        ):
            return False
    return True


def selector_for(tag: Tag) -> str:
    parts = []
    for node in [tag, *tag.parents]:
        if not isinstance(node, Tag) or node.name == "[document]":
            continue
        position = 1 + sum(
            1
            for sibling in node.previous_siblings
            if isinstance(sibling, Tag) and sibling.name == node.name
        )
        parts.append(f"{node.name}:nth-of-type({position})")
    return " > ".join(reversed(parts))


def control_identity(tag: Tag) -> str:
    root = tag
    while isinstance(root.parent, Tag):
        root = root.parent
    labels = root.find_all("label", attrs={"for": tag["id"]}) if tag.get("id") else []
    wrapping = tag.find_parent("label")
    return ui_words(
        " ".join(
            [
                *(str(tag.get(key, "")) for key in ("name", "id", "aria-label", "title")),
                *(observed_label(label) for label in labels),
                observed_label(wrapping) if wrapping else "",
            ]
        )
    )

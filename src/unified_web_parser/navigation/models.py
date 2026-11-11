"""Observed navigation capabilities; models may select IDs but cannot author bindings."""

from typing import Literal

from pydantic import model_validator

from unified_web_parser.core.exceptions import RoutingError
from unified_web_parser.core.models import Identifier, Message


class NavigationAction(Message):
    action_id: Identifier
    page_id: Identifier
    kind: Literal["open", "search", "click", "select", "scroll"]
    label: str = ""
    url: str | None = None
    selector: str | None = None
    input_selector: str | None = None
    submit_selector: str | None = None
    form_method: str | None = None
    query_name: str | None = None
    form_values: tuple[tuple[str, str], ...] = ()
    filter_selectors: tuple[str, ...] = ()
    filter_query_names: tuple[str, ...] = ()
    uses_filters: bool = False
    url_template: str | None = None
    option_value: str | None = None
    hint: str = "unknown"
    group: str | None = None


class NavigationInventory(Message):
    page_id: Identifier
    url: str
    title: str = ""
    summary: str = ""
    actions: tuple[NavigationAction, ...] = ()
    has_entity: bool = False
    has_results: bool = False
    observed_action_count: int = 0
    omitted_action_count: int = 0
    skipped_item_count: int = 0

    @model_validator(mode="after")
    def bound_actions(self) -> "NavigationInventory":
        ids = [action.action_id for action in self.actions]
        if len(set(ids)) != len(ids):
            raise ValueError("Navigation action IDs must be unique")
        if any(action.page_id != self.page_id for action in self.actions):
            raise ValueError("Navigation actions must belong to this page")
        return self

    def by_id(self) -> dict[str, NavigationAction]:
        return {action.action_id: action for action in self.actions}


class NavigationPlan(Message):
    page_id: Identifier
    action_ids: tuple[str, ...] = ()
    extract: bool = False
    reason: str = ""
    terminal: bool = False
    stop_reason: str | None = None

    def validate_against(
        self, inventory: NavigationInventory, *, allow_filters: bool = False
    ) -> "NavigationPlan":
        if self.page_id != inventory.page_id:
            raise RoutingError("Navigation plan belongs to another page state")
        known = inventory.by_id()
        if len(set(self.action_ids)) != len(self.action_ids):
            raise RoutingError("Navigation plan repeats an action")
        if any(action_id not in known for action_id in self.action_ids):
            raise RoutingError("Navigation plan selects an unknown action")
        if not allow_filters and any(is_filter_action(known[key]) for key in self.action_ids):
            raise RoutingError("Navigation filters are disabled")
        if self.terminal and self.action_ids:
            raise RoutingError("Terminal navigation plans cannot execute more actions")
        return self


def is_filter_action(action: NavigationAction) -> bool:
    return (
        action.uses_filters
        or action.hint in {"filter", "sort", "price", "location", "attribute"}
        or (action.kind == "select" and action.hint != "pagination")
    )

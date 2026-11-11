"""Optional, isolated observers for actual pipeline transitions and validated records."""

import logging
from collections.abc import Callable
from copy import deepcopy
from typing import Any

EventCallback = Callable[[str, dict[str, Any]], None]


def emit_event(callback: EventCallback | None, kind: str, data: dict[str, Any]) -> None:
    if callback is not None:
        try:
            callback(kind, deepcopy(data))
        except Exception as exc:
            logging.getLogger(__name__).warning("Pipeline observer failed (%s)", type(exc).__name__)

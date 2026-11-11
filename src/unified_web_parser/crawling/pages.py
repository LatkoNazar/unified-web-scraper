"""Stable page identity shared by HTTP and rendered fetchers."""

import hashlib
from datetime import UTC, datetime

from unified_web_parser.core.models import Page


def make_page(url: str, html: str, status_code: int) -> Page:
    digest = hashlib.sha256((url + "\0" + html).encode("utf-8")).hexdigest()
    return Page(
        page_id=digest,
        url=url,
        html=html,
        status_code=status_code,
        fetched_at=datetime.now(UTC),
    )

"""Serialize start times per host while allowing independent hosts to proceed."""

import asyncio
import time


class HostRateLimiter:
    def __init__(self, delay: float):
        self.delay = delay
        self._locks: dict[str, asyncio.Lock] = {}
        self._last_request: dict[str, float] = {}

    async def wait(self, host: str) -> None:
        async with self._locks.setdefault(host, asyncio.Lock()):
            wait = self.delay - (time.monotonic() - self._last_request.get(host, 0))
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request[host] = time.monotonic()

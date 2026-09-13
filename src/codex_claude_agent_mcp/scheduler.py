"""Per-process bounded concurrency for Claude jobs.

A single :class:`asyncio.Semaphore` limits how many Claude sessions one MCP
server process runs at once (spec §13.1). Execution and review share the same
pool, because both consume Claude API capacity.

This is **per-process**, not global. If Codex spawns several MCP server
subprocesses (one per thread), each has its own semaphore and the machine-level
total may exceed ``CLAUDE_MAX_CONCURRENCY`` (spec §13.2). v1 documents this
explicitly rather than pretending an asyncio primitive is cross-process.
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, TypeVar

from .logging import get_logger

log = get_logger("scheduler")

T = TypeVar("T")


class Scheduler:
    def __init__(self, max_concurrency: int) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be >= 1")
        self.max_concurrency = max_concurrency
        self._sem = asyncio.Semaphore(max_concurrency)
        self._inflight = 0

    @property
    def inflight(self) -> int:
        return self._inflight

    async def run(self, coro_fn: Callable[[], Awaitable[T]], *, label: str | None = None) -> T:
        """Acquire a slot, run ``coro_fn()``, release. Excess callers queue."""
        async with self._sem:
            self._inflight += 1
            try:
                log.debug("schedule start label=%s inflight=%d", label, self._inflight)
                return await coro_fn()
            finally:
                self._inflight -= 1
                log.debug("schedule done label=%s inflight=%d", label, self._inflight)

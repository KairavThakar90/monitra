"""Run slow, blocking background work without starving the request threads.

A synchronous function handed to ``BackgroundTasks`` runs on the same 40-thread
pool every synchronous route uses. A WFPM delivery (up to 10 s) or an SMTP send
(up to 20 s) per timer start therefore occupies a request thread for its whole
wait: during a morning burst enough of them queue that a *request* waits seconds
for a free thread -- while holding the connection its authentication query
already checked out. Measured on the local rig: one connection held 6.1 s,
exactly the delay of the slow WFPM stand-in, with nothing wrong in the database.

``run_blocking`` runs such work under its own, small limiter instead. At most
``BACKGROUND_DELIVERY_CONCURRENCY`` of them occupy threads at once; the rest wait
cheaply on the event loop, and the request pool keeps its capacity. The work is
the same function, run the same way -- only who may run it, and how many at a
time, changes.
"""
from __future__ import annotations

from typing import Any, Callable, Optional, TypeVar

from anyio import CapacityLimiter
from anyio.to_thread import run_sync

from app.core.config import settings

T = TypeVar("T")

_limiter: Optional[CapacityLimiter] = None


def _delivery_limiter() -> CapacityLimiter:
    # Created on first use, inside the running event loop: anyio limiters belong to
    # an event loop and cannot be built at import time.
    global _limiter
    if _limiter is None:
        _limiter = CapacityLimiter(max(1, int(settings.BACKGROUND_DELIVERY_CONCURRENCY)))
    return _limiter


async def run_blocking(function: Callable[..., T], *args: Any) -> T:
    """Run ``function(*args)`` on a thread, at most N at a time in this process."""
    return await run_sync(function, *args, limiter=_delivery_limiter())

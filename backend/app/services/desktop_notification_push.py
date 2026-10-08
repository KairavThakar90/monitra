"""Real-time delivery of desktop notification changes -- the "push" half.

What it is
----------
A desktop used to learn about a change by asking, every half minute. This keeps a
small event stream open per signed-in desktop instead
(``GET /desktop-notifications/stream``) and tells it the moment the schedule's
``version`` changes -- an administrator saving a notification, switching a
reminder, or pushing a message. The desktop then fetches the schedule it always
fetched. The stream carries a *signal*, never the schedule: the schedule route
stays the single source of truth, and a desktop that cannot hold a stream open
(an older backend, a proxy that buffers, a flaky network) simply keeps asking
every half minute as before.

Why a stream and not a long wait
--------------------------------
The desktop must stop within a few seconds when it is closed. A request that is
held for twenty seconds with nothing arriving cannot be abandoned from outside,
so the stream says something every ``HEARTBEAT_SECONDS`` -- a comment line the
client ignores -- and the client checks whether it has been asked to stop each
time. It is plain HTTP with ``X-Accel-Buffering: no``: no WebSocket upgrade, no
proxy configuration, nothing new for the deployment.

How a change reaches a stream
-----------------------------
Production runs more than one worker process, so a change made in one is
invisible to a stream held by another unless they look at the same place. One
``VersionWatcher`` per process reads the stored ``version`` -- one cheap
single-row query, on its own short-lived session, never one held across a wait
-- every ``WATCH_INTERVAL_SECONDS`` while any stream is open, and wakes them all
when it moves. The worker that *made* the change wakes its watcher at once, so a
desktop on that worker hears in milliseconds; a desktop on the other hears
within ``WATCH_INTERVAL_SECONDS``. With no stream open there is no watcher task
and no query.

A stream holds no database connection while it waits: the request's session
(which authenticated it) is released when the route returns, before the first
byte is sent (``Depends(get_db, scope="function")``). See
docs/DB_CONNECTION_LIFECYCLE.md.

Ending a stream
---------------
Every stream ends by itself after ``MAX_STREAM_SECONDS`` and the desktop
reconnects. That bound matters on deployment: restarting the service waits for
open responses to finish, and a stream that never ended would hold a restart up.
So that a restart is not held up even for that long, a stream also ends within a
heartbeat of the process being told to shut down (``begin_shutdown``, called from
a wrapper around the SIGTERM/SIGINT handler -- see ``install_shutdown_hook``).
"""
import asyncio
import json
import logging
import signal
import time
from typing import AsyncIterator, Callable, Optional

from starlette.concurrency import run_in_threadpool

logger = logging.getLogger("uvicorn.error")

#: How often a stream says something, so the desktop can notice it has been
#: asked to stop (and a proxy sees the connection is alive).
HEARTBEAT_SECONDS = 2.0
#: The longest a stream stays open. The desktop reconnects.
MAX_STREAM_SECONDS = 25.0
#: How often a watcher looks at the stored version while any stream is open --
#: the delay for a change made by *another* worker process.
WATCH_INTERVAL_SECONDS = 2.0

_shutting_down = False
_hook_installed = False


def begin_shutdown() -> None:
    """Called when the process is told to stop: every open stream ends within a
    heartbeat instead of holding the restart up."""
    global _shutting_down
    _shutting_down = True


def is_shutting_down() -> bool:
    return _shutting_down


def install_shutdown_hook() -> None:
    """Chain ``begin_shutdown`` in front of the server's own stop handlers.

    uvicorn installs its handlers before it runs the application, and it cannot
    tell a response to stop. This wraps whatever is installed for SIGTERM, SIGINT
    and (on Windows) SIGBREAK with one that sets the flag first and then calls
    the original, which still does everything it did. Best effort: it needs the
    main thread, and where it cannot be installed a stream simply lasts at most
    ``MAX_STREAM_SECONDS`` longer. Safe to call repeatedly.
    """
    global _hook_installed
    if _hook_installed:
        return
    _hook_installed = True  # one attempt per process, whatever happens
    for name in ("SIGTERM", "SIGINT", "SIGBREAK"):
        number = getattr(signal, name, None)
        if number is None:
            continue
        try:
            previous = signal.getsignal(number)
            if not callable(previous):
                continue  # SIG_DFL / SIG_IGN: not ours to wrap

            def wrapper(signum, frame, _previous=previous):
                begin_shutdown()
                _previous(signum, frame)

            signal.signal(number, wrapper)
        except (ValueError, OSError):
            # Not the main thread (a test client, a worker thread): fine.
            continue


def sse_event(version: int) -> str:
    return f"event: schedule\ndata: {json.dumps({'version': version})}\n\n"


#: A comment line: ignored by every event-stream reader, and enough for the
#: desktop to check whether it should stop.
SSE_PING = ": ping\n\n"
SSE_OPEN = ": connected\n\n"


def _read_version_from_database() -> int:
    """The stored version, on a session of its own that is closed before this
    returns (a background task never borrows a request's)."""
    from app.core.database import get_session_local
    from app.services.desktop_notifications import DesktopNotificationService

    db = get_session_local()()
    try:
        return DesktopNotificationService.current_version(db)
    finally:
        db.close()


class Subscription:
    """One open stream's view of the watcher."""

    def __init__(self, watcher: "VersionWatcher", version: int) -> None:
        self._watcher = watcher
        #: The version this stream last told its client about (or was opened at).
        self.seen = version

    @property
    def version(self) -> int:
        return self._watcher.version

    async def wait(self, timeout: float) -> bool:
        """Wait up to ``timeout`` seconds for the version to move; True if it has."""
        watcher = self._watcher
        changed = watcher.changed_event()  # taken before the comparison: no lost wake-up
        if watcher.version != self.seen:
            return True
        try:
            await asyncio.wait_for(changed.wait(), timeout)
        except asyncio.TimeoutError:
            pass
        return watcher.version != self.seen


class VersionWatcher:
    """Reads the stored version for every stream open in this process."""

    def __init__(self, read_version: Optional[Callable[[], int]] = None) -> None:
        self._read_version = read_version or _read_version_from_database
        self._version = 0
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._changed: Optional[asyncio.Event] = None
        self._poked: Optional[asyncio.Event] = None
        self._task: Optional[asyncio.Task] = None
        self._subscribers = 0

    # ── Reading ───────────────────────────────────────────────────────────

    @property
    def version(self) -> int:
        return self._version

    @property
    def subscribers(self) -> int:
        return self._subscribers

    def changed_event(self) -> asyncio.Event:
        """An event that is set the next time the version moves."""
        assert self._changed is not None
        return self._changed

    # ── Subscribing ───────────────────────────────────────────────────────

    def subscription(self) -> "_SubscriptionContext":
        return _SubscriptionContext(self)

    def _bind(self) -> None:
        """(Re)bind to the running loop. A new loop (a test client, a restart of
        the event loop) starts from nothing: events belong to the loop that made
        them."""
        loop = asyncio.get_running_loop()
        if self._loop is not loop:
            self._loop = loop
            self._changed = asyncio.Event()
            self._poked = asyncio.Event()
            self._task = None
            self._subscribers = 0
            self._version = 0

    def _ensure_running(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.ensure_future(self._watch())

    async def _refresh(self) -> None:
        self._publish(await run_in_threadpool(self._read_version))

    def _publish(self, version: int) -> None:
        if version == self._version:
            return
        self._version = version
        previous = self._changed
        self._changed = asyncio.Event()
        previous.set()  # type: ignore[union-attr]

    async def _watch(self) -> None:
        """While any stream is open, look at the stored version now and then."""
        try:
            while self._subscribers > 0:
                try:
                    await asyncio.wait_for(self._poked.wait(), WATCH_INTERVAL_SECONDS)  # type: ignore[union-attr]
                except asyncio.TimeoutError:
                    pass
                self._poked.clear()  # type: ignore[union-attr]
                if self._subscribers <= 0:
                    break
                try:
                    await self._refresh()
                except Exception:  # noqa: BLE001 - a failed read must not end the watch
                    logger.warning("DESKTOP_NOTIFICATION_WATCH_FAILED: could not read the stored version", exc_info=True)
        finally:
            self._task = None

    # ── Waking from a writer ──────────────────────────────────────────────

    def poke(self) -> None:
        """Look at the stored version now. Safe from any thread (a route that
        has just saved a change runs on the thread pool); a no-op when no
        stream is open in this process."""
        loop, poked = self._loop, self._poked
        if loop is None or poked is None or self._subscribers <= 0:
            return
        try:
            loop.call_soon_threadsafe(poked.set)
        except RuntimeError:
            pass  # the loop has closed


class _SubscriptionContext:
    def __init__(self, watcher: VersionWatcher) -> None:
        self._watcher = watcher
        self._counted = False

    async def __aenter__(self) -> Subscription:
        watcher = self._watcher
        watcher._bind()
        watcher._subscribers += 1
        self._counted = True
        try:
            # The truth at the moment of connecting, not a value a watcher read
            # some seconds ago (or never read, for the first stream).
            await watcher._refresh()
            watcher._ensure_running()
        except BaseException:
            self._release()
            raise
        return Subscription(watcher, watcher.version)

    async def __aexit__(self, *exc_info) -> None:
        self._release()

    def _release(self) -> None:
        if self._counted:
            self._counted = False
            self._watcher._subscribers -= 1


#: The one watcher of this process.
watcher = VersionWatcher()


def notify_changed() -> None:
    """A change has just been committed in this process: wake its streams."""
    watcher.poke()


async def event_stream(since: int, *, source: Optional[VersionWatcher] = None) -> AsyncIterator[str]:
    """The body of ``GET /desktop-notifications/stream``.

    If the stored version already differs from ``since`` (the client missed a
    change while it was disconnected) one event is sent at once. Otherwise
    nothing but a ping every ``HEARTBEAT_SECONDS`` until the version moves --
    then one event. Either way the stream ends after the event: the client
    fetches the schedule and opens a new one, so a change is signalled exactly
    once and there is nothing to repeat if the client is slow.
    """
    source = source or watcher
    deadline = time.monotonic() + MAX_STREAM_SECONDS
    async with source.subscription() as subscription:
        if subscription.version != since:
            yield sse_event(subscription.version)
            return
        yield SSE_OPEN
        while not _shutting_down:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            if await subscription.wait(min(HEARTBEAT_SECONDS, remaining)):
                yield sse_event(subscription.version)
                return
            yield SSE_PING

"""Request correlation, and the one log line that answers "what happened to it?".

Why this exists
---------------
A user reports "projects would not load at 10:32". Until this module, the server
could say almost nothing about that request: uvicorn's access line has a status
and no duration or user, the desktop's per-request ``X-Request-ID`` was read by
two timer routes and ignored everywhere else, a failure that escaped as an
unhandled exception was a plain-text 500 with no trace to the caller, and the
pool warnings (``DB_CONNECTION_HELD_LONG`` ...) could not be tied to a request.

What it does
------------
* Accepts the client's ``X-Request-ID`` (or mints one), keeps it for the life of
  the request, and returns it as ``X-Request-ID`` on every response, so the id
  on the user's screen or in their log is the id here.
* Logs **only what is worth reading**: a response of 500 or more
  (``REQUEST_FAILED``), a request slower than ``REQUEST_SLOW_SECONDS``
  (``SLOW_REQUEST``) and an escaped exception (``REQUEST_UNHANDLED``, with the
  traceback). A healthy request costs two clock reads and a header.
* Answers an escaped exception with a JSON 500 that carries the request id
  (and then re-raises, so the server's own traceback log and test clients are
  unchanged).
  This middleware sits *inside* CORS on purpose: Starlette's own last-resort
  500 is produced outside it, carries no ``Access-Control-Allow-Origin``, and
  the browser reports it as a network failure -- which is exactly how a server
  error looked on the web dashboard.
* Records who the request was for (``user_id``, filled in by
  ``get_current_user``) so the line names the account.

What it never logs: the query string, headers, bodies or tokens. Path only.
"""
from __future__ import annotations

import logging
import re
import time
import uuid
from contextvars import ContextVar
from typing import Any, Dict, Optional

from starlette.datastructures import MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger("app.request")

#: Client-supplied ids are accepted only if they look like one: this ends up in
#: a log line and a response header, so anything else is replaced, never echoed.
_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{8,64}$")

#: A request that takes this long is logged as SLOW_REQUEST.
DEFAULT_SLOW_SECONDS = 2.0

#: Mutable per-request record. A dict rather than separate ContextVars because
#: sync routes run in a worker thread on a *copy* of the context: setting a
#: variable there is invisible here, but mutating a shared dict is not.
_state: ContextVar[Optional[Dict[str, Any]]] = ContextVar("monitra_request_state", default=None)


def current_request_id() -> Optional[str]:
    state = _state.get()
    return state["request_id"] if state else None


def note_user(user_id: Optional[int]) -> None:
    """Record whose request this is. Called once the caller is authenticated."""
    state = _state.get()
    if state is not None and user_id is not None:
        state["user_id"] = user_id


def _clean_request_id(value: Optional[str]) -> str:
    if value and _ID_PATTERN.match(value):
        return value
    return uuid.uuid4().hex


class RequestLogMiddleware:
    """Pure-ASGI (no ``BaseHTTPMiddleware``: it breaks streaming and adds a task per request)."""

    def __init__(self, app: ASGIApp, slow_seconds: Optional[float] = None) -> None:
        self.app = app
        self.slow_seconds = slow_seconds

    def _slow_after(self) -> float:
        if self.slow_seconds is not None:
            return self.slow_seconds
        try:
            from app.core.config import settings

            return float(settings.REQUEST_SLOW_SECONDS)
        except Exception:  # noqa: BLE001
            return DEFAULT_SLOW_SECONDS

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        raw = None
        for key, value in scope.get("headers") or []:
            if key == b"x-request-id":
                raw = value.decode("latin-1")
                break
        request_id = _clean_request_id(raw)
        state: Dict[str, Any] = {"request_id": request_id, "user_id": None}
        token = _state.set(state)
        started = time.perf_counter()
        outcome: Dict[str, Any] = {"status": None, "started": False}

        async def send_with_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                outcome["status"] = message["status"]
                outcome["started"] = True
                MutableHeaders(scope=message)["X-Request-ID"] = request_id
            await send(message)

        try:
            try:
                await self.app(scope, receive, send_with_id)
            except Exception as exc:  # noqa: BLE001 - this is the last place it can be answered
                self._log_unhandled(scope, state, started, exc)
                outcome["logged"] = True
                if not outcome["started"]:
                    response = JSONResponse(
                        {"detail": "Internal server error.", "request_id": request_id},
                        status_code=500,
                    )
                    await response(scope, receive, send_with_id)
                # Re-raised either way, as Starlette's own handler does after it
                # has answered: the server still records the traceback and a test
                # client still sees the exception. This only makes sure the
                # caller got a JSON body with CORS headers first.
                raise
        finally:
            self._log_outcome(scope, state, outcome, started)
            _state.reset(token)

    # ── logging ──────────────────────────────────────────────────────────────

    def _fields(self, scope: Scope, state: Dict[str, Any], started: float) -> str:
        from app.core.request_context import current_request_context

        context = current_request_context()
        client = f"{context.client}/{context.client_version}" if context and context.client_version else (
            context.client if context else None
        )
        return (
            f"method={scope.get('method')} path={scope.get('path')} "
            f"ms={int((time.perf_counter() - started) * 1000)} req={state['request_id']} "
            f"user={state['user_id'] if state['user_id'] is not None else '-'} client={client or '-'}"
        )

    def _log_unhandled(self, scope: Scope, state: Dict[str, Any], started: float, exc: BaseException) -> None:
        # No traceback here: the exception is re-raised and the server logs it.
        # This line is the one that carries the request id and the user.
        logger.error(
            "REQUEST_UNHANDLED error=%s %s", type(exc).__name__, self._fields(scope, state, started)
        )

    def _log_outcome(self, scope: Scope, state: Dict[str, Any], outcome: Dict[str, Any], started: float) -> None:
        if outcome.get("logged"):
            return
        status = outcome["status"]
        elapsed = time.perf_counter() - started
        if status is not None and status >= 500:
            logger.warning("REQUEST_FAILED status=%s %s", status, self._fields(scope, state, started))
        elif elapsed >= self._slow_after():
            logger.warning("SLOW_REQUEST status=%s %s", status, self._fields(scope, state, started))

"""Who is calling, for the audit trail: the client kind and its address.

Every ``activity_logs`` row says *who did what, when*. Two more facts make a
row useful to the person reading it -- *from which client* (the desktop
application or the web dashboard) and *from which address* -- and both are
properties of the HTTP request, not of the service that performs the action.
Threading a ``Request`` through every service signature for the sake of a log
line would touch dozens of call sites; instead the middleware below records
the two facts in a request-scoped context variable, and
``ActivityLogService`` reads them when it writes a row. Code that runs outside
a request (a scheduled job, a test) sees ``None`` and the row simply carries no
client.

The desktop identifies itself on every call with ``User-Agent: Monitra/<version>``
(``desktop/app/api/client.py``); anything else with a browser-shaped agent is
the web dashboard, and the remainder is an API client such as ``curl``.
"""
from __future__ import annotations

import ipaddress
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Optional

from starlette.requests import Request

#: ``User-Agent`` prefix the desktop sends. Mirrors ``desktop/version.py``.
DESKTOP_USER_AGENT_PREFIX = "Monitra/"

CLIENT_DESKTOP = "desktop"
CLIENT_WEB = "web"
CLIENT_API = "api"


@dataclass(frozen=True)
class RequestContext:
    client: Optional[str] = None
    client_version: Optional[str] = None
    ip_address: Optional[str] = None


_context: ContextVar[Optional[RequestContext]] = ContextVar("monitra_request_context", default=None)


def describe_client(user_agent: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """``(client, version)`` from a ``User-Agent`` header."""
    agent = (user_agent or "").strip()
    if not agent:
        return None, None
    if agent.startswith(DESKTOP_USER_AGENT_PREFIX):
        version = agent[len(DESKTOP_USER_AGENT_PREFIX):].split(" ", 1)[0].strip() or None
        return CLIENT_DESKTOP, version
    if "Mozilla/" in agent:
        return CLIENT_WEB, None
    return CLIENT_API, None


def _valid_address(value: Optional[str]) -> Optional[str]:
    """`value` if it is an IP address, else None.

    `activity_logs.ip_address` is an ``inet`` column, and the proxy header is
    whatever the caller sent. Anything that does not parse is dropped here:
    written as-is it would make the insert fail, and the row -- not merely its
    address -- would be lost.
    """
    candidate = (value or "").strip()
    if not candidate:
        return None
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return None


def client_address(request: Request) -> Optional[str]:
    """The caller's address, honouring the proxy header nginx sets in production."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        first = _valid_address(forwarded.split(",", 1)[0])
        if first:
            return first
    return _valid_address(request.client.host if request.client else None)


def context_for(request: Request) -> RequestContext:
    client, version = describe_client(request.headers.get("user-agent"))
    return RequestContext(client=client, client_version=version, ip_address=client_address(request))


def set_request_context(context: Optional[RequestContext]) -> Token:
    return _context.set(context)


def reset_request_context(token: Token) -> None:
    _context.reset(token)


def current_request_context() -> Optional[RequestContext]:
    return _context.get()


async def request_context_middleware(request: Request, call_next):
    """Record the calling client for the duration of one request."""
    token = set_request_context(context_for(request))
    try:
        return await call_next(request)
    finally:
        reset_request_context(token)

"""A `WFPM_` log line for every `/WFPM` request that did not succeed.

Why this exists: a task created in WFPM that never shows up in Monitra is
reported as "it just did not arrive". Between the two systems, though, the
request either never came, or came and was refused -- and a refusal used to
leave no `WFPM_` trace at all unless it happened to be an unlinked id. A `401`
(an expired token), a `422` (an id or field in a shape Monitra does not accept),
a `403` (a role that may not do it) and a `400` (an assignee who is not valid)
were all visible only as a bare access-log status code, with nothing saying
*which WFPM id* or *why*. `grep WFPM_REQUEST` over the backend log now answers
"did WFPM call, and what did Monitra say" for every call.

Nothing is changed about how a request is answered: the exception is logged and
re-raised untouched, so the client gets exactly the response it always did.

What is logged is the method, the path (which carries the WFPM ids), the status,
the caller's user id when the bearer token names one, and the reason. Never the
token, never a request body, never a field's value -- a validation failure is
reported by field name and message only.
"""
from __future__ import annotations

import logging
from typing import Callable

from fastapi import HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from jose import jwt

logger = logging.getLogger("uvicorn.error")

#: Longest reason written to the log. A `detail` is a sentence; a list of 50
#: invalid ids should not become a log line of its own.
_MAX_REASON = 300


def _caller(request: Request) -> str:
    """The user id the request's bearer token claims, or `-`.

    Read **without verifying** the signature, because this is only a label for a
    log line: a forged token earns a forged label and a `401`, nothing more. It
    is what lets a `401` still say whose token it was.
    """
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return "-"
    try:
        value = jwt.get_unverified_claims(token).get("user_id")
    except Exception:  # noqa: BLE001 - a label must never break a request
        return "service-key" if token.startswith("msk_") else "-"
    return str(value) if value is not None else "-"


def _client(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "-"


def _reason(exc: Exception) -> str:
    if isinstance(exc, RequestValidationError):
        # Field and message only: the offending *value* may be anything the
        # caller typed, and the field name is what a person needs to fix it.
        parts = [f"{'.'.join(str(p) for p in error.get('loc', ()))}: {error.get('msg', '')}" for error in exc.errors()]
        return "; ".join(parts)[:_MAX_REASON]
    detail = getattr(exc, "detail", None)
    if isinstance(detail, list):
        return "; ".join(str(item.get("msg", item)) if isinstance(item, dict) else str(item) for item in detail)[:_MAX_REASON]
    return str(detail if detail is not None else exc)[:_MAX_REASON]


def _refused(request: Request, status_code: int, reason: str) -> None:
    logger.warning(
        "WFPM_REQUEST_REFUSED: %s %s status=%s user=%s client=%s reason=%s",
        request.method, request.url.path, status_code, _caller(request), _client(request), reason,
    )


class WfpmRoute(APIRoute):
    """Route class for the `/WFPM` router: logs what the route refuses or fails."""

    def get_route_handler(self) -> Callable[[Request], Response]:
        handler = super().get_route_handler()

        async def logged(request: Request) -> Response:
            try:
                response = await handler(request)
            except RequestValidationError as exc:
                _refused(request, 422, _reason(exc))
                raise
            except HTTPException as exc:
                _refused(request, exc.status_code, _reason(exc))
                raise
            except Exception:
                logger.exception(
                    "WFPM_REQUEST_ERROR: %s %s user=%s client=%s",
                    request.method, request.url.path, _caller(request), _client(request),
                )
                raise
            return response

        return logged

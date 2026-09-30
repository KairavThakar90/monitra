"""The only code in this backend that makes an HTTP request to WFPM.

It sends one JSON document and classifies the answer; it decides nothing about
retries, queues or what the document says. That split is what lets
`WfpmTimerSync` be tested without a network and lets this be replaced -- a
different transport, a signed request -- without touching the queue.

What WFPM is sent, and what each answer means, is specified in
docs/WFPM_INTEGRATION.md. Nothing here ever logs or returns the token.
"""
from __future__ import annotations

from typing import Any, Optional

import httpx

#: What this backend advertises it can decode. Without `br`, for the reason
#: app/services/external_auth_service.py records: httpx cannot decode Brotli
#: here, and a server that honours it answers with bytes nothing can read.
ACCEPT_ENCODING = "gzip, deflate"

#: Answers that say "not now" or "not you, yet" rather than "not this event".
#: 401/403 are here deliberately: a wrong or not-yet-issued token is a
#: configuration problem on one side or the other, and an event refused for
#: that reason must still be deliverable once the token is fixed.
_RETRYABLE_STATUS = frozenset({401, 403, 408, 425, 429})


class WfpmDeliveryError(Exception):
    """WFPM was not told. `retryable` says whether trying again can help.

    `status_code` is WFPM's answer, or None when it never answered (a timeout,
    a refused connection, a DNS failure).
    """

    def __init__(self, message: str, *, retryable: bool, status_code: Optional[int] = None):
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code


def redact_error(exc: Exception) -> str:
    """A failure reduced to something safe to store and log: its kind and its
    message, truncated to the column. The token is a header and is never part
    of an httpx error message, and the URL is configuration, not a secret."""
    text = f"{type(exc).__name__}: {exc}".replace("\n", " ").strip()
    return text[:500]


def post_event(
    url: str,
    *,
    token: str,
    payload: dict[str, Any],
    idempotency_key: str,
    timeout_seconds: float,
) -> int:
    """POST one event to WFPM. Returns the 2xx status, or raises.

    Any 2xx means WFPM accepted the event. Redirects are not followed: a
    redirected POST is re-sent as a GET by most clients and would arrive as a
    different request from the one WFPM's contract describes.
    """
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Accept-Encoding": ACCEPT_ENCODING,
        "User-Agent": "Monitra-WFPM-Integration/1",
        # The same value on every attempt for one event, so WFPM can recognise
        # a retry of a request it already handled. Also in the body, as
        # `event_id`, for a receiver that cannot read headers.
        "Idempotency-Key": idempotency_key,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        with httpx.Client(timeout=timeout_seconds, follow_redirects=False) as client:
            response = client.post(url, json=payload, headers=headers)
    except httpx.TimeoutException as exc:
        raise WfpmDeliveryError(f"WFPM did not answer within {timeout_seconds:g}s", retryable=True) from exc
    except httpx.HTTPError as exc:
        raise WfpmDeliveryError(f"WFPM could not be reached ({type(exc).__name__})", retryable=True) from exc

    status = response.status_code
    if 200 <= status < 300:
        return status

    detail = (response.text or "").replace("\n", " ").strip()[:200]
    message = f"WFPM answered HTTP {status}" + (f": {detail}" if detail else "")
    # A 3xx is a wrong or moved URL -- this side's configuration, like a bad
    # token -- so it is retried rather than blamed on the event.
    retryable = status >= 500 or status < 400 or status in _RETRYABLE_STATUS
    raise WfpmDeliveryError(message, retryable=retryable, status_code=status)

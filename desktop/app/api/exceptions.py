import json
import re
from typing import Optional


def error_detail(response_body: Optional[str], fallback: str = "") -> str:
    """
    Pull the backend's own explanation out of an error response body.

    FastAPI answers a rejected request with ``{"detail": ...}`` -- a string
    for an HTTPException, a list of field errors for a schema failure. Both
    say something the user can act on ("Task assignee must be assigned to
    this project"), and both were being thrown away in favour of "Server
    error (HTTP 400)", which told the user nothing and cost a production
    debugging session.

    Never raises: a body that is not JSON, or not shaped as expected, returns
    `fallback` rather than turning an error path into a second error.
    """
    if not response_body:
        return fallback
    try:
        payload = json.loads(response_body)
    except (ValueError, TypeError):
        text = str(response_body).strip()
        return text[:300] if text else fallback

    detail = payload.get("detail") if isinstance(payload, dict) else None
    if isinstance(detail, str) and detail.strip():
        return detail.strip()
    if isinstance(detail, list):
        # 422: one line per rejected field, e.g. "body.assignee_id: field required".
        messages = []
        for item in detail:
            if not isinstance(item, dict):
                continue
            location = ".".join(str(part) for part in item.get("loc", []) if part != "body")
            message = str(item.get("msg", "")).strip()
            messages.append(f"{location}: {message}" if location else message)
        joined = "; ".join(m for m in messages if m)
        if joined:
            return joined[:300]
    return fallback


#: What a redacted address is called in a user-facing message.
URL_PLACEHOLDER = "the server"

#: Any ``scheme://rest`` run of non-space characters. Deliberately broad: the
#: point is that nothing address-shaped reaches a user, not that we parse URLs.
_URL_PATTERN = re.compile(r"""[a-zA-Z][a-zA-Z0-9+.\-]*://[^\s'"<>]*""")

#: Trailing characters that belong to the sentence, not to the address.
_TRAILING_PUNCTUATION = ".,;:!?)]}'\""


def redact_urls(text: str, placeholder: str = URL_PLACEHOLDER) -> str:
    """Replace every URL in `text` with `placeholder`.

    A user has no use for an endpoint, and showing one leaks internal
    infrastructure to anyone standing behind them -- the sign-in screen once
    printed the full authentication provider URL in red under the password
    field, on a timeout.

    Applied where a message is *displayed*, as a net under the messages
    themselves: a string can arrive from the backend's own ``detail`` or from
    an unexpected exception, and neither is under this client's control.

    Sentence punctuation immediately after the address is preserved, so
    "Request to https://host/x timed out." reads "Request to the server timed
    out." rather than losing the full stop.
    """
    if not text:
        return text

    def _swap(match: "re.Match[str]") -> str:
        url = match.group(0)
        trailing = ""
        while url and url[-1] in _TRAILING_PUNCTUATION:
            trailing = url[-1] + trailing
            url = url[:-1]
        return placeholder + trailing

    return _URL_PATTERN.sub(_swap, text)


class ApiError(Exception):
    """Base exception class for all SMS Desktop API client errors.

    `message` is user-facing: every caller from the sign-in screen down shows
    it verbatim when it has nothing better. The endpoint that failed therefore
    lives in `url`, which is for logs and diagnostics only and is never
    formatted into `message`.
    """

    def __init__(self, message: str, status_code: Optional[int] = None,
                 url: Optional[str] = None) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.url = url


class ApiConnectionError(ApiError):
    """Raised when there is a connection failure or network error."""

    def __init__(self, message: str, original_exception: Optional[Exception] = None,
                 url: Optional[str] = None) -> None:
        super().__init__(message, url=url)
        self.original_exception = original_exception


class ApiTimeoutError(ApiError):
    """Raised when an API request times out."""

    def __init__(self, message: str, original_exception: Optional[Exception] = None,
                 url: Optional[str] = None) -> None:
        super().__init__(message, url=url)
        self.original_exception = original_exception


class ApiHttpError(ApiError):
    """Raised when the API returns an HTTP error response (status code >= 400)."""

    def __init__(self, status_code: int, response_body: str, message: str = "HTTP error response") -> None:
        formatted_message = f"{message} (Status: {status_code}): {response_body}"
        super().__init__(formatted_message, status_code=status_code)
        self.status_code = status_code
        self.response_body = response_body


#: The one thing a user is told when their sign-in is over. The API-level
#: causes -- 401, "Not authenticated", a 502 from the identity provider -- are
#: accurate but meaningless to someone who only wants to get back to work.
SESSION_EXPIRED_MESSAGE = (
    "Your login session has expired. Please sign in again to continue."
)


class SessionExpiredError(ApiError):
    """The backend definitively ended this session.

    Distinct from ApiConnectionError on purpose. Both leave the client unable to
    make an authenticated call, but only this one means the stored credentials
    are worthless: an unreachable server must never clear a session, and keeping
    the two as separate types is what stops a network blip from being handled as
    a sign-out.

    Reports `status_code` 401 whatever the backend actually answered, so the
    handlers already written against 401 -- SyncService's auth_required, the
    dashboard's unauthorized_error -- treat it correctly without being taught
    about a new type.
    """

    def __init__(self, message: str = SESSION_EXPIRED_MESSAGE, status_code: int = 401) -> None:
        super().__init__(message, status_code=status_code)


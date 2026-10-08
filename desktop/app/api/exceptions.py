import errno
import json
import re
import socket
import ssl
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


class FailureCode:
    """Why a request failed, as precisely as the client can tell.

    The user is shown one calm sentence; the log and the diagnostics need the
    cause. Every transport failure and every HTTP error is given one of these,
    so "Network connection error" is never the whole story again -- it used to
    cover a refused connection, a reset, a dropped keep-alive, a DNS failure
    and any unexpected exception alike.

    Mapping to the incident taxonomy (docs/API_FAILURES.md):
    DNS=A, UNREACHABLE=A, REFUSED=B, CONNECT_TIMEOUT=C, READ_TIMEOUT=D,
    HTTP_401=E/S, HTTP_403=F, HTTP_404=G, HTTP_409=H, HTTP_429=I,
    HTTP_500=J, HTTP_502=K, HTTP_503=L, HTTP_504=M, MALFORMED=N,
    CANCELLED=O, CLIENT=P, HTTP_5XX covers Q/R when the body says so, and
    UNKNOWN=T.
    """

    DNS = "dns"
    UNREACHABLE = "unreachable"
    REFUSED = "refused"
    CONNECT = "connect"
    CONNECT_TIMEOUT = "connect_timeout"
    READ_TIMEOUT = "read_timeout"
    WRITE_TIMEOUT = "write_timeout"
    POOL_TIMEOUT = "pool_timeout"
    #: The connection was dropped mid-request (reset, aborted, broken pipe).
    RESET = "reset"
    #: The server hung up without answering ("Server disconnected").
    PROTOCOL = "protocol"
    TLS = "tls"
    PROXY = "proxy"
    CLOSED = "closed"
    MALFORMED = "malformed"
    CANCELLED = "cancelled"
    CLIENT = "client"
    UNKNOWN = "unknown"

    #: Failures that say "the connection broke, not the server": a pooled
    #: keep-alive the other end (or a NAT in between) had already dropped
    #: surfaces as a reset or a hang-up with no answer, fails in milliseconds,
    #: and is gone on the next, fresh connection. A GET that fails this way can
    #: be repeated: nothing was applied and nobody was kept waiting.
    #:
    #: Deliberately absent: timeouts (a slow backend is made slower by being
    #: asked again) and the *could not connect* family -- DNS, unreachable,
    #: refused. Those mean the machine is offline or nothing is listening; a
    #: second attempt a moment later does not change that, and on Windows a
    #: refused connection takes about two seconds to fail, so repeating it
    #: tripled the time to report a backend that was down (found when a test
    #: that expects an unreachable backend started taking three times as long).
    RETRYABLE_TRANSPORT = frozenset({RESET, PROTOCOL})

    #: Gateway statuses that mean "try again", not "no".
    RETRYABLE_STATUS = frozenset({502, 503, 504})


def http_failure_code(status_code: int) -> str:
    return f"http_{int(status_code)}"


def _cause_chain(exc: BaseException):
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        yield exc
        exc = exc.__cause__ or exc.__context__


#: errno values for "the network or host cannot be reached", POSIX and Windows.
_UNREACHABLE_ERRNOS = {
    errno.ENETUNREACH, errno.EHOSTUNREACH, errno.ENETDOWN,
    10050, 10051, 10065,  # WSAENETDOWN, WSAENETUNREACH, WSAEHOSTUNREACH
}


def classify_transport_error(exc: BaseException) -> str:
    """The `FailureCode` for an exception raised by the HTTP transport.

    Reads the exception's own type first (httpx names the phase), then its
    cause chain for the OS-level reason, because httpx reports a failed DNS
    lookup, a refused connection and an unreachable network all as the same
    `ConnectError`.
    """
    import httpx

    if isinstance(exc, httpx.ConnectTimeout):
        return FailureCode.CONNECT_TIMEOUT
    if isinstance(exc, httpx.ReadTimeout):
        return FailureCode.READ_TIMEOUT
    if isinstance(exc, httpx.WriteTimeout):
        return FailureCode.WRITE_TIMEOUT
    if isinstance(exc, httpx.PoolTimeout):
        return FailureCode.POOL_TIMEOUT
    if isinstance(exc, httpx.TimeoutException):
        return FailureCode.READ_TIMEOUT
    if isinstance(exc, httpx.ProxyError):
        return FailureCode.PROXY
    if isinstance(exc, httpx.RemoteProtocolError):
        return FailureCode.PROTOCOL
    if isinstance(exc, (httpx.LocalProtocolError, httpx.UnsupportedProtocol, httpx.InvalidURL)):
        return FailureCode.CLIENT
    if isinstance(exc, httpx.CloseError):
        return FailureCode.RESET
    if isinstance(exc, (httpx.ReadError, httpx.WriteError)):
        return FailureCode.RESET
    if isinstance(exc, (httpx.ConnectError, httpx.NetworkError)):
        for link in _cause_chain(exc):
            if isinstance(link, ssl.SSLError):
                return FailureCode.TLS
            if isinstance(link, socket.gaierror):
                return FailureCode.DNS
            if isinstance(link, ConnectionRefusedError):
                return FailureCode.REFUSED
            if isinstance(link, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
                return FailureCode.RESET
            if isinstance(link, OSError) and link.errno in _UNREACHABLE_ERRNOS:
                return FailureCode.UNREACHABLE
            if isinstance(link, OSError) and getattr(link, "winerror", None) in _UNREACHABLE_ERRNOS:
                return FailureCode.UNREACHABLE
        return FailureCode.CONNECT
    return FailureCode.CLIENT


def describe_failure(exc: BaseException) -> dict:
    """The diagnosis an error carries, found through the services that wrap it.

    Domain services re-raise a transport failure as their own `ApiError` with
    a sentence the user can read ("Failed to load projects: Network connection
    error."). The original is still on `__cause__`/`__context__`; this walks to
    it, so a screen's handler can log *why* without every service learning to
    copy fields. Never raises. Keys: `code` (a `FailureCode` or None), `status`,
    `request_id`, `attempts`, `elapsed_ms`.
    """
    for link in _cause_chain(exc):
        code = getattr(link, "failure_code", None)
        if code:
            return {
                "code": code,
                "status": getattr(link, "status_code", None),
                "request_id": getattr(link, "request_id", None),
                "attempts": getattr(link, "attempts", 1),
                "elapsed_ms": getattr(link, "elapsed_ms", None),
            }
    return {
        "code": None, "status": getattr(exc, "status_code", None),
        "request_id": None, "attempts": 1, "elapsed_ms": None,
    }


def is_session_failure(exc: BaseException) -> bool:
    """True when the failure is the session ending -- never a network problem.

    A 401 (or the client's own `SessionExpiredError`), found anywhere in the
    chain. The dashboards used to infer this from the words "session expired"
    in a message; a status code does not change when someone rewrites a
    sentence.
    """
    for link in _cause_chain(exc):
        if isinstance(link, SessionExpiredError):
            return True
        if getattr(link, "status_code", None) == 401:
            return True
    return False


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
        #: Diagnostics, filled in by `ApiClient` (None when an error did not
        #: come from a request): a `FailureCode`, how long the failing attempt
        #: took, the `X-Request-ID` it carried, and how many attempts were made.
        self.failure_code: Optional[str] = None
        self.elapsed_ms: Optional[int] = None
        self.request_id: Optional[str] = None
        self.attempts: int = 1
        #: Seconds the server asked us to wait (`Retry-After`), when it did.
        self.retry_after: Optional[float] = None


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
#: The backend's refusal when an administrator has excluded this member from
#: signing in (Members directory, `users.can_login`). Every request answers
#: 401 and every sign-in 403 with `{"detail": {"code": LOGIN_DISABLED_CODE,
#: "message": ..., "note": ...}}` -- see backend app/core/login_access.py.
#: The texts are the backend's, repeated here so the client can show them even
#: when the refusal came through a path that flattened the body.
LOGIN_DISABLED_CODE = "login_disabled"
LOGIN_DISABLED_MESSAGE = (
    "You are not allowed to log in yet. Once an administrator allows you, you can log in again."
)
LOGIN_DISABLED_NOTE = (
    "If you are continuously unable to log in, please contact your administrator."
)


def is_login_disabled(response_body: Optional[str]) -> bool:
    """Whether an error body is the backend's `login_disabled` refusal.
    Never raises."""
    if not response_body:
        return False
    try:
        payload = json.loads(response_body)
    except (ValueError, TypeError):
        return False
    detail = payload.get("detail") if isinstance(payload, dict) else None
    return isinstance(detail, dict) and detail.get("code") == LOGIN_DISABLED_CODE


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


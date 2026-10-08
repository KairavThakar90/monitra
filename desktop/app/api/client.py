import httpx
import logging
import platform
import random
import sys
import time
import uuid
import threading
from urllib.parse import urlsplit
from typing import Any, Callable, Dict, List, Optional, Tuple, Union
from app.config import settings
from app.api.exceptions import (
    ApiConnectionError, ApiError, ApiTimeoutError, ApiHttpError, SessionExpiredError,
    FailureCode, LOGIN_DISABLED_CODE, classify_transport_error, http_failure_code,
    is_login_disabled,
)
from version import user_agent

log = logging.getLogger(__name__)

# Timeout tiers for different operation types
TIMEOUT_FAST = 5.0      # Start/Stop timer
TIMEOUT_NORMAL = 10.0   # Data loading
TIMEOUT_SLOW = 30.0     # Uploads, large queries

# ── Transient-failure retry (idempotent reads only) ───────────────────────────
#
# A connection that was reset, a server that hung up without answering, a
# gateway that answered 502/503/504: for a GET these say "nothing happened,
# ask again", and asking again is what turns a one-off blip -- a keep-alive
# the other end had already closed, a gateway restarting -- into a request
# that simply succeeded. Without it one dropped socket failed the whole load
# and the user saw "Network connection error" on an otherwise healthy link.
#
# Bounded on every axis: two extra attempts at most, a total ceiling on the
# time spent waiting, never after a slow failure, never for a timeout (a slow
# backend is made slower by being asked again), never for anything that is not
# a GET. A write is retried only by the durable queue, which has an idempotency
# key for it; repeating a POST here would not.
RETRY_MAX_ATTEMPTS = 3
RETRY_BASE_SECONDS = 0.3
#: Total time the retries may spend waiting between attempts.
RETRY_WAIT_CEILING_SECONDS = 4.0
#: A failure that took this long is a slow failure, not a transient one.
RETRY_FAST_FAILURE_SECONDS = 5.0
#: The longest `Retry-After` honoured; a server asking for longer is not asked.
RETRY_AFTER_CEILING_SECONDS = 5.0
IDEMPOTENT_METHODS = frozenset({"GET", "HEAD"})
#: The pool is replaced after a reset at most this often.
POOL_RECYCLE_MIN_INTERVAL_SECONDS = 5.0

#: This machine's CPU architecture, resolved once at import.
#:
#: `platform.machine()` is a syscall on some platforms and the answer cannot
#: change while the process runs, so re-reading it per request would be pure
#: cost. The value is sent as-is and folded on the server, because the spellings
#: genuinely differ between platforms for the same chip: Windows says "AMD64"
#: where macOS says "x86_64", and Apple Silicon says "arm64" where Linux says
#: "aarch64". An empty string when the platform will not say is honest -- the
#: server then treats the architecture as unknown rather than assuming one.
MACHINE_ARCH = platform.machine() or ""

# Failure messages shown to the user.
#
# None of them names an endpoint. These strings are surfaced verbatim by
# callers that have nothing better to say -- the sign-in screen prints one in
# red under the password field -- and an internal hostname there is noise to
# the user and free reconnaissance to anyone looking over their shoulder. The
# URL that failed is attached to the exception as `.url` and written to the
# log, which is where a support engineer reads it.
TIMEOUT_MESSAGE = "The request timed out. Please check your connection and try again."
UPLOAD_TIMEOUT_MESSAGE = "The upload timed out. Please try again."
NETWORK_MESSAGE = "Could not reach the server. Please check your connection."
UNEXPECTED_MESSAGE = "An unexpected connection error occurred. Please try again."
CLIENT_CLOSED_MESSAGE = "The connection is closing; the request was not sent."
SESSION_RENEWAL_MESSAGE = "Could not renew the session right now. Please try again."


class RefreshOutcome:
    """What a silent token refresh concluded. See `ApiClient._refresh_once`."""
    RENEWED = "renewed"
    EXPIRED = "expired"
    UNAVAILABLE = "unavailable"


class ApiClient:
    """Reusable synchronous HTTP client for interacting with the SMS backend API.
    
    Uses a persistent httpx.Client with connection pooling to avoid
    TCP handshake overhead on every request.
    """

    def __init__(self, base_url: Optional[str] = None, timeout: float = TIMEOUT_NORMAL) -> None:
        """
        Initialize the API client.
        
        :param base_url: Override base URL. If None, loaded from app configuration.
        :param timeout: Default connection/read timeout limit in seconds.
        """
        # Load from configuration if not explicitly provided
        configured_url = base_url or settings.SMS_API_BASE_URL
        # Strip trailing slashes to prevent double-slashes during path joining
        self.base_url: str = configured_url.rstrip("/")
        self.timeout: float = timeout
        self._access_token: Optional[str] = None
        # Guards only token mutation and client construction — never a request.
        self._lock = threading.Lock()
        self._closed = False

        # Silent re-authentication. The hook is installed by the runtime and
        # renews the access token from the stored refresh token; the lock makes
        # it single-flight, so a burst of 401s from several service threads
        # produces one refresh rather than one per caller.
        self._refresh_hook: Optional[Callable[[], bool]] = None
        self._refresh_lock = threading.Lock()
        #: Why the backend last ended this session, when it said so: set to
        #: LOGIN_DISABLED_CODE when an administrator has excluded the member
        #: from signing in. Read (and cleared) by the window that takes the
        #: user back to the sign-in screen, so it can say *why*.
        self.session_end_reason: Optional[str] = None

        # Persistent connection pool — reuses TCP connections across requests
        self._client: Optional[httpx.Client] = None
        #: Pools replaced after a reset, kept (not closed) so a request still
        #: running on one finishes; closed when newer ones displace them.
        self._retired_clients: List[httpx.Client] = []
        self._last_recycle = 0.0
        #: Set by `close()` so a retry waiting out its backoff ends at once.
        self._closing = threading.Event()
        self._ensure_client()

    def _new_http_client(self) -> httpx.Client:
        """One pooled client. A method so a replacement pool is built exactly
        like the first (and so a test can build it over a fake transport)."""
        return httpx.Client(
            timeout=self.timeout,
            limits=httpx.Limits(
                max_connections=10,
                max_keepalive_connections=5,
                keepalive_expiry=30.0,
            ),
        )

    def _ensure_client(self) -> None:
        """Create the persistent HTTP client if it does not exist."""
        with self._lock:
            if self._closed or self._client is not None:
                return
            self._client = self._new_http_client()

    @property
    def access_token(self) -> Optional[str]:
        """Retrieve the currently set Bearer access token."""
        return self._access_token

    @access_token.setter
    def access_token(self, token: Optional[str]) -> None:
        """Set or update the Bearer access token used for requests."""
        self._access_token = token

    def _build_url(self, path: str) -> str:
        """Construct the absolute URL from the base URL and relative path."""
        # Prevent double slashes at the boundary
        clean_path = path.lstrip("/")
        return f"{self.base_url}/{clean_path}"

    def _prepare_headers(self, custom_headers: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        """Construct request headers, injecting Authorization headers if an access token is set."""
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-Request-ID": str(uuid.uuid4()),
            # Identify this build on every call. `version.py` is the single
            # source of truth for the string, and the backend reads it to know
            # which desktop version each user is running -- which is what makes
            # "did everyone move off the bad build?" answerable rather than a
            # question support has to ask each person individually.
            "User-Agent": user_agent(),
            # Windows and macOS ship as separate artifacts, so a rollout can be
            # complete on one platform and not the other. Sent alongside the
            # version so the fleet view can tell them apart.
            "X-Monitra-Platform": sys.platform,
            # macOS ships arm64 and x86_64 as separate builds of the same
            # version, so the platform alone does not identify an artifact this
            # machine can run. Without this the update check would have to
            # guess, and an x86_64 .dmg on an Apple Silicon Mac is a download
            # that cannot install -- which reads to the user as a broken update.
            "X-Monitra-Arch": MACHINE_ARCH,
        }
        if self._access_token:
            headers["Authorization"] = f"Bearer {self._access_token}"
        if custom_headers:
            headers.update(custom_headers)
        return headers

    def set_refresh_hook(self, hook: Optional[Callable[[], bool]]) -> None:
        """Install the callable that renews an expired access token.

        Wired by the runtime rather than constructed here: the client must not
        know what a session is, and AuthService already owns that. The hook
        returns True when a new token is in place, and raises
        SessionExpiredError when the session is genuinely over -- which
        propagates to the caller unchanged, so the UI still sees one clear
        signal to return to the login screen.
        """
        self._refresh_hook = hook

    def request(
        self,
        method: str,
        path: str,
        json_data: Optional[Any] = None,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        timeout: Optional[float] = None,
        skip_auth_refresh: bool = False,
        retry: bool = True,
    ) -> httpx.Response:
        """
        Execute an HTTP request using the persistent connection pool.

        A GET that fails in a way that says "nothing happened" (see
        `FailureCode.RETRYABLE_TRANSPORT`, and 502/503/504) is repeated up to
        twice, after a short jittered pause; nothing else is ever repeated here.
        `retry=False` opts a caller out -- the network probe, whose whole job
        is to report the first failure.

        A 401 is retried exactly once, after a silent token refresh. The retry
        is deliberately capped at one attempt: if the renewed token is also
        rejected, the session is over and hammering the endpoint would only
        delay telling the user so.

        When the refresh does not succeed, the caller sees the original
        ApiHttpError(401) -- not a new exception type. Every 401 handler in the
        app (SyncService's auth_required, the dashboard's unauthorized_error,
        startup verification) was already written against that, and the domain
        services in between catch broadly enough that a new type would be
        flattened into a generic message and lose the status code entirely.

        :param method: HTTP Verb (GET, POST, PUT, PATCH, DELETE).
        :param path: Relative endpoint path.
        :param json_data: JSON request body payload.
        :param params: Query string parameters.
        :param headers: Custom request headers.
        :param timeout: Override timeout for this specific request.
        :param skip_auth_refresh: Do not attempt a refresh on 401. Set by the
            auth endpoints themselves, which would otherwise recurse.
        :raises ApiTimeoutError: On connection/read timeouts.
        :raises ApiConnectionError: On network or dns failures.
        :raises ApiHttpError: On non-2xx status responses.
        :raises SessionExpiredError: When the session could not be renewed.
        :return: httpx.Response object.
        """
        token_used = self._access_token
        try:
            return self._execute(method, path, json_data, params, headers, timeout, retry)
        except ApiHttpError as e:
            if e.status_code in (401, 403) and is_login_disabled(e.response_body):
                # Excluded by an administrator. A refresh would be refused the
                # same way, so the 401 goes straight to the handlers that end
                # the session, with the reason recorded for the sign-in screen.
                if self.session_end_reason != LOGIN_DISABLED_CODE:
                    log.info("the backend refused this account: excluded from signing in")
                self.session_end_reason = LOGIN_DISABLED_CODE
                raise
            if e.status_code != 401 or skip_auth_refresh or self._refresh_hook is None:
                raise
            outcome = self._refresh_once(token_used)
            if outcome == RefreshOutcome.EXPIRED:
                raise
            if outcome == RefreshOutcome.UNAVAILABLE:
                # The session is not over -- it could not be renewed *right
                # now* (the refresh endpoint answered 5xx, or was unreachable).
                # Surfacing the 401 here read as "signed out" to every handler
                # and logged the user out over a transient failure; a
                # connection error is what it actually is, and every caller
                # already retries those.
                raise ApiConnectionError(
                    SESSION_RENEWAL_MESSAGE, original_exception=e, url=self._build_url(path)
                )
        # One retry, now carrying the renewed token.
        return self._execute(method, path, json_data, params, headers, timeout, retry)

    def _refresh_once(self, token_used: Optional[str]) -> str:
        """Renew the access token, at most one refresh at a time.

        Threads that arrive while a refresh is in flight wait for it and then
        reuse its result: whoever gets the lock second finds the token already
        changed and retries with it instead of refreshing again. Several
        services hit 401 within the same second when a token expires, and a
        refresh per caller would rotate the refresh token out from under the
        others -- each rotation invalidating the token the next one is about to
        present, turning one expiry into a cascade of false sign-outs.

        :return: a `RefreshOutcome`: RENEWED (retry with the new token),
            EXPIRED (the session is over; surface the 401) or UNAVAILABLE
            (could not renew right now; treat as a connection failure).
        """
        with self._refresh_lock:
            if self._access_token != token_used:
                return RefreshOutcome.RENEWED  # another thread already renewed it
            hook = self._refresh_hook
            if hook is None:
                return RefreshOutcome.EXPIRED
            log.info("access token rejected; attempting silent refresh")
            try:
                renewed = bool(hook())
            except SessionExpiredError as exc:
                # The backend's verdict, or nothing to renew from. Reported as
                # expired so the caller re-raises the 401 it already has; the
                # session itself is torn down by whoever handles that 401.
                log.info("silent refresh did not succeed (%s)", exc)
                return RefreshOutcome.EXPIRED
            except ApiError as exc:
                log.warning("silent refresh could not reach the backend (%s)", exc)
                return RefreshOutcome.UNAVAILABLE
            if renewed:
                return RefreshOutcome.RENEWED
            log.warning("silent refresh unavailable right now; the session is kept")
            return RefreshOutcome.UNAVAILABLE

    def _execute(
        self,
        method: str,
        path: str,
        json_data: Optional[Any] = None,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        timeout: Optional[float] = None,
        retry: bool = True,
    ) -> httpx.Response:
        """Perform a request, repeating an idempotent read through a transient failure.

        One `X-Request-ID` covers every attempt, so the backend's log and the
        desktop's log describe the same request under the same id; the
        attempt number travels in `X-Monitra-Attempt` from the second on.
        Auth is not handled here -- a 401 is not a transient failure.
        """
        url = self._build_url(path)
        req_headers = self._prepare_headers(headers)
        request_id = req_headers.get("X-Request-ID")
        may_retry = retry and method.upper() in IDEMPOTENT_METHODS
        started = time.monotonic()
        waited = 0.0
        attempt = 1
        while True:
            if attempt > 1:
                req_headers["X-Monitra-Attempt"] = str(attempt)
            try:
                response = self._send(
                    method, url, json_data=json_data, params=params,
                    req_headers=req_headers, req_timeout=timeout or self.timeout,
                )
            except ApiError as exc:
                exc.attempts = attempt
                delay = self._retry_delay(exc, attempt, waited) if may_retry else None
                self._log_failure(method, url, exc, attempt, delay)
                if delay is None:
                    raise
                if exc.failure_code in (FailureCode.RESET, FailureCode.PROTOCOL):
                    self._recycle_pool(exc.failure_code)
                if self._closing.wait(delay):
                    raise
                waited += delay
                attempt += 1
                continue
            if attempt > 1:
                log.info(
                    "API_RECOVERED %s %s after %d attempts in %d ms req=%s",
                    method, urlsplit(url).path, attempt,
                    int((time.monotonic() - started) * 1000), request_id,
                )
            return response

    def _retry_delay(self, exc: ApiError, attempt: int, waited: float) -> Optional[float]:
        """Seconds to wait before the next attempt, or None to give up."""
        if attempt >= RETRY_MAX_ATTEMPTS:
            return None
        if (exc.elapsed_ms or 0) / 1000.0 >= RETRY_FAST_FAILURE_SECONDS:
            return None
        code = exc.failure_code
        if isinstance(exc, ApiHttpError):
            if exc.status_code not in FailureCode.RETRYABLE_STATUS and exc.status_code != 429:
                return None
            if exc.retry_after is not None and exc.retry_after > RETRY_AFTER_CEILING_SECONDS:
                return None
            if exc.status_code == 429 and exc.retry_after is None:
                return None  # rate limited with no hint: asking again is the problem
        elif code not in FailureCode.RETRYABLE_TRANSPORT:
            return None
        delay = RETRY_BASE_SECONDS * (3 ** (attempt - 1)) * random.uniform(0.5, 1.5)
        if exc.retry_after:
            delay = max(delay, float(exc.retry_after))
        if waited + delay > RETRY_WAIT_CEILING_SECONDS:
            return None
        return delay

    def _log_failure(self, method: str, url: str, exc: ApiError, attempt: int, delay: Optional[float]) -> None:
        """One structured line per failed attempt: what, which, how long, what next.

        The path only (never the query: a search box can put what a user typed
        there), no headers, no body. `req` is the id the backend logs too.
        """
        # An ordinary business answer (404, 409, 422 ...) is not a fault.
        expected = (
            isinstance(exc, ApiHttpError) and exc.status_code is not None
            and exc.status_code < 500 and exc.status_code != 429
        )
        log.log(
            logging.INFO if (delay is not None or expected) else logging.WARNING,
            "API_FAIL %s %s code=%s status=%s attempt=%d/%d elapsed_ms=%s req=%s %s",
            method, urlsplit(url).path, exc.failure_code,
            exc.status_code if exc.status_code is not None else "-",
            attempt, RETRY_MAX_ATTEMPTS, exc.elapsed_ms, exc.request_id,
            f"retry_in={delay:.2f}s" if delay is not None else "final",
        )

    def _recycle_pool(self, reason: str) -> None:
        """Replace the connection pool after a connection was reset under us.

        A reset or a hang-up with no answer is the signature of a pooled
        keep-alive the other side (or a NAT in between) had already dropped,
        and its neighbours in the pool were opened at the same time. Retrying
        on the same pool can pick another dead one; a fresh pool cannot. The
        old pool is retired, not closed, so a request still running on it
        finishes.
        """
        now = time.monotonic()
        with self._lock:
            if self._closed or self._client is None:
                return
            if now - self._last_recycle < POOL_RECYCLE_MIN_INTERVAL_SECONDS:
                return
            self._last_recycle = now
            old, self._client = self._client, None
        self._ensure_client()
        log.info("API_POOL_RECYCLED after %s; idle connections discarded", reason)
        self._retired_clients.append(old)
        while len(self._retired_clients) > 2:
            try:
                self._retired_clients.pop(0).close()
            except Exception:  # noqa: BLE001
                pass

    def _translate(
        self, exc: BaseException, method: str, url: str,
        req_headers: Optional[Dict[str, str]], started: float, *, upload: bool = False,
    ) -> ApiError:
        """Turn an httpx exception into this app's, carrying the diagnosis.

        The type and the user-facing message are exactly what they were; what
        is new is `failure_code`, `elapsed_ms` and `request_id`, which is what
        lets one "Network connection error" on a screen be traced to a reset
        socket, a DNS failure or a gateway 502 -- and to the request the
        backend logged.
        """
        elapsed_ms = int((time.monotonic() - started) * 1000)
        if isinstance(exc, httpx.HTTPStatusError):
            status = exc.response.status_code
            err: ApiError = ApiHttpError(
                status_code=status,
                response_body=exc.response.text,
                message=f"API responded with status code {status}",
            )
            err.failure_code = http_failure_code(status)
            headers = getattr(exc.response, "headers", None)
            err.retry_after = _parse_retry_after(
                headers.get("Retry-After") if hasattr(headers, "get") else None
            )
        else:
            code = classify_transport_error(exc)
            if isinstance(exc, httpx.TimeoutException):
                log.warning("%s timed out: %s %s", "upload" if upload else "request", method, url)
                err = ApiTimeoutError(
                    UPLOAD_TIMEOUT_MESSAGE if upload else TIMEOUT_MESSAGE,
                    original_exception=exc, url=url,
                )
            elif isinstance(exc, (httpx.ConnectError, httpx.NetworkError)):
                log.warning("network error: %s %s (%s)", method, url, exc)
                err = ApiConnectionError(NETWORK_MESSAGE, original_exception=exc, url=url)
            else:
                log.warning("unexpected request failure: %s %s (%s)", method, url, exc)
                err = ApiConnectionError(UNEXPECTED_MESSAGE, original_exception=exc, url=url)
            err.failure_code = code
        err.elapsed_ms = elapsed_ms
        err.request_id = (req_headers or {}).get("X-Request-ID")
        return err

    def _send(
        self,
        method: str,
        url: str,
        json_data: Optional[Any] = None,
        params: Optional[Dict[str, Any]] = None,
        req_headers: Optional[Dict[str, str]] = None,
        req_timeout: Optional[float] = None,
    ) -> httpx.Response:
        """One HTTP round trip against a fully-built URL and header set.

        Shared by every caller so there is exactly one connection pool, one
        timeout policy and one mapping from httpx failures onto this app's
        exception types. A second HTTP path would mean a second set of each.
        """
        req_timeout = req_timeout or self.timeout

        def refused() -> ApiConnectionError:
            err = ApiConnectionError(CLIENT_CLOSED_MESSAGE, url=url)
            err.failure_code = FailureCode.CLOSED
            err.request_id = (req_headers or {}).get("X-Request-ID")
            return err

        if self._closed:
            raise refused()

        client = self._client
        if client is None:
            self._ensure_client()
            client = self._client
        if client is None:
            raise refused()

        started = time.monotonic()
        try:
            # NOTE: deliberately NOT holding a lock here. httpx.Client is
            # thread-safe and pools connections internally. The previous
            # implementation serialised every HTTP call in the process behind
            # one mutex, so a single slow request blocked the GUI thread, the
            # sync consumer and the network monitor simultaneously — the direct
            # cause of the "loader never resolves" hang.
            response = client.request(
                method=method,
                url=url,
                json=json_data,
                params=params,
                headers=req_headers,
                timeout=req_timeout,
            )
            # Triggers httpx.HTTPStatusError if response is 4xx or 5xx
            response.raise_for_status()
            return response
        except Exception as e:  # noqa: BLE001 - classified by _translate
            raise self._translate(e, method, url, req_headers, started) from e

    def post_external(
        self,
        url: str,
        json_data: Optional[Any] = None,
        timeout: Optional[float] = None,
    ) -> httpx.Response:
        """POST to a third-party URL, carrying none of this session's identity.

        Used for exactly one thing: the sign-in page posting credentials to the
        performance portal. It goes through this client so there is still one
        connection pool and one exception taxonomy, but it deliberately skips
        `_prepare_headers`, because that attaches our `Authorization` bearer to
        every request. Sending a Monitra access token to a host outside this
        system would hand a third party a working session credential.

        No refresh interceptor either: a 401 here is the portal's verdict on a
        password, not an expired token of ours to renew.

        :param url: Absolute URL. The base URL is not applied.
        """
        if not url.startswith(("http://", "https://")):
            log.error("refusing to send credentials to a non-HTTP URL: %r", url)
            raise ApiConnectionError(
                "The sign-in service is not configured correctly.", url=url
            )

        return self._send(
            "POST",
            url,
            json_data=json_data,
            req_headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": user_agent(),
            },
            req_timeout=timeout or self.timeout,
        )

    def post_multipart(
        self,
        path: str,
        files: Union[Dict[str, Any], List[Tuple[str, Any]]],
        data: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> httpx.Response:
        """
        POST a multipart/form-data body — a file upload.

        Kept separate from `request()` rather than folded into it because the
        two differ in exactly the way that matters: the JSON path sets
        `Content-Type: application/json` on every request, and httpx must be
        left to write the multipart boundary itself. Sending a file with that
        header set produces a 422 the body cannot explain.

        The 401-refresh-and-retry behaviour is preserved, and the retry is safe
        for the one caller here: a screenshot upload is idempotent on
        `client_screenshot_id`, so replaying it returns the existing record
        rather than storing the image twice.
        """
        token_used = self._access_token
        try:
            return self._execute_multipart(path, files, data, timeout)
        except ApiHttpError as e:
            if e.status_code != 401 or self._refresh_hook is None:
                raise
            if not self._refresh_once(token_used):
                raise
        return self._execute_multipart(path, files, data, timeout)

    def _execute_multipart(
        self,
        path: str,
        files: Union[Dict[str, Any], List[Tuple[str, Any]]],
        data: Optional[Dict[str, Any]],
        timeout: Optional[float],
    ) -> httpx.Response:
        url = self._build_url(path)
        headers = self._prepare_headers()
        # httpx sets this itself, boundary included.
        headers.pop("Content-Type", None)

        if self._closed:
            raise ApiConnectionError(CLIENT_CLOSED_MESSAGE, url=url)
        client = self._client
        if client is None:
            self._ensure_client()
            client = self._client
        if client is None:
            raise ApiConnectionError(CLIENT_CLOSED_MESSAGE, url=url)

        started = time.monotonic()
        try:
            response = client.post(
                url,
                files=files,
                data=data or {},
                headers=headers,
                timeout=timeout or TIMEOUT_SLOW,
            )
            response.raise_for_status()
            return response
        except Exception as e:  # noqa: BLE001 - classified by _translate
            raise self._translate(e, "POST", url, headers, started, upload=True) from e

    def get(self, path: str, params: Optional[Dict[str, Any]] = None, headers: Optional[Dict[str, str]] = None, timeout: Optional[float] = None, skip_auth_refresh: bool = False, retry: bool = True) -> httpx.Response:
        """Execute a GET request (repeated through a transient failure unless `retry=False`)."""
        return self.request("GET", path, params=params, headers=headers, timeout=timeout, skip_auth_refresh=skip_auth_refresh, retry=retry)

    def post(self, path: str, json_data: Optional[Any] = None, params: Optional[Dict[str, Any]] = None, headers: Optional[Dict[str, str]] = None, timeout: Optional[float] = None, skip_auth_refresh: bool = False) -> httpx.Response:
        """Execute a POST request."""
        return self.request("POST", path, json_data=json_data, params=params, headers=headers, timeout=timeout, skip_auth_refresh=skip_auth_refresh)

    def put(self, path: str, json_data: Optional[Any] = None, params: Optional[Dict[str, Any]] = None, headers: Optional[Dict[str, str]] = None, timeout: Optional[float] = None, skip_auth_refresh: bool = False) -> httpx.Response:
        """Execute a PUT request."""
        return self.request("PUT", path, json_data=json_data, params=params, headers=headers, timeout=timeout, skip_auth_refresh=skip_auth_refresh)

    def patch(self, path: str, json_data: Optional[Any] = None, params: Optional[Dict[str, Any]] = None, headers: Optional[Dict[str, str]] = None, timeout: Optional[float] = None, skip_auth_refresh: bool = False) -> httpx.Response:
        """Execute a PATCH request."""
        return self.request("PATCH", path, json_data=json_data, params=params, headers=headers, timeout=timeout, skip_auth_refresh=skip_auth_refresh)

    def delete(self, path: str, json_data: Optional[Any] = None, params: Optional[Dict[str, Any]] = None, headers: Optional[Dict[str, str]] = None, timeout: Optional[float] = None, skip_auth_refresh: bool = False) -> httpx.Response:
        """Execute a DELETE request."""
        return self.request("DELETE", path, json_data=json_data, params=params, headers=headers, timeout=timeout, skip_auth_refresh=skip_auth_refresh)

    def close(self) -> None:
        """
        Close the persistent HTTP client and release connection pool resources.

        Idempotent, and one-way: once closed the client refuses further
        requests rather than transparently re-opening a pool during shutdown.
        The runtime calls this only after every service thread has stopped, so
        no request can be in flight at this point.
        """
        self._closing.set()
        with self._lock:
            if self._closed:
                return
            self._closed = True
            client, self._client = self._client, None
            retired, self._retired_clients = self._retired_clients, []
        for stale in ([client] if client is not None else []) + retired:
            try:
                stale.close()
            except Exception:
                pass


def _parse_retry_after(value: Optional[str]) -> Optional[float]:
    """`Retry-After` as seconds, or None. Only the delta-seconds form is read."""
    if not value:
        return None
    try:
        seconds = float(value.strip())
    except ValueError:
        return None
    return seconds if seconds >= 0 else None

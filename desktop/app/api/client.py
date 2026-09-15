import httpx
import logging
import platform
import sys
import uuid
import threading
from typing import Any, Callable, Dict, Optional
from app.config import settings
from app.api.exceptions import (
    ApiConnectionError, ApiError, ApiTimeoutError, ApiHttpError, SessionExpiredError,
)
from version import user_agent

log = logging.getLogger(__name__)

# Timeout tiers for different operation types
TIMEOUT_FAST = 5.0      # Start/Stop timer
TIMEOUT_NORMAL = 10.0   # Data loading
TIMEOUT_SLOW = 30.0     # Uploads, large queries

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

        # Persistent connection pool — reuses TCP connections across requests
        self._client: Optional[httpx.Client] = None
        self._ensure_client()

    def _ensure_client(self) -> None:
        """Create the persistent HTTP client if it does not exist."""
        with self._lock:
            if self._closed or self._client is not None:
                return
            self._client = httpx.Client(
                timeout=self.timeout,
                limits=httpx.Limits(
                    max_connections=10,
                    max_keepalive_connections=5,
                    keepalive_expiry=30.0,
                ),
            )

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
    ) -> httpx.Response:
        """
        Execute an HTTP request using the persistent connection pool.

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
            return self._execute(method, path, json_data, params, headers, timeout)
        except ApiHttpError as e:
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
        return self._execute(method, path, json_data, params, headers, timeout)

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
    ) -> httpx.Response:
        """Perform one HTTP round trip. No retry, no auth handling."""
        return self._send(
            method,
            self._build_url(path),
            json_data=json_data,
            params=params,
            req_headers=self._prepare_headers(headers),
            req_timeout=timeout or self.timeout,
        )

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

        if self._closed:
            raise ApiConnectionError(CLIENT_CLOSED_MESSAGE, url=url)

        client = self._client
        if client is None:
            self._ensure_client()
            client = self._client
        if client is None:
            raise ApiConnectionError(CLIENT_CLOSED_MESSAGE, url=url)

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

        except httpx.TimeoutException as e:
            log.warning("request timed out: %s %s", method, url)
            raise ApiTimeoutError(TIMEOUT_MESSAGE, original_exception=e, url=url)

        except (httpx.ConnectError, httpx.NetworkError) as e:
            log.warning("network error: %s %s (%s)", method, url, e)
            raise ApiConnectionError(NETWORK_MESSAGE, original_exception=e, url=url)
            
        except httpx.HTTPStatusError as e:
            raise ApiHttpError(
                status_code=e.response.status_code,
                response_body=e.response.text,
                message=f"API responded with status code {e.response.status_code}"
            )
            
        except Exception as e:
            # Fallback for unexpected failures (e.g. malformed responses)
            log.warning("unexpected request failure: %s %s (%s)", method, url, e)
            raise ApiConnectionError(UNEXPECTED_MESSAGE, original_exception=e, url=url)

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
        files: Dict[str, Any],
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
        files: Dict[str, Any],
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
        except httpx.TimeoutException as e:
            log.warning("upload timed out: %s", url)
            raise ApiTimeoutError(UPLOAD_TIMEOUT_MESSAGE, original_exception=e, url=url)
        except (httpx.ConnectError, httpx.NetworkError) as e:
            log.warning("network error during upload: %s (%s)", url, e)
            raise ApiConnectionError(NETWORK_MESSAGE, original_exception=e, url=url)
        except httpx.HTTPStatusError as e:
            raise ApiHttpError(
                status_code=e.response.status_code,
                response_body=e.response.text,
                message=f"API responded with status code {e.response.status_code}",
            )
        except Exception as e:
            log.warning("unexpected upload failure: %s (%s)", url, e)
            raise ApiConnectionError(UNEXPECTED_MESSAGE, original_exception=e, url=url)

    def get(self, path: str, params: Optional[Dict[str, Any]] = None, headers: Optional[Dict[str, str]] = None, timeout: Optional[float] = None, skip_auth_refresh: bool = False) -> httpx.Response:
        """Execute a GET request."""
        return self.request("GET", path, params=params, headers=headers, timeout=timeout, skip_auth_refresh=skip_auth_refresh)

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
        with self._lock:
            if self._closed:
                return
            self._closed = True
            client, self._client = self._client, None
        if client is not None:
            try:
                client.close()
            except Exception:
                pass

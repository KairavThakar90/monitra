"""
A transient failure of an idempotent read must not become a failed screen.

The reported symptom: the desktop shows "Logged in successfully" and then
"Unable to load projects / Could not load projects: Failed to load projects:
Network connection error." for some users, for a while, until they wait or sign
in again. Two defects stood behind that one sentence:

* nothing repeated a GET that failed the way a dropped keep-alive fails (a
  reset, a hang-up with no answer, a gateway 502/503/504), so one bad socket
  failed the whole load; and
* every cause -- DNS, refused, reset, protocol, TLS, a bad gateway -- was
  folded into the same sentence, so nobody could tell which it was.

These tests drive a real ``ApiClient`` against an in-process transport, so the
retry, the classification and the structured log line are exercised as shipped.
"""
from __future__ import annotations

import logging
import socket
import ssl
import threading
import time

import httpx
import pytest

from app.api import client as client_module
from app.api.client import ApiClient
from app.api.exceptions import (
    ApiConnectionError, ApiError, ApiHttpError, ApiTimeoutError, FailureCode,
    SessionExpiredError, classify_transport_error, describe_failure, is_session_failure,
)


class Script:
    """A transport that plays back a list of outcomes, one per request."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.requests = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        outcome = self.outcomes.pop(0) if self.outcomes else httpx.Response(200, json={"ok": True})
        if isinstance(outcome, Exception):
            raise outcome
        if isinstance(outcome, int):
            return httpx.Response(outcome, json={"detail": "x"})
        return outcome


@pytest.fixture
def make_client(monkeypatch):
    monkeypatch.setattr(client_module, "RETRY_BASE_SECONDS", 0.001)
    created = []

    def build(*outcomes):
        script = Script(*outcomes)
        api = ApiClient(base_url="http://backend.test")
        # Every pool this client builds -- the first and any replacement after a
        # reset -- plays the same script.
        api._new_http_client = lambda: httpx.Client(transport=httpx.MockTransport(script), timeout=5)
        api._client.close()
        api._client = api._new_http_client()
        created.append(api)
        return api, script

    yield build
    for api in created:
        api.close()


def _connect_error(cause=None):
    err = httpx.ConnectError("connect failed")
    if cause is not None:
        err.__cause__ = cause
    return err


# ═══ Classification: every cause has its own name ═════════════════════════════

@pytest.mark.parametrize(
    "exc, expected",
    [
        (httpx.ConnectTimeout("t"), FailureCode.CONNECT_TIMEOUT),
        (httpx.ReadTimeout("t"), FailureCode.READ_TIMEOUT),
        (httpx.WriteTimeout("t"), FailureCode.WRITE_TIMEOUT),
        (httpx.PoolTimeout("t"), FailureCode.POOL_TIMEOUT),
        (httpx.ReadError("r"), FailureCode.RESET),
        (httpx.WriteError("w"), FailureCode.RESET),
        (httpx.RemoteProtocolError("Server disconnected without sending a response."), FailureCode.PROTOCOL),
        (httpx.ProxyError("p"), FailureCode.PROXY),
        (_connect_error(socket.gaierror(11001, "getaddrinfo failed")), FailureCode.DNS),
        (_connect_error(ConnectionRefusedError(10061, "refused")), FailureCode.REFUSED),
        (_connect_error(ssl.SSLError("bad cert")), FailureCode.TLS),
        (_connect_error(ConnectionResetError(10054, "reset")), FailureCode.RESET),
        (_connect_error(OSError(10051, "network unreachable")), FailureCode.UNREACHABLE),
        (_connect_error(), FailureCode.CONNECT),
        (httpx.UnsupportedProtocol("u"), FailureCode.CLIENT),
        (RuntimeError("anything else"), FailureCode.CLIENT),
    ],
)
def test_every_transport_failure_has_its_own_code(exc, expected):
    assert classify_transport_error(exc) == expected


def test_the_code_survives_the_services_that_re_wrap_the_error(make_client):
    """ProjectService turns a connection error into 'Network connection error.'
    -- the original is still reachable, and so is its diagnosis."""
    api, _ = make_client(*[httpx.ReadError("reset")] * 3)
    from app.projects.service import ProjectService

    with pytest.raises(ApiError) as caught:
        ProjectService(api).get_projects()
    assert "Network connection error" in str(caught.value)
    info = describe_failure(caught.value)
    assert info["code"] == FailureCode.RESET
    assert info["request_id"] and info["attempts"] == 3


def test_a_401_is_a_session_failure_and_a_reset_is_not():
    http = ApiHttpError(status_code=401, response_body="", message="x")
    assert is_session_failure(http)
    assert is_session_failure(SessionExpiredError())
    wrapped = ApiError("Session expired. Please log in again.", status_code=401)
    assert is_session_failure(wrapped)
    reset = ApiConnectionError("net")
    reset.failure_code = FailureCode.RESET
    assert not is_session_failure(reset)


# ═══ The retry: bounded, idempotent-only, and honest ══════════════════════════

def test_a_healthy_request_is_one_request_and_adds_no_delay(make_client):
    api, script = make_client()
    started = time.monotonic()
    assert api.get("/api/v1/projects").status_code == 200
    assert len(script.requests) == 1
    assert time.monotonic() - started < 0.5


def test_a_reset_connection_is_retried_and_succeeds(make_client, caplog):
    caplog.set_level(logging.INFO)
    api, script = make_client(httpx.ReadError("connection reset"))
    assert api.get("/api/v1/projects").status_code == 200
    assert len(script.requests) == 2
    ids = {r.headers["X-Request-ID"] for r in script.requests}
    assert len(ids) == 1, "one logical request, one id: that is what lets the logs be joined"
    assert script.requests[1].headers["X-Monitra-Attempt"] == "2"
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "API_FAIL" in text and "code=reset" in text and "API_RECOVERED" in text


@pytest.mark.parametrize(
    "exc",
    [
        _connect_error(socket.gaierror(11001, "getaddrinfo failed")),
        _connect_error(ConnectionRefusedError(10061, "refused")),
        _connect_error(OSError(10051, "network unreachable")),
        _connect_error(),
        httpx.ConnectTimeout("t"),
    ],
    ids=["dns", "refused", "unreachable", "connect", "connect_timeout"],
)
def test_failing_to_connect_is_reported_at_once_not_repeated(make_client, exc):
    """Offline, or nothing listening: a second try a moment later changes nothing,
    and on Windows a refused connection takes ~2 s to fail, so repeating it made
    "the backend is down" take three times as long to say so."""
    api, script = make_client(exc)
    with pytest.raises(ApiError):
        api.get("/api/v1/projects")
    assert len(script.requests) == 1


def test_a_server_that_hung_up_is_retried(make_client):
    api, script = make_client(httpx.RemoteProtocolError("Server disconnected without sending a response."))
    assert api.get("/api/v1/projects").status_code == 200
    assert len(script.requests) == 2


@pytest.mark.parametrize("status", [502, 503, 504])
def test_a_gateway_error_on_a_read_is_retried(make_client, status):
    api, script = make_client(status)
    assert api.get("/api/v1/projects").status_code == 200
    assert len(script.requests) == 2


def test_it_gives_up_after_three_attempts_and_says_so(make_client, caplog):
    caplog.set_level(logging.INFO)
    api, script = make_client(*[httpx.ReadError("reset")] * 5)
    with pytest.raises(ApiConnectionError) as caught:
        api.get("/api/v1/projects")
    assert len(script.requests) == client_module.RETRY_MAX_ATTEMPTS == 3
    assert caught.value.attempts == 3 and caught.value.failure_code == FailureCode.RESET
    assert any("final" in r.getMessage() for r in caplog.records if "API_FAIL" in r.getMessage())


@pytest.mark.parametrize(
    "outcome",
    [httpx.ReadTimeout("slow"), httpx.PoolTimeout("busy"), httpx.WriteTimeout("slow")],
)
def test_a_timeout_is_not_retried(make_client, outcome):
    """A slow backend is made slower by being asked again."""
    api, script = make_client(outcome)
    with pytest.raises(ApiTimeoutError):
        api.get("/api/v1/projects")
    assert len(script.requests) == 1


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 422, 500])
def test_a_definitive_answer_is_never_retried(make_client, status):
    api, script = make_client(status)
    with pytest.raises(ApiHttpError):
        api.get("/api/v1/projects", skip_auth_refresh=True)
    assert len(script.requests) == 1


def test_a_write_is_never_retried_here(make_client):
    """Only the durable queue repeats a write, because only it has an
    idempotency key to make repeating safe."""
    for outcome in (httpx.ReadError("reset"), httpx.RemoteProtocolError("hung up"), 503):
        api, script = make_client(outcome)
        with pytest.raises(ApiError):
            api.post("/time-entries/start", json_data={"x": 1})
        assert len(script.requests) == 1


def test_retry_false_opts_out(make_client):
    """The network probe's job is to report the first failure."""
    api, script = make_client(httpx.ReadError("reset"))
    with pytest.raises(ApiConnectionError):
        api.get("/auth/me", retry=False)
    assert len(script.requests) == 1


def test_a_429_is_retried_only_when_the_server_names_a_short_wait(make_client):
    short = httpx.Response(429, headers={"Retry-After": "0"}, json={})
    api, script = make_client(short)
    assert api.get("/x").status_code == 200
    assert len(script.requests) == 2

    long_wait = httpx.Response(429, headers={"Retry-After": "120"}, json={})
    api, script = make_client(long_wait)
    with pytest.raises(ApiHttpError):
        api.get("/x")
    assert len(script.requests) == 1

    no_hint = httpx.Response(429, json={})
    api, script = make_client(no_hint)
    with pytest.raises(ApiHttpError):
        api.get("/x")
    assert len(script.requests) == 1, "rate limited with no hint: asking again is the problem"


def test_a_server_asking_for_a_long_wait_is_not_asked_again(make_client):
    """The backend's statement-timeout 504 carries Retry-After: 30 -- repeating
    that request would just repeat the expensive work."""
    cancelled = httpx.Response(504, headers={"Retry-After": "30"}, json={})
    api, script = make_client(cancelled)
    with pytest.raises(ApiHttpError):
        api.get("/api/v1/react/dashboard")
    assert len(script.requests) == 1


def test_a_slow_failure_is_not_retried(make_client, monkeypatch):
    monkeypatch.setattr(client_module, "RETRY_FAST_FAILURE_SECONDS", 0.0)
    api, script = make_client(httpx.ReadError("reset"))
    with pytest.raises(ApiConnectionError):
        api.get("/x")
    assert len(script.requests) == 1


def test_the_total_wait_is_capped(make_client, monkeypatch):
    monkeypatch.setattr(client_module, "RETRY_BASE_SECONDS", 10.0)
    monkeypatch.setattr(client_module, "RETRY_WAIT_CEILING_SECONDS", 0.5)
    api, script = make_client(httpx.ReadError("reset"))
    started = time.monotonic()
    with pytest.raises(ApiConnectionError):
        api.get("/x")
    assert len(script.requests) == 1 and time.monotonic() - started < 2


def test_a_closing_client_does_not_sleep_out_its_backoff(make_client, monkeypatch):
    monkeypatch.setattr(client_module, "RETRY_BASE_SECONDS", 3.0)
    api, script = make_client(httpx.ReadError("reset"))
    threading.Timer(0.05, api.close).start()
    started = time.monotonic()
    with pytest.raises(ApiConnectionError):
        api.get("/x")
    assert time.monotonic() - started < 2.0


def test_the_401_refresh_path_is_unchanged_and_still_retries_once(make_client):
    api, script = make_client(401, 200)
    calls = []
    api.set_refresh_hook(lambda: calls.append(1) or True)
    assert api.get("/x").status_code == 200
    assert calls == [1] and len(script.requests) == 2


def test_a_reset_replaces_the_pool_so_the_retry_cannot_pick_another_dead_socket(make_client):
    api, _ = make_client(httpx.ReadError("reset"))
    before = api._client
    api._last_recycle = 0.0
    api.get("/x")
    assert api._client is not before
    assert before in api._retired_clients, "retired, not closed: a request still on it finishes"


def test_the_pool_is_not_replaced_more_than_once_in_a_while(make_client):
    api, _ = make_client(httpx.ReadError("reset"), httpx.ReadError("reset"))
    api.get("/x")
    assert len(api._retired_clients) <= 1


def test_the_query_string_never_reaches_the_failure_log(make_client, caplog):
    caplog.set_level(logging.INFO)
    api, _ = make_client(httpx.ReadError("reset"))
    api.get("/api/v1/projects", params={"search": "private-client-name"})
    text = " ".join(r.getMessage() for r in caplog.records if "API_" in r.getMessage())
    assert "private-client-name" not in text and "Authorization" not in text

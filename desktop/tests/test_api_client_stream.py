"""
`ApiClient.stream_get` and `NotificationScheduleApiService.open_stream`, against a
real HTTP server on localhost.

The change stream is the first response this client reads *as it arrives* rather
than all at once, so what is pinned is what only a real socket shows: lines are
delivered when the server writes them (not when it finishes); silence for longer
than the read timeout, a connection cut mid-stream and a refused connection each
surface as this application's own exception types; an error status is raised
before the caller sees a stream; and leaving the block early closes the
connection, so the server stops writing to a client that has gone.
"""
from __future__ import annotations

import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.api.client import ApiClient
from app.api.exceptions import ApiConnectionError, ApiError, ApiHttpError, ApiTimeoutError
from app.desktop_notifications.service import NotificationScheduleApiService


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # keep the test output clean
        pass

    def _chunk(self, text: str) -> None:
        data = text.encode()
        self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
        self.wfile.flush()

    def _open(self, status=200, content_type="text/event-stream") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        state = self.server.state
        state["requests"].append({"path": parsed.path, "query": parse_qs(parsed.query), "headers": dict(self.headers)})

        if parsed.path == "/ok":
            self._open()
            self._chunk(": connected\n\n")
            state["first_sent"].set()
            state["release"].wait(5)
            self._chunk("event: schedule\ndata: {\"version\": 4}\n\n")
            self.wfile.write(b"0\r\n\r\n")
        elif parsed.path == "/missing":
            body = b'{"detail": "Not Found"}'
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif parsed.path == "/unauthorized":
            body = b'{"detail": "Not authenticated"}'
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif parsed.path == "/silent":
            self._open()
            self._chunk(": connected\n\n")
            state["stop"].wait(3)
        elif parsed.path == "/cut":
            self._open()
            self._chunk(": connected\n\n")
            self.wfile.flush()
            self.connection.shutdown(socket.SHUT_RDWR)    # gone mid-body: no terminating chunk
            self.close_connection = True
        elif parsed.path == "/forever":
            self._open()
            try:
                while not state["stop"].is_set():
                    self._chunk(": ping\n\n")
                    time.sleep(0.05)
            except (BrokenPipeError, ConnectionError, OSError):
                state["client_gone"].set()
        else:
            self.send_error(500)


class QuietServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address):  # a connection the test cut on purpose
        pass


@pytest.fixture()
def server():
    httpd = QuietServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    httpd.state = {
        "requests": [], "first_sent": threading.Event(), "release": threading.Event(),
        "stop": threading.Event(), "client_gone": threading.Event(),
    }
    thread = threading.Thread(target=lambda: httpd.serve_forever(poll_interval=0.02), daemon=True)
    thread.start()
    yield httpd
    httpd.state["stop"].set()
    httpd.state["release"].set()
    httpd.shutdown()
    httpd.server_close()


def client_for(httpd) -> ApiClient:
    client = ApiClient(base_url=f"http://127.0.0.1:{httpd.server_address[1]}")
    client.access_token = "tok"
    return client


def test_lines_are_delivered_as_the_server_writes_them_not_when_it_finishes(server):
    client = client_for(server)
    with client.stream_get("/ok", read_timeout=5) as stream:
        lines = stream.lines()
        assert next(lines) == ": connected"
        assert server.state["release"].is_set() is False       # the server is still holding the rest back
        server.state["release"].set()
        rest = list(lines)

    assert "data: {\"version\": 4}" in rest


def test_it_asks_for_an_event_stream_uncompressed_with_the_session_and_the_query(server):
    client = client_for(server)
    server.state["release"].set()
    with client.stream_get("/ok", {"since": 7}, read_timeout=5) as stream:
        list(stream.lines())

    request = server.state["requests"][0]
    assert request["query"] == {"since": ["7"]}
    headers = {k.lower(): v for k, v in request["headers"].items()}
    assert headers["accept"] == "text/event-stream"
    assert headers["accept-encoding"] == "identity"
    assert headers["cache-control"] == "no-cache"
    assert headers["authorization"] == "Bearer tok"
    assert headers["x-request-id"]


@pytest.mark.parametrize("path,status", [("/missing", 404), ("/unauthorized", 401)])
def test_an_error_status_is_raised_before_the_caller_has_a_stream(server, path, status):
    client = client_for(server)
    with pytest.raises(ApiHttpError) as caught:
        with client.stream_get(path, read_timeout=5):
            pytest.fail("a refused request must not yield a stream")
    assert caught.value.status_code == status
    assert "detail" in caught.value.response_body


def test_silence_longer_than_the_read_timeout_is_an_api_timeout(server):
    client = client_for(server)
    started = time.monotonic()
    with client.stream_get("/silent", read_timeout=0.5) as stream:
        lines = stream.lines()
        assert next(lines) == ": connected"
        with pytest.raises(ApiTimeoutError):
            for _ in lines:
                pass

    assert time.monotonic() - started < 2.5               # at the timeout, not at the server's three seconds


def test_a_connection_cut_mid_stream_is_an_api_connection_error(server):
    client = client_for(server)
    with client.stream_get("/cut", read_timeout=5) as stream:
        with pytest.raises(ApiConnectionError):
            for _ in stream.lines():
                pass


def test_a_server_that_cannot_be_reached_is_an_api_connection_error():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]                     # nothing listens here any more
    client = ApiClient(base_url=f"http://127.0.0.1:{port}")

    with pytest.raises(ApiConnectionError):
        with client.stream_get("/ok", read_timeout=1):
            pytest.fail("there is nothing to stream from")


def test_leaving_the_block_early_closes_the_connection_so_the_server_stops_writing(server):
    client = client_for(server)
    with client.stream_get("/forever", read_timeout=5) as stream:
        for line in stream.lines():
            if line == ": ping":
                break                                     # the desktop is closing: it leaves mid-stream

    assert server.state["client_gone"].wait(3), "the server kept writing to a client that had left"


def test_a_closed_client_refuses_to_open_a_stream(server):
    client = client_for(server)
    client.close()

    with pytest.raises(ApiConnectionError):
        with client.stream_get("/ok", read_timeout=1):
            pytest.fail("a closed client must not open a stream")


def test_the_schedule_api_asks_for_the_stream_from_the_version_it_has(server):
    client = client_for(server)

    class Api(NotificationScheduleApiService):
        pass

    api = Api(client)
    server.state["release"].set()
    original = client.stream_get
    seen = {}

    def spy(path, params=None, **kwargs):
        seen.update(path=path, params=params, kwargs=kwargs)
        return original("/ok", params, **kwargs)

    client.stream_get = spy                               # the route's own path is the backend's; the transport is what is under test
    with api.open_stream(12, read_timeout=3) as stream:
        list(stream.lines())

    assert seen["path"] == "/desktop-notifications/stream"
    assert seen["params"] == {"since": 12}
    assert seen["kwargs"] == {"read_timeout": 3}


def test_the_schedule_api_reports_a_refusal_as_an_api_error_keeping_the_status(server):
    client = client_for(server)
    original = client.stream_get
    client.stream_get = lambda path, params=None, **kw: original("/missing", params, **kw)
    api = NotificationScheduleApiService(client)

    with pytest.raises(ApiError) as caught:
        with api.open_stream(0, read_timeout=2):
            pytest.fail("refused")

    assert caught.value.status_code == 404

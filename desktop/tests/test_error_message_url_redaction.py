"""
No error a user is shown may contain a URL.

The sign-in screen printed the full authentication-provider endpoint in red
under the password field whenever the portal was slow:

    Request to https://<portal-host>/wp-json/.../auth/hubstaff/login timed out.

It reached the screen because `ApiTimeoutError` is a *sibling* of
`ApiConnectionError`, not a subclass, so `_provider_login`'s transport
handling missed it entirely and the API client's own diagnostic string was
displayed verbatim.

Three layers are covered here, because the fix is defence in depth and each
layer is independently reachable:

  * the API client never formats a URL into a user-facing message;
  * AuthService maps a timeout onto wording of its own;
  * the sign-in screen redacts whatever it is handed before displaying it.
"""
from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.api.client import ApiClient
from app.api.exceptions import (
    ApiConnectionError,
    ApiError,
    ApiTimeoutError,
    redact_urls,
)
from app.auth.service import AuthService
from ui.login_window import LoginWindow

PORTAL_URL = "https://nothing.peakworkos.com/wp-json/st-performance/v1/auth/hubstaff/login"


def _assert_no_url(text: str) -> None:
    assert "://" not in text, f"message leaks an address: {text!r}"


# ── redact_urls ──────────────────────────────────────────────────────────────

def test_redact_urls_replaces_the_address_and_keeps_the_sentence():
    assert redact_urls(f"Request to {PORTAL_URL} timed out.") == (
        "Request to the server timed out."
    )


def test_redact_urls_handles_any_scheme():
    _assert_no_url(redact_urls("Sent to ftp://host/path now."))
    _assert_no_url(redact_urls("Sent to http://10.0.0.1:8000/x now."))


def test_redact_urls_replaces_every_address_in_one_message():
    redacted = redact_urls("https://a.example/x failed, so did https://b.example/y.")
    _assert_no_url(redacted)
    assert redacted.count("the server") == 2


def test_redact_urls_leaves_ordinary_text_alone():
    assert redact_urls("Incorrect username/email or password.") == (
        "Incorrect username/email or password."
    )
    assert redact_urls("") == ""


# ── Layer 1: the API client ──────────────────────────────────────────────────

@patch("httpx.Client.request")
def test_a_timeout_message_carries_no_url_but_the_exception_does(mock_request):
    mock_request.side_effect = httpx.TimeoutException("Read timeout", request=MagicMock())

    client = ApiClient(base_url="https://api.example.test")
    with pytest.raises(ApiTimeoutError) as excinfo:
        client.get("/projects")

    _assert_no_url(str(excinfo.value))
    # Still diagnosable: the endpoint is on the exception, for the log.
    assert excinfo.value.url == "https://api.example.test/projects"


@patch("httpx.Client.request")
def test_a_network_error_message_carries_no_url(mock_request):
    mock_request.side_effect = httpx.ConnectError("refused", request=MagicMock())

    client = ApiClient(base_url="https://api.example.test")
    with pytest.raises(ApiConnectionError) as excinfo:
        client.get("/projects")

    _assert_no_url(str(excinfo.value))
    assert excinfo.value.url == "https://api.example.test/projects"


@patch("httpx.Client.request")
def test_an_unexpected_failure_message_carries_no_url(mock_request):
    mock_request.side_effect = RuntimeError("something odd")

    client = ApiClient(base_url="https://api.example.test")
    with pytest.raises(ApiConnectionError) as excinfo:
        client.get("/projects")

    _assert_no_url(str(excinfo.value))


def test_a_closed_client_reports_no_url():
    client = ApiClient(base_url="https://api.example.test")
    client.close()

    with pytest.raises(ApiConnectionError) as excinfo:
        client.get("/projects")

    _assert_no_url(str(excinfo.value))


def test_refusing_a_non_http_credential_url_reports_no_url():
    client = ApiClient(base_url="https://api.example.test")

    with pytest.raises(ApiConnectionError) as excinfo:
        client.post_external("ftp://evil.example/login", json_data={})

    _assert_no_url(str(excinfo.value))


# ── Layer 2: AuthService ─────────────────────────────────────────────────────

def _auth_service(side_effect):
    api_client = MagicMock()
    api_client.post_external.side_effect = side_effect
    return AuthService(api_client=api_client, session_manager=MagicMock())


def test_a_portal_timeout_is_mapped_to_wording_of_our_own():
    """The regression: this path had no `except ApiTimeoutError` at all."""
    service = _auth_service(
        ApiTimeoutError(f"Request to {PORTAL_URL} timed out.", url=PORTAL_URL)
    )

    with pytest.raises(ApiError) as excinfo:
        service.login("someone@example.test", "correct horse")

    _assert_no_url(str(excinfo.value))
    assert "did not respond in time" in str(excinfo.value)


def test_a_portal_connection_failure_still_reports_no_url():
    service = _auth_service(
        ApiConnectionError("Network error trying to connect to " + PORTAL_URL)
    )

    with pytest.raises(ApiError) as excinfo:
        service.login("someone@example.test", "correct horse")

    _assert_no_url(str(excinfo.value))


# ── Layer 3: the sign-in screen ──────────────────────────────────────────────

@pytest.fixture
def window(qapp):
    widget = LoginWindow(MagicMock())
    yield widget
    widget.deleteLater()


def test_the_sign_in_screen_redacts_a_url_it_is_handed(window):
    """The net: this handler is given str() of any exception, from anywhere."""
    window._on_login_error(f"Request to {PORTAL_URL} timed out.")

    shown = window.error_label.text()
    _assert_no_url(shown)
    assert shown == "Request to the server timed out."


def test_the_sign_in_screen_still_shows_the_error(window):
    """Redaction must not swallow the message -- the user needs to be told."""
    window._on_login_error("Incorrect username/email or password.")

    assert window.error_label.text() == "Incorrect username/email or password."

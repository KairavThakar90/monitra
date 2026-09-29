"""
The Members directory's Allow / Exclude switch for signing in, on the desktop.

When an administrator excludes a member, the backend stops their running
timer, revokes their sessions, and answers every request with 401 and a
structured `login_disabled` detail. The desktop's part:

* the API client recognises that refusal, records it, and does not try a
  silent refresh that would be refused the same way;
* the main window, taking the user back to the sign-in screen (the dashboard's
  5-second sync probe gets that 401 first), says *why*: the administrator's
  message with the contact note in bold, on the card and in a pop-up, instead
  of "your session has expired";
* a sign-in attempt while still excluded shows the same.
"""
from __future__ import annotations

import gc
import json
from unittest.mock import MagicMock

import httpx
import pytest
from PySide6.QtWidgets import QMessageBox

from app.api.client import ApiClient
from app.api.exceptions import (
    LOGIN_DISABLED_CODE, LOGIN_DISABLED_MESSAGE, LOGIN_DISABLED_NOTE, SESSION_EXPIRED_MESSAGE,
    ApiHttpError, is_login_disabled,
)
from tests.test_timer_lifecycle_reliability import (  # noqa: F401  (fixtures)
    isolated_settings, live_runtime,
)
from ui.login_window import LoginWindow

REFUSAL = json.dumps({"detail": {
    "code": LOGIN_DISABLED_CODE, "message": LOGIN_DISABLED_MESSAGE, "note": LOGIN_DISABLED_NOTE,
}})


@pytest.fixture(autouse=True)
def _finalize_dead_qobjects_between_tests(qapp):
    yield
    qapp.processEvents()
    gc.collect()
    qapp.processEvents()


# ── Recognising the refusal ──────────────────────────────────────────────────


def test_the_refusal_is_recognised_and_nothing_else_is():
    assert is_login_disabled(REFUSAL)
    assert not is_login_disabled(json.dumps({"detail": "Not authenticated"}))
    assert not is_login_disabled(json.dumps({"detail": {"code": "other"}}))
    assert not is_login_disabled("<html>502</html>")
    assert not is_login_disabled(None)


def _client_answering(status, body):
    client = ApiClient(base_url="http://127.0.0.1:9")
    client._execute = MagicMock(side_effect=ApiHttpError(status, body))
    hook = MagicMock(return_value=True)
    client.set_refresh_hook(hook)
    return client, hook


def test_the_client_records_the_reason_and_does_not_refresh_into_it():
    client, hook = _client_answering(401, REFUSAL)
    with pytest.raises(ApiHttpError) as excinfo:
        client.get("/api/v1/sync/revision")
    assert excinfo.value.status_code == 401
    assert client.session_end_reason == LOGIN_DISABLED_CODE
    hook.assert_not_called()


def test_an_ordinary_401_still_refreshes_and_records_nothing():
    client, hook = _client_answering(401, json.dumps({"detail": "Not authenticated"}))
    with pytest.raises(ApiHttpError):
        client.get("/auth/me")
    hook.assert_called_once()
    assert client.session_end_reason is None


# ── The sign-in screen ───────────────────────────────────────────────────────


@pytest.fixture
def login(qapp):
    widget = LoginWindow(MagicMock())
    widget.show()
    yield widget
    box = getattr(widget, "_login_disabled_box", None)
    if box is not None:
        try:
            box.close()
        except RuntimeError:
            pass
    widget.deleteLater()


def test_a_refused_sign_in_shows_the_message_with_the_note_in_bold(login, qapp):
    login._on_login_error(LOGIN_DISABLED_MESSAGE)

    text = login.error_label.text()
    assert LOGIN_DISABLED_MESSAGE in text
    assert f"<b>{LOGIN_DISABLED_NOTE}</b>" in text
    box = login._login_disabled_box
    assert isinstance(box, QMessageBox) and box.isVisible()
    assert f"<b>{LOGIN_DISABLED_NOTE}</b>" in box.text()
    assert box.windowTitle() == "Login not allowed"


def test_other_sign_in_errors_stay_plain_text(login):
    login._on_login_error("<b>Invalid email or password</b>")
    assert login.error_label.text() == "<b>Invalid email or password</b>"
    from PySide6.QtCore import Qt
    assert login.error_label.textFormat() == Qt.TextFormat.PlainText
    assert getattr(login, "_login_disabled_box", None) is None


# ── Being signed out by the administrator ────────────────────────────────────


@pytest.fixture
def window(qapp, live_runtime, isolated_settings, monkeypatch):
    import main as main_module

    monkeypatch.setattr(main_module.MainWindow, "_on_exit_ready", lambda self: None)
    win = main_module.MainWindow(live_runtime)
    win.show()
    win._stack.setCurrentWidget(win._dashboard)
    win.api.notify = MagicMock()
    qapp.processEvents()
    yield win
    box = getattr(win._login, "_login_disabled_box", None)
    if box is not None:
        try:
            box.close()
        except RuntimeError:
            pass
    win._dashboard.reset_state()
    win.deleteLater()
    qapp.processEvents()


def test_an_excluded_member_is_signed_out_and_told_why(window, live_runtime):
    live_runtime.api_client.session_end_reason = LOGIN_DISABLED_CODE

    window._on_session_expired()

    assert window._stack.currentWidget() is window._login
    assert f"<b>{LOGIN_DISABLED_NOTE}</b>" in window._login.error_label.text()
    assert window._login._login_disabled_box.isVisible()
    window.api.notify.assert_called_once()
    assert window.api.notify.call_args.args[0] == LOGIN_DISABLED_MESSAGE
    assert live_runtime.api_client.session_end_reason is None, "read once, then cleared"


def test_an_ordinary_expiry_still_says_the_session_expired(window, live_runtime):
    live_runtime.api_client.session_end_reason = None

    window._on_session_expired()

    assert window._stack.currentWidget() is window._login
    assert window._login.error_label.text() == SESSION_EXPIRED_MESSAGE
    assert getattr(window._login, "_login_disabled_box", None) is None


def test_the_probe_401_is_what_signs_the_member_out(window):
    """The dashboard's 5-second sync probe is the first request to meet the
    refusal; its error path is what raises unauthorized_error."""
    from app.api.exceptions import ApiError

    seen = []
    window._dashboard.unauthorized_error.connect(lambda: seen.append(True))
    window._dashboard._on_sync_revision_error(ApiError("Session expired. Please log in again.", status_code=401))
    assert seen == [True]

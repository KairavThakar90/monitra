"""
Coverage for the redesigned login screen.

The screen carries exactly two controls -- the credentials and Sign In. In
particular the password reveal toggle must never survive a reset(), or a
cleared form could leave the next person's typing on screen.
"""
from unittest.mock import MagicMock

import pytest
from PySide6.QtWidgets import QLineEdit

from ui.login_window import LoginWindow


@pytest.fixture
def window(qapp):
    auth = MagicMock()
    auth.login.return_value = {"id": 1, "name": "Kairav"}
    widget = LoginWindow(auth)
    yield widget
    widget.deleteLater()


# ── Password reveal ──────────────────────────────────────────────────────────

def test_password_starts_masked(window):
    assert window.password_input.echoMode() == QLineEdit.EchoMode.Password


def test_reveal_toggle_shows_and_re_hides(window):
    window.reveal_button.setChecked(True)
    assert window.password_input.echoMode() == QLineEdit.EchoMode.Normal
    window.reveal_button.setChecked(False)
    assert window.password_input.echoMode() == QLineEdit.EchoMode.Password


def test_reset_re_masks_a_revealed_password(window):
    window.password_input.setText("secret")
    window.reveal_button.setChecked(True)
    window.reset()

    assert not window.reveal_button.isChecked()
    assert window.password_input.echoMode() == QLineEdit.EchoMode.Password
    assert window.password_input.text() == ""


# ── Messages ─────────────────────────────────────────────────────────────────

def test_the_screen_carries_no_remember_or_reset_controls(window):
    """Both were removed: a "Remember me" box and a "Forgot password?" link
    that no reset flow exists behind."""
    assert not hasattr(window, "remember_checkbox")
    assert not hasattr(window, "forgot_button")


def test_login_is_called_with_just_the_credentials(window):
    window.username_input.setText("kairav")
    window.password_input.setText("secret")
    window._handle_login()
    window.auth_service.login.assert_called_once_with("kairav", "secret")


def test_empty_credentials_are_refused_without_calling_the_service(window):
    window._handle_login()
    assert "required" in window.error_label.text()
    window.auth_service.login.assert_not_called()


def test_a_failed_login_reports_the_error_and_re_enables_the_form(window):
    window.auth_service.login.side_effect = RuntimeError("Invalid credentials")
    window.username_input.setText("kairav")
    window.password_input.setText("wrong")
    window._handle_login()

    assert window.error_label.text() == "Invalid credentials"
    assert window.login_button.isEnabled()
    assert window.username_input.isEnabled()


def test_a_successful_login_emits_the_user_and_clears_the_password(window):
    seen = []
    window.login_success.connect(seen.append)
    window.username_input.setText("kairav")
    window.password_input.setText("secret")
    window._handle_login()

    assert seen == [{"id": 1, "name": "Kairav"}]
    assert window.password_input.text() == ""


# ── Field geometry ───────────────────────────────────────────────────────────
#
# A stale `QLineEdit#LoginInput { padding: 10px 14px }` rule survived in
# LOGIN_QSS after the input was wrapped in `_Field`. Qt merges that rule with
# the frame's own, and padding is taken out of the text rect, which the frame's
# fixed height leaves no slack to absorb -- so the bottom of every descender
# was sheared off and a typed "…@gmail.com" rendered as "…@amail.com".
#
# These are geometry assertions rather than pixel ones on purpose: the suite
# runs under QT_QPA_PLATFORM=offscreen, which paints placeholder boxes instead
# of real glyphs, so no amount of reading the rendered image can see a clipped
# tail here. Height is the signature that survives the offscreen platform --
# padding inflates the widget's box, and every pixel of that inflation is
# taken back out of the room left for the text.


def _padding_slack(edit) -> int:
    """How much taller the input's box is than one line of its own font.

    With no padding this is a couple of pixels of frame. Anything larger is
    padding, and padding here is exactly what shears the descenders off.
    """
    return edit.height() - edit.fontMetrics().height()


@pytest.fixture
def shown_window(window):
    """The window realised at a realistic size.

    An unsized window leaves the card squeezed below the height the fields
    actually get on screen, which is not the geometry under test.
    """
    from PySide6.QtWidgets import QApplication

    window.resize(518, 576)
    window.show()
    QApplication.processEvents()
    yield window
    window.hide()


@pytest.mark.parametrize("field", ["username_input", "password_input"])
def test_the_input_box_is_not_inflated_by_padding(shown_window, field):
    edit = getattr(shown_window, field)

    assert _padding_slack(edit) <= 8, (
        f"{field} is {_padding_slack(edit)}px taller than its own line height; "
        "that excess is padding, and Qt takes it out of the text rect, which "
        "clips the tails of 'g', 'y' and 'p'"
    )


def test_no_stylesheet_pads_the_login_input_from_a_distance(shown_window):
    """The frame owns the field's box; a global rule must not reach into it.

    This is the rule that was actually violated. The pixel-level symptom is
    invisible to an offscreen run, so assert the cause directly.
    """
    from ui.styles import LOGIN_QSS

    assert "LoginInput" not in LOGIN_QSS, (
        "style the login input in _Field, not in LOGIN_QSS -- a rule here "
        "merges with the frame's own and silently re-pads the text rect"
    )
    assert "padding: 0" in shown_window._username_field.styleSheet()

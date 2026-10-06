"""The person looking at their own screen is told what is happening to their
screenshots -- quietly, truthfully, and only when there is something to say.

Production showed the web grid reporting "No capture" for hours while the
desktop said nothing, or said "Screenshot captured" at the instant of capture,
before a byte had reached Drive. The line under test sits in the Activity
panel's header and is driven by the screenshot service's own state through the
`BackgroundApi`, the only route the UI has.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from background_services import public_api
from background_services.screenshot import health
from background_services.public_api import BackgroundApi
from ui.activity_section import ActivitySection


@pytest.fixture
def section(runtime):
    api = BackgroundApi(runtime)
    widget = ActivitySection(api=api, api_client=SimpleNamespace())
    yield widget, runtime
    widget.deleteLater()


def _status(**kw):
    base = dict(state=health.OK, severity="ok", headline="Screenshot uploaded 10:34 AM",
                detail="", pending=0, last_uploaded_at=None)
    base.update(kw)
    return base


class TestTheLine:
    def test_it_is_hidden_when_there_is_nothing_true_to_say(self, section):
        widget, _ = section
        assert widget._shot_status.isHidden()
        widget.set_screenshot_status(_status(state=health.INACTIVE, headline=""))
        assert widget._shot_status.isHidden()

    def test_uploaded_is_said_only_with_the_confirmed_wording(self, section):
        widget, _ = section
        widget.set_screenshot_status(_status())
        assert not widget._shot_status.isHidden()
        assert widget._shot_status.text() == "✓ Screenshot uploaded 10:34 AM"

    def test_uploading_is_not_dressed_up_as_done(self, section):
        widget, _ = section
        widget.set_screenshot_status(_status(
            state=health.UPLOADING, severity="info", headline="Uploading screenshot"))
        text = widget._shot_status.text()
        assert "Uploading" in text and "✓" not in text

    def test_a_warning_and_a_failure_are_marked_and_coloured_differently(self, section):
        widget, _ = section
        widget.set_screenshot_status(_status(
            state=health.RETRYING, severity="warning", headline="Screenshot upload is retrying",
            detail="It is saved on this computer and will keep trying until it uploads."))
        warning_sheet = widget._shot_status.styleSheet()
        assert "⚠" in widget._shot_status.text()
        assert "will keep trying" in widget._shot_status.toolTip()

        widget.set_screenshot_status(_status(
            state=health.FAILED, severity="error", headline="Screenshot capture failed"))
        assert widget._shot_status.styleSheet() != warning_sheet

    def test_it_clears_again_when_the_state_goes_quiet(self, section):
        widget, _ = section
        widget.set_screenshot_status(_status())
        widget.set_screenshot_status(_status(state=health.INACTIVE, headline=""))
        assert widget._shot_status.isHidden() and widget._shot_status.text() == ""

    def test_a_missing_status_hides_it_rather_than_raising(self, section):
        widget, _ = section
        widget.set_screenshot_status(None)
        assert widget._shot_status.isHidden()

    def test_its_style_names_its_own_widget_so_it_cannot_repaint_a_tooltip(self, section):
        # DO_NOT_DO.md: a selector-less sheet on a single widget reaches its tooltip.
        widget, _ = section
        widget.set_screenshot_status(_status())
        assert widget._shot_status.styleSheet().lstrip().startswith("QLabel#ScreenshotStatus")


class TestItFollowsTheService:
    def test_a_change_in_the_service_reaches_the_line(self, section):
        widget, runtime = section
        runtime.screenshot.status_changed.emit(_status(
            state=health.RETRYING, severity="warning", headline="Screenshot upload is retrying"))
        assert "retrying" in widget._shot_status.text()

    def test_the_api_exposes_the_current_state_without_the_ui_touching_the_service(self, section):
        _, runtime = section
        api = BackgroundApi(runtime)
        assert api.screenshot_status()["state"] == health.INACTIVE

    def test_the_real_service_publishes_through_to_the_line(self, section, cache):
        """End to end inside the process: a queued capture on disk becomes the
        word "Uploading" on screen, through the service's own derivation."""
        widget, runtime = section
        runtime.screenshot._tracking = True
        runtime.cache.save_screenshot(
            client_screenshot_id="s1", local_file_path="x.webp",
            captured_at="2026-10-06T05:04:00+00:00", window_start="2026-10-06T05:00:00+00:00",
            width=1000, height=1000, file_size_bytes=10, time_entry_id=7,
        )
        runtime.screenshot._refresh_status()
        # The pool delivers on the GUI thread; pump until it has.
        from PySide6.QtWidgets import QApplication
        import time

        deadline = time.time() + 5
        while time.time() < deadline and widget._shot_status.isHidden():
            QApplication.processEvents()
            time.sleep(0.01)
        assert "Uploading" in widget._shot_status.text()
        runtime.screenshot._tracking = False

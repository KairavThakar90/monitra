"""macOS diagnostics: logging only, never behaviour, never content.

The diagnostics exist to tell the possible causes of a wallpaper-only
screenshot apart from a single reproduction on a real Mac (see
`screenshot/mac_diagnostics.py`). These tests pin what the diagnostics must
*not* do -- change a result, raise, run off macOS, or write a window title,
URL or pixel into the log -- and that what they compute is right. Quartz and
AppKit are faked: nothing here says what a real Mac answers.
"""
from __future__ import annotations

import logging
import sys
import types
from types import SimpleNamespace

import pytest

from background_services.notifications import mac_window
from background_services.notifications.toast_popup import ToastPopup
from background_services.screenshot import mac_diagnostics, screen_access
from background_services.screenshot.capture import MergedCapture
from background_services.screenshot.compositor import CanvasBounds, Placement
from background_services.screenshot.displays import Display
from background_services.screenshot.screen_access import AccessStatus, ScreenAccess

SECRET_TITLE = "Quarterly salaries - confidential.xlsx"
SECRET_URL = "https://intranet.example/hr/salaries"


def _bgra(width, height, painter):
    out = bytearray()
    for y in range(height):
        for x in range(width):
            b, g, r, a = painter(x, y)
            out += bytes((b, g, r, a))
    return bytes(out)


# ── The statistics ────────────────────────────────────────────────────────────

class TestImageStats:
    def test_a_flat_image_is_reported_flat(self):
        stats = mac_diagnostics.image_stats(
            _bgra(200, 120, lambda x, y: (40, 90, 160, 255)), 200, 120
        )
        assert stats["flat"] is True
        assert stats["dominant_fraction"] == 1.0
        assert stats["distinct_colors"] == 1
        assert stats["edge_density"] == 0.0
        assert stats["luma_std"] == 0.0

    def test_a_smooth_wallpaper_gradient_is_not_flat_but_has_almost_no_edges(self):
        stats = mac_diagnostics.image_stats(
            _bgra(400, 240, lambda x, y: (x // 4, 80, y // 3, 255)), 400, 240
        )
        assert stats["flat"] is False
        assert stats["edge_density"] < 0.05

    def test_application_like_content_has_many_edges(self):
        # Alternating light and dark pixels: text and window chrome.
        stats = mac_diagnostics.image_stats(
            _bgra(400, 240, lambda x, y: (255, 255, 255, 255) if (x // 2) % 2 else (0, 0, 0, 255)),
            400, 240,
        )
        assert stats["edge_density"] > 0.3
        assert stats["flat"] is False

    def test_a_transparent_image_says_so(self):
        # What a capture of "applications only" is where no application drew.
        stats = mac_diagnostics.image_stats(
            _bgra(300, 200, lambda x, y: (0, 0, 0, 0)), 300, 200
        )
        assert stats["transparent_fraction"] == 1.0
        assert stats["alpha_max"] == 0

    def test_row_padding_is_honoured(self):
        # 100 px of content in 512-byte rows: reading it as tight rows would
        # slide each row sideways and report garbage.
        width, height, row = 100, 80, 512
        buffer = bytearray(row * height)
        for y in range(height):
            for x in range(width):
                i = y * row + x * 4
                buffer[i:i + 4] = bytes((10, 20, 30, 255))
        stats = mac_diagnostics.image_stats(bytes(buffer), width, height, row)
        assert stats["flat"] is True and "error" not in stats

    def test_bad_input_is_reported_not_raised(self):
        assert mac_diagnostics.image_stats(b"\x00" * 10, 100, 100)["error"] == "short_buffer"
        assert mac_diagnostics.image_stats(b"", 0, 10)["error"] == "bad_geometry"
        assert "error" in mac_diagnostics.image_stats(None, 10, 10)


# ── Off macOS it does nothing ─────────────────────────────────────────────────

class TestOffMacOS:
    def test_it_is_disabled_here(self):
        assert mac_diagnostics.enabled() is False

    def test_nothing_is_logged_or_imported(self, caplog, monkeypatch):
        monkeypatch.setitem(sys.modules, "Quartz", None)   # would raise on import
        caplog.set_level(logging.DEBUG)
        mac_diagnostics.log_access(AccessStatus(ScreenAccess.NOT_REQUIRED))
        mac_diagnostics.log_capture(_merged())
        mac_diagnostics.log_environment_once()
        assert "MACDIAG" not in caplog.text

    def test_the_switch_turns_it_off_on_a_mac(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setenv("MONITRA_MAC_DIAGNOSTICS", "0")
        assert mac_diagnostics.enabled() is False
        monkeypatch.setenv("MONITRA_MAC_DIAGNOSTICS", "1")
        assert mac_diagnostics.enabled() is True

    def test_the_card_helpers_are_silent_off_macos(self, caplog, qapp):
        caplog.set_level(logging.DEBUG)
        card = ToastPopup()
        mac_window.log_card_state(card, "anything")
        assert mac_window.describe(card) == "not-macos"
        assert "MACDIAG" not in caplog.text
        card.deleteLater()


# ── With a (faked) Mac ────────────────────────────────────────────────────────

def _merged():
    display = Display(number=1, left=0, top=0, width=40, height=30, is_primary=True)
    pixels = _bgra(40, 30, lambda x, y: (200, 200, 200, 255))
    placement = Placement(display=display, pixels=pixels, width=40, height=30)
    return MergedCapture(
        placements=[placement], bounds=CanvasBounds(0, 0, 40, 30), displays=[display]
    )


class _FakeImage:
    def __init__(self, width, height, painter):
        self.width, self.height = width, height
        self.data = _bgra(width, height, painter)


def _fake_quartz(window_painter=None):
    quartz = types.ModuleType("Quartz")
    for name, value in {
        "kCGNullWindowID": 0,
        "kCGWindowListOptionOnScreenOnly": 1,
        "kCGWindowListOptionIncludingWindow": 8,
        "kCGWindowListExcludeDesktopElements": 16,
        "kCGWindowImageBoundsIgnoreFraming": 1,
        "kCGWindowImageNominalResolution": 16,
        "CGRectNull": "NULLRECT",
    }.items():
        setattr(quartz, name, value)

    me = __import__("os").getpid()
    windows = [
        {"kCGWindowLayer": 0, "kCGWindowOwnerPID": 4242, "kCGWindowNumber": 77,
         "kCGWindowOwnerName": "Google Chrome", "kCGWindowName": SECRET_TITLE,
         "kCGWindowBounds": {"Width": 1200, "Height": 800}},
        {"kCGWindowLayer": 0, "kCGWindowOwnerPID": 4243, "kCGWindowNumber": 78,
         "kCGWindowOwnerName": "Slack", "kCGWindowName": "",
         "kCGWindowBounds": {"Width": 900, "Height": 700}},
        {"kCGWindowLayer": 0, "kCGWindowOwnerPID": me, "kCGWindowNumber": 79,
         "kCGWindowOwnerName": "Monitra", "kCGWindowName": "Monitra 1.3.1",
         "kCGWindowBounds": {"Width": 400, "Height": 300}},
        {"kCGWindowLayer": 25, "kCGWindowOwnerPID": 1, "kCGWindowNumber": 1,
         "kCGWindowOwnerName": "Control Center", "kCGWindowName": SECRET_URL,
         "kCGWindowBounds": {"Width": 300, "Height": 300}},
    ]
    quartz.CGWindowListCopyWindowInfo = lambda options, window_id: windows
    quartz.CGMainDisplayID = lambda: 1
    quartz.CGDisplayBounds = lambda display: SimpleNamespace(
        origin=SimpleNamespace(x=0, y=0), size=SimpleNamespace(width=1440, height=900)
    )

    painter = window_painter or (lambda x, y: (0, 0, 0, 0))

    def create_image(rect, option, window_id, flags):
        if window_id == 77 and window_painter is not None:
            return _FakeImage(60, 40, window_painter)
        return _FakeImage(60, 40, painter)

    quartz.CGWindowListCreateImage = create_image
    quartz.CGImageGetWidth = lambda image: image.width
    quartz.CGImageGetHeight = lambda image: image.height
    quartz.CGImageGetAlphaInfo = lambda image: 2
    quartz.CGImageGetBytesPerRow = lambda image: image.width * 4
    quartz.CGImageGetDataProvider = lambda image: image
    quartz.CGDataProviderCopyData = lambda provider: provider.data
    return quartz


@pytest.fixture
def mac(monkeypatch):
    monkeypatch.setattr(mac_diagnostics, "enabled", lambda: True)
    mac_diagnostics.reset_for_tests()
    monkeypatch.setitem(sys.modules, "Quartz", _fake_quartz())
    appkit = types.ModuleType("AppKit")
    appkit.NSWorkspace = SimpleNamespace(
        sharedWorkspace=lambda: SimpleNamespace(
            frontmostApplication=lambda: SimpleNamespace(
                localizedName=lambda: "Google Chrome",
                bundleIdentifier=lambda: "com.google.Chrome",
                processIdentifier=lambda: 4242,
            )
        )
    )
    monkeypatch.setitem(sys.modules, "AppKit", appkit)
    monkeypatch.setattr(screen_access, "_preflight", lambda: True)
    yield
    mac_diagnostics.reset_for_tests()


class TestOnAMac:
    def test_access_reports_the_permission_and_the_window_counts(self, mac, caplog):
        caplog.set_level(logging.INFO)
        mac_diagnostics.log_access(AccessStatus(ScreenAccess.GRANTED, "titles visible"))

        text = caplog.text
        assert "MACDIAG_ENV" in text
        line = next(l for l in text.splitlines() if "MACDIAG_ACCESS" in l)
        assert "decision=granted" in line
        assert "preflight_now=True" in line
        assert "other_app_windows=2" in line
        assert "other_app_windows_with_title=1" in line
        assert "own_windows=1" in line
        assert "frontmost_app=Google Chrome" in line
        assert "frontmost_is_monitra=False" in line
        assert "gate_titled=" in line

    def test_a_refused_access_is_logged_too(self, mac, caplog):
        caplog.set_level(logging.INFO)
        mac_diagnostics.log_access(AccessStatus(ScreenAccess.DENIED, "CGPreflight=False"))
        assert "decision=denied" in caplog.text and "allowed=False" in caplog.text

    def test_capture_reports_the_geometry_the_pixels_and_every_probe(self, mac, caplog):
        caplog.set_level(logging.INFO)
        mac_diagnostics.log_capture(_merged())

        text = caplog.text
        assert "MACDIAG_CAPTURE display=1" in text
        assert "grabbed=40x30" in text and "flat=True" in text
        assert "MACDIAG_CAPTURE_SUMMARY displays=1 expected=1 complete=True" in text
        assert "MACDIAG_OWN_COVERAGE own_window_area_fraction=" in text
        for probe in ("full_display", "applications_only", "frontmost_window_only"):
            assert f"probe={probe}" in text, probe
        applications_only = next(
            l for l in text.splitlines() if "probe=applications_only" in l
        )
        # Nothing was drawn by any application in the fake: the signature of a
        # restricted capture, which is what the probe exists to show.
        assert "transparent_fraction=1.0" in applications_only
        frontmost = next(l for l in text.splitlines() if "probe=frontmost_window_only" in l)
        assert "owner=Google Chrome" in frontmost and "has_title=True" in frontmost

    def test_a_window_with_real_content_is_told_apart_from_a_blank_one(self, monkeypatch, mac, caplog):
        monkeypatch.setitem(
            sys.modules, "Quartz",
            _fake_quartz(lambda x, y: (255, 255, 255, 255) if (x // 2) % 2 else (0, 0, 0, 255)),
        )
        caplog.set_level(logging.INFO)
        mac_diagnostics.log_capture(_merged())
        line = next(l for l in caplog.text.splitlines() if "probe=frontmost_window_only" in l)
        assert "flat=False" in line and "transparent_fraction=0.0" in line

    def test_no_title_url_or_content_ever_reaches_the_log(self, mac, caplog):
        caplog.set_level(logging.DEBUG)
        mac_diagnostics.log_access(AccessStatus(ScreenAccess.GRANTED))
        mac_diagnostics.log_capture(_merged())
        assert SECRET_TITLE not in caplog.text
        assert "salaries" not in caplog.text
        assert SECRET_URL not in caplog.text
        assert "intranet" not in caplog.text

    def test_the_environment_is_logged_once_per_run(self, mac, caplog):
        caplog.set_level(logging.INFO)
        for _ in range(3):
            mac_diagnostics.log_access(AccessStatus(ScreenAccess.GRANTED))
        assert caplog.text.count("MACDIAG_ENV") == 1

    def test_a_broken_quartz_never_raises(self, mac, monkeypatch, caplog):
        quartz = _fake_quartz()

        def boom(*_a, **_k):
            raise RuntimeError("CoreGraphics exploded")

        quartz.CGWindowListCopyWindowInfo = boom
        quartz.CGWindowListCreateImage = boom
        monkeypatch.setitem(sys.modules, "Quartz", quartz)
        caplog.set_level(logging.INFO)

        mac_diagnostics.log_access(AccessStatus(ScreenAccess.GRANTED))
        mac_diagnostics.log_capture(_merged())     # must simply return

    def test_a_missing_quartz_never_raises(self, mac, monkeypatch, caplog):
        monkeypatch.setitem(sys.modules, "Quartz", None)
        caplog.set_level(logging.INFO)
        mac_diagnostics.log_access(AccessStatus(ScreenAccess.GRANTED))
        mac_diagnostics.log_capture(_merged())
        assert "MACDIAG_PROBE unavailable" in caplog.text


# ── The notification card ─────────────────────────────────────────────────────

class _FakeNSWindow:
    def className(self): return "QNSPanel"
    def styleMask(self): return 8320
    def level(self): return 3
    def isVisible(self): return True
    def occlusionState(self): return 2
    def isOnActiveSpace(self): return True
    def alphaValue(self): return 1.0
    def hidesOnDeactivate(self): return False
    def canBecomeKeyWindow(self): return False
    def isKeyWindow(self): return False
    def collectionBehavior(self): return 257
    def frame(self):
        return SimpleNamespace(
            size=SimpleNamespace(width=392, height=130),
            origin=SimpleNamespace(x=1000, y=40),
        )


class TestCardDiagnostics:
    @pytest.fixture
    def native(self, monkeypatch):
        monkeypatch.setattr(mac_window, "is_macos", lambda: True)
        monkeypatch.setattr(mac_window, "_ns_window", lambda widget: _FakeNSWindow())
        appkit = types.ModuleType("AppKit")
        appkit.NSApplication = SimpleNamespace(
            sharedApplication=lambda: SimpleNamespace(isActive=lambda: False)
        )
        appkit.NSPanel = object
        monkeypatch.setitem(sys.modules, "AppKit", appkit)

    def test_describe_says_whether_the_card_is_visible_and_the_app_active(self, native, qapp):
        line = mac_window.describe(object())
        for expected in (
            "class=QNSPanel", "visible=True", "occlusion=2", "hides_on_deactivate=False",
            "can_become_key=False", "is_key=False", "app_active=False", "frame=392x130@1000,40",
        ):
            assert expected in line, expected

    def test_a_failing_describe_is_reported_not_raised(self, monkeypatch, qapp):
        monkeypatch.setattr(mac_window, "is_macos", lambda: True)

        def boom(widget):
            raise RuntimeError("no native window")

        monkeypatch.setattr(mac_window, "_ns_window", boom)
        assert mac_window.describe(object()).startswith("describe_failed")
        mac_window.log_card_state(object(), "stage")     # must not raise

    def test_presenting_a_card_logs_each_stage(self, native, qapp, monkeypatch, caplog):
        monkeypatch.setattr(mac_window, "make_passive", lambda w: True)
        monkeypatch.setattr(mac_window, "order_front_without_activating", lambda w: True)
        caplog.set_level(logging.INFO)
        card = ToastPopup()

        assert card.present("Title", "Body", "info") is True

        for stage in ("passive_ok", "ordered_front", "after_show"):
            assert f"MACDIAG_CARD stage={stage}" in caplog.text, stage
        card.hide()
        card.deleteLater()

    def test_a_card_that_could_not_be_made_passive_logs_why_and_still_returns_false(
        self, native, qapp, monkeypatch, caplog
    ):
        monkeypatch.setattr(mac_window, "make_passive", lambda w: False)
        caplog.set_level(logging.INFO)
        card = ToastPopup()

        assert card.present("Title", "Body", "info") is False

        assert "MACDIAG_CARD stage=passive_failed" in caplog.text
        assert not card.isVisible()
        card.deleteLater()

    def test_the_failing_step_is_named_in_the_error(self, qapp, monkeypatch, caplog):
        monkeypatch.setattr(mac_window, "is_macos", lambda: True)

        class Window(_FakeNSWindow):
            def isKindOfClass_(self, cls): return False
            def setHidesOnDeactivate_(self, value): raise RuntimeError("AppKit said no")

        monkeypatch.setattr(mac_window, "_ns_window", lambda widget: Window())
        appkit = types.ModuleType("AppKit")
        appkit.NSPanel = object
        monkeypatch.setitem(sys.modules, "AppKit", appkit)

        assert mac_window.make_passive(object()) is False
        assert "failed at: hidesOnDeactivate" in caplog.text

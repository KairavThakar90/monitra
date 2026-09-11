"""
The window opens at a size this machine can actually show, and reopens where
the user left it.

Two defects behind this:

* `resize(1280, 800)` ran unconditionally. A 1366x768 laptop has a work area
  about 728px tall, so the window opened 72px taller than the desktop and the
  status bar along its bottom edge was never visible -- and could not be
  dragged into view, because the title bar was already at the top.
* `setMinimumSize(1024, 680)` ran unconditionally too. On the same laptop at
  125% scaling the logical work area is roughly 1092x578, which is *shorter*
  than the declared minimum. Qt honours a minimum size, so there was no size
  the user could resize the window to that fitted their screen.

Both are now clamped to the work area, and the geometry is remembered between
runs -- but only when it still lands on a screen that is plugged in.
"""
import pytest
from PySide6.QtCore import QByteArray, QRect

import main as main_module


@pytest.fixture(autouse=True)
def isolated_settings(qapp, tmp_path, monkeypatch):
    """Keep these tests out of the developer's real registry/ini settings."""
    from PySide6.QtCore import QSettings

    store = QSettings(str(tmp_path / "window.ini"), QSettings.Format.IniFormat)
    monkeypatch.setattr(main_module, "window_settings", lambda: store)
    return store


class FakeWindow:
    """Just enough QMainWindow surface for the sizing logic.

    The logic under test is about arithmetic against the work area and about
    which branch is taken; driving a real window would make the test depend on
    the developer's own monitor.
    """

    def __init__(self, restore_ok=True, frame=QRect(100, 100, 1280, 800)):
        self.minimum = None
        self.size = None
        self.moved_to = None
        self._restore_ok = restore_ok
        self._frame = frame
        self.restored_with = None

    # -- the QMainWindow methods _apply_window_sizing uses
    def setMinimumSize(self, width, height):
        self.minimum = (width, height)

    def resize(self, width, height):
        self.size = (width, height)

    def rect(self):
        return QRect(0, 0, *(self.size or (0, 0)))

    def move(self, point):
        self.moved_to = point

    def restoreGeometry(self, data):
        self.restored_with = data
        return self._restore_ok

    def frameGeometry(self):
        return self._frame

    # -- the methods under test, bound to this stand-in
    _apply_window_sizing = main_module.MainWindow._apply_window_sizing
    _restore_geometry = main_module.MainWindow._restore_geometry


def _use_work_area(monkeypatch, rect, on_screen=True):
    monkeypatch.setattr(main_module, "available_desktop_rect", lambda: rect)
    monkeypatch.setattr(main_module, "geometry_is_on_a_screen", lambda _rect: on_screen)


class TestFirstRun:
    def test_a_roomy_desktop_gets_the_intended_size(self, qapp, monkeypatch):
        _use_work_area(monkeypatch, QRect(0, 0, 2560, 1400))
        window = FakeWindow()

        window._apply_window_sizing()

        assert window.size == (main_module.DEFAULT_WINDOW_WIDTH,
                               main_module.DEFAULT_WINDOW_HEIGHT)
        assert window.minimum == (main_module.MINIMUM_WINDOW_WIDTH,
                                  main_module.MINIMUM_WINDOW_HEIGHT)

    def test_a_1366x768_laptop_never_opens_taller_than_its_work_area(self, qapp, monkeypatch):
        """The defect: 800px tall on a 728px work area put the status bar
        under the taskbar with no way to recover it."""
        work_area = QRect(0, 0, 1366, 728)
        _use_work_area(monkeypatch, work_area)
        window = FakeWindow()

        window._apply_window_sizing()

        assert window.size[1] <= work_area.height()
        assert window.size == (1280, 728)

    def test_a_scaled_laptop_can_still_shrink_the_window_to_fit(self, qapp, monkeypatch):
        """1366x768 at 125% is about 1092x578 logical -- shorter than the
        declared 680px minimum, which Qt would otherwise enforce."""
        work_area = QRect(0, 0, 1092, 578)
        _use_work_area(monkeypatch, work_area)
        window = FakeWindow()

        window._apply_window_sizing()

        # The width already fitted; only the height had to come down.
        assert window.minimum == (main_module.MINIMUM_WINDOW_WIDTH, 578)
        assert window.size == (1092, 578)

    def test_a_desktop_narrower_than_the_minimum_clamps_both_axes(self, qapp, monkeypatch):
        _use_work_area(monkeypatch, QRect(0, 0, 900, 540))
        window = FakeWindow()

        window._apply_window_sizing()

        assert window.minimum == (900, 540)
        assert window.size == (900, 540)

    def test_the_window_is_centred_on_the_work_area(self, qapp, monkeypatch):
        _use_work_area(monkeypatch, QRect(0, 0, 2000, 1200))
        window = FakeWindow()

        window._apply_window_sizing()

        assert window.moved_to is not None

    def test_a_headless_host_reporting_no_screen_is_not_clamped_to_zero(self, qapp, monkeypatch):
        """`None` means "do not clamp", not "the desktop is 0x0"."""
        _use_work_area(monkeypatch, None)
        window = FakeWindow()

        window._apply_window_sizing()

        assert window.minimum == (main_module.MINIMUM_WINDOW_WIDTH,
                                  main_module.MINIMUM_WINDOW_HEIGHT)
        assert window.size == (main_module.DEFAULT_WINDOW_WIDTH,
                               main_module.DEFAULT_WINDOW_HEIGHT)
        assert window.moved_to is None


class TestRememberedGeometry:
    def test_a_remembered_geometry_is_reapplied(self, qapp, monkeypatch, isolated_settings):
        _use_work_area(monkeypatch, QRect(0, 0, 2560, 1400))
        isolated_settings.setValue(
            main_module.SETTINGS_GEOMETRY_KEY, QByteArray(b"saved-geometry")
        )
        window = FakeWindow()

        window._apply_window_sizing()

        assert window.restored_with == QByteArray(b"saved-geometry")
        assert window.size is None, "the default size must not overwrite it"

    def test_nothing_remembered_falls_through_to_the_default(self, qapp, monkeypatch):
        _use_work_area(monkeypatch, QRect(0, 0, 2560, 1400))
        window = FakeWindow()

        window._apply_window_sizing()

        assert window.restored_with is None
        assert window.size is not None

    def test_a_geometry_qt_refuses_falls_through_to_the_default(
        self, qapp, monkeypatch, isolated_settings
    ):
        _use_work_area(monkeypatch, QRect(0, 0, 2560, 1400))
        isolated_settings.setValue(
            main_module.SETTINGS_GEOMETRY_KEY, QByteArray(b"not really geometry")
        )
        window = FakeWindow(restore_ok=False)

        window._apply_window_sizing()

        assert window.size is not None

    def test_a_window_left_on_an_unplugged_monitor_is_recentred(
        self, qapp, monkeypatch, isolated_settings
    ):
        """Restoring onto a screen that is no longer there is a window the
        user can neither see nor drag back."""
        _use_work_area(monkeypatch, QRect(0, 0, 1920, 1040), on_screen=False)
        isolated_settings.setValue(
            main_module.SETTINGS_GEOMETRY_KEY, QByteArray(b"on-the-old-second-screen")
        )
        window = FakeWindow()

        window._apply_window_sizing()

        assert window.size is not None, "fell back to the default size"
        assert window.moved_to is not None, "and was placed on a real screen"


class TestSavingGeometry:
    def test_a_minimised_window_is_not_recorded(self, qapp, isolated_settings):
        """Its geometry is not the arrangement the user made, and saving it
        would bring the window back somewhere they never put it."""

        class Minimised:
            isMinimized = staticmethod(lambda: True)
            isVisible = staticmethod(lambda: True)
            saveGeometry = staticmethod(lambda: QByteArray(b"minimised"))
            _remember_geometry = main_module.MainWindow._remember_geometry

        Minimised()._remember_geometry()

        assert isolated_settings.value(main_module.SETTINGS_GEOMETRY_KEY) is None

    def test_a_window_hidden_to_the_tray_is_not_recorded(self, qapp, isolated_settings):
        class Hidden:
            isMinimized = staticmethod(lambda: False)
            isVisible = staticmethod(lambda: False)
            saveGeometry = staticmethod(lambda: QByteArray(b"hidden"))
            _remember_geometry = main_module.MainWindow._remember_geometry

        Hidden()._remember_geometry()

        assert isolated_settings.value(main_module.SETTINGS_GEOMETRY_KEY) is None

    def test_a_visible_window_is_recorded(self, qapp, isolated_settings):
        class Visible:
            isMinimized = staticmethod(lambda: False)
            isVisible = staticmethod(lambda: True)
            saveGeometry = staticmethod(lambda: QByteArray(b"arranged"))
            _remember_geometry = main_module.MainWindow._remember_geometry

        Visible()._remember_geometry()

        assert isolated_settings.value(main_module.SETTINGS_GEOMETRY_KEY) == QByteArray(b"arranged")

"""
`toast_identity` -- Monitra's own name and logo at the top of every Windows notification.

The registry is faked (the real one is exercised by `tests/e2e/test_native_toast_e2e.py`, on a real
desktop, with a throwaway identity that it removes again). What is pinned here is the decision and the
file: what is written, that it is written only when it is wrong, that the icon is a real .ico with a frame
at each size, drawn natively in the small-size mark, at a backslashed path, and that nothing the machine does can stop the application starting.
"""
from __future__ import annotations

import os
import struct
import sys

import pytest
from PySide6.QtGui import QImage

from background_services.notifications import toast_identity

APP = "Monitra"
NAME = "Monitra — Staff Management"

# Captured at import, before the autouse fixture in conftest.py redirects it.
_REAL_ICON_PATH = toast_identity.icon_path


@pytest.fixture
def windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")


@pytest.fixture
def registry(monkeypatch):
    """A fake registry: {app_id: {name: value}}; every write is recorded."""
    store: dict = {}
    writes: list = []

    monkeypatch.setattr(toast_identity, "_read_values", lambda app_id: dict(store.get(app_id, {})))

    def write(app_id, values):
        writes.append((app_id, dict(values)))
        store.setdefault(app_id, {}).update(values)

    monkeypatch.setattr(toast_identity, "_write_values", write)
    return SimpleStore(store, writes)


class SimpleStore:
    def __init__(self, store, writes):
        self.store, self.writes = store, writes


def test_the_name_and_the_icon_are_registered(qapp, windows, registry, tmp_path):
    icon = tmp_path / "notification-icon.ico"

    assert toast_identity.register(APP, NAME, icon) is True

    assert registry.writes == [(APP, {"DisplayName": NAME, "IconUri": str(icon)})]
    assert icon.is_file()


def _frames(path):
    """The frames of an .ico as {size: QImage}, read from the container itself."""
    data = path.read_bytes()
    reserved, kind, count = struct.unpack_from("<HHH", data, 0)
    assert (reserved, kind) == (0, 1)
    frames = {}
    for index in range(count):
        width, height, _c, _r, _planes, _bits, length, offset = struct.unpack_from("<BBBBHHII", data, 6 + 16 * index)
        image = QImage.fromData(data[offset:offset + length], "PNG")
        assert not image.isNull(), f"frame {index} is not a PNG"
        assert (image.width(), image.height()) == (width, height)
        frames[width] = image
    return frames


def test_the_icon_is_an_ico_with_a_native_frame_for_every_size_the_header_can_ask_for(qapp, windows, registry, tmp_path):
    icon = tmp_path / "notification-icon.ico"
    toast_identity.register(APP, NAME, icon)

    frames = _frames(icon)

    assert sorted(frames) == sorted(toast_identity.ICON_SIZES)
    assert {16, 20, 24, 32} <= set(frames), "the header is 16 px at 100% scaling, 24 at 150%"
    for size, image in frames.items():
        assert image.width() == image.height() == size
        assert image.pixelColor(0, 0).alpha() == 0                  # the mark on a transparent ground


def test_each_frame_is_the_mark_drawn_at_that_size_not_a_larger_one_left_to_windows_to_shrink(qapp, windows, registry, tmp_path):
    """A single large PNG was shrunk by Windows into a fuzzy blob in the header: photographed on a real toast."""
    from core import branding

    icon = tmp_path / "notification-icon.ico"
    toast_identity.register(APP, NAME, icon)

    for size, image in _frames(icon).items():
        assert image == branding.logo_small_pixmap(size).toImage().convertToFormat(image.format()), size


def test_the_old_single_png_is_removed_once_the_icon_is_in_place(qapp, windows, registry, tmp_path):
    old = tmp_path / "notification-icon.png"
    old.write_bytes(b"what earlier builds wrote")

    toast_identity.register(APP, NAME, tmp_path / "notification-icon.ico")

    assert not old.exists()
    assert (tmp_path / "notification-icon.ico").is_file()


def test_an_icon_given_the_old_name_is_not_deleted_by_the_cleanup(qapp, windows, registry, tmp_path):
    icon = tmp_path / "notification-icon.png"

    toast_identity.register(APP, NAME, icon)

    assert icon.is_file()


@pytest.mark.skipif(sys.platform != "win32", reason="the value is a Windows path")
def test_the_icon_path_is_backslashed_because_windows_draws_no_icon_for_a_slashed_one(qapp, registry, tmp_path):
    """Measured on a real Windows 11 toast: `C:/Users/...` is accepted and nothing is drawn."""
    slashed = str(tmp_path / "notification-icon.ico").replace("\\", "/")

    toast_identity.register(APP, NAME, type(tmp_path)(slashed))

    (_, values), = registry.writes
    assert "/" not in values["IconUri"]
    assert os.path.isfile(values["IconUri"])


def test_nothing_is_written_when_the_registration_is_already_right(qapp, windows, registry, tmp_path):
    icon = tmp_path / "notification-icon.ico"
    toast_identity.register(APP, NAME, icon)
    registry.writes.clear()

    assert toast_identity.register(APP, NAME, icon) is True

    assert registry.writes == []


def test_a_registration_that_is_wrong_is_corrected(qapp, windows, registry, tmp_path):
    icon = tmp_path / "notification-icon.ico"
    registry.store[APP] = {"DisplayName": "Monitra", "IconUri": r"C:\somewhere\else.png"}

    toast_identity.register(APP, NAME, icon)

    assert registry.store[APP] == {"DisplayName": NAME, "IconUri": str(icon)}


def test_a_changed_mark_is_written_again_and_an_unchanged_one_is_not(qapp, windows, registry, tmp_path):
    icon = tmp_path / "notification-icon.ico"
    toast_identity.register(APP, NAME, icon)
    first = icon.stat().st_mtime_ns

    toast_identity.register(APP, NAME, icon)
    assert icon.stat().st_mtime_ns == first                      # identical bytes: not rewritten

    icon.write_bytes(b"an out-of-date mark")
    toast_identity.register(APP, NAME, icon)
    assert sorted(_frames(icon)) == sorted(toast_identity.ICON_SIZES)   # replaced with the current one
    assert not list(tmp_path.glob("*.tmp"))                       # and no half-written file is left behind


def test_an_icon_that_cannot_be_written_still_registers_the_name(qapp, windows, registry, tmp_path):
    blocked = tmp_path / "a-file"
    blocked.write_text("not a directory")

    assert toast_identity.register(APP, NAME, blocked / "notification-icon.ico") is True

    assert registry.writes == [(APP, {"DisplayName": NAME})]      # the header reads "Monitra — Staff Management"; no logo


def test_it_does_nothing_off_windows(qapp, monkeypatch, registry, tmp_path):
    monkeypatch.setattr(sys, "platform", "darwin")

    assert toast_identity.register(APP, NAME, tmp_path / "notification-icon.ico") is False

    assert registry.writes == [] and not list(tmp_path.iterdir())


def test_a_registry_that_refuses_is_a_false_not_a_crash(qapp, windows, monkeypatch, tmp_path):
    monkeypatch.setattr(toast_identity, "_read_values", lambda app_id: {})

    def refuse(app_id, values):
        raise PermissionError("the key is locked by policy")

    monkeypatch.setattr(toast_identity, "_write_values", refuse)

    assert toast_identity.register(APP, NAME, tmp_path / "notification-icon.ico") is False


def test_the_default_icon_lives_in_the_data_directory_not_the_install_folder(qapp, monkeypatch, tmp_path):
    from core import paths

    monkeypatch.setenv("MONITRA_DATA_DIR", str(tmp_path))
    paths.reset_cache()
    try:
        assert _REAL_ICON_PATH() == tmp_path / toast_identity.ICON_FILE_NAME
    finally:
        monkeypatch.undo()
        paths.reset_cache()


def test_the_notification_service_registers_its_identity_when_it_starts(qapp, windows, monkeypatch):
    """Without it a toast that carries the Monitra circle is headed by the bare id: no name, no logo."""
    from unittest.mock import MagicMock

    from background_services.notifications import notification_service as module
    from version import NOTIFICATION_HEADER_NAME

    calls = []
    monkeypatch.setattr(toast_identity, "register", lambda app_id, name, icon=None: calls.append((app_id, name)) or True)
    monkeypatch.setattr(module, "set_windows_app_identity", lambda: None)
    monkeypatch.setattr(module.QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: False))

    service = module.NotificationService(MagicMock())
    service.on_start()

    assert calls == [(module.APP_USER_MODEL_ID, NOTIFICATION_HEADER_NAME)]
    service._dismiss_timer.stop()

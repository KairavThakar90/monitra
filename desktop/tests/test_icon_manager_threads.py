"""
IconManager builds every QPixmap on the GUI thread.

Its favicon and application-icon workers run on a thread pool. They used to
build the QPixmap there and emit it across threads -- undefined behaviour in
Qt, which surfaced as intermittent access violations on the GUI thread
during unrelated tests, whenever a slow favicon fetch landed while another
widget was pumping events. Now only a QImage, a path or None crosses the
thread boundary, and `_on_resolved` makes the pixmap where Qt allows it.
"""
from __future__ import annotations

import threading
import time

import pytest
from PySide6.QtCore import QThread
from PySide6.QtGui import QImage, QPixmap

from ui import icon_manager as icon_module
from ui.icon_manager import IconManager

#: The real workers, captured at import -- before any fixture runs. The
#: suite-wide autouse fixture in conftest keeps the pool idle for every other
#: test; these tests are about the workers themselves, so they put the real
#: ones back.
_REAL_FAVICON_WORKER = IconManager.__dict__["_fetch_favicon_worker"]
_REAL_APP_ICON_WORKER = IconManager.__dict__["_extract_app_icon_worker"]


@pytest.fixture
def manager(qapp, monkeypatch):
    monkeypatch.setattr(IconManager, "_fetch_favicon_worker", _REAL_FAVICON_WORKER)
    monkeypatch.setattr(IconManager, "_extract_app_icon_worker", _REAL_APP_ICON_WORKER)
    return IconManager()


def _pump(qapp, until, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if until():
            return True
        time.sleep(0.01)
    return until()


def test_a_favicon_fetched_on_a_worker_arrives_as_a_pixmap_built_on_the_gui_thread(
    qapp, manager, monkeypatch
):
    image = QImage(16, 16, QImage.Format.Format_ARGB32)
    image.fill(0xFF336699)
    built_on = []
    real_from_image = QPixmap.fromImage

    def recording_from_image(img, *args, **kwargs):
        built_on.append(QThread.currentThread())
        return real_from_image(img, *args, **kwargs)

    monkeypatch.setattr(QPixmap, "fromImage", staticmethod(recording_from_image))

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b"payload"

    monkeypatch.setattr(icon_module.urllib.request, "urlopen", lambda *a, **k: _Response())
    monkeypatch.setattr(
        QImage, "loadFromData",
        lambda self, data, *a, **k: (self.swap(QImage(image)) or True),
    )
    received = []
    manager.favicon_ready.connect(lambda domain, pixmap: received.append((domain, pixmap)))

    assert manager.get_favicon("example.com") is None  # not cached yet: fetch scheduled
    assert _pump(qapp, lambda: bool(received)), "the favicon never arrived"

    domain, pixmap = received[0]
    assert domain == "example.com" and not pixmap.isNull()
    assert built_on == [qapp.thread()], "the pixmap must be built on the GUI thread"
    cached = manager.get_favicon("example.com")
    assert cached is not None and cached.cacheKey() == pixmap.cacheKey(), "and cached there"


def test_a_failed_favicon_fetch_is_cached_as_absent_without_a_pixmap(qapp, manager, monkeypatch):
    def failing(*a, **k):
        raise OSError("no network")

    monkeypatch.setattr(icon_module.urllib.request, "urlopen", failing)
    received = []
    manager.favicon_ready.connect(lambda *a: received.append(a))

    manager.get_favicon("nowhere.invalid")
    assert _pump(qapp, lambda: "nowhere.invalid" in manager._favicon_cache)

    assert manager._favicon_cache["nowhere.invalid"] is None
    assert received == []


def test_the_workers_never_touch_a_pixmap(qapp, manager, monkeypatch):
    """Static guard: the pool-side methods reference no QPixmap at all."""
    import inspect

    for worker in (manager._fetch_favicon_worker, manager._extract_app_icon_worker,
                   icon_module._hicon_to_image):
        source = inspect.getsource(worker)
        for token in ("QPixmap(", "QPixmap.", ".pixmap("):
            assert token not in source, f"{worker.__name__} builds a pixmap off the GUI thread"


def test_monitras_own_icon_is_resolved_on_the_gui_thread(qapp, manager):
    received = []
    manager.app_icon_ready.connect(lambda key, pixmap: received.append((key, pixmap)))
    worker_thread = []
    original = manager._extract_app_icon_worker

    def spy(*args, **kwargs):
        worker_thread.append(threading.current_thread())
        return original(*args, **kwargs)

    manager._extract_app_icon_worker = spy

    assert manager.get_app_icon("Monitra") is None
    assert _pump(qapp, lambda: bool(received)), "the icon never arrived"

    key, pixmap = received[0]
    assert key == "monitra" and not pixmap.isNull()
    assert worker_thread and worker_thread[0] is not threading.main_thread()

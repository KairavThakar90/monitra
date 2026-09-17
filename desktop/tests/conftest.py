"""
Shared pytest fixtures for the Monitra desktop test suite.

Tests run headless. Every fixture that touches storage uses a temporary
database so a test can never mutate the developer's real ~/.monitra/cache.db.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# The desktop package is the import root.
DESKTOP_ROOT = Path(__file__).resolve().parent.parent
if str(DESKTOP_ROOT) not in sys.path:
    sys.path.insert(0, str(DESKTOP_ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("MONITRA_LOG_LEVEL", "WARNING")

if sys.platform == "darwin":
    # Realising a widget that carries a QGraphicsDropShadowEffect under the
    # offscreen platform on the macOS release runners kills the interpreter
    # inside Qt -- a bus error on Apple Silicon, a segmentation fault on
    # Intel -- before any assertion runs, and takes the rest of the suite
    # with it (first seen at the login window, then at the maintenance
    # toast's host window). The shadow is decoration: nothing this suite
    # asserts depends on it being painted, so on macOS every drop shadow is
    # constructed disabled and Qt paints the widget directly. The packaged
    # macOS application is unaffected; this file is never imported by it.
    from PySide6.QtWidgets import QGraphicsDropShadowEffect

    _shadow_init = QGraphicsDropShadowEffect.__init__

    def _disabled_shadow_init(self, *args, **kwargs):
        _shadow_init(self, *args, **kwargs)
        self.setEnabled(False)

    QGraphicsDropShadowEffect.__init__ = _disabled_shadow_init


@pytest.fixture(scope="session")
def qapp():
    """A single QApplication for the whole session."""
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def storage(tmp_path):
    """A StorageManager backed by a throwaway database."""
    from storage.manager import StorageManager

    manager = StorageManager(str(tmp_path / "test-cache.db"))
    yield manager
    manager.close()


@pytest.fixture
def cache(storage):
    from sync.local_cache import LocalCache

    return LocalCache(storage=storage)


@pytest.fixture
def runtime(qapp, tmp_path, monkeypatch):
    """
    A fully constructed ApplicationRuntime on a temporary database, with
    services started and torn down around the test.
    """
    from core.runtime import ApplicationRuntime
    from storage.manager import StorageManager

    manager = StorageManager(str(tmp_path / "runtime-cache.db"))
    rt = ApplicationRuntime(storage=manager)
    yield rt
    rt.shutdown(timeout_ms=2000)

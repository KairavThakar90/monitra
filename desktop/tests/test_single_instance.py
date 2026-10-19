"""
The single-instance guard, on both platforms.

Two Monitra processes at once is the packaged-build form of the duplicate
background implementation DO_NOT_DO.md records: two timers deriving elapsed
time, two sync services draining the same durable queue, two activity trackers
writing overlapping windows, and two writers on one SQLite database.

Windows holds a named mutex; macOS and Linux hold an `flock()` on a file in
the data directory. The Windows side is already covered by
`test_packaging.py` (the installer's `CheckForMutexes` name must match the
one the app takes). This file covers the POSIX side — the macOS guard — which
had no test of its own.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import single_instance


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """Point the data directory at a throwaway path for the lock file."""
    from core import paths

    monkeypatch.setenv("MONITRA_DATA_DIR", str(tmp_path))
    paths.reset_cache()
    yield tmp_path
    paths.reset_cache()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX flock path")
def test_a_second_instance_is_refused_on_posix(data_dir):
    first = single_instance._acquire_posix()
    assert first is not None, "the first instance must get the lock"
    try:
        # The second copy — a user double-clicking the .app while it is
        # already running in the menu bar — must not start tracking.
        assert single_instance._acquire_posix() is None
    finally:
        first.close()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX flock path")
def test_the_lock_is_released_when_the_holder_exits(data_dir):
    # flock is released by the OS when the file handle goes, including on a
    # crash or a kill. That is why there is no stale-lock cleanup to get
    # wrong: a leftover lock *file* is harmless, because the lock itself is
    # what is tested, not the file's existence.
    first = single_instance._acquire_posix()
    assert first is not None
    first.close()

    second = single_instance._acquire_posix()
    assert second is not None
    second.close()


def test_macos_uses_the_posix_lock(monkeypatch):
    """`acquire()` must route macOS to the flock path, not skip the guard."""
    called = {}

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(
        single_instance, "_acquire_posix",
        lambda: called.setdefault("posix", True) and "handle",
    )
    monkeypatch.setattr(
        single_instance, "_acquire_windows",
        lambda: pytest.fail("macOS must not take the Windows mutex path"),
    )

    assert single_instance.acquire() is True
    assert called.get("posix") is True


def test_a_lock_that_cannot_be_evaluated_still_lets_the_app_start(monkeypatch):
    # Refusing to launch because the *lock* failed would be a worse failure
    # than the duplicate it guards against.
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(
        single_instance, "_acquire_posix",
        lambda: (_ for _ in ()).throw(OSError("read-only data directory")),
    )

    assert single_instance.acquire() is True


if __name__ == "__main__":
    pytest.main([__file__])


# ═══ Activation: the second launch asks the first to show itself ══════════════
#
# The production report: a user whose Monitra was blocked by an idle popup they
# could not see launched it again, was told "Monitra is already running", and
# was left exactly where they were. The second launch must hand them over to
# the first instance, which restores its window and the popup.

def _pump(qapp, until, timeout=5.0):
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if until():
            return True
        time.sleep(0.02)
    return until()


def test_a_request_is_taken_by_the_listener_and_the_window_is_asked_for(qapp, data_dir):
    listener = single_instance.ActivationListener()
    asked = []
    listener.activation_requested.connect(lambda: asked.append(True))
    request = single_instance.activation_dir() / "activate-1-abcd.request"
    request.write_text("{}", encoding="utf-8")

    assert _pump(qapp, lambda: asked), "the listener never noticed the request"
    assert not request.exists(), "taking the request is the acknowledgement; it must be removed"
    listener.deleteLater()
    qapp.processEvents()


def test_request_activation_returns_true_once_the_request_is_taken(qapp, data_dir):
    """The requester's view: it waits for its file to disappear, bounded."""
    import threading
    import time

    directory = single_instance.activation_dir()

    def running_instance():
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            for path in directory.glob("*.request"):
                path.unlink()
                return
            time.sleep(0.02)

    consumer = threading.Thread(target=running_instance, daemon=True)
    consumer.start()
    assert single_instance.request_activation(timeout=3.0) is True
    consumer.join(timeout=3)
    assert list(directory.glob("*.request")) == []


def test_an_unanswered_request_is_withdrawn_and_reported_false(data_dir):
    """A hung first instance must not hold the user at a blank screen for
    ever, and must not find a stale request waiting when it recovers."""
    assert single_instance.request_activation(timeout=0.3) is False
    assert list(single_instance.activation_dir().glob("*.request")) == []


def test_a_stale_request_is_cleared_at_startup_without_being_acted_on(qapp, data_dir):
    """Left by a launch that gave up (or crashed) before this instance was
    listening. The lock holder owns the directory, so clearing it is safe;
    restoring the window for nobody would not be."""
    import os
    import time

    directory = single_instance.activation_dir()
    stale = directory / "activate-9-stale.request"
    stale.write_text("{}", encoding="utf-8")
    old = time.time() - single_instance.REQUEST_MAX_AGE_SECONDS - 5
    os.utime(stale, (old, old))

    asked = []
    listener = single_instance.ActivationListener()
    listener.activation_requested.connect(lambda: asked.append(True))
    qapp.processEvents()

    assert not stale.exists()
    assert asked == []
    listener.deleteLater()
    qapp.processEvents()


def test_a_fresh_request_that_raced_startup_is_honoured(qapp, data_dir):
    directory = single_instance.activation_dir()
    (directory / "activate-9-fresh.request").write_text("{}", encoding="utf-8")
    asked = []
    listener = single_instance.ActivationListener()
    listener.activation_requested.connect(lambda: asked.append(True))
    assert _pump(qapp, lambda: asked, timeout=2.0), "a request that raced start-up was dropped"
    assert not (directory / "activate-9-fresh.request").exists()
    listener.deleteLater()
    qapp.processEvents()


def test_the_second_launch_exits_quietly_when_the_first_takes_over(monkeypatch):
    import main as main_module

    monkeypatch.setattr(single_instance, "request_activation", lambda timeout=None: True)
    monkeypatch.setattr(
        main_module.QMessageBox, "information",
        lambda *a, **k: pytest.fail("the user was told to look in the tray although the window is coming back"),
    )
    assert main_module._yield_to_running_instance() == 0


def test_the_second_launch_explains_itself_when_the_first_does_not_answer(monkeypatch):
    import main as main_module

    shown = []
    monkeypatch.setattr(single_instance, "request_activation", lambda timeout=None: False)
    monkeypatch.setattr(main_module.QMessageBox, "information", lambda parent, title, text: shown.append(text))
    assert main_module._yield_to_running_instance() == 0
    assert len(shown) == 1
    assert "did not answer" in shown[0]
    assert "notification area" in shown[0], "the tray advice must survive: it is still the way back"


def test_the_message_is_honest_about_whether_it_asked():
    assert "did not answer" not in single_instance.already_running_message()
    assert "did not answer" in single_instance.already_running_message(unanswered=True)

"""
A listener that is installed and not hearing what the OS reports.

The October evidence: on a Mac, days with 13-100 keystrokes and 4,000-26,000
pointer events over a working day, beside days with 6,000-9,000 keystrokes
and 560,000 pointer events. The low days are what a listener hears when it is
only delivered the events aimed at Monitra's own window -- and every such
window was recorded, uploaded and averaged as a quiet minute, because counts
alone cannot tell "the user did nothing" from "the listener heard nothing".

The check compares, per window, the seconds in which the OS's own idle clock
said the user produced input (`_os_active`) with the seconds in which the
counter heard anything (`_heard`). These tests drive that comparison with
deterministic stand-ins; nothing here says what a real listener hears.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from background_services.activity import input_access
from background_services.activity.activity_service import ActivityService


def _counts(keystrokes=0, clicks=0, movements=0):
    return {"keystrokes": keystrokes, "clicks": clicks, "movements": movements}


class _Runtime:
    def __init__(self):
        self.timer = MagicMock()
        self.timer.active_session.return_value = {"entry_id": 101}
        self.notifications = MagicMock()


@pytest.fixture
def service(monkeypatch):
    monkeypatch.setattr(input_access, "_is_macos", lambda: True)
    monkeypatch.setattr(input_access, "_preflight", lambda: True)
    monkeypatch.setattr(input_access, "_prompt_requested", False)
    svc = ActivityService(_Runtime(), MagicMock())
    counter = MagicMock()
    counter.supported = True
    counter.counting = True
    counter.failure = None
    counter.start.return_value = True
    counter.restart.return_value = True
    counter.snapshot_and_reset.return_value = _counts()
    counter.drain_watched_presses.return_value = {}
    counter.diagnostics.return_value = {"installed": True, "received": {}}
    svc._counter = counter
    probe = MagicMock()
    probe.supported = True
    probe.sample.return_value = {"active": True, "mouse": True}   # the OS saw input
    probe.idle_seconds.return_value = 0.4
    probe.last_input_age.return_value = 0.4
    probe.diagnostics.return_value = {"supported": True}
    svc._probe = probe
    svc.log = MagicMock()
    svc.changes = []
    svc.capture_status_changed.connect(svc.changes.append)
    return svc


def _lines(mock_method, marker):
    return [c for c in mock_method.call_args_list if c.args and marker in c.args[0]]


def _run_window(service, heard_seconds: int, seconds: int = ActivityService.WINDOW_SECONDS):
    """One full window: the OS sees input every second; the counter hears
    something in `heard_seconds` of them (a trickle: a click per second)."""
    for i in range(seconds):
        service._counter.snapshot_and_reset.return_value = (
            _counts(clicks=1) if i < heard_seconds else _counts()
        )
        service.tick()


# ── Detection ────────────────────────────────────────────────────────────────

def test_a_window_the_os_saw_and_the_listener_did_not_restarts_the_listener(service):
    service.start_tracker({"entry_id": 101})
    _run_window(service, heard_seconds=0)

    assert service._counter.restart.call_count == 1
    deaf = _lines(service.log.warning, "ACTIVITY_LISTENER_DEAF")
    assert len(deaf) == 1
    assert _lines(service.log.warning, "ACTIVITY_LISTENER_RESTARTED")
    # One deaf window is a restart, not yet a notice to the user.
    assert service.capture_status()["blocked"] is None
    assert service.runtime.notifications.notify.call_count == 0
    # And the window itself is recorded exactly as measured -- nothing invented.
    kwargs = service._cache.save_activity_sample.call_args.kwargs
    assert kwargs["mouse_clicks"] == 0
    assert kwargs["active_seconds"] == ActivityService.WINDOW_SECONDS
    assert kwargs["activity_percent"] == 0


def test_a_second_consecutive_deaf_window_names_the_state_to_the_user(service):
    service.start_tracker({"entry_id": 101})
    _run_window(service, heard_seconds=0)
    _run_window(service, heard_seconds=0)

    status = service.capture_status()
    assert status["blocked"] == "not_in_effect"
    assert "consecutive windows" in status["detail"]
    notify = service.runtime.notifications.notify
    assert notify.call_count == 1
    assert notify.call_args.kwargs["key"] == "activity-capture:not_in_effect"
    assert notify.call_args.kwargs["link"] == input_access.SETTINGS_URL   # macOS: the switch
    assert "not receiving" in notify.call_args.args[0]
    assert service.changes == ["not_in_effect"]
    assert service._counter.restart.call_count == 1   # restarted once, not every window


def test_the_october_trickle_is_deaf(service):
    """The real shape: a working minute in which the listener heard only the
    few seconds the user spent in Monitra's own window."""
    service.start_tracker({"entry_id": 101})
    _run_window(service, heard_seconds=8)     # 8 of 60 OS-active seconds heard
    assert service._counter.restart.call_count == 1


def test_a_normally_heard_window_is_not_deaf(service):
    """Someone reading for part of a minute hears far more than a fifth of
    the OS-active seconds; a healthy listener must never be restarted."""
    service.start_tracker({"entry_id": 101})
    _run_window(service, heard_seconds=30)
    assert service._counter.restart.call_count == 0
    assert _lines(service.log.warning, "ACTIVITY_LISTENER_DEAF") == []


def test_a_window_with_little_os_input_proves_nothing(service):
    """The user was away for most of the minute: the OS saw 10 s of input
    and the listener heard none. Too little to judge by, in either direction."""
    service.start_tracker({"entry_id": 101})
    active = iter([True] * 10 + [False] * 50)
    service._probe.sample.side_effect = lambda w: {"active": next(active), "mouse": False}
    for _ in range(ActivityService.WINDOW_SECONDS):
        service.tick()
    assert service._counter.restart.call_count == 0
    assert service.capture_status()["blocked"] is None


def test_an_idle_window_does_not_clear_a_named_deaf_state(service):
    service.start_tracker({"entry_id": 101})
    _run_window(service, heard_seconds=0)
    _run_window(service, heard_seconds=0)
    assert service.capture_status()["blocked"] == "not_in_effect"

    service._probe.sample.return_value = {"active": False, "mouse": False}   # user away
    _run_window(service, heard_seconds=0)
    assert service.capture_status()["blocked"] == "not_in_effect"


def test_hearing_again_clears_the_state(service):
    service.start_tracker({"entry_id": 101})
    _run_window(service, heard_seconds=0)
    _run_window(service, heard_seconds=0)
    assert service.capture_status()["blocked"] == "not_in_effect"

    _run_window(service, heard_seconds=45)
    assert service.capture_status()["blocked"] is None
    assert service.changes == ["not_in_effect", ""]
    assert _lines(service.log.info, "ACTIVITY_LISTENER_HEARING")
    assert _lines(service.log.info, "ACTIVITY_CAPTURE_RESTORED")
    # The notice was given once for the episode.
    assert service.runtime.notifications.notify.call_count == 1


def test_a_long_deaf_stretch_is_told_once(service):
    service.start_tracker({"entry_id": 101})
    for _ in range(12):
        _run_window(service, heard_seconds=0)
    assert service.runtime.notifications.notify.call_count == 1
    assert service._counter.restart.call_count == 1
    assert len(_lines(service.log.warning, "ACTIVITY_LISTENER_DEAF")) == 12  # the log keeps the record


def test_a_failed_restart_falls_through_to_the_blocked_states(service, monkeypatch):
    monkeypatch.setattr(input_access, "_preflight", lambda: False)
    service._counter.restart.return_value = False
    service._counter.failure = "tap_refused"
    service.start_tracker({"entry_id": 101})
    _run_window(service, heard_seconds=0)
    assert service.capture_status()["blocked"] == "denied"


# ── Where the check must stay quiet ──────────────────────────────────────────

def test_without_a_presence_probe_nothing_can_be_judged(service):
    """macOS before the clock is trusted, or any platform without a probe:
    `sample()` is None, the OS has said nothing, and the counter's silence
    is only the counter's silence."""
    service._probe.sample.return_value = None
    service.start_tracker({"entry_id": 101})
    _run_window(service, heard_seconds=0)
    assert service._counter.restart.call_count == 0
    assert service.capture_status()["blocked"] is None


def test_a_listener_that_is_not_installed_is_a_different_state(service):
    service._counter.counting = False
    service.start_tracker({"entry_id": 101})
    _run_window(service, heard_seconds=0)
    assert service._counter.restart.call_count == 0


def test_a_short_tail_window_is_not_judged(service):
    service.start_tracker({"entry_id": 101})
    for _ in range(ActivityService.DEAF_MIN_OS_ACTIVE_SECONDS - 1):
        service.tick()
    service.stop_tracker()
    assert service._counter.restart.call_count == 0


def test_a_new_session_starts_with_a_clean_slate(service):
    service.start_tracker({"entry_id": 101})
    _run_window(service, heard_seconds=0)
    service.stop_tracker()
    service.start_tracker({"entry_id": 102})
    _run_window(service, heard_seconds=0)
    # The run restarted: this is deaf window 1 of the new session -> a restart, no notice yet.
    assert service._counter.restart.call_count == 2
    assert service.capture_status()["blocked"] is None


def test_the_window_log_line_carries_both_sides_of_the_comparison(service):
    service.start_tracker({"entry_id": 101})
    _run_window(service, heard_seconds=8)
    lines = [c for c in service.log.info.call_args_list if c.args and c.args[0].startswith("activity window:")]
    assert lines, "the per-window line is the support record"
    rendered = lines[0].args[0] % lines[0].args[1:]
    assert "os_active=60" in rendered
    assert "heard=8" in rendered


def test_on_windows_the_notice_has_no_settings_link(monkeypatch, service):
    monkeypatch.setattr(input_access, "_is_macos", lambda: False)
    service.start_tracker({"entry_id": 101})
    _run_window(service, heard_seconds=0)
    _run_window(service, heard_seconds=0)
    kwargs = service.runtime.notifications.notify.call_args.kwargs
    assert kwargs["key"] == "activity-capture:not_in_effect"
    assert kwargs["link"] is None
    assert "administrator" in service.runtime.notifications.notify.call_args.args[0]

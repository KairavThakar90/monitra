"""
Capture blocked: a denied permission must never look like a quiet user.

A Mac without Input Monitoring ran a timer all day, the tap was refused, no
window was ever recorded, and the card said "Not tracking yet" -- on screen and
in the data indistinguishable from a user who sat still. These pin the
replacement: the state is named (`input_access.classify`), told to the user
once with the switch's location, exposed through `capture_status()`, retried
every `CAPTURE_RETRY_SECONDS` while the timer runs, and -- once counting -- a
whole window with nothing heard is diagnosed with the OS's own idle clock
beside the zero.

macOS is faked throughout; the counter, probe and cache are deterministic
stand-ins, as in test_activity_service_counts.py. Nothing here says what a
real Mac answers.
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


def _service(*, counter_ok: bool, failure, probe_sample):
    service = ActivityService(_Runtime(), MagicMock())
    counter = MagicMock()
    counter.supported = counter_ok
    counter.counting = counter_ok
    counter.failure = failure
    counter.start.return_value = counter_ok
    counter.snapshot_and_reset.return_value = _counts()
    counter.drain_watched_presses.return_value = {}
    counter.diagnostics.return_value = {
        "installed": counter_ok, "events_seen": 0, "re_enabled": 0, "os_idle_seconds": 3.0,
    }
    service._counter = counter
    probe = MagicMock()
    probe.supported = probe_sample is not None
    probe.sample.return_value = probe_sample
    probe.idle_seconds.return_value = None
    service._probe = probe
    service.log = MagicMock()
    service.changes = []
    service.capture_status_changed.connect(service.changes.append)
    return service


@pytest.fixture
def macos(monkeypatch):
    monkeypatch.setattr(input_access, "_is_macos", lambda: True)
    monkeypatch.setattr(input_access, "_preflight", lambda: False)
    monkeypatch.setattr(input_access, "_prompt_requested", False)
    requested = []
    monkeypatch.setattr(input_access, "_request", lambda: requested.append(1) or False)
    return requested


@pytest.fixture
def blocked_service(macos):
    """The macOS shape with the tap refused: probe unsupported, permission off."""
    return _service(counter_ok=False, failure="tap_refused", probe_sample=None)


def _grant(service):
    """The permission is granted: the *next* `start()` installs the tap.

    Like the real counter, `supported`/`counting` flip only inside `start()`
    -- the service has no other way of learning that the switch was turned
    on, which is the whole reason it retries.
    """
    counter = service._counter

    def _start():
        counter.supported = True
        counter.counting = True
        counter.failure = None
        return True

    counter.start.side_effect = _start


def _refuse(service):
    """The next `start()` is refused again."""
    counter = service._counter

    def _start():
        counter.supported = False
        counter.counting = False
        counter.failure = "tap_refused"
        return False

    counter.start.side_effect = _start


def _lines(mock_method, marker):
    """The log lines a mocked logger method was given, rendered as the log
    file would show them."""
    rendered = []
    for call in mock_method.call_args_list:
        message = call.args[0] if call.args else ""
        try:
            rendered.append(message % call.args[1:] if len(call.args) > 1 else message)
        except (TypeError, ValueError):
            rendered.append(str(call))
    return [line for line in rendered if marker in line]


# ── Blocked ──────────────────────────────────────────────────────────────────

def test_a_refused_tap_is_named_told_once_and_records_nothing(blocked_service, macos):
    service = blocked_service
    service.start_tracker({"entry_id": 101})

    assert service.capture_status() == {
        "counting": False, "blocked": "denied",
        "detail": "CGPreflightListenEventAccess=False", "tracking": True,
    }
    notify = service.runtime.notifications.notify
    assert notify.call_count == 1
    assert notify.call_args.kwargs["key"] == "activity-capture:denied"
    assert notify.call_args.kwargs["link"] == input_access.SETTINGS_URL
    assert "Input Monitoring" in notify.call_args.args[0]
    assert macos == [1]                      # the system prompt was asked for, once
    assert service.changes == ["denied"]
    assert len(_lines(service.log.warning, "ACTIVITY_CAPTURE_BLOCKED")) == 1

    for _ in range(30):
        service.tick()
    # No window is sampled or written: an absence of measurement, not a 0%.
    assert service._sampled == 0
    assert service._cache.save_activity_sample.call_args_list == []
    assert service.live_window_totals().has_measurement is False


def test_the_same_blocked_state_is_not_announced_twice(blocked_service):
    service = blocked_service
    service.start_tracker({"entry_id": 101})
    service.stop_tracker()
    service.start_tracker({"entry_id": 102})
    assert service.runtime.notifications.notify.call_count == 1
    assert service.changes == ["denied"]


def test_a_changed_blocked_state_is_announced(blocked_service, monkeypatch):
    service = blocked_service
    service.start_tracker({"entry_id": 101})
    # The switch is flipped; macOS says granted but still refuses this
    # process a tap: a different state, with different advice.
    monkeypatch.setattr(input_access, "_preflight", lambda: True)
    service.stop_tracker()
    service.start_tracker({"entry_id": 102})

    assert service.capture_status()["blocked"] == "restart_required"
    keys = [c.kwargs["key"] for c in service.runtime.notifications.notify.call_args_list]
    assert keys == ["activity-capture:denied", "activity-capture:restart_required"]
    # Restarting is the fix, so there is no Settings link to send them to.
    assert service.runtime.notifications.notify.call_args.kwargs["link"] is None
    assert service.changes == ["denied", "restart_required"]


def test_a_service_without_a_notification_service_still_names_the_state(blocked_service):
    service = blocked_service
    del service.runtime.notifications
    service.start_tracker({"entry_id": 101})  # must not raise
    assert service.capture_status()["blocked"] == "denied"


def test_off_macos_a_failed_counter_is_unavailable_and_sends_nobody_to_settings(monkeypatch):
    monkeypatch.setattr(input_access, "_is_macos", lambda: False)
    service = _service(
        counter_ok=False, failure="pynput_missing",
        probe_sample={"active": True, "mouse": False},
    )
    service.start_tracker({"entry_id": 101})

    assert service.capture_status()["blocked"] == "unavailable"
    kwargs = service.runtime.notifications.notify.call_args.kwargs
    assert kwargs["key"] == "activity-capture:unavailable"
    assert kwargs["link"] is None


# ── Retry and recovery ───────────────────────────────────────────────────────

def test_the_counter_is_retried_once_a_minute_while_blocked(blocked_service):
    service = blocked_service
    service.start_tracker({"entry_id": 101})
    assert service._counter.start.call_count == 1

    for _ in range(ActivityService.CAPTURE_RETRY_SECONDS - 1):
        service.tick()
    assert service._counter.start.call_count == 1     # not yet
    service.tick()
    assert service._counter.start.call_count == 2     # the minute is up
    for _ in range(ActivityService.CAPTURE_RETRY_SECONDS):
        service.tick()
    assert service._counter.start.call_count == 3     # and again a minute later
    assert service.runtime.notifications.notify.call_count == 1  # still told only once


def test_a_permission_granted_mid_session_is_picked_up_without_restarting_the_timer(blocked_service):
    service = blocked_service
    service.start_tracker({"entry_id": 101})
    for _ in range(ActivityService.CAPTURE_RETRY_SECONDS - 1):
        service.tick()
    assert service._sampled == 0

    _grant(service)                       # the user turned Monitra on in Settings
    service.tick()                        # the retry tick installs the tap
    assert service.capture_status()["counting"] is True
    assert service.capture_status()["blocked"] is None
    assert service.changes == ["denied", ""]
    assert len(_lines(service.log.info, "ACTIVITY_CAPTURE_RESTORED")) == 1

    # From here the session measures; the retry tick itself sampled a second.
    service._counter.snapshot_and_reset.return_value = _counts(keystrokes=3)
    service.tick()
    assert service._sampled == 2
    assert service._keyboard_strokes == 3
    assert service.live_window_totals().has_measurement is True
    # Nothing was fabricated for the blocked minute: the window began at the grant.
    assert service._active == 1


def test_a_recovered_counter_is_not_started_again_every_minute(blocked_service):
    """Once counting, the retry path is off: `start()` is only called by the
    tracker lifecycle. (The counter's own guard makes a second call a no-op
    anyway -- see test_mac_input_tap -- but the service must not lean on it.)"""
    service = blocked_service
    _grant(service)
    service.start_tracker({"entry_id": 101})
    for _ in range(3 * ActivityService.CAPTURE_RETRY_SECONDS):
        service.tick()
    assert service._counter.start.call_count == 1
    assert service.capture_status()["blocked"] is None
    assert service.runtime.notifications.notify.call_count == 0
    assert service.changes == []


def test_a_counter_that_stops_working_between_sessions_is_noticed(blocked_service):
    service = blocked_service
    _grant(service)
    service.start_tracker({"entry_id": 101})
    service.stop_tracker()
    assert service.changes == []

    _refuse(service)
    service.start_tracker({"entry_id": 102})
    assert service.capture_status()["blocked"] == "denied"
    assert service.changes == ["denied"]


# ── Silent windows: a deaf listener must be distinguishable from an idle user ─

@pytest.fixture
def counting_service():
    return _service(counter_ok=True, failure=None, probe_sample=None)


def test_a_full_window_with_nothing_heard_is_diagnosed_once_per_streak(counting_service):
    service = counting_service
    service.start_tracker({"entry_id": 101})
    for _ in range(ActivityService.WINDOW_SECONDS):
        service.tick()                                   # window 1: silent
    lines = _lines(service.log.warning, "ACTIVITY_CAPTURE_SILENT")
    assert len(lines) == 1
    assert "os_idle_seconds=3" in lines[0]               # the OS saw input: the tap did not
    assert "events_seen" in lines[0]

    for _ in range(ActivityService.WINDOW_SECONDS):
        service.tick()                                   # window 2: silent, not re-logged
    assert len(_lines(service.log.warning, "ACTIVITY_CAPTURE_SILENT")) == 1

    service._counter.snapshot_and_reset.return_value = _counts(movements=4)
    service.tick()
    service._counter.snapshot_and_reset.return_value = _counts()
    for _ in range(ActivityService.WINDOW_SECONDS - 1):
        service.tick()                                   # window 3: heard something
    assert len(_lines(service.log.info, "ACTIVITY_CAPTURE_HEARD")) == 1

    for _ in range(ActivityService.WINDOW_SECONDS):
        service.tick()                                   # window 4: a new silent streak
    assert len(_lines(service.log.warning, "ACTIVITY_CAPTURE_SILENT")) == 2


def test_a_short_tail_window_is_not_diagnosed_as_silent(counting_service):
    """The last few seconds of a session are not evidence of anything."""
    service = counting_service
    service.start_tracker({"entry_id": 101})
    for _ in range(ActivityService.SILENT_WINDOW_MIN_SECONDS - 1):
        service.tick()
    service.stop_tracker()
    assert _lines(service.log.warning, "ACTIVITY_CAPTURE_SILENT") == []


def test_the_silent_diagnostic_never_changes_what_is_recorded(counting_service):
    service = counting_service
    service.start_tracker({"entry_id": 101})
    for _ in range(ActivityService.WINDOW_SECONDS):
        service.tick()
    kwargs = service._cache.save_activity_sample.call_args.kwargs
    assert kwargs["activity_percent"] == 0
    assert kwargs["active_seconds"] == 0
    assert kwargs["keyboard_strokes"] == 0
    assert kwargs["window_seconds"] == ActivityService.WINDOW_SECONDS


def test_a_window_on_a_blocked_session_is_not_a_silent_window(blocked_service):
    """Blocked is already named; it must not also be reported as silence."""
    service = blocked_service
    service.start_tracker({"entry_id": 101})
    for _ in range(ActivityService.WINDOW_SECONDS):
        service.tick()
    assert _lines(service.log.warning, "ACTIVITY_CAPTURE_SILENT") == []

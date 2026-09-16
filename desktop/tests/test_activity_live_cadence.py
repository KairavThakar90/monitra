"""
TODAY'S ACTIVITY follows the user's hands second by second.

The card renders `activity_percent_changed`, which `ActivityService.tick()`
emits while a window is being sampled. It used to emit every fifth sampled
second, so a burst of typing showed up to five seconds late. It now emits on
every sampled second -- the slot only re-renders from state already held, so
this costs what the timer's own display tick costs -- and the value it
carries is the live window's own percentage, computed from the counts the
input counter actually saw. The flush at the end of the window still writes
exactly one record.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from background_services.activity.activity_service import (
    ActivityService, calculate_activity_percentage,
)


def _counts(keystrokes=0, clicks=0, movements=0):
    return {"keystrokes": keystrokes, "clicks": clicks, "movements": movements}


def _service(per_second_counts):
    runtime = MagicMock()
    runtime.timer.active_session.return_value = {"entry_id": 42, "client_op": "op"}
    cache = MagicMock()
    service = ActivityService(runtime, cache)
    counter = MagicMock()
    counter.supported = True
    counter.start.return_value = True
    counter.snapshot_and_reset.side_effect = list(per_second_counts)
    counter.drain_watched_presses.return_value = {}
    service._counter = counter
    probe = MagicMock()
    probe.sample.return_value = {"active": True, "mouse": True}
    service._probe = probe
    return service, cache


def test_the_percentage_is_emitted_on_every_sampled_second(qapp):
    seconds = 12
    service, _cache = _service([_counts(5, 1, 20)] * seconds)
    emitted = []
    service.activity_percent_changed.connect(emitted.append)
    service.start_tracker({"entry_id": 42, "client_op": "op"})

    for _ in range(seconds):
        service.tick()

    assert len(emitted) == seconds, "one emission per sampled second, not one in five"


def test_the_emitted_value_is_the_live_window_computed_from_the_counts_seen(qapp):
    # Ten quiet seconds, then a burst: the value must move the second the
    # burst is counted, and it must be the weighted formula over the window
    # sampled so far -- never a fabricated or smoothed number.
    quiet = [_counts()] * 10
    burst = [_counts(30, 5, 100)]
    service, _cache = _service(quiet + burst)
    emitted = []
    service.activity_percent_changed.connect(emitted.append)
    service.start_tracker({"entry_id": 42, "client_op": "op"})

    for _ in range(10):
        service.tick()
    assert emitted[-1] == 0
    service.tick()

    expected = calculate_activity_percentage(30, 5, 100, active_seconds=11, window_seconds=11)
    assert emitted[-1] == expected
    assert expected > 0
    assert service.current_percent() == expected


def test_a_full_window_still_flushes_exactly_one_record(qapp):
    seconds = ActivityService.WINDOW_SECONDS
    service, cache = _service([_counts(2, 1, 6)] * seconds)
    recorded = []
    service.activity_window_recorded.connect(recorded.append)
    service.start_tracker({"entry_id": 42, "client_op": "op"})

    for _ in range(seconds):
        service.tick()

    assert len(recorded) == 1
    assert recorded[0]["window_seconds"] == seconds
    assert recorded[0]["keyboard_strokes"] == 2 * seconds
    assert recorded[0]["mouse_clicks"] == seconds
    assert recorded[0]["mouse_movements"] == 6 * seconds
    assert recorded[0]["activity_percent"] == calculate_activity_percentage(
        2 * seconds, seconds, 6 * seconds, active_seconds=seconds, window_seconds=seconds,
    )

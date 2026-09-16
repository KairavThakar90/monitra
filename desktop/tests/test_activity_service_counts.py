"""
ActivityService integration with the input counter and the
unwanted-activity monitor: counts flow into the flushed window, macOS's
probe-less path still measures, detection events (and their deductions)
land in the offline queues, and everything is attributed to the right
entry -- including events detected before the backend issued an entry id.

The real InputEventCounter/InputProbe are replaced per-test: no listeners,
no OS calls, fully deterministic.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from background_services.activity.activity_service import ActivityService


def _counts(keystrokes=0, clicks=0, movements=0):
    return {"keystrokes": keystrokes, "clicks": clicks, "movements": movements}


class StubRuntime:
    def __init__(self):
        self.timer = MagicMock()
        self.timer.active_session.return_value = {"entry_id": 101}


class ActivityServiceTestBase(unittest.TestCase):
    def setUp(self):
        self.cache = MagicMock()
        self.service = ActivityService(StubRuntime(), self.cache)
        # Replace the OS-facing pieces with deterministic stand-ins.
        self.counter = MagicMock()
        self.counter.supported = True
        self.counter.start.return_value = True
        self.counter.snapshot_and_reset.return_value = _counts()
        self.counter.drain_watched_presses.return_value = {}
        self.service._counter = self.counter
        self.probe = MagicMock()
        self.service._probe = self.probe


class TestCountsInWindows(ActivityServiceTestBase):
    def test_counts_accumulate_into_the_flushed_window(self):
        self.probe.sample.return_value = {"active": True, "mouse": False}
        self.service.start_tracker({"entry_id": 101})

        self.counter.snapshot_and_reset.return_value = _counts(keystrokes=7, clicks=2, movements=40)
        self.service.tick()
        self.counter.snapshot_and_reset.return_value = _counts(keystrokes=3, clicks=1, movements=10)
        self.service.tick()

        self.service.stop_tracker()  # flushes the partial window

        kwargs = self.cache.save_activity_sample.call_args.kwargs
        self.assertEqual(kwargs["time_entry_id"], 101)
        self.assertEqual(kwargs["keyboard_strokes"], 10)
        self.assertEqual(kwargs["mouse_clicks"], 3)
        self.assertEqual(kwargs["mouse_movements"], 50)
        self.assertEqual(kwargs["window_seconds"], 2)
        self.assertEqual(kwargs["active_seconds"], 2)

    def test_probe_less_platform_still_measures_via_the_counter(self):
        """The macOS shape: InputProbe.sample() returns None (unsupported),
        but the pynput counter works -- its counts are what the percentage is
        computed from, so activity works there too."""
        self.probe.sample.return_value = None
        self.service.start_tracker({"entry_id": 101})

        self.counter.snapshot_and_reset.return_value = _counts(keystrokes=5)
        self.service.tick()  # a second with input
        self.counter.snapshot_and_reset.return_value = _counts()
        self.service.tick()  # a second without

        self.assertEqual(self.service._sampled, 2)
        self.assertEqual(self.service._active, 1)
        # 5 keystrokes in a 2-second window, against a per-minute threshold of
        # 120 scaled to 2 seconds (4): the keyboard component saturates and
        # contributes its full 40% weight, with no clicks or movement.
        self.assertEqual(self.service.current_percent(), 40)

    def test_the_percentage_is_computed_from_counts_not_from_presence(self):
        """Presence saturates -- anyone moving a mouse scores 100% -- and it
        can exceed what was actually observed, because the OS sees input the
        client's hooks cannot. The number now cannot exceed the evidence."""
        self.probe.sample.return_value = {"active": True, "mouse": False}
        self.service.start_tracker({"entry_id": 101})

        self.counter.snapshot_and_reset.return_value = _counts()
        for _ in range(5):
            self.service.tick()

        # Every second counted as "present", and nothing at all was observed.
        self.assertEqual(self.service._active, 5)
        self.assertEqual(self.service.current_percent(), 0)

    def test_the_counter_is_the_only_source_of_counts(self):
        """
        The probe must not be able to contribute counts, even if it offers
        them.

        It used to. `tick()` added the counter's counts *and* the probe's
        into the same totals, and the probe had its own pair of Win32 hooks
        keeping its own tallies. Those hooks never installed on 64-bit
        Windows (SetWindowsHookExW returned NULL, measured), so the sum
        silently added a permanent zero -- but the shape was a double count
        waiting for someone to "fix" the hooks. There is one owner of input
        counting, and this asserts the wiring rather than the intention.
        """
        self.probe.sample.return_value = {
            "active": True, "mouse": True,
            # A probe that has started counting again: these must be ignored.
            "keyboard_strokes": 500, "mouse_clicks": 500, "mouse_movements": 500,
        }
        self.service.start_tracker({"entry_id": 101})

        self.counter.snapshot_and_reset.return_value = _counts(
            keystrokes=7, clicks=2, movements=9
        )
        self.service.tick()
        self.service.stop_tracker()

        kwargs = self.cache.save_activity_sample.call_args.kwargs
        self.assertEqual(kwargs["keyboard_strokes"], 7)
        self.assertEqual(kwargs["mouse_clicks"], 2)
        self.assertEqual(kwargs["mouse_movements"], 9)

    def test_a_keyboard_second_requires_an_actual_keystroke(self):
        """
        `key_events` counts seconds that contained typing. The probe's old
        `keyboard` flag was `active and not moved` -- "the user was there and
        the cursor sat still" -- which measures nothing about the keyboard
        and reported scrolling, or simply reading, as typing.
        """
        self.probe.sample.return_value = {"active": True, "mouse": False}
        self.service.start_tracker({"entry_id": 101})

        self.counter.snapshot_and_reset.return_value = _counts()
        for _ in range(3):
            self.service.tick()          # present, cursor still, nothing typed
        self.assertEqual(self.service._key_events, 0)

        self.counter.snapshot_and_reset.return_value = _counts(keystrokes=4)
        self.service.tick()
        self.assertEqual(self.service._key_events, 1)

    def test_no_mechanism_at_all_records_nothing(self):
        """Neither probe nor counter available: unmeasured, not fabricated."""
        self.probe.sample.return_value = None
        self.counter.supported = False
        self.counter.snapshot_and_reset.return_value = _counts()
        self.service.start_tracker({"entry_id": 101})

        self.service.tick()
        self.assertEqual(self.service._sampled, 0)

    def test_listeners_start_and_stop_with_the_session(self):
        """Privacy contract: capture only between start and stop."""
        self.probe.sample.return_value = None
        self.service.start_tracker({"entry_id": 101})
        self.counter.start.assert_called_once()
        self.service.stop_tracker()
        self.counter.stop.assert_called()


class TestUnwantedEventPersistence(ActivityServiceTestBase):
    def _event(self, deduction=0, index=1):
        return {
            "client_event_id": "evt-abc",
            "activity_type": "repeated_key",
            "key_or_action": "ctrl",
            "occurrence_count": 15,
            "alerted": True,
            "alert_count": index,
            "recorded_at": "2026-08-31T12:00:00+00:00",
            "deduction_seconds": deduction,
            "occurrence_index": index,
        }

    def test_event_is_queued_against_the_active_entry(self):
        self.service.start_tracker({"entry_id": 101})
        self.service._on_unwanted_event(self._event())

        kwargs = self.cache.save_unwanted_activity.call_args.kwargs
        self.assertEqual(kwargs["time_entry_id"], 101)
        self.assertEqual(kwargs["record_id"], "evt-abc")
        self.assertEqual(kwargs["occurrence_count"], 15)
        self.cache.save_adjustment.assert_not_called()  # no deduction on this one

    def test_third_occurrence_queues_the_deduction_too(self):
        self.service.start_tracker({"entry_id": 101})
        self.service._on_unwanted_event(self._event(deduction=600, index=3))

        kwargs = self.cache.save_adjustment.call_args.kwargs
        self.assertEqual(kwargs["time_entry_id"], 101)
        self.assertEqual(kwargs["adjustment_seconds"], -600)
        self.assertEqual(kwargs["source_client_event_id"], "evt-abc")
        self.assertEqual(kwargs["record_id"], "adj-evt-abc")
        self.assertIn("occurrence 3", kwargs["reason"])

    def test_events_before_the_entry_id_arrives_are_held_then_attributed(self):
        """Offline start: no entry id yet. The event must not be dropped and
        must not be persisted with a null entry -- it is held and written
        the moment bind_entry_id() delivers the real id."""
        self.service.start_tracker({"entry_id": None})
        self.service._on_unwanted_event(self._event())
        self.cache.save_unwanted_activity.assert_not_called()

        self.service.bind_entry_id(202)
        kwargs = self.cache.save_unwanted_activity.call_args.kwargs
        self.assertEqual(kwargs["time_entry_id"], 202)

    def test_alert_signal_reaches_subscribers(self):
        received = []
        self.service.unwanted_activity_alert.connect(received.append)
        self.service.start_tracker({"entry_id": 101})

        self.service._monitor._on_alert("warning text")
        self.assertEqual(received, ["warning text"])


if __name__ == "__main__":
    unittest.main()

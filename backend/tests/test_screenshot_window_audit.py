"""The window audit names what the database knows, and only that.

`scripts/screenshot_window_audit.py` is what an operator runs when a report says
"the admin page shows No capture for these windows". Its value is the vocabulary:
every verdict it prints must mean exactly one thing, and `NO RECORD` -- the one
window the database cannot explain -- must never be used for a window it can.
"""
import importlib.util
import os
import unittest
from datetime import datetime, timedelta, timezone

from app.core.time_format import IST

_PATH = os.path.join(os.path.dirname(__file__), "..", "scripts", "screenshot_window_audit.py")
_spec = importlib.util.spec_from_file_location("screenshot_window_audit", _PATH)
audit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(audit)

W0 = datetime(2026, 10, 6, 5, 0, tzinfo=timezone.utc)


def _window(shots=(), state="none", reason=None, tracked=600, measured=120, start=W0, attempts=0):
    return {
        "window_start": start, "window_end": start + timedelta(minutes=10),
        "activity_percentage": 62, "activity_measured_seconds": measured,
        "tracked_seconds": tracked, "screenshots": list(shots),
        "screenshot_count": len(shots), "capture_state": state,
        "capture_reason": reason, "capture_attempts": attempts,
    }


class VerdictTests(unittest.TestCase):
    def test_a_window_with_an_image_is_captured_whatever_was_reported(self):
        self.assertEqual(audit.verdict_for(_window(shots=[{"id": 1}], state="pending")), "captured")

    def test_each_reported_state_is_its_own_verdict(self):
        for state in ("pending", "failed", "blocked", "excluded", "unavailable"):
            self.assertEqual(audit.verdict_for(_window(state=state)), state)

    def test_only_a_worked_window_nothing_was_reported_for_is_no_record(self):
        self.assertEqual(audit.verdict_for(_window(state="none", tracked=600)), "NO RECORD")

    def test_an_unworked_empty_window_is_not_an_accusation(self):
        self.assertEqual(audit.verdict_for(_window(state="none", tracked=0)), "-")


class FormatTests(unittest.TestCase):
    def test_a_line_names_the_time_in_ist_the_work_and_the_reason(self):
        line = audit.format_window(
            _window(state="failed", reason="screen_unreadable", attempts=6), IST)
        self.assertIn("10:30-10:40", line)           # 05:00Z is 10:30 IST
        self.assertIn("worked= 600s", line)
        self.assertIn("failed", line)
        self.assertIn("reason=screen_unreadable attempts=6", line)


class EdgeSpillTests(unittest.TestCase):
    def _shot(self, offset_seconds, shot_id=2):
        return {"id": shot_id, "captured_at": W0 + timedelta(minutes=10, seconds=offset_seconds)}

    def test_a_capture_just_after_a_boundary_following_an_empty_window_is_flagged(self):
        empty = _window()
        spilled = _window(shots=[self._shot(1)], start=W0 + timedelta(minutes=10))
        notes = audit.edge_spills([empty, spilled], edge_seconds=5)
        self.assertEqual(len(notes), 1)
        self.assertIn("1s into the window", notes[0])

    def test_a_capture_well_inside_its_window_is_not(self):
        empty = _window()
        fine = _window(shots=[self._shot(300)], start=W0 + timedelta(minutes=10))
        self.assertEqual(audit.edge_spills([empty, fine], edge_seconds=5), [])

    def test_an_empty_window_with_no_activity_is_not_a_missing_capture(self):
        idle = _window(measured=0)
        spilled = _window(shots=[self._shot(1)], start=W0 + timedelta(minutes=10))
        self.assertEqual(audit.edge_spills([idle, spilled], edge_seconds=5), [])

    def test_windows_that_are_not_neighbours_are_not_compared(self):
        empty = _window()
        later = _window(shots=[self._shot(1)], start=W0 + timedelta(minutes=30))
        self.assertEqual(audit.edge_spills([empty, later], edge_seconds=5), [])


if __name__ == "__main__":
    unittest.main()

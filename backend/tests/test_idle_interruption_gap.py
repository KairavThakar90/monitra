"""
An interruption gap reported through the idle-period mechanism.

The desktop reports the gap between a dead process's last heartbeat and the
recovery instant as an ordinary idle period. Nothing on the backend is
special-cased for it; these tests pin that the existing endpoint accepts a
period that started hours before it was detected, that a repeat of the same
report (a second launch, a retry) returns the period already pending rather
than a second one, and that the four answers account for it exactly as for
any other idle period.
"""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from app.schemas.time_entry_idle_period import IdlePeriodCreate
from app.services.time_entry_idle_period import TimeEntryIdlePeriodService, counts_idle_time
from tests.test_idle_periods import _entry, _idle, _user, SVC

UTC = timezone.utc
_ANCHOR = (datetime.now(UTC) - timedelta(days=1)).replace(hour=13, minute=0, second=0, microsecond=0)
STARTED = _ANCHOR                                  # 1 PM: timer started
LAST_BEAT = _ANCHOR + timedelta(hours=4)           # 5 PM: power cut
RECOVERED = _ANCHOR + timedelta(hours=5)           # 6 PM: Monitra back


class InterruptionGapTests(unittest.TestCase):
    def test_a_gap_hours_long_is_accepted_as_one_idle_period(self):
        db = MagicMock()
        created = _idle(idle_started_at=LAST_BEAT, idle_detected_at=RECOVERED)
        with patch(f"{SVC}.TimeEntryIdlePeriodService._owned_entry",
                   return_value=_entry(start_time=STARTED)), \
             patch(f"{SVC}.TimeEntryIdlePeriodRepository.get_by_client_event_id", return_value=None), \
             patch(f"{SVC}.TimeEntryIdlePeriodRepository.get_pending_for_entry", return_value=None), \
             patch(f"{SVC}.TimeEntryIdlePeriodRepository.create", return_value=created) as create, \
             patch(f"{SVC}._with_entry_adjustment", side_effect=lambda db, p: p):
            period = TimeEntryIdlePeriodService.report_idle_period(
                db,
                IdlePeriodCreate(time_entry_id=100, idle_started_at=LAST_BEAT,
                                 idle_detected_at=RECOVERED,
                                 client_event_id="interruption:timer:7:k:2026"),
                _user(),
            )
        self.assertIs(period, created)
        kwargs = create.call_args.kwargs
        self.assertEqual(kwargs["idle_started_at"], LAST_BEAT)
        self.assertEqual(kwargs["idle_detected_at"], RECOVERED)
        self.assertEqual(kwargs["client_event_id"], "interruption:timer:7:k:2026")

    def test_a_gap_before_the_entry_started_is_refused(self):
        from fastapi import HTTPException

        db = MagicMock()
        with patch(f"{SVC}.TimeEntryIdlePeriodService._owned_entry",
                   return_value=_entry(start_time=STARTED)), \
             patch(f"{SVC}.TimeEntryIdlePeriodRepository.get_by_client_event_id", return_value=None):
            with self.assertRaises(HTTPException) as raised:
                TimeEntryIdlePeriodService.report_idle_period(
                    db,
                    IdlePeriodCreate(time_entry_id=100, idle_started_at=STARTED - timedelta(minutes=1),
                                     idle_detected_at=RECOVERED),
                    _user(),
                )
        self.assertEqual(raised.exception.status_code, 400)

    def test_a_repeat_of_the_same_report_returns_the_pending_period(self):
        """A second launch, or a retry after a lost reply, must not open a
        second period: the client event id answers first, and failing that
        the one-pending-period-per-entry rule does."""
        db = MagicMock()
        pending = _idle(idle_started_at=LAST_BEAT, idle_detected_at=RECOVERED)
        payload = IdlePeriodCreate(time_entry_id=100, idle_started_at=LAST_BEAT,
                                   idle_detected_at=RECOVERED, client_event_id="interruption:x")
        with patch(f"{SVC}.TimeEntryIdlePeriodRepository.get_by_client_event_id", return_value=pending), \
             patch(f"{SVC}.TimeEntryIdlePeriodRepository.create") as create, \
             patch(f"{SVC}._with_entry_adjustment", side_effect=lambda db, p: p):
            self.assertIs(TimeEntryIdlePeriodService.report_idle_period(db, payload, _user()), pending)
        create.assert_not_called()

        with patch(f"{SVC}.TimeEntryIdlePeriodService._owned_entry",
                   return_value=_entry(start_time=STARTED)), \
             patch(f"{SVC}.TimeEntryIdlePeriodRepository.get_by_client_event_id", return_value=None), \
             patch(f"{SVC}.TimeEntryIdlePeriodRepository.get_pending_for_entry", return_value=pending), \
             patch(f"{SVC}.TimeEntryIdlePeriodRepository.create") as create, \
             patch(f"{SVC}._with_entry_adjustment", side_effect=lambda db, p: p):
            second = IdlePeriodCreate(time_entry_id=100, idle_started_at=LAST_BEAT + timedelta(minutes=1),
                                      idle_detected_at=RECOVERED + timedelta(minutes=2),
                                      client_event_id="interruption:y")
            self.assertIs(TimeEntryIdlePeriodService.report_idle_period(db, second, _user()), pending)
        create.assert_not_called()

    def test_a_stopped_entry_refuses_the_gap(self):
        """Stopped from the web while Monitra was down: the backend's answer
        is final and the desktop ends its session instead of asking."""
        from fastapi import HTTPException

        db = MagicMock()
        with patch(f"{SVC}.TimeEntryIdlePeriodService._owned_entry",
                   return_value=_entry(start_time=STARTED, end_time=LAST_BEAT + timedelta(minutes=30),
                                       status="stopped")), \
             patch(f"{SVC}.TimeEntryIdlePeriodRepository.get_by_client_event_id", return_value=None):
            with self.assertRaises(HTTPException) as raised:
                TimeEntryIdlePeriodService.report_idle_period(
                    db,
                    IdlePeriodCreate(time_entry_id=100, idle_started_at=LAST_BEAT,
                                     idle_detected_at=RECOVERED),
                    _user(),
                )
        self.assertEqual(raised.exception.status_code, 409)

    def test_the_four_answers_account_for_the_gap_like_any_idle_period(self):
        self.assertTrue(counts_idle_time(True, "resume"))
        self.assertFalse(counts_idle_time(False, "resume"))
        self.assertFalse(counts_idle_time(True, "stop"))
        self.assertFalse(counts_idle_time(False, "stop"))


if __name__ == "__main__":
    unittest.main()

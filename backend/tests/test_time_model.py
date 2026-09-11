"""
Coverage for the canonical time model.

One rule underlies all of it: a duration is derived from two timestamps and
nothing else. It is never accumulated from UI counters, never reconstructed
from rounded decimal hours, and never re-derived differently by a second
consumer. `total_seconds` is the integer answer; every screen formats that.

Two defects these tests pin, both measured on the live database before they
were fixed:

  * `stop_timer` computed `int(delta.total_seconds())`, which truncates. 90 of
    2449 completed entries stored a value exactly one second below
    `round(end_time - start_time)`, and none stored one above -- a systematic,
    one-directional loss that broke the invariant the whole system is checked
    against.
  * start and stop were stamped with the server's clock at *request* time. The
    desktop queues those calls durably and retries them, so an entry stopped at
    14:29 whose stop landed at 14:34 was recorded as five minutes longer than
    the desktop had been showing.
"""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from app.core.time_format import (
    IST, elapsed_seconds, format_hms, ist_day_end_utc, ist_day_start_utc,
)
from app.models.time_entry import TimeEntry
from app.models.user import User
from app.schemas.time_entry import TimeEntryRead, TimeEntryStart, TimeEntryStop
from app.services.time_entry import MAX_CLIENT_BACKDATE_SECONDS, TimeEntryService


UTC = timezone.utc


def _user():
    return User(id=54, organization_id=1, role_name="administrator", permissions={})


# ── the duration itself ──────────────────────────────────────────────────────

def test_the_documented_example_is_exact():
    """10:00:00 -> 11:12:55 is 4375 seconds, rendered 01:12:55."""
    start = datetime(2026, 9, 2, 10, 0, 0, tzinfo=UTC)
    end = datetime(2026, 9, 2, 11, 12, 55, tzinfo=UTC)

    assert elapsed_seconds(start, end) == 4375
    assert format_hms(4375) == "01:12:55"


def test_sub_second_remainders_round_and_do_not_truncate():
    """The old `int()` lost the fraction every time, so a day of short entries
    bled a second each. Rounding is symmetric and agrees with Postgres's own
    round(extract(epoch ...))."""
    start = datetime(2026, 9, 2, 10, 0, 0, tzinfo=UTC)

    assert elapsed_seconds(start, start + timedelta(seconds=1031.4)) == 1031
    assert elapsed_seconds(start, start + timedelta(seconds=1031.6)) == 1032
    # The real regression: 17m11.54s was stored as 1031, not 1032.
    assert elapsed_seconds(start, start + timedelta(seconds=1031.54)) == 1032


def test_a_duration_is_timezone_independent():
    """11:25 -> 14:29 IST and 05:55 -> 08:59 UTC are the same interval."""
    ist_pair = (
        datetime(2026, 9, 2, 11, 25, 53, tzinfo=IST),
        datetime(2026, 9, 2, 14, 29, 3, tzinfo=IST),
    )
    utc_pair = (
        datetime(2026, 9, 2, 5, 55, 53, tzinfo=UTC),
        datetime(2026, 9, 2, 8, 59, 3, tzinfo=UTC),
    )
    assert elapsed_seconds(*ist_pair) == elapsed_seconds(*utc_pair)


def test_a_backwards_clock_cannot_produce_a_negative_duration():
    start = datetime(2026, 9, 2, 11, 0, 0, tzinfo=UTC)
    assert elapsed_seconds(start, start - timedelta(minutes=5)) == 0


def test_hours_never_wrap_past_a_day():
    assert format_hms(90061) == "25:01:01"


# ── running entries ──────────────────────────────────────────────────────────

def test_a_running_entry_reports_elapsed_time_not_zero():
    """`total_seconds` is only written on stop, so a running entry stores 0.
    The API's canonical field measures against now instead."""
    start = datetime.now(UTC) - timedelta(minutes=30)
    entry = TimeEntryRead.model_validate(_entry_row(start_time=start, end_time=None,
                                                    total_seconds=0, status="running"))
    assert entry.is_running is True
    assert 1795 <= entry.elapsed_seconds <= 1805
    assert entry.elapsed_time.startswith("00:29") or entry.elapsed_time.startswith("00:30")


def test_a_running_entrys_duration_grows_with_the_clock():
    start = datetime.now(UTC) - timedelta(seconds=100)
    first = TimeEntryRead.model_validate(
        _entry_row(start_time=start, end_time=None, total_seconds=0, status="running")
    ).elapsed_seconds
    later = TimeEntryRead.model_validate(
        _entry_row(start_time=start - timedelta(seconds=60), end_time=None,
                   total_seconds=0, status="running")
    ).elapsed_seconds
    assert later - first == 60


def test_a_completed_entry_reports_its_stored_total():
    start = datetime(2026, 9, 2, 10, 0, 0, tzinfo=UTC)
    entry = TimeEntryRead.model_validate(
        _entry_row(start_time=start, end_time=start + timedelta(seconds=4375),
                   total_seconds=4375, status="stopped")
    )
    assert entry.is_running is False
    assert entry.elapsed_seconds == 4375
    assert entry.elapsed_time == "01:12:55"


def _entry_row(**overrides):
    row = dict(
        id=1, organization_id=1, user_id=54, project_id=2, task_id=3,
        start_time=datetime(2026, 9, 2, 10, 0, 0, tzinfo=UTC), end_time=None,
        total_seconds=0, status="running", is_manual=False, is_billable=True,
        description=None,
        created_at=datetime(2026, 9, 2, 10, 0, 0, tzinfo=UTC),
        updated_at=datetime(2026, 9, 2, 10, 0, 0, tzinfo=UTC),
    )
    row.update(overrides)
    return row


# ── IST calendar day boundaries ──────────────────────────────────────────────

def test_an_ist_day_is_a_half_open_utc_interval_of_exactly_24_hours():
    from datetime import date

    start = ist_day_start_utc(date(2026, 9, 2))
    end = ist_day_end_utc(date(2026, 9, 2))

    # 00:00 IST is 18:30 UTC the previous day.
    assert start == datetime(2026, 9, 1, 18, 30, tzinfo=UTC)
    assert end == datetime(2026, 9, 2, 18, 30, tzinfo=UTC)
    assert (end - start) == timedelta(days=1)


def test_early_morning_ist_belongs_to_the_ist_day_not_the_utc_one():
    """01:00 IST on 2 Sept is 19:30 UTC on 1 Sept. A UTC-day filter puts it on
    the wrong day; this is the shift the desktop's naive filter had."""
    from datetime import date

    moment = datetime(2026, 9, 2, 1, 0, tzinfo=IST)
    assert ist_day_start_utc(date(2026, 9, 2)) <= moment < ist_day_end_utc(date(2026, 9, 2))
    assert moment.astimezone(UTC).date() == date(2026, 9, 1)  # ... but UTC calls it the 1st


# ── client-supplied event instants ───────────────────────────────────────────

class TestEventTime(unittest.TestCase):
    """`_event_time` decides *when a timer event happened*."""

    def test_a_plausible_client_instant_is_authoritative(self):
        """This is the fix for a queued stop: the entry must record when the
        user pressed Stop, not when the retry reached the API."""
        pressed = datetime.now(UTC) - timedelta(minutes=5)
        assert TimeEntryService._event_time(pressed, label="stopped_at") == pressed

    def test_a_missing_instant_falls_back_to_the_server_clock(self):
        before = datetime.now(UTC)
        resolved = TimeEntryService._event_time(None, label="stopped_at")
        assert before <= resolved <= datetime.now(UTC)

    def test_a_naive_instant_is_read_as_utc(self):
        naive = (datetime.now(UTC) - timedelta(minutes=2)).replace(tzinfo=None)
        assert TimeEntryService._event_time(naive, label="stopped_at").tzinfo is not None

    def test_a_future_instant_cannot_manufacture_time(self):
        future = datetime.now(UTC) + timedelta(hours=3)
        resolved = TimeEntryService._event_time(future, label="stopped_at")
        assert resolved < future

    def test_an_implausibly_old_instant_is_refused(self):
        ancient = datetime.now(UTC) - timedelta(seconds=MAX_CLIENT_BACKDATE_SECONDS + 60)
        resolved = TimeEntryService._event_time(ancient, label="started_at")
        assert resolved > ancient

    def test_a_stop_before_its_own_start_is_refused(self):
        start = datetime.now(UTC) - timedelta(minutes=10)
        bogus = start - timedelta(minutes=5)
        resolved = TimeEntryService._event_time(
            bogus, label="stopped_at", not_before=start
        )
        assert resolved >= start


class TestStopTimer(unittest.TestCase):
    def setUp(self):
        self.db = MagicMock()
        self.user = _user()

    def _stop(self, start_time, stopped_at):
        entry = TimeEntry(id=1, organization_id=1, user_id=54, project_id=2, task_id=3,
                          start_time=start_time, end_time=None, total_seconds=0,
                          status="running")
        with patch("app.services.time_entry.TimeEntryRepository.get_by_id", return_value=entry), \
             patch("app.services.time_entry.TimeEntryRepository.stop") as stop:
            self.db.scalar.return_value = 0
            TimeEntryService.stop_timer(self.db, 1, None, self.user, stopped_at=stopped_at)
        return stop.call_args.kwargs

    def test_the_stored_total_equals_end_minus_start(self):
        """The system's central invariant."""
        start = datetime.now(UTC) - timedelta(seconds=4375)
        stopped = datetime.now(UTC)
        kwargs = self._stop(start, stopped)

        assert kwargs["end_time"] == stopped
        assert kwargs["total_seconds"] == elapsed_seconds(start, kwargs["end_time"])
        assert kwargs["total_seconds"] == 4375

    def test_a_late_stop_records_the_users_instant_not_the_arrival_time(self):
        """The reported divergence: the desktop showed 12m55s, the backend
        stored 17m55s, because the queued stop landed five minutes late."""
        start = datetime.now(UTC) - timedelta(seconds=1075)   # 17m55s ago
        pressed = start + timedelta(seconds=775)              # user stopped at 12m55s

        kwargs = self._stop(start, pressed)

        assert kwargs["total_seconds"] == 775
        assert format_hms(kwargs["total_seconds"]) == "00:12:55"

    def test_without_a_client_instant_the_server_clock_is_used(self):
        start = datetime.now(UTC) - timedelta(seconds=60)
        kwargs = self._stop(start, None)
        assert 59 <= kwargs["total_seconds"] <= 61


# ── the API contract the UIs format ──────────────────────────────────────────

def test_the_start_and_stop_payloads_accept_the_clients_instants():
    moment = datetime(2026, 9, 2, 5, 55, 53, tzinfo=UTC)
    assert TimeEntryStart(project_id=1, task_id=2, started_at=moment).started_at == moment
    assert TimeEntryStop(stopped_at=moment).stopped_at == moment
    # Both remain optional, so an older client is unaffected.
    assert TimeEntryStart(project_id=1, task_id=2).started_at is None
    assert TimeEntryStop().stopped_at is None


def test_the_api_carries_integer_seconds_not_decimal_hours():
    """Decimal hours are a reporting projection, never the source of truth."""
    fields = TimeEntryRead.model_fields
    assert fields["total_seconds"].annotation is int
    assert "hours" not in fields
    # And the derived value is exact when it is wanted.
    assert 4375 / 3600 == TimeEntryRead.model_validate(
        _entry_row(end_time=datetime(2026, 9, 2, 11, 12, 55, tzinfo=UTC),
                   total_seconds=4375, status="stopped")
    ).total_seconds / 3600


# ── aggregation over a day ───────────────────────────────────────────────────

def test_multiple_entries_sum_exactly():
    """The real 2 Sept data: twelve entries summing to 4375s = 01:12:55."""
    durations = [129, 1031, 5, 119, 168, 1088, 28, 6, 979, 789, 4, 29]
    total = sum(durations)
    assert total == 4375
    assert format_hms(total) == "01:12:55"

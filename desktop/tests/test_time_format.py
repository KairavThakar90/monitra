"""
Tests for the desktop's authoritative duration formatter and IST day helper.

The timer service already guarantees that *elapsed seconds* are derived from a
durable timestamp. These tests cover the layer immediately above it: turning
those seconds into the string the user actually reads, and naming the calendar
day the elapsed time is folded into.

Both used to be duplicated — a private `_fmt_seconds` in `task_table` and a
private `_format_seconds` in `sidebar`, plus a machine-local `date.today()` for
the cache bucket. Duplicated time logic is exactly how two views end up
disagreeing, so these tests also assert that the widgets delegate rather than
keeping their own copy.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from core.time_format import format_hms, ist_today, to_ist
# Reuse the timer harness rather than building a second one.
from tests.test_timer_service import FakeRuntime, FakeTimeEntryService
from background_services.timer.timer_service import TimerService


@pytest.fixture
def timer(qapp, cache):
    backend = FakeTimeEntryService(entry_id=42)
    runtime = FakeRuntime(cache, backend)
    service = TimerService(runtime, backend, cache)
    runtime.timer = service
    yield service
    service.stop(timeout_ms=500)


@pytest.mark.parametrize(
    "seconds,expected",
    [
        (0, "00:00:00"),
        (1, "00:00:01"),
        (59, "00:00:59"),
        (60, "00:01:00"),
        (61, "00:01:01"),
        (3599, "00:59:59"),
        (3600, "01:00:00"),
        (3661, "01:01:01"),
        (86399, "23:59:59"),
        (86400, "24:00:00"),
        (90061, "25:01:01"),
        (20742, "05:45:42"),
    ],
)
def test_format_hms_boundaries(seconds, expected):
    assert format_hms(seconds) == expected


def test_durations_never_wrap_past_24_hours():
    # A tracked duration of 25:10:30 must stay 25:10:30, not become 01:10:30.
    assert format_hms(25 * 3600 + 10 * 60 + 30) == "25:10:30"
    assert format_hms(360309) == "100:05:09"


def test_no_hidden_rounding():
    assert format_hms(5 * 3600 + 59 * 60 + 59) == "05:59:59"


def test_negative_and_none_clamp_to_zero():
    assert format_hms(-5) == "00:00:00"
    assert format_hms(None) == "00:00:00"


def test_widgets_share_the_one_formatter():
    """A widget must not carry its own duration formatting."""
    from ui.sidebar import _format_seconds
    from ui.task_table import _fmt_seconds

    assert _format_seconds is format_hms
    assert _fmt_seconds is format_hms


def test_ist_today_matches_the_ist_wall_clock():
    assert ist_today() == datetime.now(to_ist(datetime.now(timezone.utc)).tzinfo).date()


def test_to_ist_applies_the_real_offset_and_rolls_the_date_over():
    utc = datetime(2026, 9, 1, 18, 30, tzinfo=timezone.utc)
    assert to_ist(utc).strftime("%Y-%m-%d %H:%M:%S") == "2026-09-02 00:00:00"


def test_timer_folds_elapsed_time_into_the_ist_day(timer, cache, monkeypatch):
    """
    The cache bucket must name the IST day, because that is the day the backend
    reports against. A machine in another timezone previously folded elapsed
    time into a day the server would never show it under.
    """
    recorded = {}

    def capture(target_date, task_id, elapsed, **_details):
        recorded["target_date"] = target_date

    monkeypatch.setattr(cache, "add_elapsed_to_cached_time_entry", capture)

    timer.start_tracking(1, 7)
    timer._session["started_at_utc"] = (
        datetime.now(timezone.utc) - timedelta(seconds=30)
    ).isoformat()
    timer.stop_tracking()

    assert recorded["target_date"] == ist_today().isoformat()
    assert isinstance(ist_today(), date)

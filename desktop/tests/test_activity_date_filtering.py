"""
Coverage for date-scoped activity: the day window, the desktop's retention
windows, and the midnight split.

Three separate failures are guarded here, each of which showed up as "the
numbers are wrong" and each of which has a different cause:

  1. **All-time totals.** The Apps and URLs summaries used to ask the backend
     for everything and to add every pending local row, so last week's usage
     sat in today's list. Both halves of the merge must be filtered by the same
     day window.
  2. **A day that is not the day.** An inclusive `23:59:59` upper bound drops
     the final second; a UTC day instead of an IST one shifts everything by
     five and a half hours.
  3. **Midnight.** A segment that runs from 23:59:40 to 00:00:40 is one row
     with one timestamp; stored whole it credits the whole minute to the
     earlier day.

No Qt application and no network: this is arithmetic, filtering and string
handling.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock

from background_services.activity.app_usage import build_app_usage_summary
from background_services.activity.day_split import split_by_ist_day
from background_services.activity.retention import (
    ACTIVITY_DESKTOP_DAYS, DateAvailability, SCREENSHOT_DESKTOP_DAYS,
    activity_availability, screenshot_availability,
)
from background_services.activity.url_usage import build_url_usage_summary
from core.time_format import IST, ist_day_bounds_utc


# ── the desktop's retention windows ──────────────────────────────────────────

TODAY = date(2026, 9, 8)


def test_screenshots_are_served_for_today_and_the_previous_three_days():
    for back in range(0, SCREENSHOT_DESKTOP_DAYS + 1):
        day = TODAY - timedelta(days=back)
        assert screenshot_availability(day, TODAY) == DateAvailability.AVAILABLE


def test_a_fourth_day_of_screenshots_is_archived_not_empty():
    """The distinction matters: "archived" sends the user to the web client,
    "empty" would tell them nothing was captured, which is false."""
    day = TODAY - timedelta(days=SCREENSHOT_DESKTOP_DAYS + 1)
    assert screenshot_availability(day, TODAY) == DateAvailability.ARCHIVED


def test_apps_and_urls_are_served_for_a_week_and_archived_beyond_it():
    assert activity_availability(
        TODAY - timedelta(days=ACTIVITY_DESKTOP_DAYS), TODAY
    ) == DateAvailability.AVAILABLE
    assert activity_availability(
        TODAY - timedelta(days=ACTIVITY_DESKTOP_DAYS + 1), TODAY
    ) == DateAvailability.ARCHIVED


def test_the_two_windows_are_independent():
    """A date five days back is archived for screenshots and still live for
    usage. The Screenshots tab must be able to decline while Apps and URLs
    load."""
    day = TODAY - timedelta(days=5)
    assert screenshot_availability(day, TODAY) == DateAvailability.ARCHIVED
    assert activity_availability(day, TODAY) == DateAvailability.AVAILABLE


def test_tomorrow_is_future_for_both():
    tomorrow = TODAY + timedelta(days=1)
    assert screenshot_availability(tomorrow, TODAY) == DateAvailability.FUTURE
    assert activity_availability(tomorrow, TODAY) == DateAvailability.FUTURE


# ── the midnight split ───────────────────────────────────────────────────────

def _ist(year, month, day, hour, minute, second=0) -> str:
    """An IST wall-clock instant as the UTC ISO string the cache stores."""
    moment = datetime(year, month, day, hour, minute, second, tzinfo=IST)
    return moment.astimezone(timezone.utc).isoformat()


def test_a_segment_inside_one_day_is_left_alone():
    start = _ist(2026, 9, 8, 14, 0)
    assert split_by_ist_day(start, 60) == [(start, 60)]


def test_a_segment_crossing_midnight_is_split_at_the_boundary():
    """23:59:40 + 60s: 20 seconds belong to the 8th and 40 to the 9th."""
    start = _ist(2026, 9, 8, 23, 59, 40)
    chunks = split_by_ist_day(start, 60)

    assert len(chunks) == 2
    assert [seconds for _, seconds in chunks] == [20, 40]

    first_day = datetime.fromisoformat(chunks[0][0]).astimezone(IST).date()
    second_day = datetime.fromisoformat(chunks[1][0]).astimezone(IST).date()
    assert first_day == date(2026, 9, 8)
    assert second_day == date(2026, 9, 9)


def test_a_split_never_invents_or_loses_time():
    """The whole point of splitting is re-attribution, not adjustment."""
    for duration in (1, 59, 60, 3600, 86_400, 200_000):
        start = _ist(2026, 9, 8, 23, 59, 59)
        assert sum(seconds for _, seconds in split_by_ist_day(start, duration)) == duration


def test_a_segment_ending_exactly_on_midnight_is_not_split():
    """Half-open days: a segment that ends at 00:00:00 belongs wholly to the
    day before. Splitting it would write a second row of zero seconds."""
    start = _ist(2026, 9, 8, 23, 59, 0)
    assert split_by_ist_day(start, 60) == [(start, 60)]


def test_an_unreadable_timestamp_keeps_the_segment_rather_than_dropping_it():
    assert split_by_ist_day("not a timestamp", 30) == [("not a timestamp", 30)]


def test_a_zero_length_segment_produces_no_rows():
    assert split_by_ist_day(_ist(2026, 9, 8, 10, 0), 0) == []


# ── the app usage summary is scoped to one day ───────────────────────────────

def _api_returning(payload):
    api_client = MagicMock()
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = payload
    api_client.get.return_value = response
    return api_client


def test_app_usage_asks_the_backend_for_exactly_the_selected_ist_day():
    api_client = _api_returning({"applications": []})
    build_app_usage_summary(api_client, cache=None, day=date(2026, 9, 8))

    params = api_client.get.call_args.kwargs["params"]
    start, end = ist_day_bounds_utc(date(2026, 9, 8))
    assert params["start_date"] == start.isoformat()
    # Exclusive upper bound, so the day's last second is included exactly once
    # and a record on the boundary is not counted in two days.
    assert params["end_before"] == end.isoformat()
    assert "end_date" not in params


def test_app_usage_merges_only_local_rows_from_the_selected_day():
    """The local half of the merge must use the same window as the remote
    half, or an offline row from last week reappears in today's total."""
    api_client = _api_returning(
        {"applications": [{"application_name": "VS Code", "duration_seconds": 600}]}
    )
    cache = MagicMock()
    cache.get_unsynced_app_usage_between.return_value = [
        {"application_name": "VS Code", "duration_seconds": 30},
    ]

    rows = build_app_usage_summary(api_client, cache, day=date(2026, 9, 8))

    start, end = ist_day_bounds_utc(date(2026, 9, 8))
    cache.get_unsynced_app_usage_between.assert_called_once_with(
        start.isoformat(), end.isoformat()
    )
    assert rows[0]["seconds"] == 630


def test_app_usage_without_a_day_returns_nothing_rather_than_an_all_time_total():
    """The defect this whole change removes. A caller that forgets the date
    must get an empty list, never every application the account has ever
    used."""
    api_client = _api_returning({"applications": [{"application_name": "X", "duration_seconds": 9}]})
    assert build_app_usage_summary(api_client, cache=None) == []
    api_client.get.assert_not_called()


def test_app_usage_falls_back_to_local_rows_when_the_backend_is_unreachable():
    """Offline is not an error state: the day's real local rows still render."""
    api_client = MagicMock()
    api_client.get.side_effect = RuntimeError("offline")
    cache = MagicMock()
    cache.get_unsynced_app_usage_between.return_value = [
        {"application_name": "chrome", "duration_seconds": 3600},
    ]

    rows = build_app_usage_summary(api_client, cache, day=date(2026, 9, 8))

    assert len(rows) == 1
    # Resolved through the shared catalogue, so a pending local row and an
    # already-uploaded one name the same browser the same way and merge into
    # one line instead of appearing as two.
    assert rows[0]["name"] == "Google Chrome"
    assert rows[0]["time_str"] == "1h"


# ── the url summary is scoped and aggregated, not truncated ──────────────────

def test_url_usage_reads_the_day_scoped_aggregate_not_the_capped_listing():
    """`/url-usage` returns raw rows behind limit=100, so summing them
    reported a partial day. The summary endpoint aggregates in the database."""
    api_client = _api_returning({"data": {"pages": [], "total_duration_seconds": 0}})
    build_url_usage_summary(api_client, cache=None, day=date(2026, 9, 8))

    path = api_client.get.call_args.args[0]
    params = api_client.get.call_args.kwargs["params"]
    start, end = ist_day_bounds_utc(date(2026, 9, 8))
    assert path == "/url-usage/summary"
    assert params["start_date"] == start.isoformat()
    assert params["end_date"] == end.isoformat()
    assert "limit" not in params


def test_url_usage_adds_the_backend_total_to_the_days_unsynced_rows():
    api_client = _api_returning({
        "data": {
            "pages": [{
                "domain": "github.com",
                "url": "https://github.com/monitra",
                "page_title": "monitra",
                "duration_seconds": 300,
            }],
        }
    })
    cache = MagicMock()
    cache.get_unsynced_url_usage_between.return_value = [
        {"domain": "github.com", "url": "https://github.com/monitra",
         "page_title": "monitra", "duration_seconds": 45},
    ]

    rows = build_url_usage_summary(api_client, cache, day=date(2026, 9, 8))

    start, end = ist_day_bounds_utc(date(2026, 9, 8))
    cache.get_unsynced_url_usage_between.assert_called_once_with(
        start.isoformat(), end.isoformat()
    )
    assert len(rows) == 1
    assert rows[0]["seconds"] == 345


def test_url_usage_without_a_day_returns_nothing():
    api_client = _api_returning({"data": {"pages": [{"domain": "x.com", "duration_seconds": 5}]}})
    assert build_url_usage_summary(api_client, cache=None) == []
    api_client.get.assert_not_called()

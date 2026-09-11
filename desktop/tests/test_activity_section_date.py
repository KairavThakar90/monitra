"""
Coverage for the Activity panel's selected date.

What the panel has to get right, beyond fetching the correct day:

  * A date it does not serve must cost **no request at all**. The whole point
    of the retention window is that the desktop stops pulling unbounded
    history; a panel that fetched anyway and then hid the result would keep
    every cost and gain only a message.
  * All three tabs move together. Three tabs of one panel showing three days
    would be unreadable.
  * A response for a date the user has moved on from must not be rendered.
  * Only today is polled. A finished day cannot change.
"""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock

from background_services.activity.retention import (
    DateAvailability, activity_availability, screenshot_availability,
)
from core.time_format import ist_today
from ui.activity_section import (
    MODE_ARCHIVED, MODE_DATA, MODE_EMPTY, MODE_FUTURE, MODE_LOADING,
    ActivitySection,
)


def _make_section(enabled: bool = True) -> ActivitySection:
    """A section wired to a stub BackgroundApi.

    `run_in_background` runs its callable inline and delivers the result the
    way the real pool does on the GUI thread, so a test can assert on what the
    panel actually rendered rather than on what it queued. The availability
    calls delegate to the real retention rules — the point is the panel's use
    of them, not a re-statement of the thresholds.
    """
    api = MagicMock()
    api.screenshot_availability.side_effect = screenshot_availability
    api.activity_availability.side_effect = activity_availability

    def run(fn, on_success=None, on_error=None, key=None):
        try:
            result = fn()
        except BaseException as exc:  # noqa: BLE001 - mirrors the pool
            if on_error is not None:
                on_error(exc)
        else:
            if on_success is not None:
                on_success(result)
        return None

    api.run_in_background.side_effect = run
    api.app_usage_summary.return_value = []
    api.url_usage_summary.return_value = []

    api_client = MagicMock()
    api_client.get.return_value.json.return_value = {"windows": []}

    section = ActivitySection(api, api_client)
    if enabled:
        section.set_enabled(True)
    return section


# ── the date reaches every fetch ─────────────────────────────────────────────

def test_the_selected_date_is_today_on_open(qapp):
    section = _make_section()
    assert section.selected_date == ist_today()


def test_every_tab_fetches_the_same_selected_day(qapp):
    section = _make_section()
    yesterday = ist_today() - timedelta(days=1)
    section.set_selected_date(yesterday)

    section.api.app_usage_summary.assert_called_with(yesterday)
    section.api.url_usage_summary.assert_called_with(yesterday)
    params = section.api_client.get.call_args.kwargs["params"]
    assert params["date"] == yesterday.isoformat()


def test_moving_between_two_historical_days_re_asks_all_three_tabs(qapp):
    """Each hop must re-scope every tab. A tab left on the previous day would
    show one date's rows under another's heading -- the exact confusion the
    single selected date exists to prevent."""
    section = _make_section()
    for offset in (1, 3, 5):
        day = ist_today() - timedelta(days=offset)
        section.set_selected_date(day)

        assert section.selected_date == day
        assert section.api.app_usage_summary.call_args.args[0] == day
        assert section.api.url_usage_summary.call_args.args[0] == day


def test_an_empty_historical_day_shows_the_empty_state_not_an_error(qapp):
    section = _make_section()
    section.set_selected_date(ist_today() - timedelta(days=2))

    assert section.view_apps._mode == MODE_EMPTY
    assert section.view_urls._mode == MODE_EMPTY
    assert section.view_ss._mode == MODE_EMPTY


def test_coming_back_to_today_re_scopes_every_tab_to_today(qapp):
    section = _make_section()
    section.set_selected_date(ist_today() - timedelta(days=2))
    section.set_selected_date(ist_today())

    assert section.selected_date == ist_today()
    assert section.api.app_usage_summary.call_args.args[0] == ist_today()
    assert section.api.url_usage_summary.call_args.args[0] == ist_today()


def test_the_screenshot_timeline_is_asked_for_one_day_not_all_of_history(qapp):
    """The endpoint's own IST date filter, so only the selected day's captures
    cross the wire."""
    section = _make_section()
    path = section.api_client.get.call_args.args[0]
    assert path == "/time-entry-screenshots/timeline"
    assert "date" in section.api_client.get.call_args.kwargs["params"]


# ── dates the desktop does not serve cost nothing ────────────────────────────

def test_a_future_date_shows_the_future_state_and_fetches_nothing(qapp):
    section = _make_section()
    section.api.app_usage_summary.reset_mock()
    section.api.url_usage_summary.reset_mock()
    section.api_client.get.reset_mock()

    section.set_selected_date(ist_today() + timedelta(days=2))

    assert section.view_ss._mode == MODE_FUTURE
    assert section.view_apps._mode == MODE_FUTURE
    assert section.view_urls._mode == MODE_FUTURE
    section.api.app_usage_summary.assert_not_called()
    section.api.url_usage_summary.assert_not_called()
    section.api_client.get.assert_not_called()


def test_an_archived_date_offers_the_profile_link_and_fetches_nothing(qapp):
    section = _make_section()
    section.api.app_usage_summary.reset_mock()
    section.api.url_usage_summary.reset_mock()
    section.api_client.get.reset_mock()

    section.set_selected_date(ist_today() - timedelta(days=30))

    assert section.view_apps._mode == MODE_ARCHIVED
    assert section.view_urls._mode == MODE_ARCHIVED
    assert section.view_ss._mode == MODE_ARCHIVED
    section.api.app_usage_summary.assert_not_called()
    section.api_client.get.assert_not_called()


def test_a_date_archived_only_for_screenshots_still_loads_apps_and_urls(qapp):
    """The two windows are different lengths, so the tabs must decide
    independently rather than share one verdict."""
    section = _make_section()
    day = ist_today() - timedelta(days=5)
    assert screenshot_availability(day) == DateAvailability.ARCHIVED
    assert activity_availability(day) == DateAvailability.AVAILABLE

    section.api_client.get.reset_mock()
    section.set_selected_date(day)

    assert section.view_ss._mode == MODE_ARCHIVED
    section.api.app_usage_summary.assert_called_with(day)
    section.api_client.get.assert_not_called()


def test_the_profile_button_names_its_tab_and_the_selected_date(qapp):
    section = _make_section()
    day = ist_today() - timedelta(days=30)
    seen = []
    section.profile_requested.connect(lambda kind, d: seen.append((kind, d)))

    section.set_selected_date(day)
    section.view_apps.profile_requested.emit()

    assert seen == [("apps", day)]


# ── stale responses and stale data ───────────────────────────────────────────

def test_changing_the_date_clears_the_previous_days_rows_first(qapp):
    """A refresh that left the old rows up until new ones landed would show
    one date's totals under another's heading."""
    section = _make_section()
    section.view_apps.set_data([{"name": "VS Code", "seconds": 60, "percentage": 100}])

    section.set_selected_date(ist_today() - timedelta(days=1))

    assert section.view_apps._apps == []


def test_a_response_for_a_date_the_user_has_left_is_not_rendered(qapp):
    section = _make_section()
    stale_day = ist_today() - timedelta(days=1)

    captured = {}

    def run(fn, on_success=None, on_error=None, key=None):
        # Hold the apps callback instead of delivering it, so it can be fired
        # after the user has moved on -- the real race, made deterministic.
        if key == "activity-apps":
            captured["on_success"] = on_success
            return None
        if on_success is not None:
            on_success(fn())
        return None

    section.api.run_in_background.side_effect = run
    section.set_selected_date(stale_day)
    deliver = captured["on_success"]

    section.set_selected_date(ist_today() - timedelta(days=2))
    deliver([{"name": "Stale", "seconds": 999, "percentage": 100}])

    assert section.view_apps._apps == []


def test_screenshot_thumbnails_from_the_previous_date_are_dropped(qapp):
    """The cache is keyed by screenshot id, so stale entries would not be
    shown -- but they would keep every image of every browsed day resident,
    which is the growth the retention window exists to prevent."""
    section = _make_section()
    section.view_ss._images[42] = b"png-bytes"

    section.set_selected_date(ist_today() - timedelta(days=1))

    assert section.view_ss._images == {}


# ── polling policy ───────────────────────────────────────────────────────────

def test_today_is_polled_automatically(qapp):
    section = _make_section()
    assert section._auto_timer.isActive()


def test_a_historical_date_is_not_polled(qapp):
    section = _make_section()
    section.set_selected_date(ist_today() - timedelta(days=2))
    assert not section._auto_timer.isActive()


def test_a_future_date_is_not_polled(qapp):
    section = _make_section()
    section.set_selected_date(ist_today() + timedelta(days=2))
    assert not section._auto_timer.isActive()


def test_returning_to_today_resumes_polling(qapp):
    section = _make_section()
    section.set_selected_date(ist_today() - timedelta(days=2))
    section.set_selected_date(ist_today())
    assert section._auto_timer.isActive()


def test_logout_stops_polling(qapp):
    section = _make_section()
    section.set_enabled(False)
    assert not section._auto_timer.isActive()


# ── ordinary states still work ───────────────────────────────────────────────

def test_an_available_day_with_no_activity_reads_as_empty_not_archived(qapp):
    section = _make_section()
    assert section.view_apps._mode == MODE_EMPTY
    assert section.view_urls._mode == MODE_EMPTY


def test_an_available_day_with_activity_renders_its_rows(qapp):
    section = _make_section(enabled=False)
    section.api.app_usage_summary.return_value = [
        {"name": "VS Code", "seconds": 3600, "percentage": 100, "time_str": "1h"}
    ]
    section.set_enabled(True)

    assert section.view_apps._mode == MODE_DATA
    assert len(section.view_apps._apps) == 1


def test_loading_is_shown_before_a_result_arrives(qapp):
    section = _make_section(enabled=False)
    section.api.run_in_background.side_effect = lambda *a, **k: None
    section.set_enabled(True)

    assert section.view_apps._mode == MODE_LOADING

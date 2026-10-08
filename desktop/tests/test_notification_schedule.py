"""
The administrator-managed notification schedule, end to end on the desktop.

Two halves, and the line between them is the point of the design:

* `NotificationScheduleService` fetches, parses, holds and persists the
  schedule. It is the only thing that talks to the backend.
* `WellbeingService` stays the one scheduler and does no network work: it only
  reads the snapshot on each tick.

`tick()` is called directly, the way `test_update_service.py` and
`test_wellbeing_reminders.py` do it, and both of the wellbeing service's clocks
are driven by the test rather than by sleeping. Friday 2026-09-11 and Sunday
2026-09-13 are the IST weekdays used throughout (asserted below, so a wrong
calendar fails loudly rather than quietly testing the wrong day).
"""
from __future__ import annotations

import dataclasses
import json
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.api.exceptions import ApiConnectionError, ApiError
from background_services.network import NetworkState
from background_services.notifications.schedule_service import (
    SCHEDULE_STATE_KEY,
    NotificationScheduleService,
    parse_schedule,
)
from background_services.wellbeing import WellbeingService
from background_services.wellbeing.wellbeing_service import DAILY_STATE_KEY
from core.time_format import IST

TICK_SECONDS = WellbeingService.interval_ms / 1000
CLOCK_START = 1000.0
#: These tests are about the schedule, not the hourly limit, so the services they build are not limited
#: (the limit has its own tests in test_notification_hourly_limit.py).
UNCAPPED = 1000
ALL_DAYS = [0, 1, 2, 3, 4, 5, 6]
WORKDAYS = [0, 1, 2, 3, 4]

FRIDAY = datetime(2026, 9, 11, tzinfo=IST)
SUNDAY = datetime(2026, 9, 13, tzinfo=IST)


def test_the_calendar_used_by_these_tests_is_what_they_say_it_is():
    assert FRIDAY.weekday() == 4
    assert SUNDAY.weekday() == 6


def payload(version=1, builtin=(), custom=()):
    return {
        "version": version,
        "updated_at": "2026-10-01T09:00:00Z",
        "server_time": "2026-10-01T09:30:12Z",
        "builtin": list(builtin),
        "custom": list(custom),
    }


def builtin(key, enabled=True, time=None, weekdays=None):
    return {
        "key": key, "enabled": enabled, "time": time,
        "weekdays": ALL_DAYS if weekdays is None else weekdays,
    }


def custom(id="abc123", title="Standup", message="Daily standup in 5 minutes.",
           time="10:25", weekdays=None):
    return {
        "id": id, "title": title, "message": message, "time": time,
        "weekdays": WORKDAYS if weekdays is None else weekdays,
    }


class FakeCache:
    """An app_state store that round-trips through JSON, like the real one."""

    def __init__(self, store=None):
        self.store = store if store is not None else {}
        self.saves = []

    def load_app_state(self, key):
        raw = self.store.get(key)
        return json.loads(raw) if raw is not None else None

    def save_app_state(self, key, value):
        self.saves.append(key)
        self.store[key] = json.dumps(value)


# ── Parsing ──────────────────────────────────────────────────────────────────

def test_a_valid_response_is_parsed_in_full():
    schedule = parse_schedule(payload(
        version=7,
        builtin=[
            builtin("lunch", enabled=False, time="13:45", weekdays=WORKDAYS),
            builtin("hydrate"),
        ],
        custom=[custom()],
    ))

    assert schedule.version == 7
    lunch = schedule.builtin["lunch"]
    assert (lunch.enabled, lunch.at.hour, lunch.at.minute) == (False, 13, 45)
    assert lunch.weekdays == frozenset(WORKDAYS)
    assert schedule.builtin["hydrate"].at is None   # an interval reminder
    assert schedule.builtin_off == 1
    (notification,) = schedule.custom
    assert (notification.id, notification.title) == ("abc123", "Standup")
    assert (notification.at.hour, notification.at.minute) == (10, 25)


def test_an_unknown_builtin_key_is_ignored():
    schedule = parse_schedule(payload(builtin=[
        builtin("some_future_reminder", enabled=False),
        builtin("lunch", enabled=False),
    ]))

    assert set(schedule.builtin) == {"lunch"}


@pytest.mark.parametrize("bad", [
    "not a dict", None, 5, [], {},
    {"version": "7"}, {"version": True}, {"version": -1}, {"version": 1.5},
    {"version": 1, "builtin": "nope"}, {"version": 1, "custom": {"a": 1}},
])
def test_an_unusable_response_as_a_whole_parses_to_nothing(bad):
    assert parse_schedule(bad) is None


@pytest.mark.parametrize("item", [
    "text", None, 3, [],
    {},                                                       # nothing
    {"id": "", "title": "T", "message": "M", "time": "10:00", "weekdays": [0]},
    {"id": "a", "title": "  ", "message": "M", "time": "10:00", "weekdays": [0]},
    {"id": "a", "title": "T", "message": "", "time": "10:00", "weekdays": [0]},
    {"id": 7, "title": "T", "message": "M", "time": "10:00", "weekdays": [0]},
    {"id": "a", "title": "T", "message": "M", "time": "9:30", "weekdays": [0]},
    {"id": "a", "title": "T", "message": "M", "time": "24:00", "weekdays": [0]},
    {"id": "a", "title": "T", "message": "M", "time": "10:00:00", "weekdays": [0]},
    {"id": "a", "title": "T", "message": "M", "time": 1000, "weekdays": [0]},
    {"id": "a", "title": "T", "message": "M", "time": "10:00", "weekdays": []},
    {"id": "a", "title": "T", "message": "M", "time": "10:00", "weekdays": [7]},
    {"id": "a", "title": "T", "message": "M", "time": "10:00", "weekdays": [-1]},
    {"id": "a", "title": "T", "message": "M", "time": "10:00", "weekdays": ["1"]},
    {"id": "a", "title": "T", "message": "M", "time": "10:00", "weekdays": [True]},
    {"id": "a", "title": "T", "message": "M", "time": "10:00", "weekdays": "0123456"},
])
def test_a_malformed_custom_item_is_dropped_and_the_rest_survive(item):
    schedule = parse_schedule(payload(custom=[item, custom(id="good")]))

    assert [c.id for c in schedule.custom] == ["good"]


@pytest.mark.parametrize("item", [
    "lunch", None, {}, {"key": "lunch"},
    {"key": "lunch", "enabled": "yes", "time": "13:30", "weekdays": ALL_DAYS},
    {"key": "lunch", "enabled": True, "time": "1:30", "weekdays": ALL_DAYS},
    {"key": "lunch", "enabled": True, "time": "13:30", "weekdays": []},
    {"key": "lunch", "enabled": True, "time": "13:30", "weekdays": [9]},
])
def test_a_malformed_builtin_entry_is_dropped_so_the_reminder_keeps_its_default(item):
    schedule = parse_schedule(payload(builtin=[item, builtin("hydrate", enabled=False)]))

    assert "lunch" not in schedule.builtin
    assert not schedule.builtin["hydrate"].enabled


def test_a_duplicate_custom_id_keeps_the_first():
    schedule = parse_schedule(payload(custom=[
        custom(id="a", title="First"), custom(id="a", title="Second"),
    ]))

    assert [c.title for c in schedule.custom] == ["First"]


def test_the_snapshot_cannot_be_changed_by_a_reader():
    schedule = parse_schedule(payload(builtin=[builtin("lunch")], custom=[custom()]))

    with pytest.raises(dataclasses.FrozenInstanceError):
        schedule.version = 99
    with pytest.raises(dataclasses.FrozenInstanceError):
        schedule.custom[0].title = "changed"
    with pytest.raises(TypeError):
        schedule.builtin["lunch"] = None
    assert isinstance(schedule.custom, tuple)
    assert isinstance(schedule.builtin["lunch"].weekdays, frozenset)


# ── The service: holds, edges, failure, persistence ──────────────────────────

class FakeScheduleApi:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = 0

    def get_schedule(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.response

    def open_stream(self, since, *, read_timeout):
        """An older backend: no change stream (404), so the poll is all there is.
        The stream itself is covered by test_notification_push.py."""
        raise ApiError("no change stream", status_code=404)


class LogCapture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())

    def applied(self):
        return [m for m in self.messages if m.startswith("NOTIFICATION_SCHEDULE_APPLIED")]


def make_service(response=None, error=None, *, signed_in=True,
                 network_state=NetworkState.BACKEND_REACHABLE, cache=None,
                 first_tick=True):
    wakes = []
    runtime = SimpleNamespace(
        api_client=SimpleNamespace(access_token="token" if signed_in else None),
        network=SimpleNamespace(network_state=network_state),
        wellbeing=SimpleNamespace(wake=lambda: wakes.append(1)),
        storage=None,
    )
    api = FakeScheduleApi(response, error)
    cache = cache if cache is not None else FakeCache()
    service = NotificationScheduleService(runtime, api, cache)
    capture = LogCapture()
    service.log.addHandler(capture)
    service.log.setLevel(logging.DEBUG)
    if first_tick:
        service.tick()   # reads the persisted record; makes no request
    return service, api, cache, wakes, capture


def test_the_first_tick_loads_what_was_persisted_and_asks_nothing():
    service, api, _cache, _wakes, _log = make_service(payload())

    assert api.calls == 0
    assert service.ready


def test_it_is_not_ready_until_the_persisted_record_has_been_read():
    service, *_ = make_service(payload(), first_tick=False)

    assert not service.ready
    service.tick()
    assert service.ready


def test_with_nothing_fetched_and_nothing_persisted_the_snapshot_is_none():
    service, *_ = make_service(error=ApiConnectionError("down"))

    assert service.schedule is None
    service.tick()
    assert service.schedule is None, "a failed poll must not invent a schedule"


def test_a_poll_adopts_the_schedule():
    service, api, _cache, _wakes, _log = make_service(
        payload(version=3, builtin=[builtin("lunch", enabled=False)], custom=[custom()]),
    )

    service.tick()

    assert api.calls == 1
    assert service.schedule.version == 3
    assert service.schedule.builtin["lunch"].enabled is False


def test_a_version_is_applied_logged_persisted_and_woken_once_however_often_it_is_polled():
    service, api, cache, wakes, log = make_service(
        payload(version=4, builtin=[builtin("lunch", enabled=False)], custom=[custom()]),
    )
    wakes.clear()   # the wake the first tick sends is a different edge

    for _ in range(5):
        service.tick()

    assert api.calls == 5, "the poll itself runs every time"
    assert len(log.applied()) == 1
    assert "version=4" in log.applied()[0]
    assert "builtin_off=1" in log.applied()[0]
    assert "custom=1" in log.applied()[0]
    assert cache.saves == [SCHEDULE_STATE_KEY]
    assert wakes == [1]


def test_a_new_version_is_applied_again():
    service, api, cache, wakes, log = make_service(payload(version=1))
    service.tick()

    api.response = payload(version=2, custom=[custom()])
    service.tick()
    service.tick()

    assert service.schedule.version == 2
    assert len(log.applied()) == 2
    assert len(cache.saves) == 2


def test_the_log_never_carries_the_payload():
    service, _api, _cache, _wakes, log = make_service(
        payload(custom=[custom(title="Secret title", message="Secret message body")]),
    )
    service.tick()

    assert not any("Secret" in m or "token" in m for m in log.messages)


def test_a_malformed_response_never_raises_and_never_changes_the_snapshot():
    service, api, cache, _wakes, log = make_service(payload(version=5, custom=[custom()]))
    service.tick()
    good = service.schedule
    saves = len(cache.saves)

    for garbage in ("<html>", None, [], {"version": "x"}, {"version": 6, "builtin": 9}):
        api.response = garbage
        service.tick()   # must not raise

        assert service.schedule is good
    assert len(cache.saves) == saves
    assert len(log.applied()) == 1


def test_a_failed_poll_keeps_the_last_good_schedule():
    service, api, _cache, _wakes, _log = make_service(payload(version=2, custom=[custom()]))
    service.tick()
    good = service.schedule

    api.error = ApiConnectionError("offline")
    service.tick()
    api.error = ApiError("boom", status_code=500)
    service.tick()

    assert service.schedule is good


def test_an_absent_endpoint_waits_quietly_and_keeps_what_it_has():
    service, api, _cache, _wakes, log = make_service(payload(version=2))
    service.tick()
    good = service.schedule

    api.error = ApiError("not found", status_code=404)
    delay = service.tick()

    assert service.schedule is good
    assert delay >= service.ABSENT_INTERVAL_MS * 0.85, "a missing route is not asked every half minute"
    assert not [m for m in log.messages if "ERROR" in m]


@pytest.mark.parametrize("error", [
    ApiConnectionError("offline"),
    ApiError("boom", status_code=500),
    ApiError("bad gateway", status_code=502),
])
def test_a_failed_poll_is_retried_soon_not_after_minutes(error):
    service, api, *_ = make_service(payload(version=2))
    service.tick()

    api.error = error
    delay = service.tick()

    assert delay <= service.RETRY_INTERVAL_MS * 1.15
    assert service.RETRY_INTERVAL_MS <= 60_000, (
        "one lost request must not leave a desktop deaf to a change for minutes"
    )


def test_it_holds_while_signed_out_without_asking():
    service, api, *_ = make_service(payload(), signed_in=False)

    delay = service.tick()

    assert api.calls == 0
    assert delay == service.HOLD_INTERVAL_MS
    assert service.schedule is None


@pytest.mark.parametrize("state", [NetworkState.NO_NETWORK, NetworkState.BACKEND_UNREACHABLE])
def test_it_holds_while_the_backend_is_not_worth_trying(state):
    service, api, *_ = make_service(payload(), network_state=state)

    service.tick()

    assert api.calls == 0


def test_it_does_try_while_the_network_state_is_merely_unknown():
    service, api, *_ = make_service(payload(), network_state=NetworkState.UNKNOWN)

    service.tick()

    assert api.calls == 1


def test_the_poll_is_prompt_but_bounded_and_jittered():
    service, *_ = make_service(payload())

    delays = {service.tick() for _ in range(40)}

    assert len(delays) > 1, "a fleet must not poll in lockstep"
    assert all(service.POLL_INTERVAL_MS * 0.85 <= d <= service.POLL_INTERVAL_MS * 1.15
               for d in delays)
    # Prompt enough that a notification saved a minute ahead is known before its
    # time (the cadence of the maintenance notice) ...
    assert service.POLL_INTERVAL_MS * 1.15 <= 40_000
    # ... and never a hammer: one small read per desktop, no faster than this.
    assert service.POLL_INTERVAL_MS >= 15_000


def test_the_last_good_schedule_is_persisted_and_an_offline_start_uses_it():
    cache = FakeCache()
    first, *_ = make_service(
        payload(version=9, builtin=[builtin("lunch", enabled=False, time="13:45")],
                custom=[custom()]),
        cache=cache,
    )
    first.tick()

    second, api, _c, wakes, log = make_service(
        error=ApiConnectionError("offline"), cache=cache,
    )

    assert api.calls == 0
    assert second.schedule == first.schedule, "the round trip is lossless"
    assert second.ready
    assert wakes, "the scheduler is told the schedule is now loaded"
    assert "source=persisted" in log.applied()[0]
    assert cache.load_app_state(SCHEDULE_STATE_KEY)["version"] == 9


def test_a_poll_of_the_persisted_version_is_not_a_change():
    cache = FakeCache()
    first, *_ = make_service(payload(version=9), cache=cache)
    first.tick()
    saves = len(cache.saves)

    second, api, _c, wakes, log = make_service(payload(version=9), cache=cache)
    wakes.clear()
    second.tick()
    second.tick()

    assert api.calls == 2
    assert len(cache.saves) == saves
    assert wakes == []
    assert len(log.applied()) == 1   # only the persisted load


def test_an_unusable_persisted_record_is_ignored_not_trusted():
    cache = FakeCache({SCHEDULE_STATE_KEY: json.dumps({"version": "?", "custom": 1})})

    service, *_ = make_service(cache=cache)

    assert service.schedule is None
    assert service.ready


def test_logout_wiping_app_state_is_repaired_by_the_next_poll():
    cache = FakeCache()
    service, api, _c, _w, _l = make_service(payload(version=3), cache=cache)
    service.tick()
    cache.store.clear()          # what logout does to app_state
    service.reset_session()
    cache.saves.clear()

    service.tick()               # same version: not a change, but persisted again

    assert cache.saves == [SCHEDULE_STATE_KEY]
    service.tick()
    assert cache.saves == [SCHEDULE_STATE_KEY], "and only once"


def test_a_failing_cache_never_takes_the_service_down():
    class BrokenCache:
        def load_app_state(self, key):
            raise RuntimeError("disk")

        def save_app_state(self, key, value):
            raise RuntimeError("disk")

    service, *_ = make_service(payload(version=2), cache=BrokenCache())
    service.tick()

    assert service.schedule.version == 2
    assert service.ready


# ── WellbeingService reads the schedule ──────────────────────────────────────

class FakeNotifications:
    def __init__(self):
        self.shown = []
        self.ist = lambda: None

    def notify(self, message, level=None, title=None, key=None, link=None, native=False):
        self.shown.append({"body": message, "title": title, "key": key,
                           "level": level, "ist": self.ist()})
        return True

    @property
    def keys(self):
        return [item["key"] for item in self.shown]

    def of(self, key):
        return [item for item in self.shown if item["key"] == f"wellbeing:{key}"]


def make_wellbeing(schedule_payload=None, *, start, cache=None, ready=True,
                   with_source=True):
    notifications = FakeNotifications()
    source = SimpleNamespace(
        schedule=None if schedule_payload is None else parse_schedule(schedule_payload),
        ready=ready,
    )
    runtime = SimpleNamespace(
        api_client=SimpleNamespace(access_token="token"),
        notifications=notifications,
        cache=cache,
        storage=None,
    )
    if with_source:
        runtime.notification_schedule = source
    service = WellbeingService(runtime, cache)
    service.DEFAULT_MAX_PER_HOUR = UNCAPPED
    service._clock = CLOCK_START
    service._ist = start
    service._now_monotonic = lambda: service._clock
    service._now_ist = lambda: service._ist
    notifications.ist = lambda: service._ist
    return service, notifications, source


def run_for(service, minutes):
    for _ in range(int(minutes * 60 // TICK_SECONDS)):
        service._clock += TICK_SECONDS
        service._ist += timedelta(seconds=TICK_SECONDS)
        service.tick()


def run_as_the_loop_does(service, minutes):
    end = service._clock + minutes * 60
    while service._clock < end:
        delay_ms = service.tick()
        step = (service.interval_ms if delay_ms is None else delay_ms) / 1000.0
        service._clock += step
        service._ist += timedelta(seconds=step)


EVENING = dict(hour=18, minute=0)


def at(day, hour, minute, second=0):
    return day.replace(hour=hour, minute=minute, second=second)


def test_no_schedule_means_the_behaviour_the_service_had_before():
    # An explicit "none" source, and no source at all, both mean the defaults:
    # the same reminders at the same times as the catalogue says.
    for with_source in (True, False):
        service, notifications, _ = make_wellbeing(
            None, start=at(FRIDAY, 13, 29), with_source=with_source,
        )
        run_as_the_loop_does(service, 5)

        assert "wellbeing:lunch" in notifications.keys


def test_a_disabled_interval_reminder_is_never_shown_but_its_grid_advances():
    service, notifications, source = make_wellbeing(
        payload(builtin=[builtin("rule_20_20_20", enabled=False)]),
        start=at(FRIDAY, **EVENING),
    )

    run_for(service, 70)
    assert notifications.of("rule_20_20_20") == []
    assert notifications.of("eye_blink"), "the others are not affected"

    # Switched on at minute 70: the next deadline on its own grid is minute
    # 80, not the three (or four) it would be owed if suppression had stood
    # still.
    source.schedule = None
    run_for(service, 30)

    shown = [(i["ist"] - at(FRIDAY, **EVENING)).total_seconds() / 60
             for i in notifications.of("rule_20_20_20")]
    assert shown, "it comes back once it is on"
    assert shown[0] >= 80 - 0.01, f"released a backlog: first at minute {shown[0]}"
    assert shown[0] < 82
    assert len([m for m in shown if m < 100]) == 1, "one reminder, not a burst"


def test_an_interval_reminder_is_shown_only_on_its_weekdays_in_ist():
    sched = payload(builtin=[builtin("hydrate", weekdays=WORKDAYS)])

    friday, friday_n, _ = make_wellbeing(sched, start=at(FRIDAY, **EVENING))
    run_for(friday, 130)
    sunday, sunday_n, _ = make_wellbeing(sched, start=at(SUNDAY, **EVENING))
    run_for(sunday, 130)

    assert friday_n.of("hydrate"), "Friday is allowed"
    assert sunday_n.of("hydrate") == [], "Sunday is not"
    assert sunday_n.of("posture"), "an unrestricted sibling still shows on Sunday"


def test_the_weekday_is_the_ist_weekday_not_the_utc_one():
    # 00:30 IST on Sunday is 19:00 UTC on Saturday. Saturday-only must not show.
    start = at(SUNDAY, 0, 30)
    service, notifications, _ = make_wellbeing(
        payload(builtin=[builtin("hydrate", weekdays=[5])]), start=start,
    )

    run_for(service, 100)

    assert notifications.of("hydrate") == []


def test_a_daily_reminder_uses_the_schedules_time():
    service, notifications, _ = make_wellbeing(
        payload(builtin=[builtin("lunch", time="13:45")]), start=at(FRIDAY, 13, 31),
    )

    run_as_the_loop_does(service, 20)

    (shown,) = notifications.of("lunch")
    offset = (shown["ist"] - at(FRIDAY, 13, 45)).total_seconds()
    assert 0 <= offset <= 2, f"shown {offset}s after the scheduled time"


def test_a_daily_reminder_moved_off_its_default_time_does_not_fire_at_the_old_one():
    service, notifications, _ = make_wellbeing(
        payload(builtin=[builtin("lunch", time="15:00")]), start=at(FRIDAY, 13, 29),
    )

    run_as_the_loop_does(service, 8)

    assert notifications.of("lunch") == []


def test_a_disabled_daily_reminder_is_not_shown():
    service, notifications, _ = make_wellbeing(
        payload(builtin=[builtin("lunch", enabled=False)]), start=at(FRIDAY, 13, 29),
    )

    run_as_the_loop_does(service, 8)

    assert notifications.of("lunch") == []


def test_a_daily_reminder_is_not_shown_on_a_weekday_it_is_not_allowed():
    service, notifications, _ = make_wellbeing(
        payload(builtin=[builtin("lunch", weekdays=WORKDAYS)]), start=at(SUNDAY, 13, 29),
    )

    run_as_the_loop_does(service, 8)

    assert notifications.of("lunch") == []


def test_a_custom_notification_is_shown_at_its_time_and_only_once_a_day():
    cache = FakeCache()
    service, notifications, _ = make_wellbeing(
        payload(custom=[custom()]), start=at(FRIDAY, 10, 20), cache=cache,
    )

    run_as_the_loop_does(service, 12 * 60)

    (shown,) = notifications.of("custom:abc123")
    assert shown["title"] == "Standup", "plain: no emoji prefix is added"
    assert shown["body"] == "Daily standup in 5 minutes."
    assert shown["level"] == "info"
    offset = (shown["ist"] - at(FRIDAY, 10, 25)).total_seconds()
    assert 0 <= offset <= 2, f"shown {offset}s after its time"
    assert json.loads(cache.store[DAILY_STATE_KEY])["custom:abc123"] == "2026-09-11@10:25"


def test_a_custom_notification_is_shown_again_the_next_day():
    service, notifications, _ = make_wellbeing(
        payload(custom=[custom(weekdays=ALL_DAYS)]), start=at(FRIDAY, 10, 20),
    )

    run_as_the_loop_does(service, 26 * 60)

    days = {i["ist"].date() for i in notifications.of("custom:abc123")}
    assert days == {FRIDAY.date(), (FRIDAY + timedelta(days=1)).date()}


def test_a_custom_notification_respects_its_weekdays():
    service, notifications, _ = make_wellbeing(
        payload(custom=[custom(weekdays=WORKDAYS)]), start=at(SUNDAY, 10, 20),
    )

    run_as_the_loop_does(service, 60)

    assert notifications.of("custom:abc123") == []


def test_a_custom_notification_more_than_the_grace_late_is_missed_not_shown():
    cache = FakeCache()
    service, notifications, _ = make_wellbeing(
        payload(custom=[custom(time="10:25")]), start=at(FRIDAY, 10, 41), cache=cache,
    )

    run_as_the_loop_does(service, 30)

    assert notifications.of("custom:abc123") == []
    assert json.loads(cache.store[DAILY_STATE_KEY])["custom:abc123"] == "2026-09-11@10:25", (
        "recorded as spent so it is not shown late on a later tick"
    )


def test_a_custom_notification_within_the_grace_is_shown_late():
    service, notifications, _ = make_wellbeing(
        payload(custom=[custom(time="10:25")]), start=at(FRIDAY, 10, 29),
    )

    run_as_the_loop_does(service, 5)

    assert notifications.of("custom:abc123")


def test_two_notifications_at_the_same_time_are_spaced_not_merged_or_lost():
    service, notifications, _ = make_wellbeing(
        payload(custom=[custom(time="10:30")]), start=at(FRIDAY, 10, 28),
    )

    run_as_the_loop_does(service, 6)

    tea, mine = notifications.of("tea_morning"), notifications.of("custom:abc123")
    assert len(tea) == 1 and len(mine) == 1
    gap = abs((mine[0]["ist"] - tea[0]["ist"]).total_seconds())
    assert gap >= WellbeingService.MIN_SPACING_SECONDS


def test_a_custom_notification_is_not_shown_while_signed_out():
    service, notifications, _ = make_wellbeing(
        payload(custom=[custom()]), start=at(FRIDAY, 10, 24),
    )
    service.runtime.api_client.access_token = None

    run_as_the_loop_does(service, 5)

    assert notifications.shown == []


def test_a_custom_notifications_deadline_wakes_the_loop():
    service, _, _ = make_wellbeing(
        payload(custom=[custom(time="10:25")]), start=at(FRIDAY, 10, 24),
    )
    service.tick()   # gated -> schedules
    delay_ms = service.tick()

    # Sixty seconds out, well inside the thirty-second idle ceiling's reach:
    # the loop must come back for it rather than sleep through it.
    assert delay_ms <= 30_000
    service._ist = at(FRIDAY, 10, 24, 50)
    service._clock += 50
    assert service.tick() <= 11_000


def test_the_daily_record_forgets_deleted_custom_notifications():
    cache = FakeCache({DAILY_STATE_KEY: json.dumps({
        "custom:gone": "2026-09-10",
        "custom:abc123": "2026-09-10",
        "tea_morning": "2026-09-10",
        "a_reminder_this_build_no_longer_has": "2026-09-10",
    })})
    service, notifications, _ = make_wellbeing(
        payload(custom=[custom()]), start=at(FRIDAY, 10, 20), cache=cache,
    )

    run_as_the_loop_does(service, 15)

    record = json.loads(cache.store[DAILY_STATE_KEY])
    assert "custom:gone" not in record
    assert "a_reminder_this_build_no_longer_has" not in record
    assert "custom:abc123" in record and "tea_morning" in record


def test_with_no_schedule_the_record_is_left_alone():
    cache = FakeCache({DAILY_STATE_KEY: json.dumps({"custom:abc123": "2026-09-10"})})
    service, _, _ = make_wellbeing(None, start=at(FRIDAY, 10, 20), cache=cache)

    run_as_the_loop_does(service, 15)

    assert "custom:abc123" in json.loads(cache.store[DAILY_STATE_KEY]), (
        "not knowing the schedule is not the same as a notification being deleted"
    )


def test_nothing_is_shown_until_the_schedule_has_been_loaded():
    service, notifications, source = make_wellbeing(
        payload(builtin=[builtin("lunch", enabled=False)]),
        start=at(FRIDAY, 13, 31), ready=False,
    )

    run_for(service, 3)
    assert notifications.shown == []

    source.ready = True
    run_for(service, 3)
    assert notifications.of("lunch") == [], "and the loaded schedule is the one honoured"


# ── Both services together: an administrator adds one while the desktop is running ──
#
# Every test above starts the scheduler with the notification already in the
# schedule. The case that matters to an administrator is the other one: the
# desktop has been up and signed in for hours, and *then* a notification is
# added. It reaches the desktop only through the schedule service's poll, which
# swaps the snapshot and wakes the scheduler -- so these run the two real
# services on one runtime, the way `ApplicationRuntime` wires them, and let the
# poll land at a chosen moment.

def make_running_desktop(start):
    notifications = FakeNotifications()
    cache = FakeCache()
    api = FakeScheduleApi(payload(version=0))   # nothing configured yet
    runtime = SimpleNamespace(
        api_client=SimpleNamespace(access_token="token"),
        network=SimpleNamespace(network_state=NetworkState.BACKEND_REACHABLE),
        notifications=notifications, cache=cache, storage=None,
    )
    wellbeing = WellbeingService(runtime, cache)
    wellbeing.DEFAULT_MAX_PER_HOUR = UNCAPPED
    wellbeing._clock = CLOCK_START
    wellbeing._ist = start
    wellbeing._now_monotonic = lambda: wellbeing._clock
    wellbeing._now_ist = lambda: wellbeing._ist
    notifications.ist = lambda: wellbeing._ist
    schedule = NotificationScheduleService(runtime, api, cache)
    runtime.notification_schedule = schedule
    # What `LoopService.wake()` does: run the next iteration now.
    runtime.wellbeing = SimpleNamespace(wake=wellbeing.tick)
    schedule.tick()   # the first tick reads the persisted record and asks nothing
    return wellbeing, schedule, api, notifications


def run_with_polls(wellbeing, schedule, minutes, polls_at):
    """Drive the scheduler on its own returned delays, and the schedule service's
    poll at each of `polls_at` (seconds from now)."""
    begin = wellbeing._clock
    end = begin + minutes * 60
    polls = sorted(begin + p for p in polls_at)
    while wellbeing._clock < end:
        delay_ms = wellbeing.tick()
        step = max(0.2, (wellbeing.interval_ms if delay_ms is None else delay_ms) / 1000.0)
        if polls and wellbeing._clock + step >= polls[0]:
            jump = max(0.0, polls.pop(0) - wellbeing._clock)
            wellbeing._clock += jump
            wellbeing._ist += timedelta(seconds=jump)
            schedule.tick()
            continue
        wellbeing._clock += step
        wellbeing._ist += timedelta(seconds=step)


def run_both_loops(wellbeing, schedule, seconds, admin=()):
    """Drive both services on the delays they return themselves -- the way the
    runtime does -- with nothing scripted about *when* the poll lands.

    `admin` is `[(seconds_from_now, action)]`: things the administrator does
    on the web, which the desktop only learns of through its own next poll.
    """
    begin = wellbeing._clock
    end = begin + seconds
    next_wellbeing = begin
    # `make_running_desktop` has made the schedule service's first tick, which
    # reads the persisted record and answers with the delay to its first poll.
    next_schedule = begin + schedule.FIRST_POLL_DELAY_MS / 1000.0
    actions = sorted(((begin + at, act) for at, act in admin), key=lambda item: item[0])
    while True:
        due = [next_wellbeing, next_schedule] + [t for t, _ in actions[:1]]
        now = min(due)
        if now >= end:
            return
        jump = max(0.0, now - wellbeing._clock)
        wellbeing._clock += jump
        wellbeing._ist += timedelta(seconds=jump)
        if actions and actions[0][0] <= now:
            actions.pop(0)[1]()
        if next_schedule <= now:
            delay = schedule.tick()
            next_schedule = now + (schedule.interval_ms if delay is None else delay) / 1000.0
        if next_wellbeing <= now:
            delay = wellbeing.tick()
            step = max(0.2, (wellbeing.interval_ms if delay is None else delay) / 1000.0)
            next_wellbeing = now + step


def test_a_notification_saved_a_minute_ahead_is_shown_on_time(monkeypatch):
    """The reported case: saved at 11:12:13 for 11:13, it was shown at 11:15.

    Worst case for the desktop: the poll jitter at its longest, and the
    administrator saves just after one poll has gone by.
    """
    import random
    monkeypatch.setattr(random, "random", lambda: 0.999)   # the longest jittered wait
    wellbeing, schedule, api, notifications = make_running_desktop(at(FRIDAY, 10, 20, 30))

    def administrator_adds_it():
        api.response = payload(version=1, custom=[custom(time="10:22")])

    # Polls land at +5s, then every ~34.5s (+39.5s, +74s...). Saving at +40s --
    # 0.5s after a poll -- is the unluckiest moment: 10:21:10 for 10:22:00.
    run_both_loops(wellbeing, schedule, 3 * 60, admin=[(40, administrator_adds_it)])

    (shown,) = notifications.of("custom:abc123")
    late = (shown["ist"] - at(FRIDAY, 10, 22)).total_seconds()
    assert 0 <= late <= 2, f"shown {late}s after its time"


def test_a_custom_notification_added_while_the_desktop_runs_is_shown_when_the_poll_brings_it():
    wellbeing, schedule, api, notifications = make_running_desktop(at(FRIDAY, 10, 20, 30))
    run_with_polls(wellbeing, schedule, 1, polls_at=[5])
    assert schedule.schedule.version == 0 and not schedule.schedule.custom

    # The administrator adds it for 10:23. The desktop's next poll lands at
    # 10:24:30 -- inside the five-minute poll the contract promises.
    api.response = payload(version=1, custom=[custom(time="10:23")])
    run_with_polls(wellbeing, schedule, 3.5, polls_at=[3 * 60])
    assert schedule.schedule.version == 1

    (shown,) = notifications.of("custom:abc123")
    assert (shown["title"], shown["body"]) == ("Standup", "Daily standup in 5 minutes.")
    late = (shown["ist"] - at(FRIDAY, 10, 23)).total_seconds()
    assert 0 < late <= WellbeingService.DAILY_GRACE_SECONDS, f"shown {late}s after its time"

    # ...and the next polls, which answer the same version, do not show it again.
    run_with_polls(wellbeing, schedule, 12, polls_at=[60, 6 * 60, 11 * 60])
    assert len(notifications.of("custom:abc123")) == 1


def test_a_poll_that_arrives_after_the_grace_window_misses_today_but_not_tomorrow():
    wellbeing, schedule, api, notifications = make_running_desktop(at(FRIDAY, 10, 20, 30))
    run_with_polls(wellbeing, schedule, 1, polls_at=[5])

    # Added for 10:23, but the poll that would carry it is more than the grace
    # window late (a machine asleep, a run of failed polls). A reminder that
    # arrives that long after its time is worse than none: it is spent for today.
    api.response = payload(version=1, custom=[custom(time="10:23", weekdays=ALL_DAYS)])
    late_poll = (WellbeingService.DAILY_GRACE_SECONDS + 3 * 60) + 90
    run_with_polls(wellbeing, schedule, late_poll / 60 + 1, polls_at=[late_poll])
    assert notifications.of("custom:abc123") == []

    # It is still on the schedule, so it is shown at its time the next day.
    wellbeing._ist = at(FRIDAY + timedelta(days=1), 10, 22, 30)
    run_with_polls(wellbeing, schedule, 3, polls_at=[30])
    assert len(notifications.of("custom:abc123")) == 1


def test_the_wellbeing_service_does_no_network_work():
    """The scheduler only reads the snapshot; fetching it is the other service's job."""
    source = Path(__file__).resolve().parent.parent / "background_services" / "wellbeing"
    for module in source.glob("*.py"):
        text = module.read_text(encoding="utf-8")
        for forbidden in ("httpx", "import requests", "app.api", "ApiError",
                          "get_schedule", "schedule_api", ".get(\"/"):
            assert forbidden not in text, f"{module.name} must not contain {forbidden!r}"

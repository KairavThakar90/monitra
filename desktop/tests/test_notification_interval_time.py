"""
A repeating reminder ("every 60 minutes") an administrator has given a time of day.

The administrator manages every notification by time. A built-in daily reminder
and a custom notification always had one; a repeating reminder is the third kind,
and until now the page offered it only an on/off switch and weekdays. Giving it a
time (`time: "12:40"` in the schedule) turns it into a time-of-day notification:

* it is shown **once a day at that time**, with its own wording, and **no longer
  repeats** -- "show it only at this time";
* it follows every daily rule: once per IST day, late beyond the grace window is
  missed, recorded as `date@HH:MM` so moving it fires it again, and shown at its
  time whatever the hourly limit says (a place is held for it in the hour before);
* with the time removed it goes back to its cadence, on its own grid.

Both services' clocks are driven by the tests, as in `test_notification_hourly_limit.py`.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from background_services.notifications.schedule_service import parse_schedule
from background_services.wellbeing import WellbeingService
from background_services.wellbeing.reminders import DAILY_REMINDERS, INTERVAL_REMINDERS
from background_services.wellbeing.wellbeing_service import DAILY_STATE_KEY
from core.time_format import IST

CLOCK_START = 1000.0
ALL_DAYS = [0, 1, 2, 3, 4, 5, 6]
FRIDAY = datetime(2026, 9, 11, tzinfo=IST)
HYDRATE = next(r for r in INTERVAL_REMINDERS if r.key == "hydrate")


def at(hour, minute, second=0, day=FRIDAY):
    return day.replace(hour=hour, minute=minute, second=second)


def entry(key, enabled=True, time=None, weekdays=None):
    return {"key": key, "enabled": enabled, "time": time, "weekdays": list(ALL_DAYS if weekdays is None else weekdays)}


def only_hydrate(time=None, enabled=True, weekdays=None, version=1, custom=(), max_per_hour=None):
    """A schedule in which every other built-in reminder is off, so what is
    shown is down to the one under test."""
    others = [
        entry(r.key, enabled=False)
        for r in (*INTERVAL_REMINDERS, *DAILY_REMINDERS) if r.key != "hydrate"
    ]
    body = {
        "version": version,
        "builtin": [entry("hydrate", enabled=enabled, time=time, weekdays=weekdays), *others],
        "custom": list(custom),
    }
    if max_per_hour is not None:
        body["max_per_hour"] = max_per_hour
    return body


def custom(id="abc123", title="Refresh", message="give the Project list", time="12:41"):
    return {"id": id, "title": title, "message": message, "time": time, "weekdays": ALL_DAYS}


class FakeNotifications:
    def __init__(self):
        self.shown = []
        self.ist = lambda: None

    def notify(self, message, level=None, title=None, key=None, link=None):
        self.shown.append({"body": message, "title": title, "key": key, "ist": self.ist()})
        return True

    def of(self, key):
        return [item for item in self.shown if item["key"] == f"wellbeing:{key}"]

    def times_of(self, key):
        return [item["ist"].strftime("%H:%M") for item in self.of(key)]


class FakeCache:
    def __init__(self, store=None):
        self.store = store if store is not None else {}

    def load_app_state(self, key):
        raw = self.store.get(key)
        return json.loads(raw) if raw is not None else None

    def save_app_state(self, key, value):
        self.store[key] = json.dumps(value)


def make(start, schedule_payload, cache=None):
    notifications = FakeNotifications()
    source = SimpleNamespace(schedule=parse_schedule(schedule_payload), ready=True)
    runtime = SimpleNamespace(
        api_client=SimpleNamespace(access_token="token"),
        notifications=notifications, cache=cache, storage=None, notification_schedule=source,
    )
    service = WellbeingService(runtime, cache)
    service._clock = CLOCK_START
    service._ist = start
    service._now_monotonic = lambda: service._clock
    service._now_ist = lambda: service._ist
    notifications.ist = lambda: service._ist
    return service, notifications, source


def run(service, minutes):
    """Tick, sleep for exactly what `tick()` asked for, tick again -- as the real loop does."""
    end = service._clock + minutes * 60
    while service._clock < end:
        delay_ms = service.tick()
        step = (service.interval_ms if delay_ms is None else delay_ms) / 1000.0
        service._clock += step
        service._ist += timedelta(seconds=step)


def most_in_any_hour(times):
    return max((sum(1 for o in times if t - timedelta(hours=1) < o <= t) for t in times), default=0)


# ── The schedule carries it ──────────────────────────────────────────────────

def test_a_repeating_reminders_time_survives_parsing_and_persisting():
    schedule = parse_schedule(only_hydrate(time="12:40"))

    assert schedule.builtin["hydrate"].at.strftime("%H:%M") == "12:40"
    assert parse_schedule(schedule.to_json()) == schedule          # what is persisted parses back equal
    assert next(b for b in schedule.to_json()["builtin"] if b["key"] == "hydrate")["time"] == "12:40"


def test_a_repeating_reminder_without_a_time_still_has_none():
    schedule = parse_schedule(only_hydrate(time=None))
    assert schedule.builtin["hydrate"].at is None


# ── "Show it only at this time" ──────────────────────────────────────────────

def test_it_is_shown_once_at_its_time_with_its_own_wording():
    service, notifications, _ = make(at(12, 0), only_hydrate(time="12:40"))

    run(service, 30)
    assert notifications.of("hydrate") == []                       # not before its time

    run(service, 20)                                               # to 12:50
    shown = notifications.of("hydrate")
    assert [i["ist"].strftime("%H:%M") for i in shown] == ["12:40"]
    assert (shown[0]["ist"] - at(12, 40)).total_seconds() <= 3     # on the minute, not a tick late
    assert (shown[0]["title"], shown[0]["body"]) == (HYDRATE.title, HYDRATE.body)


def test_it_no_longer_repeats_on_its_cadence():
    """Hydrate repeats every 60 minutes, first due 70 minutes into the session
    (13:10). With a time it is shown at that time and at no other."""
    service, notifications, _ = make(at(12, 0), only_hydrate(time="12:40"))

    run(service, 5 * 60)                                           # 12:00 -> 17:00

    assert notifications.times_of("hydrate") == ["12:40"]


def test_without_a_time_it_repeats_as_before():
    service, notifications, _ = make(at(12, 0), only_hydrate(time=None))

    run(service, 3 * 60 + 15)                                      # to 15:15

    # Due 70 minutes in, then every 60: 13:10, 14:10, 15:10.
    assert notifications.times_of("hydrate") == ["13:10", "14:10", "15:10"]


def test_it_is_shown_again_the_next_day_but_only_once_each_day():
    service, notifications, _ = make(at(12, 0), only_hydrate(time="12:40"))

    run(service, 26 * 60)                                          # into Saturday 14:00

    assert [i["ist"].strftime("%a %H:%M") for i in notifications.of("hydrate")] == ["Fri 12:40", "Sat 12:40"]


def test_it_respects_its_weekdays_and_on_off_state():
    monday_to_thursday = [0, 1, 2, 3]
    service, notifications, _ = make(at(12, 0), only_hydrate(time="12:40", weekdays=monday_to_thursday))
    run(service, 90)                                               # a Friday
    assert notifications.of("hydrate") == []

    service, notifications, _ = make(at(12, 0), only_hydrate(time="12:40", enabled=False))
    run(service, 5 * 60)
    assert notifications.of("hydrate") == []                       # off: neither at the time nor on the cadence


def test_more_than_the_grace_late_it_is_missed_not_shown():
    cache = FakeCache()
    service, notifications, _ = make(at(12, 55), only_hydrate(time="12:40"), cache)   # started 15 minutes late

    run(service, 30)

    assert notifications.of("hydrate") == []
    assert json.loads(cache.store[DAILY_STATE_KEY])["hydrate"] == "2026-09-11@12:40"   # spent for today


# ── It is a scheduled notification, like the others ─────────────────────────

def test_the_record_names_the_time_so_moving_it_fires_it_again():
    cache = FakeCache()
    service, notifications, source = make(at(12, 35), only_hydrate(time="12:40"), cache)

    run(service, 10)                                               # fires at 12:40
    assert notifications.times_of("hydrate") == ["12:40"]
    assert json.loads(cache.store[DAILY_STATE_KEY])["hydrate"] == "2026-09-11@12:40"

    source.schedule = parse_schedule(only_hydrate(time="12:50", version=2))   # the administrator moves it
    run(service, 15)

    assert notifications.times_of("hydrate") == ["12:40", "12:50"]
    assert json.loads(cache.store[DAILY_STATE_KEY])["hydrate"] == "2026-09-11@12:50"


def test_its_record_is_not_pruned_as_unknown():
    """A repeating reminder's key is not a daily reminder's, and the record is
    pruned of keys that name nothing. It must survive the ticks after it fired,
    or a restart would show it again."""
    cache = FakeCache()
    service, notifications, _ = make(at(12, 35), only_hydrate(time="12:40"), cache)

    run(service, 30)

    assert "hydrate" in json.loads(cache.store[DAILY_STATE_KEY])
    assert notifications.times_of("hydrate") == ["12:40"]


def test_a_restart_after_it_fired_does_not_show_it_again():
    cache = FakeCache()
    service, notifications, _ = make(at(12, 35), only_hydrate(time="12:40"), cache)
    run(service, 10)

    restarted, after_restart, _ = make(at(12, 45), only_hydrate(time="12:40"), cache)
    run(restarted, 20)

    assert notifications.times_of("hydrate") == ["12:40"]
    assert after_restart.of("hydrate") == []


def test_removing_the_time_puts_it_back_on_its_cadence_with_no_backlog():
    service, notifications, source = make(at(12, 0), only_hydrate(time="12:40"))
    run(service, 20)                                               # 12:20: still waiting for 12:40

    source.schedule = parse_schedule(only_hydrate(time=None, version=2))   # "repeat" again
    run(service, 3 * 60)                                           # to 15:20

    # Not at 12:40 any more -- and on its own grid (13:10, 14:10, 15:10), with
    # none of the periods it spent as a timed reminder owed to it.
    assert notifications.times_of("hydrate") == ["13:10", "14:10", "15:10"]


def test_it_is_shown_at_its_time_whatever_the_hourly_limit_says():
    """A limit of one an hour, and two scheduled notifications a minute apart:
    both are shown at their time, as an administrator's own notification always is."""
    service, notifications, _ = make(
        at(12, 30), only_hydrate(time="12:40", custom=[custom(time="12:41")], max_per_hour=1),
    )

    run(service, 20)

    assert notifications.times_of("hydrate") == ["12:40"]
    assert notifications.times_of("custom:abc123") == ["12:41"]


def test_a_place_is_held_for_it_so_repeating_reminders_cannot_use_the_hour_up():
    """Two an hour, and hydrate fixed to 12:50 while the other repeating
    reminders are running: the hour before it keeps a place for it, so the hour
    that ends with it holds two notifications, not three."""
    everything_on = {
        "version": 1,
        "builtin": [entry("hydrate", time="12:50")],
        "custom": [],
    }
    service, notifications, _ = make(at(12, 0), everything_on)

    run(service, 3 * 60)

    assert notifications.times_of("hydrate") == ["12:50"]
    assert most_in_any_hour([item["ist"] for item in notifications.shown]) <= 2

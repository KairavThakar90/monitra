"""
Two rules of the wellbeing scheduler that an administrator feels directly.

**1. Moving a notification re-arms it.** "Shown today" used to be recorded as the
date alone, so a notification that had already fired today at 11:13 and was then
edited to 12:40 never fired at 12:40 -- the administrator saw nothing at the time
they had just set (reproduced from a real session, 2026-10-08: the record held
`custom:8f747c55a50e: "2026-10-08"`). The record now carries the time it fired for,
`2026-10-08@11:13`, so a different time is a different turn. The same time is still
shown only once a day, and a date-only record from before this change is honoured.

**2. The desktop shows at most N notifications in any rolling hour** (2 unless an
administrator chose another number). An administrator's own notification and the
daily break times are shown *at their time* whatever the limit says -- they are
scheduled events, and holding one back would be the first bug again -- so the limit
governs the repeating reminders around them: it holds a place for each scheduled
notification that is coming, and lets the repeating ones share what is left. A
reminder the limit holds back is not dropped: it stays due, and when room opens the
one that has waited longest goes first.

Both services' clocks are driven by the tests, as in `test_wellbeing_reminders.py`.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from background_services.notifications.schedule_service import parse_schedule
from background_services.wellbeing import WellbeingService
from background_services.wellbeing.reminders import DEFAULT_MAX_PER_HOUR
from background_services.wellbeing.wellbeing_service import DAILY_STATE_KEY
from core.time_format import IST

CLOCK_START = 1000.0
ALL_DAYS = [0, 1, 2, 3, 4, 5, 6]
FRIDAY = datetime(2026, 9, 11, tzinfo=IST)


def at(hour, minute, second=0, day=FRIDAY):
    return day.replace(hour=hour, minute=minute, second=second)


def payload(version=1, custom=(), max_per_hour=None, builtin=()):
    body = {"version": version, "builtin": list(builtin), "custom": list(custom)}
    if max_per_hour is not None:
        body["max_per_hour"] = max_per_hour
    return body


def custom(id="abc123", title="Refresh", message="give the Project list", time="12:40"):
    return {"id": id, "title": title, "message": message, "time": time, "weekdays": ALL_DAYS}


class FakeNotifications:
    def __init__(self):
        self.shown = []
        self.ist = lambda: None

    def notify(self, message, level=None, title=None, key=None, link=None, native=False):
        self.shown.append({"body": message, "title": title, "key": key, "ist": self.ist()})
        return True

    def of(self, key):
        return [item for item in self.shown if item["key"] == f"wellbeing:{key}"]

    @property
    def times(self):
        return [item["ist"] for item in self.shown]


class FakeCache:
    def __init__(self, store=None):
        self.store = store if store is not None else {}

    def load_app_state(self, key):
        raw = self.store.get(key)
        return json.loads(raw) if raw is not None else None

    def save_app_state(self, key, value):
        self.store[key] = json.dumps(value)


def make(start, schedule_payload=None, cache=None):
    """A wellbeing service on the REAL default limit (nothing raised), with the
    schedule a poll would have delivered."""
    notifications = FakeNotifications()
    source = SimpleNamespace(
        schedule=None if schedule_payload is None else parse_schedule(schedule_payload),
        ready=True,
    )
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
    ticks = 0
    while service._clock < end:
        delay_ms = service.tick()
        ticks += 1
        step = (service.interval_ms if delay_ms is None else delay_ms) / 1000.0
        service._clock += step
        service._ist += timedelta(seconds=step)
    return ticks


def most_in_any_hour(times):
    """The most notifications that fell inside any one rolling hour."""
    best = 0
    for t in times:
        best = max(best, sum(1 for other in times if t - timedelta(hours=1) < other <= t))
    return best


# ── 1. Moving a notification re-arms it ──────────────────────────────────────

def test_a_notification_edited_to_a_later_time_after_it_fired_fires_again_at_the_new_time():
    """The reported bug, step for step."""
    cache = FakeCache()
    service, notifications, source = make(at(11, 10), payload(custom=[custom(time="11:13")]), cache)

    run(service, 6)                                  # it fires at 11:13
    assert [i["ist"].strftime("%H:%M") for i in notifications.of("custom:abc123")] == ["11:13"]
    assert json.loads(cache.store[DAILY_STATE_KEY])["custom:abc123"] == "2026-09-11@11:13"

    # The administrator edits the same notification (same id) to 12:40.
    source.schedule = parse_schedule(payload(version=2, custom=[custom(time="12:40", message="give the Project list")]))
    run(service, 100)

    shown = [i["ist"] for i in notifications.of("custom:abc123")]
    assert [t.strftime("%H:%M") for t in shown] == ["11:13", "12:40"], "it fired at the time they set"
    assert (shown[1] - at(12, 40)).total_seconds() <= 2
    assert json.loads(cache.store[DAILY_STATE_KEY])["custom:abc123"] == "2026-09-11@12:40"


def test_the_same_time_is_still_shown_only_once_a_day_even_if_the_text_is_edited():
    service, notifications, source = make(at(11, 10), payload(custom=[custom(time="11:13")]))
    run(service, 6)

    source.schedule = parse_schedule(payload(version=2, custom=[custom(time="11:13", message="a new sentence")]))
    run(service, 60)

    assert len(notifications.of("custom:abc123")) == 1


def test_moved_to_a_time_already_past_by_more_than_the_grace_it_is_missed_not_shown():
    service, notifications, source = make(at(11, 10), payload(custom=[custom(time="11:13")]))
    run(service, 6)

    source.schedule = parse_schedule(payload(version=2, custom=[custom(time="11:15")]))   # 11:15 is past by the grace
    service._ist = at(11, 40)
    service._clock += 30 * 60
    run(service, 20)

    assert len(notifications.of("custom:abc123")) == 1, "it is not shown late for a time that has long gone"


def test_a_builtin_daily_reminder_moved_later_fires_again_at_its_new_time():
    service, notifications, source = make(at(10, 28))
    run(service, 5)
    assert len(notifications.of("tea_morning")) == 1

    source.schedule = parse_schedule(payload(version=2, builtin=[
        {"key": "tea_morning", "enabled": True, "time": "11:20", "weekdays": ALL_DAYS},
    ]))
    run(service, 60)

    assert [i["ist"].strftime("%H:%M") for i in notifications.of("tea_morning")] == ["10:30", "11:20"]


def test_a_record_from_before_the_time_was_recorded_is_honoured_for_today():
    """An upgrade must not repeat what was already shown: a date-only record means
    "today, at whatever time it had"."""
    cache = FakeCache({DAILY_STATE_KEY: json.dumps({"custom:abc123": "2026-09-11"})})
    service, notifications, _ = make(at(12, 35), payload(custom=[custom(time="12:40")]), cache)

    run(service, 10)

    assert notifications.of("custom:abc123") == []


def test_a_date_only_record_is_replaced_by_the_full_one_the_next_day():
    cache = FakeCache({DAILY_STATE_KEY: json.dumps({"custom:abc123": "2026-09-11"})})
    next_day = FRIDAY + timedelta(days=1)
    service, notifications, _ = make(at(12, 35, day=next_day), payload(custom=[custom(time="12:40")]), cache)

    run(service, 10)

    assert len(notifications.of("custom:abc123")) == 1
    assert json.loads(cache.store[DAILY_STATE_KEY])["custom:abc123"] == "2026-09-12@12:40"


# ── 2. The hourly limit ──────────────────────────────────────────────────────

def test_the_default_is_two_an_hour():
    assert DEFAULT_MAX_PER_HOUR == 2
    assert WellbeingService.DEFAULT_MAX_PER_HOUR == 2


def test_no_more_than_two_notifications_are_shown_in_any_hour():
    service, notifications, _ = make(at(18, 0))   # the day's break times are behind it: repeating reminders only

    run(service, 6 * 60)

    assert most_in_any_hour(notifications.times) <= 2
    assert len(notifications.times) >= 8, "limited, not silenced: about two an hour still arrive"


def test_the_limit_is_the_administrators_number():
    for limit in (1, 4, 6):
        service, notifications, _ = make(at(18, 0), payload(max_per_hour=limit))
        run(service, 6 * 60)
        assert most_in_any_hour(notifications.times) <= limit, limit
        assert len(notifications.times) >= limit * 3, f"{limit}: still shown, not silenced"


def test_a_higher_limit_shows_more_than_the_default_would():
    low, low_notifications, _ = make(at(18, 0))
    high, high_notifications, _ = make(at(18, 0), payload(max_per_hour=6))
    run(low, 4 * 60)
    run(high, 4 * 60)

    assert len(high_notifications.times) > len(low_notifications.times)


def test_a_reminder_held_back_stays_due_and_the_one_that_waited_longest_goes_first():
    service, notifications, _ = make(at(18, 0))

    run(service, 100)

    keys = [i["key"].split(":", 1)[1] for i in notifications.shown]
    stamps = [(i["ist"] - at(18, 0)).total_seconds() / 60 for i in notifications.shown]
    # 20-20-20 falls due at 20 and 40 minutes, blinking at 35 and 65. Two are shown in the first hour (20, 35).
    assert keys[:2] == ["rule_20_20_20", "eye_blink"]
    assert stamps[0] == pytest.approx(20, abs=1) and stamps[1] == pytest.approx(35, abs=1)
    # The 40-minute 20-20-20 was held. It was not dropped: when the first of the two leaves the hour (at 80
    # minutes) the reminder that has been due longest goes first, and that is the 20-20-20 from minute 40,
    # not something that fell due later.
    assert keys[2] == "rule_20_20_20"
    assert stamps[2] == pytest.approx(80, abs=1.5)


def test_the_loop_does_not_spin_while_the_limit_holds_a_reminder():
    service, notifications, _ = make(at(18, 0))
    run(service, 45)                    # two shown, then 20-20-20 falls due at 40 and is held
    assert len(notifications.times) == 2

    ticks = run(service, 30)

    assert ticks <= 80, f"{ticks} ticks in 30 minutes: it is waking far more often than its half-minute ceiling"


def test_an_administrators_notification_is_shown_at_its_time_whatever_the_limit_says():
    # Started at 11:50: the repeating reminders begin at 12:10, 12:15 ... and would use the hour up.
    service, notifications, _ = make(at(11, 50), payload(custom=[custom(time="12:40")]))

    run(service, 70)

    (shown,) = notifications.of("custom:abc123")
    assert 0 <= (shown["ist"] - at(12, 40)).total_seconds() <= 2
    assert most_in_any_hour(notifications.times) <= 2, "a place was held for it, so the hour still comes to two"


def test_scheduled_notifications_are_never_held_back_even_when_there_are_more_than_the_limit():
    items = [custom(id=f"c{n}", title=f"T{n}", time=t) for n, t in enumerate(("12:40", "12:45", "12:50"))]
    service, notifications, _ = make(at(11, 50), payload(custom=items))

    run(service, 70)

    shown = {i["key"]: i["ist"] for i in notifications.shown}
    assert set(shown) == {"wellbeing:custom:c0", "wellbeing:custom:c1", "wellbeing:custom:c2"}, (
        "all three, and nothing else in that hour"
    )
    for key, expected in (("c0", at(12, 40)), ("c1", at(12, 45)), ("c2", at(12, 50))):
        assert 0 <= (shown[f"wellbeing:custom:{key}"] - expected).total_seconds() <= 2


def test_the_daily_break_times_count_toward_the_hour():
    service, notifications, _ = make(at(10, 5))     # tea at 10:30; repeating reminders from 10:25 on

    run(service, 70)

    assert len(notifications.of("tea_morning")) == 1
    assert most_in_any_hour(notifications.times) <= 2


# ── The limit as the schedule carries it ─────────────────────────────────────

@pytest.mark.parametrize("value,expected", [
    (1, 1), (2, 2), (6, 6), (12, 12),
    (0, None), (13, None), (-1, None), (2.5, None), ("2", None), (True, None), (None, None),
])
def test_the_schedule_keeps_a_usable_limit_and_ignores_anything_else(value, expected):
    schedule = parse_schedule({"version": 1, "builtin": [], "custom": [], "max_per_hour": value})

    assert schedule is not None, "an unusable limit never spoils the rest of the schedule"
    assert schedule.max_per_hour == expected


def test_a_schedule_without_a_limit_leaves_the_desktops_default_in_charge():
    older_backend = parse_schedule({"version": 1, "builtin": [], "custom": []})

    assert older_backend.max_per_hour is None
    service, notifications, _ = make(at(18, 0))
    assert service._max_per_hour(older_backend) == DEFAULT_MAX_PER_HOUR


def test_the_limit_survives_being_persisted_and_read_back():
    schedule = parse_schedule(payload(max_per_hour=4, custom=[custom()]))

    assert parse_schedule(json.loads(json.dumps(schedule.to_json()))).max_per_hour == 4
    assert parse_schedule(json.loads(json.dumps(schedule.to_json()))) == schedule

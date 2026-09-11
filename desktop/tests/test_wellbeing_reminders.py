"""
Wellbeing reminders: the catalogue, the cadence, and the times of day.

`tick()` is called directly here, the way `test_update_service.py` does it: it
is written to run off the GUI thread and touches no widgets, so it is
exercisable without starting the loop thread.

Both of the service's clocks are driven by the test rather than by sleeping.
Simulated time is advanced in real tick-sized steps by `run_for`, because the
scheduler deliberately treats a *large* jump as "the machine was asleep" and
restarts the cadence instead of replaying what was missed -- advancing twenty
minutes in one step would exercise that path rather than the ordinary one.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from background_services.wellbeing import (
    DAILY_REMINDERS,
    INTERVAL_REMINDERS,
    WellbeingService,
)
from background_services.wellbeing.wellbeing_service import DAILY_STATE_KEY
from core.time_format import IST

TICK_SECONDS = WellbeingService.interval_ms / 1000


class FakeNotifications:
    def __init__(self, fail: bool = False):
        self.shown: list[dict] = []
        self.fail = fail

    def notify(self, message, level=None, title=None, key=None, link=None):
        if self.fail:
            raise RuntimeError("tray exploded")
        self.shown.append({"body": message, "title": title, "key": key})
        return True

    @property
    def keys(self) -> list[str]:
        return [item["key"] for item in self.shown]


class FakeCache:
    """An app_state store that round-trips through JSON, like the real one."""

    def __init__(self, store: dict | None = None):
        self.store = store if store is not None else {}

    def load_app_state(self, key):
        raw = self.store.get(key)
        return json.loads(raw) if raw is not None else None

    def save_app_state(self, key, value):
        self.store[key] = json.dumps(value)


def make_service(*, signed_in=True, cache=None, start_ist=None, fail_notify=False):
    notifications = FakeNotifications(fail=fail_notify)
    runtime = SimpleNamespace(
        api_client=SimpleNamespace(access_token="token" if signed_in else None),
        notifications=notifications,
        cache=cache,
        storage=None,
    )
    service = WellbeingService(runtime, cache)
    service._clock = 1000.0
    service._ist = start_ist or datetime(2026, 9, 11, 9, 0, tzinfo=IST)
    service._now_monotonic = lambda: service._clock
    service._now_ist = lambda: service._ist
    return service, notifications


def run_for(service, minutes: float) -> None:
    """Advance simulated time in real tick-sized steps, ticking each time."""
    for _ in range(int(minutes * 60 // TICK_SECONDS)):
        service._clock += TICK_SECONDS
        service._ist += timedelta(seconds=TICK_SECONDS)
        service.tick()


def jump(service, **delta) -> None:
    """Advance both clocks in one step, as sleep or hibernation would."""
    seconds = timedelta(**delta).total_seconds()
    service._clock += seconds
    service._ist += timedelta(seconds=seconds)


# ── The catalogue ────────────────────────────────────────────────────────────

def test_the_specified_cadences_are_what_the_catalogue_says():
    cadence = {r.key: r.every_minutes for r in INTERVAL_REMINDERS}

    assert cadence["rule_20_20_20"] == 20
    assert cadence["eye_blink"] == 30
    assert cadence["hydrate"] == 60
    assert cadence["posture"] == 60
    assert cadence["short_walk"] == 120


def test_the_specified_daily_times_are_what_the_catalogue_says():
    at = {r.key: r.at for r in DAILY_REMINDERS}

    assert at["tea_morning"] == time(10, 30)
    assert at["lunch"] == time(13, 30)
    assert at["back_to_work"] == time(14, 15)
    assert at["tea_afternoon"] == time(16, 30)


def test_every_reminder_is_uniquely_keyed_and_has_real_text():
    reminders = list(INTERVAL_REMINDERS) + list(DAILY_REMINDERS)
    keys = [r.key for r in reminders]

    assert len(keys) == len(set(keys)), "reminder keys must be unique"
    for reminder in reminders:
        assert reminder.title.strip()
        assert reminder.body.strip()


def test_every_interval_is_a_positive_number_of_minutes():
    for reminder in INTERVAL_REMINDERS:
        assert reminder.every_minutes > 0


# ── Gating ───────────────────────────────────────────────────────────────────

def test_nothing_is_shown_while_signed_out():
    service, notifications = make_service(signed_in=False)

    service.tick()
    run_for(service, 180)

    assert notifications.shown == []


def test_signing_in_starts_the_cadence_from_that_moment():
    """Held time must not bank up into a burst at sign-in."""
    service, notifications = make_service(signed_in=False)
    run_for(service, 120)

    service.runtime.api_client.access_token = "token"
    service.tick()          # the tick that notices, and schedules
    assert notifications.shown == []

    run_for(service, 19)
    assert notifications.shown == []

    run_for(service, 2)
    assert notifications.keys == ["wellbeing:rule_20_20_20"]


# ── Interval cadence ─────────────────────────────────────────────────────────

def test_the_first_tick_only_starts_the_cadence():
    service, notifications = make_service()

    service.tick()

    assert notifications.shown == []


def test_a_reminder_appears_once_its_interval_has_passed():
    service, notifications = make_service()
    service.tick()

    run_for(service, 19)
    assert notifications.shown == []

    run_for(service, 2)
    assert notifications.keys == ["wellbeing:rule_20_20_20"]
    assert notifications.shown[0]["title"] == "👁️ Follow the 20-20-20 Rule"


def test_a_fired_reminder_waits_out_its_interval_again():
    service, notifications = make_service()
    service.tick()
    run_for(service, 21)
    assert notifications.keys.count("wellbeing:rule_20_20_20") == 1

    # Minute 36. Other cadences have come due in between (blinking is every
    # 30), so this counts only the reminder under test.
    run_for(service, 15)
    assert notifications.keys.count("wellbeing:rule_20_20_20") == 1, (
        "it must not repeat inside its own interval"
    )

    run_for(service, 6)
    assert notifications.keys.count("wellbeing:rule_20_20_20") == 2


def test_the_hourly_reminders_appear_on_the_hour():
    service, notifications = make_service()
    service.tick()

    run_for(service, 62)

    # 20-20-20 three times, blink twice, water and posture once each. They are
    # spread across ticks rather than stacked, but all of them arrive.
    assert notifications.keys.count("wellbeing:rule_20_20_20") == 3
    assert notifications.keys.count("wellbeing:eye_blink") == 2
    assert notifications.keys.count("wellbeing:hydrate") == 1
    assert notifications.keys.count("wellbeing:posture") == 1


def test_only_one_reminder_is_shown_per_tick():
    """Three cadences coincide every two hours; toasts must not stack."""
    service, notifications = make_service()
    service.tick()

    # Put several deadlines in the past, as coinciding cadences would.
    overdue = service._clock - 5
    for key in ("hydrate", "posture", "short_walk"):
        service._due_at[key] = overdue

    service._clock += TICK_SECONDS
    service.tick()

    assert len(notifications.shown) == 1


def test_the_most_overdue_reminder_goes_first():
    service, notifications = make_service()
    service.tick()

    service._due_at["posture"] = service._clock - 5
    service._due_at["hydrate"] = service._clock - 90

    service._clock += TICK_SECONDS
    service.tick()

    assert notifications.keys == ["wellbeing:hydrate"]


def test_a_long_gap_restarts_the_cadence_instead_of_replaying_it():
    """Waking from sleep must not deliver four hours of backlog at once."""
    service, notifications = make_service()
    service.tick()

    jump(service, hours=4)
    service.tick()
    assert notifications.shown == [], "the backlog must not be replayed"

    # And the cadence is running again from the moment of waking.
    run_for(service, 21)
    assert notifications.keys == ["wellbeing:rule_20_20_20"]


# ── Daily reminders ──────────────────────────────────────────────────────────

def _at(hour, minute, day=11):
    return datetime(2026, 9, day, hour, minute, tzinfo=IST)


def test_a_daily_reminder_fires_at_its_time():
    cache = FakeCache()
    service, notifications = make_service(cache=cache, start_ist=_at(10, 25))
    service.tick()

    run_for(service, 4)
    assert notifications.shown == []

    run_for(service, 2)
    assert notifications.keys == ["wellbeing:tea_morning"]
    assert notifications.shown[0]["title"] == "☕ Have a Tea Break"


def test_a_daily_reminder_does_not_fire_twice_in_one_day():
    cache = FakeCache()
    service, notifications = make_service(cache=cache, start_ist=_at(10, 25))
    service.tick()
    run_for(service, 8)

    assert notifications.keys.count("wellbeing:tea_morning") == 1


def test_a_daily_reminder_is_not_repeated_after_a_restart():
    """The regression this persistence exists for: reopening at 10:45."""
    cache = FakeCache()
    first, first_notifications = make_service(cache=cache, start_ist=_at(10, 25))
    first.tick()
    run_for(first, 8)
    assert first_notifications.keys == ["wellbeing:tea_morning"]

    # A new process, the same on-disk record, still inside the grace window.
    second, second_notifications = make_service(cache=cache, start_ist=_at(10, 33))
    second.tick()
    run_for(second, 10)

    assert second_notifications.shown == []
    assert json.loads(cache.store[DAILY_STATE_KEY])["tea_morning"] == "2026-09-11"


def test_a_daily_reminder_missed_by_hours_is_not_shown_late():
    """Opening the laptop at 18:00 must not produce a 10:30 tea break."""
    cache = FakeCache()
    service, notifications = make_service(cache=cache, start_ist=_at(18, 0))
    service.tick()
    run_for(service, 5)

    assert notifications.shown == []
    # All four are recorded as spent for the day, so none of them ambushes the
    # user later in the evening.
    recorded = json.loads(cache.store[DAILY_STATE_KEY])
    assert set(recorded) == {r.key for r in DAILY_REMINDERS}


def test_a_daily_reminder_fires_again_the_next_day():
    cache = FakeCache()
    service, notifications = make_service(cache=cache, start_ist=_at(10, 25))
    service.tick()
    run_for(service, 8)
    assert len(notifications.shown) == 1

    jump(service, days=1, minutes=-8)   # the next day, back at 10:25
    service.tick()                      # the jump restarts the cadence
    run_for(service, 8)

    assert notifications.keys.count("wellbeing:tea_morning") == 2


def test_a_daily_reminder_does_not_fire_before_its_time():
    cache = FakeCache()
    service, notifications = make_service(cache=cache, start_ist=_at(9, 0))
    service.tick()
    run_for(service, 15)

    assert notifications.shown == []


def test_a_time_of_day_reminder_is_preferred_over_an_interval_one():
    """Its window is minutes wide; an interval reminder can wait a tick."""
    cache = FakeCache()
    service, notifications = make_service(cache=cache, start_ist=_at(10, 29))
    service.tick()
    service._due_at["hydrate"] = service._clock - 600

    service._clock += TICK_SECONDS
    service._ist += timedelta(minutes=2)
    service.tick()

    assert notifications.keys == ["wellbeing:tea_morning"]


# ── Robustness ───────────────────────────────────────────────────────────────

def test_a_notification_failure_does_not_break_the_loop():
    service, _ = make_service(fail_notify=True)
    service.tick()

    run_for(service, 45)   # must not raise

    assert service._due_at, "the cadence is still scheduled"


def test_a_corrupt_daily_record_is_discarded_rather_than_fatal():
    cache = FakeCache({DAILY_STATE_KEY: '"not-a-dict"'})
    service, notifications = make_service(cache=cache, start_ist=_at(10, 25))
    service.tick()
    run_for(service, 8)

    assert notifications.keys == ["wellbeing:tea_morning"]


def test_the_service_runs_with_no_cache_at_all():
    service, notifications = make_service(cache=None, start_ist=_at(10, 25))
    service.tick()
    run_for(service, 8)

    assert notifications.keys == ["wellbeing:tea_morning"]


# ── The real loop thread ─────────────────────────────────────────────────────

def test_the_started_service_delivers_a_reminder_and_stops_cleanly(qapp):
    """Everything above drives `tick()` directly; this starts the real thing.

    It is what proves the service is wired to the runtime's loop machinery --
    that ticks actually happen on its own thread, that a reminder reaches the
    notification service from there, and that the thread stands down inside
    its budget rather than being terminated.
    """
    import itertools
    import time as real_time

    from PySide6.QtWidgets import QApplication

    service, notifications = make_service()
    # Each tick advances the cadence clock by one real tick's worth, so the
    # twenty-minute reminder comes due after ~40 ticks instead of 20 minutes.
    # The steps stay below RESUME_GAP_SECONDS, so this is the ordinary path.
    steps = itertools.count()
    service._now_monotonic = lambda: 1000.0 + TICK_SECONDS * next(steps)
    service.interval_ms = 5

    service.start()
    try:
        deadline = real_time.monotonic() + 10.0
        while not notifications.shown and real_time.monotonic() < deadline:
            QApplication.processEvents()
            real_time.sleep(0.01)
        assert notifications.shown, "the loop thread delivered no reminder"
        assert notifications.keys[0] == "wellbeing:rule_20_20_20"
    finally:
        stopped = service.stop(2000)

    assert stopped, "the loop thread did not stop within its budget"


# ── Runtime contract ─────────────────────────────────────────────────────────

def test_the_service_declares_a_stop_budget_and_a_name():
    from core.service import LoopService

    assert issubclass(WellbeingService, LoopService)
    assert WellbeingService.name == "wellbeing"
    assert WellbeingService.stop_timeout_ms > 0

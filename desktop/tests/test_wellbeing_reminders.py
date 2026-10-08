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
import math
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
from background_services.wellbeing.reminders import MIN_SEPARATION_MINUTES
from background_services.wellbeing.wellbeing_service import DAILY_STATE_KEY, PUSH_STATE_KEY
from core.time_format import IST

TICK_SECONDS = WellbeingService.interval_ms / 1000
#: Where the simulated monotonic clock starts. Arbitrary, and not zero so a
#: deadline computed from the clock cannot pass for one computed from nothing.
CLOCK_START = 1000.0
#: These tests are about the cadence, the spacing and the times of day, not the hourly limit, so the
#: services they build are not limited (the limit has its own tests in test_notification_hourly_limit.py).
UNCAPPED = 1000


class FakeNotifications:
    def __init__(self, fail: bool = False):
        self.shown: list[dict] = []
        self.fail = fail
        #: Reads the simulated cadence clock, so each reminder is recorded
        #: with the instant it was shown. Set by `make_service`.
        self.clock = lambda: 0.0

    def notify(self, message, level=None, title=None, key=None, link=None, native=False):
        if self.fail:
            raise RuntimeError("tray exploded")
        self.shown.append({"body": message, "title": title, "key": key, "at": self.clock()})
        return True

    @property
    def keys(self) -> list[str]:
        return [item["key"] for item in self.shown]

    def times(self, key: str) -> list[float]:
        """Seconds into the cadence at which `key` was shown, in order."""
        return [item["at"] for item in self.shown if item["key"] == f"wellbeing:{key}"]


class FakeCache:
    """An app_state store that round-trips through JSON, like the real one."""

    def __init__(self, store: dict | None = None):
        self.store = store if store is not None else {}

    def load_app_state(self, key):
        raw = self.store.get(key)
        return json.loads(raw) if raw is not None else None

    def save_app_state(self, key, value):
        self.store[key] = json.dumps(value)


def make_service(*, signed_in=True, cache=None, start_ist=None, fail_notify=False, max_per_hour=UNCAPPED):
    notifications = FakeNotifications(fail=fail_notify)
    runtime = SimpleNamespace(
        api_client=SimpleNamespace(access_token="token" if signed_in else None),
        notifications=notifications,
        cache=cache,
        storage=None,
    )
    service = WellbeingService(runtime, cache)
    service.DEFAULT_MAX_PER_HOUR = max_per_hour
    service._clock = CLOCK_START
    service._ist = start_ist or datetime(2026, 9, 11, 9, 0, tzinfo=IST)
    service._now_monotonic = lambda: service._clock
    service._now_ist = lambda: service._ist
    notifications.clock = lambda: service._clock - CLOCK_START
    return service, notifications


def make_evening_service():
    """A session with the day's time-of-day reminders already behind it.

    For tests about the interval cadence over many hours: started at 18:00
    IST, every daily reminder is long past its grace window (and the next
    10:30 is sixteen hours off), so the only reminders that can appear are
    the interval ones under test.
    """
    return make_service(start_ist=datetime(2026, 9, 11, 18, 0, tzinfo=IST))


def run_for(service, minutes: float) -> None:
    """Advance simulated time in real tick-sized steps, ticking each time."""
    for _ in range(int(minutes * 60 // TICK_SECONDS)):
        service._clock += TICK_SECONDS
        service._ist += timedelta(seconds=TICK_SECONDS)
        service.tick()


def run_as_the_loop_does(service, minutes: float) -> None:
    """Tick, sleep for exactly what `tick()` asked for, and tick again.

    This is what the real loop does (`_LoopWorker._iterate`), and it is the
    only way to measure *when* a reminder is shown: `run_for` ticks on a fixed
    half-minute grid whatever the service returns, so it cannot tell a service
    that wakes for its deadlines from one that does not.
    """
    end = service._clock + minutes * 60
    while service._clock < end:
        delay_ms = service.tick()
        step = (service.interval_ms if delay_ms is None else delay_ms) / 1000.0
        service._clock += step
        service._ist += timedelta(seconds=step)


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


def test_the_first_hours_follow_the_staggered_timetable():
    """What a session actually sees, minute by minute.

    This used to assert that 20-20-20, blink, water and posture had all
    arrived by minute 62 -- which they had, within ninety seconds of each
    other at the hour. That burst is the defect; the reminders are now
    staggered, and this is the timetable the stagger produces.
    """
    service, notifications = make_evening_service()
    service.tick()

    run_as_the_loop_does(service, 126)

    minute = lambda key: [round(t / 60, 2) for t in notifications.times(key)]  # noqa: E731
    assert minute("rule_20_20_20") == [20, 40, 60, 80, 100, 120]
    assert minute("eye_blink") == [35, 65, 95, 125]
    assert minute("hydrate") == [70]
    assert minute("posture") == [75]
    assert minute("wrist_stretch") == [115]
    assert minute("deep_breaths") == []          # first at 145
    assert minute("short_walk") == []            # first at 150


# ── Accuracy: on time, on its own grid, and never in a burst ─────────────────
#
# The owner's report (2026-09-30): the health reminders sometimes arrive "too
# quickly", for some users, some of the time. The log of a real session showed
# why. Every cadence started from the same instant, so they came due together
# at each common multiple, and one reminder went out per thirty-second tick:
#
#     11:32:46  Drink Water          12:02:47  Stretch Your Hands & Wrists
#     11:33:16  Fix Your Posture     12:03:17  Take Deep Breaths
#     11:33:46  Blink Your Eyes      12:03:47  Blink Your Eyes
#     11:34:16  20-20-20
#
# It only happens to a session that has run for an unbroken hour, which is
# why it was "some users, sometimes". Each reminder was also re-anchored on
# the moment it was shown, so the 20-20-20 above -- due at 11:32:46, shown at
# 11:34:16 -- was ninety seconds late for the rest of the session.

#: One full cycle of the catalogue: the least common multiple of the cadences,
#: after which the whole timetable repeats.
CYCLE_MINUTES = math.lcm(*(r.every_minutes for r in INTERVAL_REMINDERS))


def _catalogue_due_minutes(cycles: int = 2) -> list[tuple[int, str]]:
    """Every (minute, key) the catalogue itself says falls due, in order."""
    horizon = cycles * CYCLE_MINUTES + max(r.offset_minutes for r in INTERVAL_REMINDERS)
    due = []
    for reminder in INTERVAL_REMINDERS:
        at = reminder.offset_minutes + reminder.every_minutes
        while at <= horizon:
            due.append((at, reminder.key))
            at += reminder.every_minutes
    return sorted(due)


def test_the_catalogue_never_puts_two_reminders_within_five_minutes():
    """A property of the data, checked across the whole repeating timetable,
    so an edit to a cadence or an offset that recreates a collision fails
    here."""
    due = _catalogue_due_minutes()

    gaps = [
        (later[0] - earlier[0], earlier, later)
        for earlier, later in zip(due, due[1:])
    ]
    closest = min(gaps)

    assert closest[0] >= MIN_SEPARATION_MINUTES, (
        f"{closest[1][1]} (minute {closest[1][0]}) and {closest[2][1]} "
        f"(minute {closest[2][0]}) fall due {closest[0]} minutes apart"
    )


def test_an_offset_never_changes_how_often_a_reminder_repeats():
    for reminder in INTERVAL_REMINDERS:
        assert 0 <= reminder.offset_minutes < reminder.every_minutes, (
            f"{reminder.key}: an offset of a whole period or more only delays it"
        )


def test_no_two_reminders_arrive_within_five_minutes_in_a_long_session():
    """The same property, observed: two full cycles of the running service."""
    service, notifications = make_evening_service()
    service.tick()

    run_as_the_loop_does(service, 2 * CYCLE_MINUTES + 60)

    times = [item["at"] for item in notifications.shown]
    assert len(times) > 100, "the session must be long enough to mean something"
    closest = min(later - earlier for earlier, later in zip(times, times[1:]))
    assert closest >= MIN_SEPARATION_MINUTES * 60 - 1


def test_every_reminder_is_shown_when_it_falls_due():
    """On time to the second, not at whichever half-minute tick comes next."""
    service, notifications = make_evening_service()
    service.tick()

    run_as_the_loop_does(service, 2 * CYCLE_MINUTES)

    expected = [
        (minute * 60, key) for minute, key in _catalogue_due_minutes()
        if minute < 2 * CYCLE_MINUTES
    ]
    shown = [(item["at"], item["key"].split(":", 1)[1]) for item in notifications.shown]
    assert [key for _, key in shown] == [key for _, key in expected]
    latest = max(at - due for (at, _), (due, _) in zip(shown, expected))
    earliest = min(at - due for (at, _), (due, _) in zip(shown, expected))
    assert earliest >= 0, "a reminder was shown before it was due"
    assert latest < 1.0, f"a reminder was shown {latest:.1f}s after it was due"


def test_each_reminder_repeats_exactly_as_often_as_it_says():
    """Sixty minutes means sixty minutes between one and the next, for the
    whole session -- no drift."""
    service, notifications = make_evening_service()
    service.tick()

    run_as_the_loop_does(service, 2 * CYCLE_MINUTES + 60)

    for reminder in INTERVAL_REMINDERS:
        times = notifications.times(reminder.key)
        assert len(times) >= 3, f"{reminder.key} was shown {len(times)} times"
        gaps = {round(later - earlier, 3) for earlier, later in zip(times, times[1:])}
        assert gaps == {reminder.every_minutes * 60}, f"{reminder.key}: {sorted(gaps)}"


def test_a_reminder_shown_late_does_not_push_the_ones_after_it():
    """Its next deadline is one period after it was *due*.

    Re-anchoring on the moment it was shown made every late reminder
    permanent: the whole cadence slid later each time, by a different amount
    for each reminder.
    """
    service, notifications = make_service()
    service.tick()
    due = service._due_at["rule_20_20_20"]
    run_for(service, 19.5)                     # half a minute to go

    # The loop comes back ninety seconds after the deadline -- a stall shorter
    # than the gap that restarts the cadence.
    service._clock += 120
    service._ist += timedelta(seconds=120)
    assert service._clock == due + 90
    service.tick()

    assert notifications.keys == ["wellbeing:rule_20_20_20"]
    assert service._due_at["rule_20_20_20"] == due + 20 * 60


def test_deadlines_missed_by_whole_periods_are_skipped_not_banked():
    """Shown once for now, never twice for then -- and still on the grid."""
    service, _ = make_service()
    service.tick()
    reminder = next(r for r in INTERVAL_REMINDERS if r.key == "rule_20_20_20")
    period = reminder.every_minutes * 60
    due = service._due_at[reminder.key]
    now = due + 3 * period + 5

    service._advance(reminder, now)

    assert service._due_at[reminder.key] == due + 4 * period
    assert service._due_at[reminder.key] > now


# ── The loop's own timing ────────────────────────────────────────────────────

def test_the_loop_wakes_for_the_next_deadline():
    service, _ = make_service()
    service.tick()
    due = service._due_at["rule_20_20_20"]
    run_for(service, 19.5)                     # half a minute to go

    service._clock += 18
    service._ist += timedelta(seconds=18)
    assert service._clock == due - 12
    delay_ms = service.tick()

    assert delay_ms == 12_000


def test_the_loop_never_sleeps_longer_than_its_interval():
    """Signing out, and a gap in its own ticks, are found by polling."""
    service, _ = make_service()

    assert service.tick() == service.interval_ms      # the scheduling tick
    service._clock += TICK_SECONDS
    assert service.tick() == service.interval_ms


def test_the_loop_never_spins_on_a_deadline_it_cannot_act_on_yet():
    service, notifications = make_service()
    service.tick()
    # Two deadlines on the same instant: one is shown, the other has to wait
    # out the spacing window.
    service._due_at["hydrate"] = service._clock + 30
    service._due_at["posture"] = service._clock + 30

    service._clock += 30
    delay_ms = service.tick()

    assert len(notifications.shown) == 1
    assert delay_ms == service.interval_ms, "it must not wake before it may show anything"


def test_the_loop_wakes_for_a_time_of_day_reminder():
    cache = FakeCache()
    service, _ = make_service(cache=cache, start_ist=_at(10, 29))
    service.tick()

    service._clock += 45
    service._ist += timedelta(seconds=45)
    delay_ms = service.tick()

    assert delay_ms == 15_000          # 10:29:45 -> 10:30:00


# ── Spacing ──────────────────────────────────────────────────────────────────

def test_a_reminder_never_arrives_while_the_previous_one_is_still_on_screen():
    """What the catalogue cannot stagger: a time of day landing on an
    interval. The second one waits out the spacing window -- and its own grid
    does not move because it waited."""
    cache = FakeCache()
    service, notifications = make_service(cache=cache, start_ist=_at(10, 25))
    service.tick()
    due = service._clock + 5 * 60          # 10:30:00, the tea break's own time
    service._due_at["hydrate"] = due

    run_as_the_loop_does(service, 7)

    assert notifications.keys == ["wellbeing:tea_morning", "wellbeing:hydrate"]
    tea, water = (item["at"] for item in notifications.shown)
    assert tea == pytest.approx(5 * 60, abs=1)
    assert water - tea == pytest.approx(WellbeingService.MIN_SPACING_SECONDS, abs=1)
    assert service._due_at["hydrate"] == due + 60 * 60, "the wait moved its grid"


def test_the_spacing_window_outlasts_the_notification_card():
    from background_services.notifications.notification_service import NotificationService

    assert WellbeingService.MIN_SPACING_SECONDS * 1000 > NotificationService.DISPLAY_MS


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


def test_a_sleep_the_monotonic_clock_cannot_see_still_restarts_the_cadence():
    """On macOS and Linux `time.monotonic()` stands still while the machine
    is asleep. The loop then resumed mid-count: whatever had been a minute
    from due when the lid closed was shown a minute after it opened -- a
    reminder to take a break, to somebody just back from one. The wall clock
    sees the sleep, so it is read as well."""
    service, notifications = make_service()
    service.tick()
    run_for(service, 19)                       # 20-20-20 is a minute away
    assert notifications.shown == []

    # Two hours asleep: the wall clock moves, the monotonic one barely does.
    service._clock += TICK_SECONDS
    service._ist += timedelta(hours=2)
    service.tick()
    assert notifications.shown == [], "nothing is due the moment the lid opens"

    run_for(service, 19)
    assert notifications.shown == [], "the cadence restarted from the wake"
    run_for(service, 2)
    assert notifications.keys == ["wellbeing:rule_20_20_20"]


def test_a_clock_set_back_is_not_read_as_a_gap():
    """Only a forward jump is time the user may have been away."""
    service, notifications = make_service()
    service.tick()
    run_for(service, 19)
    deadlines = dict(service._due_at)

    service._clock += TICK_SECONDS
    service._ist -= timedelta(hours=3)
    service.tick()

    assert service._due_at == deadlines, "the cadence must carry on undisturbed"
    run_for(service, 1)
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


def test_a_daily_reminder_is_shown_at_its_time_to_the_second():
    """10:30 means 10:30:00, not the first half-minute tick after it."""
    cache = FakeCache()
    service, notifications = make_service(
        cache=cache, start_ist=datetime(2026, 9, 11, 10, 25, 17, tzinfo=IST)
    )
    service.tick()

    run_as_the_loop_does(service, 6)

    assert notifications.keys == ["wellbeing:tea_morning"]
    # 10:25:17 -> 10:30:00 is 283 seconds.
    assert notifications.shown[0]["at"] == pytest.approx(283, abs=1)


def test_a_daily_reminder_is_shown_before_its_record_is_written():
    """The write can wait on the database for as long as its busy timeout.

    It used to come first, so a locked database sat between a reminder's
    time and its appearing: in the session this was diagnosed from, the
    10:30 break was shown 14.5 seconds after the tick that found it due.
    """
    order = []

    class RecordingCache(FakeCache):
        def save_app_state(self, key, value):
            order.append(("saved", len(notifications.shown)))
            super().save_app_state(key, value)

    cache = RecordingCache()
    service, notifications = make_service(cache=cache, start_ist=_at(10, 29))
    service.tick()

    run_for(service, 2)

    assert notifications.keys == ["wellbeing:tea_morning"]
    assert order == [("saved", 1)], "the record was written before the reminder was shown"


def test_the_daily_record_is_read_when_the_cadence_starts():
    """The first read opens this thread's database connection. That belongs
    on the tick with nothing to be late for, not on the one that has a
    reminder to show."""
    reads = []

    class CountingCache(FakeCache):
        def load_app_state(self, key):
            reads.append(key)
            return super().load_app_state(key)

    service, _ = make_service(cache=CountingCache(), start_ist=_at(9, 0))

    service.tick()                              # the tick that starts the cadence

    # Both records -- the daily reminders' and the pushed messages' -- on this quiet tick.
    assert reads == [DAILY_STATE_KEY, PUSH_STATE_KEY]
    run_for(service, 5)
    assert reads == [DAILY_STATE_KEY, PUSH_STATE_KEY], "each record is read once per process"


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
    assert json.loads(cache.store[DAILY_STATE_KEY])["tea_morning"] == "2026-09-11@10:30"


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


def test_the_stop_budget_covers_a_write_waiting_on_the_database():
    """The one thing a tick can block in is the daily record's write, behind
    another writer, for as long as the connection's busy timeout. A budget
    shorter than that turns a quit during the wait into `terminate()`."""
    import re

    from storage import manager

    source = Path(manager.__file__).read_text(encoding="utf-8")
    busy_timeout_ms = int(re.search(r"PRAGMA busy_timeout=(\d+)", source).group(1))

    assert WellbeingService.stop_timeout_ms > busy_timeout_ms


# ── Diagnosability ────────────────────────────────────────────────────────────
#
# A service that is held before it ever runs used to say nothing whatsoever:
# `_gated` starts True, so the "held" branch never fired, and the cadence
# restart was not logged either. The whole feature was therefore silent in the
# log whether it was working or not, which is how "I get no reminders" became
# undiagnosable.

def test_a_service_held_from_startup_says_so_once(caplog):
    service, _ = make_service(signed_in=False)

    with caplog.at_level("INFO", logger="monitra.wellbeing"):
        service.tick()
        run_for(service, 5)

    held = [r for r in caplog.records if "reminders held" in r.getMessage()]
    assert len(held) == 1, "the hold must be reported exactly once, not per tick"
    assert "not signed in" in held[0].getMessage()


def test_the_cadence_start_is_logged_with_the_first_due_time(caplog):
    service, _ = make_service()

    with caplog.at_level("INFO", logger="monitra.wellbeing"):
        service.tick()

    started = [r for r in caplog.records if "cadence running" in r.getMessage()]
    assert len(started) == 1
    soonest = min(r.every_minutes for r in INTERVAL_REMINDERS)
    assert f"{soonest} minutes" in started[0].getMessage()


def test_a_hold_is_reported_again_after_reminders_resume(caplog):
    """Signing out, back in, and out again must not be swallowed as a repeat."""
    service, _ = make_service(signed_in=False)

    with caplog.at_level("INFO", logger="monitra.wellbeing"):
        service.tick()
        service.runtime.api_client.access_token = "token"
        service.tick()
        service.runtime.api_client.access_token = None
        service.tick()

    held = [r for r in caplog.records if "reminders held" in r.getMessage()]
    assert len(held) == 2

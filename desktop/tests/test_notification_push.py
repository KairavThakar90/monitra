"""
Push, on the desktop: hearing about a change at once, and showing a pushed message.

Three things are pinned here.

**1. The schedule carries pushed messages, and parses them defensively.** Each has
an age measured by the server; the desktop turns it into its own monotonic clock
once, when it parses, so its wall clock is never compared with the server's. A
push past its lifetime, or with nothing usable in it, is dropped. Pushes are not
persisted and not part of what two snapshots are compared on: a restart must never
replay one.

**2. `WellbeingService` shows a pushed message once, first, and at once.** Before
the daily and repeating reminders, never held back by the hourly limit, counted
toward the hour, recorded by id so it is not shown again, and dropped if it ran
out of time before it could be shown.

**3. `NotificationScheduleService` listens to the change stream.** The stream is an
accelerator, never the only path: an event means "fetch now"; a stream that fails
backs off (with jitter) while the poll carries on; a backend without it is not
asked again for minutes; a stream that ends at once is a failure, not a reconnect
loop; the loop leaves within a ping of being asked to stop, of signing out, or of
the network going; and the whole schedule is still fetched now and then even while
the stream says nothing.
"""
from __future__ import annotations

import json
import logging
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.api.exceptions import ApiConnectionError, ApiError, ApiTimeoutError
from background_services.network import NetworkState
from background_services.notifications.schedule_service import (
    NotificationScheduleService,
    parse_schedule,
)
from background_services.wellbeing import WellbeingService
from background_services.wellbeing.reminders import PUSH_TTL_SECONDS
from background_services.wellbeing.wellbeing_service import PUSH_STATE_KEY
from core.time_format import IST

CLOCK_START = 1000.0
ALL_DAYS = [0, 1, 2, 3, 4, 5, 6]
FRIDAY = datetime(2026, 9, 11, tzinfo=IST)


def at(hour, minute, second=0, day=FRIDAY):
    return day.replace(hour=hour, minute=minute, second=second)


def push(id="p1", title="Server restart", message="Please save your work.", seconds_ago=0):
    return {"id": id, "title": title, "message": message, "seconds_ago": seconds_ago}


def payload(version=1, pushes=(), builtin=(), custom=(), max_per_hour=None):
    body = {"version": version, "builtin": list(builtin), "custom": list(custom), "pushes": list(pushes)}
    if max_per_hour is not None:
        body["max_per_hour"] = max_per_hour
    return body


def everything_off():
    """Every built-in reminder switched off, so only what is under test is shown."""
    from background_services.wellbeing.reminders import DAILY_REMINDERS, INTERVAL_REMINDERS
    return [
        {"key": r.key, "enabled": False, "time": None, "weekdays": ALL_DAYS}
        for r in (*INTERVAL_REMINDERS, *DAILY_REMINDERS)
    ]


# ── 1. Parsing ───────────────────────────────────────────────────────────────

def test_a_push_is_parsed_with_its_lifetime_anchored_to_this_machines_clock():
    schedule = parse_schedule(payload(pushes=[push(seconds_ago=90)]), now_mono=5000.0)

    (pushed,) = schedule.pushes
    assert (pushed.id, pushed.title, pushed.message) == ("p1", "Server restart", "Please save your work.")
    assert pushed.expires_at_mono == 5000.0 + (PUSH_TTL_SECONDS - 90)


@pytest.mark.parametrize("bad", [
    "junk", None, 5, {}, {"id": "x"},
    push(id=""), push(id=7), push(title=" "), push(message=""), push(title=None),
    push(seconds_ago=-1), push(seconds_ago=PUSH_TTL_SECONDS + 1), push(seconds_ago="5"), push(seconds_ago=True),
    {"id": "x", "title": "t", "message": "m"},
])
def test_a_push_with_nothing_usable_is_dropped_and_the_rest_survive(bad):
    schedule = parse_schedule(payload(pushes=[bad, push(id="good")]), now_mono=0.0)
    assert [p.id for p in schedule.pushes] == ["good"]


def test_a_push_at_the_very_edge_of_its_lifetime_is_still_taken():
    assert len(parse_schedule(payload(pushes=[push(seconds_ago=PUSH_TTL_SECONDS)]), now_mono=0.0).pushes) == 1


def test_a_duplicate_id_keeps_the_first():
    schedule = parse_schedule(payload(pushes=[push(id="a", title="First"), push(id="a", title="Second")]), now_mono=0.0)
    assert [p.title for p in schedule.pushes] == ["First"]


@pytest.mark.parametrize("value", [None, "x", 5, {"id": "a"}])
def test_pushes_that_are_not_a_list_do_not_spoil_the_schedule(value):
    body = payload()
    body["pushes"] = value
    schedule = parse_schedule(body, now_mono=0.0)
    assert schedule is not None and schedule.pushes == ()


def test_a_response_from_a_backend_without_pushes_still_parses():
    body = payload()
    del body["pushes"]
    assert parse_schedule(body).pushes == ()


def test_pushes_are_neither_persisted_nor_compared():
    with_push = parse_schedule(payload(version=3, pushes=[push()]), now_mono=0.0)
    without = parse_schedule(payload(version=3), now_mono=0.0)

    assert "pushes" not in with_push.to_json()
    assert with_push == without                                   # two readings of one version are the same schedule
    assert parse_schedule(with_push.to_json(), now_mono=0.0).pushes == ()


# ── 2. Showing a pushed message ──────────────────────────────────────────────

class FakeNotifications:
    def __init__(self):
        self.shown = []
        self.ist = lambda: None

    def notify(self, message, level=None, title=None, key=None, link=None, native=False):
        self.shown.append({"body": message, "title": title, "key": key, "ist": self.ist()})
        return True

    def of(self, key):
        return [item for item in self.shown if item["key"] == key]

    def pushes(self):
        return [item for item in self.shown if item["key"].startswith("wellbeing:push:")]


class FakeCache:
    def __init__(self, store=None):
        self.store = store if store is not None else {}

    def load_app_state(self, key):
        raw = self.store.get(key)
        return json.loads(raw) if raw is not None else None

    def save_app_state(self, key, value):
        self.store[key] = json.dumps(value)


def make(start, schedule_payload, cache=None, signed_in=True):
    notifications = FakeNotifications()
    source = SimpleNamespace(schedule=parse_schedule(schedule_payload, now_mono=CLOCK_START), ready=True)
    runtime = SimpleNamespace(
        api_client=SimpleNamespace(access_token="token" if signed_in else None),
        notifications=notifications, cache=cache, storage=None, notification_schedule=source,
    )
    service = WellbeingService(runtime, cache)
    service._clock = CLOCK_START
    service._ist = start
    service._now_monotonic = lambda: service._clock
    service._now_ist = lambda: service._ist
    notifications.ist = lambda: service._ist
    return service, notifications, source, runtime


def run(service, minutes):
    end = service._clock + minutes * 60
    while service._clock < end:
        delay_ms = service.tick()
        step = (service.interval_ms if delay_ms is None else delay_ms) / 1000.0
        service._clock += step
        service._ist += timedelta(seconds=step)


def test_a_pushed_message_is_shown_once_on_the_next_tick_with_its_own_words():
    service, notifications, _, _ = make(at(12, 0), payload(builtin=everything_off(), pushes=[push()]))

    service.tick()                     # starts the cadence
    service.tick()

    (shown,) = notifications.pushes()
    assert (shown["title"], shown["body"], shown["key"]) == ("Server restart", "Please save your work.", "wellbeing:push:p1")


def test_it_is_shown_only_once_however_long_it_stays_in_the_schedule():
    service, notifications, _, _ = make(at(12, 0), payload(builtin=everything_off(), pushes=[push()]))

    run(service, 9)

    assert len(notifications.pushes()) == 1


def test_it_is_recorded_so_a_restart_does_not_show_it_again():
    cache = FakeCache()
    first, notifications, _, _ = make(at(12, 0), payload(builtin=everything_off(), pushes=[push()]), cache)
    run(first, 3)
    assert json.loads(cache.store[PUSH_STATE_KEY]) == ["p1"]

    restarted, after_restart, _, _ = make(at(12, 5), payload(builtin=everything_off(), pushes=[push()]), cache)
    run(restarted, 3)

    assert len(notifications.pushes()) == 1 and after_restart.pushes() == []


def test_a_push_that_ran_out_of_time_before_it_could_be_shown_is_dropped():
    service, notifications, _, _ = make(at(12, 0), payload(builtin=everything_off(), pushes=[push(seconds_ago=PUSH_TTL_SECONDS - 5)]))
    service.tick()                                       # starts the cadence
    service._clock += 30                                 # ... and 30 s pass before the next tick (the lifetime was 5 s from over)
    service._ist += timedelta(seconds=30)

    run(service, 5)

    assert notifications.pushes() == []


def test_it_comes_before_a_due_daily_reminder_and_a_due_repeating_one():
    """12:00 sharp: the push, a daily reminder at 12:00 and a custom one at 12:00 are all due."""
    from background_services.wellbeing.reminders import DAILY_REMINDERS
    lunch = next(r for r in DAILY_REMINDERS if r.key == "lunch")
    builtin = [b for b in everything_off() if b["key"] != "lunch"] + [
        {"key": "lunch", "enabled": True, "time": "12:00", "weekdays": ALL_DAYS},
    ]
    service, notifications, _, _ = make(at(11, 59, 30), payload(builtin=builtin, pushes=[push()],
                                                                custom=[{"id": "c1", "title": "Standup", "message": "m", "time": "12:00", "weekdays": ALL_DAYS}]))

    run(service, 4)

    keys = [item["key"] for item in notifications.shown]
    assert keys[0] == "wellbeing:push:p1"
    assert set(keys) == {"wellbeing:push:p1", "wellbeing:lunch", "wellbeing:custom:c1"}
    assert lunch.key == "lunch"


def test_it_is_never_held_back_by_the_hourly_limit_but_counts_toward_it():
    """A limit of one an hour, and a notification already shown this hour."""
    schedule = payload(
        builtin=everything_off(), max_per_hour=1,
        custom=[{"id": "c1", "title": "Standup", "message": "m", "time": "12:01", "weekdays": ALL_DAYS}],
    )
    service, notifications, source, _ = make(at(12, 0, 30), schedule)
    run(service, 2)                                      # the custom notification is shown at 12:01
    assert len(notifications.of("wellbeing:custom:c1")) == 1

    source.schedule = parse_schedule({**schedule, "version": 2, "pushes": [push()]}, now_mono=service._clock)
    run(service, 2)

    assert len(notifications.pushes()) == 1              # shown although the hour's one was used
    assert len(service._recent_shown) == 2               # ... and counted


def test_two_pushes_are_both_shown_in_order_a_minute_apart():
    service, notifications, _, _ = make(at(12, 0), payload(builtin=everything_off(), pushes=[push(id="a", title="First"), push(id="b", title="Second")]))

    run(service, 4)

    assert [item["title"] for item in notifications.pushes()] == ["First", "Second"]
    first, second = notifications.pushes()
    assert (second["ist"] - first["ist"]).total_seconds() >= WellbeingService.MIN_SPACING_SECONDS


def test_a_waiting_push_wakes_the_loop_instead_of_waiting_out_its_sleep():
    service, _, _, _ = make(at(12, 0), payload(builtin=everything_off(), pushes=[push()]))
    service.tick()                                       # cadence started; the push has not been shown yet

    assert service.tick() is not None
    # With a push waiting, the next look is as soon as the spacing rule allows -- not 30 seconds away.
    fresh, _, _, _ = make(at(12, 0), payload(builtin=everything_off(), pushes=[push(id="b")]))
    fresh.tick()
    delay = fresh._next_delay_ms(fresh._clock, fresh._ist, fresh._schedule())
    assert delay <= WellbeingService.MIN_TICK_MS


def test_nothing_is_shown_while_signed_out_and_a_push_still_in_time_is_shown_on_signing_in():
    service, notifications, _, runtime = make(at(12, 0), payload(builtin=everything_off(), pushes=[push(seconds_ago=0)]), signed_in=False)

    run(service, 2)
    assert notifications.pushes() == []

    runtime.api_client.access_token = "token"
    run(service, 2)

    assert len(notifications.pushes()) == 1


def test_a_push_still_in_the_schedule_after_a_newer_version_arrives_is_not_shown_again():
    service, notifications, source, _ = make(at(12, 0), payload(builtin=everything_off(), pushes=[push()]))
    run(service, 3)

    source.schedule = parse_schedule(payload(version=2, builtin=everything_off(), pushes=[push()]), now_mono=service._clock)
    run(service, 3)

    assert len(notifications.pushes()) == 1


def test_no_pushes_changes_nothing_about_the_other_reminders():
    service, notifications, _, _ = make(at(12, 0), payload(version=1))

    run(service, 75)

    assert notifications.pushes() == []
    assert notifications.shown                           # the repeating reminders still run


def test_a_damaged_push_record_degrades_to_nothing_remembered():
    cache = FakeCache({PUSH_STATE_KEY: json.dumps({"not": "a list"})})
    service, notifications, _, _ = make(at(12, 0), payload(builtin=everything_off(), pushes=[push()]), cache)

    run(service, 3)

    assert len(notifications.pushes()) == 1


# ── 3. The change stream ─────────────────────────────────────────────────────

class Clock:
    def __init__(self):
        self.t = 5000.0


def quiet(clock):
    """The server saying nothing but a ping every two seconds, for as long as it is read."""
    yield ": connected"
    yield ""
    while True:
        clock.t += 2.0
        yield ": ping"
        yield ""


def event(version, after=3.0):
    def script(clock):
        yield ": connected"
        yield ""
        clock.t += after
        yield "event: schedule"
        yield f'data: {{"version": {version}}}'
        yield ""
    return script


def nothing(clock):
    return iter(())


def fails_after(exc, seconds=2.0):
    def script(clock):
        yield ": connected"
        yield ""
        clock.t += seconds
        raise exc
    return script


def short(seconds):
    """Connects, says hello, and closes after `seconds`."""
    def script(clock):
        yield ": connected"
        yield ""
        clock.t += seconds
    return script


class FakeStream:
    def __init__(self, clock, script):
        self._clock = clock
        self._script = script

    def lines(self):
        return self._script(self._clock)


class StreamApi:
    def __init__(self, clock):
        self.clock = clock
        self.payload = payload(version=1)
        self.fetch_error = None
        self.fetches = []
        self.opens = []
        self.scripts = []
        self.open_error = None

    def get_schedule(self):
        self.fetches.append(self.clock.t)
        if self.fetch_error is not None:
            raise self.fetch_error
        return json.loads(json.dumps(self.payload))

    @contextmanager
    def open_stream(self, since, *, read_timeout):
        self.opens.append((self.clock.t, since))
        if self.open_error is not None:
            raise self.open_error
        script = self.scripts.pop(0) if self.scripts else quiet
        yield FakeStream(self.clock, script)


class Logs(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())

    def with_prefix(self, prefix):
        return [m for m in self.messages if m.startswith(prefix)]


def make_listener(*, signed_in=True, network_state=NetworkState.BACKEND_REACHABLE):
    clock = Clock()
    runtime = SimpleNamespace(
        api_client=SimpleNamespace(access_token="token" if signed_in else None),
        network=SimpleNamespace(network_state=network_state),
        wellbeing=SimpleNamespace(wake=lambda: None),
        storage=None,
    )
    api = StreamApi(clock)
    service = NotificationScheduleService(runtime, api, FakeCache())
    service._monotonic = lambda: clock.t
    logs = Logs()
    service.log.addHandler(logs)
    service.log.setLevel(logging.DEBUG)
    service.tick()                                       # reads the persisted record; asks nothing
    return service, api, clock, runtime, logs


def test_it_fetches_once_then_listens_and_does_not_fetch_again_while_the_stream_is_quiet():
    service, api, clock, _, logs = make_listener()

    assert service.tick() == 0                           # fetched, listened ~22 s, came back to listen again
    assert service.tick() == 0
    assert service.tick() == 0

    assert len(api.fetches) == 1                         # one read of the schedule, not one per listen
    assert [since for _, since in api.opens] == [1, 1, 1]  # always listening from the version it has
    assert len(logs.with_prefix("NOTIFICATION_STREAM_UP")) == 1


def test_an_event_ends_the_listening_and_the_next_tick_fetches_the_new_schedule():
    service, api, clock, _, logs = make_listener()
    api.scripts = [event(2)]
    api.payload = payload(version=1)
    service.tick()                                       # fetch v1, listen -> event for v2
    assert service._need_fetch

    api.payload = payload(version=2, pushes=[push()])
    delay = service.tick()                               # fetch v2 (the event's delay was returned by the previous tick)

    assert len(api.fetches) == 2
    assert service.schedule.version == 2
    assert [p.id for p in service.schedule.pushes] == ["p1"]
    assert api.opens[-1][1] == 2                         # and it listens on from the new version
    assert logs.with_prefix("NOTIFICATION_STREAM_EVENT")


def test_the_tick_that_hears_an_event_asks_to_come_back_almost_at_once():
    service, api, clock, _, _ = make_listener()
    api.scripts = [event(2)]

    delay = service.tick()

    assert delay == NotificationScheduleService.EVENT_FETCH_DELAY_MS


def test_a_pushed_message_reaches_the_snapshot_within_seconds_of_the_event():
    service, api, clock, _, _ = make_listener()
    api.scripts = [event(2, after=4.0)]
    started = clock.t

    service.tick()
    api.payload = payload(version=2, pushes=[push(seconds_ago=1)])
    clock.t += NotificationScheduleService.EVENT_FETCH_DELAY_MS / 1000
    service.tick()

    assert [p.id for p in service.schedule.pushes] == ["p1"]
    assert api.fetches[-1] - started < 10                # fetched seconds after the change, not a poll interval later


def test_the_same_version_signalled_twice_without_progress_is_a_failure_not_a_loop():
    service, api, clock, _, logs = make_listener()
    api.scripts = [event(2), event(2)]
    api.payload = payload(version=1)                     # the fetch never catches up with the event

    first = service.tick()
    second = service.tick()

    assert first == NotificationScheduleService.EVENT_FETCH_DELAY_MS
    assert second > NotificationScheduleService.EVENT_FETCH_DELAY_MS * 10   # backed off, not 300 ms again
    assert logs.with_prefix("NOTIFICATION_STREAM_FAILED")
    assert service._stream_blocked_until > clock.t


def test_a_response_it_cannot_parse_is_not_signalled_again_and_again():
    """The server's version is what the stream is told, even when the rest of the response is unusable."""
    service, api, clock, _, _ = make_listener()
    api.payload = {"version": 7, "builtin": "not a list", "custom": []}

    service.tick()

    assert service.schedule is None
    assert api.opens[-1][1] == 7


def test_a_backend_without_the_stream_is_polled_and_not_asked_again_for_minutes():
    service, api, clock, _, logs = make_listener()
    api.open_error = ApiError("no route", status_code=404)

    first = service.tick()                               # fetches, tries the stream, learns it is absent
    assert len(api.opens) == 1 and len(logs.with_prefix("NOTIFICATION_STREAM_UNAVAILABLE")) == 1

    for _ in range(5):
        clock.t += 31
        assert service.tick() >= 25_000                  # an ordinary poll interval (jittered)

    assert len(api.opens) == 1                           # never asked again within the five minutes
    assert len(api.fetches) == 6                         # ... and it fetched at every poll instead

    clock.t += NotificationScheduleService.STREAM_ABSENT_MS / 1000 + 5
    service.tick()
    assert len(api.opens) == 2                           # and tries again after them


def test_a_failing_stream_backs_off_with_doubling_jittered_waits_while_the_poll_carries_on():
    service, api, clock, _, logs = make_listener()
    api.open_error = ApiConnectionError("down")
    waits = []

    for _ in range(7):
        clock.t = max(clock.t, service._stream_blocked_until) + 0.01     # the moment the block lifts
        before = clock.t
        service.tick()
        waits.append(service._stream_blocked_until - before)

    base = NotificationScheduleService.STREAM_RETRY_BASE_MS / 1000
    cap = NotificationScheduleService.STREAM_RETRY_MAX_MS / 1000
    for index, wait in enumerate(waits):
        expected = min(cap, base * 2 ** index)
        assert expected * 0.85 - 0.5 <= wait <= expected * 1.15 + 0.5, (index, wait, expected)
    assert waits[-1] > waits[0] * 8                       # it really did grow
    assert len(logs.with_prefix("NOTIFICATION_STREAM_FAILED")) == 7


def test_while_the_stream_is_blocked_every_tick_is_an_ordinary_poll():
    service, api, clock, _, _ = make_listener()
    api.open_error = ApiConnectionError("down")
    service.tick()

    fetches = len(api.fetches)
    clock.t += 1
    delay = service.tick()

    assert len(api.fetches) == fetches + 1               # fetched again: that is the poll
    assert 25_000 <= delay <= 35_000
    assert len(api.opens) == 1                           # and did not try the stream


def test_a_stream_that_ends_at_once_or_straight_after_hello_is_a_failure_not_a_reconnect_loop():
    for script in (nothing, short(0.5)):
        service, api, clock, _, logs = make_listener()
        api.scripts = [script]

        delay = service.tick()

        assert delay >= 25_000, script
        assert logs.with_prefix("NOTIFICATION_STREAM_FAILED"), script
        assert service._stream_blocked_until > clock.t, script


def test_a_stream_that_runs_its_course_is_healthy_and_resets_the_failures():
    service, api, clock, _, logs = make_listener()
    service._stream_failures = 4

    service.tick()                                       # listens for ~22 s, then comes back

    assert service._stream_failures == 0
    assert service._stream_up
    assert not logs.with_prefix("NOTIFICATION_STREAM_FAILED")


def test_silence_on_the_stream_is_a_failure_and_so_is_a_connection_cut_mid_stream():
    for exc in (ApiTimeoutError("silent"), ApiConnectionError("cut")):
        service, api, clock, _, logs = make_listener()
        api.scripts = [fails_after(exc)]

        delay = service.tick()

        assert delay >= 25_000, exc
        assert logs.with_prefix("NOTIFICATION_STREAM_FAILED"), exc


def test_it_leaves_the_stream_within_a_ping_of_being_asked_to_stop():
    service, api, clock, _, _ = make_listener()

    def stop_after_a_while(clock_):
        yield ": connected"
        yield ""
        clock_.t += 2
        yield ": ping"
        service._stop_requested = True                   # the application is closing
        clock_.t += 2
        yield ": ping"                                   # the next ping after the request
        raise AssertionError("it kept reading after it was asked to stop")

    api.scripts = [stop_after_a_while]

    service.tick()                                       # returns instead of reading on or failing

    assert len(api.fetches) == 1


def test_it_leaves_the_stream_when_the_user_signs_out_or_the_network_goes():
    for change, expected in (
        (lambda rt: setattr(rt.api_client, "access_token", None), NotificationScheduleService.HOLD_INTERVAL_MS),
        (lambda rt: setattr(rt.network, "network_state", NetworkState.NO_NETWORK), NotificationScheduleService.HOLD_INTERVAL_MS),
    ):
        service, api, clock, runtime, logs = make_listener()

        def goes_wrong(clock_, runtime=runtime, change=change):
            yield ": connected"
            yield ""
            change(runtime)
            clock_.t += 2
            yield ": ping"
            raise AssertionError("it kept reading after the user signed out / the network went")

        api.scripts = [goes_wrong]

        assert service.tick() == expected
        assert not logs.with_prefix("NOTIFICATION_STREAM_FAILED")     # a hold is not a failure


def test_the_whole_schedule_is_still_fetched_now_and_then_while_the_stream_says_nothing():
    service, api, clock, _, _ = make_listener()
    service.tick()
    assert len(api.fetches) == 1

    clock.t += NotificationScheduleService.LEVEL_CHECK_MS / 1000 + 1     # five quiet minutes
    service.tick()

    assert len(api.fetches) == 2


def test_check_now_makes_the_next_tick_fetch_even_while_listening():
    service, api, clock, _, _ = make_listener()
    service.tick()
    assert len(api.fetches) == 1

    service.check_now()                                   # safe from any thread; the loop is not running here
    service.tick()

    assert len(api.fetches) == 2


def test_a_failed_fetch_keeps_the_last_schedule_and_does_not_open_the_stream():
    service, api, clock, _, _ = make_listener()
    service.tick()
    kept = service.schedule
    api.fetch_error = ApiConnectionError("down")
    service.check_now()
    opens = len(api.opens)

    delay = service.tick()

    assert service.schedule is kept
    assert len(api.opens) == opens
    assert 25_000 <= delay <= 40_000


def test_the_first_tick_after_start_streams_nothing():
    clock = Clock()
    runtime = SimpleNamespace(
        api_client=SimpleNamespace(access_token="token"), network=SimpleNamespace(network_state=NetworkState.BACKEND_REACHABLE),
        wellbeing=SimpleNamespace(wake=lambda: None), storage=None,
    )
    api = StreamApi(clock)
    service = NotificationScheduleService(runtime, api, FakeCache())
    service._monotonic = lambda: clock.t

    service.tick()

    assert api.opens == [] and api.fetches == []


@pytest.mark.parametrize("line,expected", [
    ('data: {"version": 3}', 3), ('data:{"version":0}', 0), ('data:   {"version": 12}  ', 12),
    (": ping", None), ("", None), ("event: schedule", None), ("retry: 5000", None),
    ('data: {"version": true}', None), ('data: {"version": -1}', None), ('data: {"version": "3"}', None),
    ("data: not json", None), ("data: [1, 2]", None), ('data: {"other": 1}', None), (None, None), (5, None),
])
def test_only_a_data_line_with_a_version_is_an_event(line, expected):
    assert NotificationScheduleService._event_version(line) == expected


def test_a_state_of_the_stream_is_logged_on_the_edge_not_every_listen():
    service, api, clock, _, logs = make_listener()

    for _ in range(4):
        service.tick()
    assert len(logs.with_prefix("NOTIFICATION_STREAM_UP")) == 1

    api.scripts = [fails_after(ApiConnectionError("cut"))]
    service.tick()
    clock.t = service._stream_blocked_until + 1
    service.tick()
    service.tick()

    assert len(logs.with_prefix("NOTIFICATION_STREAM_UP")) == 2          # and again after a failure


# ── Native delivery ──────────────────────────────────────────────────────────

def test_the_administrators_notifications_are_asked_to_be_native():
    """Reminders, custom notifications and pushed messages go out as the platform's own notification."""
    seen = []

    class Recording(FakeNotifications):
        def notify(self, message, level=None, title=None, key=None, link=None, native=False):
            seen.append((key, native))
            return super().notify(message, level, title, key, link, native)

    service, notifications, source, runtime = make(
        at(11, 59, 30),
        payload(builtin=everything_off(), pushes=[push()],
                custom=[{"id": "c1", "title": "Standup", "message": "m", "time": "12:00", "weekdays": ALL_DAYS}]),
    )
    runtime.notifications = Recording()
    runtime.notifications.ist = lambda: service._ist

    run(service, 4)

    assert {key for key, _ in seen} == {"wellbeing:push:p1", "wellbeing:custom:c1"}
    assert all(native for _, native in seen)

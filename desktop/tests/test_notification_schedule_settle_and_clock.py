"""
Two things the notification schedule service tells the scheduler beyond the schedule itself.

**Whether it knows enough to schedule (`ready`).** With nothing persisted -- the first sign-in
on a machine, or the first after a sign-out, which wipes `app_state` -- the snapshot is None
until the backend answers. A daily reminder already inside its grace window would then be
shown at its *default* time although the administrator moved it or switched it off. So `ready`
holds after sign-in until the first answer (a failure counts) or a bounded wait, so a machine
that cannot reach the backend is not held for ever.

**What time it is (`clock_offset_seconds`).** The schedule is in IST wall-clock times and the
scheduler reads this machine's clock. A computer whose clock or time zone is wrong would show
"13:30" at the wrong moment, or beyond its grace and not at all. Every response carries the
server's clock; the offset, measured at the midpoint of the request, is what the scheduler adds.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.api.exceptions import ApiConnectionError
from background_services.network import NetworkState
from background_services.notifications import schedule_service as module
from background_services.notifications.schedule_service import (
    SCHEDULE_STATE_KEY,
    NotificationScheduleService,
)

UTC = timezone.utc


class FakeCache:
    def __init__(self, store=None):
        self.store = store if store is not None else {}

    def load_app_state(self, key):
        raw = self.store.get(key)
        return json.loads(raw) if raw is not None else None

    def save_app_state(self, key, value):
        self.store[key] = json.dumps(value)


class FakeApi:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.calls = response, error, 0

    def get_schedule(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.response

    def open_stream(self, since, *, read_timeout):
        from app.api.exceptions import ApiError

        raise ApiError("no change stream", status_code=404)


def body(server_time=None, version=1):
    data = {"version": version, "builtin": [], "custom": []}
    if server_time is not None:
        data["server_time"] = server_time
    return data


class Clock:
    """A controllable monotonic clock (the service's seam) and wall clock (module `datetime`)."""

    def __init__(self):
        self.mono = 1000.0

    def __call__(self):
        return self.mono


def make(response=None, error=None, *, cache=None, signed_in=True):
    wakes = []
    runtime = SimpleNamespace(
        api_client=SimpleNamespace(access_token="t" if signed_in else None),
        network=SimpleNamespace(network_state=NetworkState.BACKEND_REACHABLE),
        wellbeing=SimpleNamespace(wake=lambda: wakes.append(1)),
        storage=None,
    )
    api = FakeApi(response, error)
    service = NotificationScheduleService(runtime, api, cache if cache is not None else FakeCache())
    clock = Clock()
    service._monotonic = clock
    service.log.setLevel(logging.DEBUG)
    service.tick()                       # the first tick: reads the persisted record, asks nothing
    wakes.clear()                        # (and wakes the scheduler: not what these tests count)
    return service, api, wakes, clock


# ── ready: not on a guess ────────────────────────────────────────────────────

def test_with_nothing_known_it_is_not_ready_for_the_scheduler():
    service, _api, _wakes, _clock = make(body())
    assert service.schedule is None
    assert not service.ready


def test_the_first_answer_makes_it_ready_and_wakes_the_scheduler_once():
    service, _api, wakes, _clock = make(body())
    assert not service.ready

    service.tick()                                              # fetches

    assert service.schedule is not None and service.ready
    assert wakes == [1]                                         # the version edge; not woken twice for one answer


def test_a_failed_first_answer_also_ends_the_wait_because_the_desktop_is_now_on_defaults():
    service, api, wakes, _clock = make(error=ApiConnectionError("down"))

    service.tick()

    assert api.calls == 1 and service.schedule is None
    assert service.ready                                        # the backend has been asked and has not answered
    assert wakes == [1]                                         # and the scheduler is told, so it need not wait out its idle sleep


def test_a_response_that_cannot_be_used_ends_the_wait_too():
    service, _api, wakes, _clock = make({"version": "garbage"})

    service.tick()

    assert service.schedule is None
    assert service.ready and wakes == [1]


def test_a_machine_that_cannot_reach_the_backend_is_not_held_for_ever():
    service, _api, _wakes, clock = make(body(), signed_in=True)

    assert not service.ready                                    # starts the wait
    clock.mono += NotificationScheduleService.SETTLE_SECONDS - 1
    assert not service.ready
    clock.mono += 2
    assert service.ready                                        # the defaults take over after the bounded wait


def test_the_wait_starts_when_the_scheduler_first_asks_while_signed_in_not_at_start():
    """Starting the service at 09:00 and signing in at 09:40 must not find the wait already over."""
    service, _api, _wakes, clock = make(body())
    clock.mono += 40 * 60                                       # forty minutes at the login screen
    assert not service.ready                                    # the wait starts now, at sign-in
    clock.mono += NotificationScheduleService.SETTLE_SECONDS + 1
    assert service.ready


def test_a_schedule_already_known_never_waits():
    cache = FakeCache({SCHEDULE_STATE_KEY: json.dumps({"version": 4, "builtin": [], "custom": []})})
    service, api, wakes, _clock = make(body(), cache=cache)

    assert service.ready and api.calls == 0


def test_stopping_or_failing_never_holds_the_scheduler_up():
    service, *_ = make(body())
    service._stop_requested = True
    assert service.ready


# ── the clock offset ─────────────────────────────────────────────────────────

class FixedNow:
    """Stands in for `datetime.now(timezone.utc)` inside the service module: each call returns the next instant."""

    def __init__(self, *instants):
        self.instants = list(instants)

    def now(self, tz=None):
        return self.instants.pop(0)


def _with_wall_clock(monkeypatch, *instants):
    fake = FixedNow(*instants)
    monkeypatch.setattr(module, "datetime", SimpleNamespace(now=fake.now))


def test_a_clock_that_is_behind_gives_a_positive_offset(monkeypatch):
    base = datetime(2026, 10, 12, 8, 0, 0, tzinfo=UTC)
    # This machine says 08:00:00 when the request goes and 08:00:00.4 when the answer arrives;
    # the server stamped 09:00:00.2 -- an hour ahead of the machine's clock.
    _with_wall_clock(monkeypatch, base, base + timedelta(seconds=0.4))
    service, *_ = make(body(server_time="2026-10-12T09:00:00.2Z"))

    service.tick()

    assert service.clock_offset_seconds == pytest.approx(3600.0, abs=0.01)   # midpoint cancels the round trip


def test_a_clock_that_is_ahead_gives_a_negative_offset(monkeypatch):
    base = datetime(2026, 10, 12, 8, 0, 0, tzinfo=UTC)
    _with_wall_clock(monkeypatch, base, base + timedelta(seconds=0.2))
    service, *_ = make(body(server_time="2026-10-12T07:55:00.1Z"))

    service.tick()

    assert service.clock_offset_seconds == pytest.approx(-300.0, abs=0.01)


def test_a_clock_that_agrees_gives_about_zero(monkeypatch):
    base = datetime(2026, 10, 12, 8, 0, 0, tzinfo=UTC)
    _with_wall_clock(monkeypatch, base, base + timedelta(seconds=0.2))
    service, *_ = make(body(server_time="2026-10-12T08:00:00.1Z"))

    service.tick()

    assert abs(service.clock_offset_seconds) < 0.01


def test_before_any_answer_the_offset_is_zero():
    service, *_ = make(body())
    assert service.clock_offset_seconds == 0.0


@pytest.mark.parametrize("server_time", [None, "", "not a time", 12345])
def test_an_answer_without_a_usable_server_time_changes_nothing(server_time):
    service, api, _wakes, _clock = make(body())
    service._clock_offset = 42.0
    api.response = {**body(), "server_time": server_time}

    service.tick()

    assert service.clock_offset_seconds == 42.0                 # the last offset stands


def test_an_implausible_offset_is_ignored_not_applied(monkeypatch):
    base = datetime(2026, 10, 12, 8, 0, 0, tzinfo=UTC)
    _with_wall_clock(monkeypatch, base, base)
    service, *_ = make(body(server_time="2031-10-12T08:00:00Z"))    # five years ahead

    service.tick()

    assert service.clock_offset_seconds == 0.0


def test_a_large_skew_is_reported_once_and_its_end_once(monkeypatch, caplog):
    base = datetime(2026, 10, 12, 8, 0, 0, tzinfo=UTC)
    _with_wall_clock(monkeypatch, *([base] * 8))
    service, api, *_ = make(body(server_time="2026-10-12T11:00:00Z"))

    with caplog.at_level(logging.INFO, logger="monitra.notification_schedule"):
        for version in (1, 2, 3):                               # the same skew on three polls
            api.response = body(server_time="2026-10-12T11:00:00Z", version=version)
            service._need_fetch = True
            service.tick()
        api.response = body(server_time="2026-10-12T08:00:00Z", version=4)
        service._need_fetch = True
        service.tick()

    skew = [r.getMessage() for r in caplog.records if "NOTIFICATION_CLOCK_SKEW" in r.getMessage()]
    assert len(skew) == 2
    assert "behind" in skew[0] and "reminders are timed by the server's clock" in skew[0]
    assert "cleared" in skew[1]

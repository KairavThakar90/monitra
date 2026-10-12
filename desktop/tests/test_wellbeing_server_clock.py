"""
The scheduler reads the server's clock, not only this machine's.

The administrator's times are IST wall-clock times ("lunch at 13:30"). On a computer whose
clock is wrong -- or whose time zone was set wrong while the displayed time was corrected by hand,
which puts the real instant hours out -- comparing them with the machine's own reading showed the
reminder at the wrong moment, or beyond its ten-minute grace and not at all. The schedule service
measures the offset to the server's clock on every answer (`clock_offset_seconds`) and
`WellbeingService._now_ist` adds it.

The real `_now_ist` runs here (the shared harness replaces it), with the machine's clock frozen.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from background_services.wellbeing import WellbeingService
from background_services.wellbeing import wellbeing_service as module
from core.time_format import IST

LUNCH = datetime(2026, 9, 11, 13, 30, tzinfo=IST)           # a Friday, the default lunch time


class _Frozen(datetime):
    """`datetime` whose `now()` is a settable instant: this machine's clock."""

    machine_clock = LUNCH

    @classmethod
    def now(cls, tz=None):
        moment = cls.machine_clock
        return moment.astimezone(tz) if tz is not None else moment


def make(machine_clock, offset, monkeypatch, *, ready=True):
    """A service whose machine clock reads `machine_clock` and whose schedule service reports `offset`."""
    _Frozen.machine_clock = machine_clock
    monkeypatch.setattr(module, "datetime", _Frozen)
    shown = []
    source = SimpleNamespace(schedule=None, ready=ready, clock_offset_seconds=offset)
    notifications = SimpleNamespace(
        notify=lambda body, level=None, title=None, key=None, link=None, native=False: shown.append(key) or True,
    )
    runtime = SimpleNamespace(
        api_client=SimpleNamespace(access_token="t"), notifications=notifications,
        notification_schedule=source, cache=None, storage=None,
    )
    service = WellbeingService(runtime, None)
    service.DEFAULT_MAX_PER_HOUR = 1000
    clock = {"mono": 1000.0}
    service._now_monotonic = lambda: clock["mono"]
    return service, shown, source, clock


def settle(service, clock, seconds=60, step=5):
    """Tick as the loop does for a while (the first tick only starts the cadence)."""
    for _ in range(seconds // step):
        clock["mono"] += step
        _Frozen.machine_clock = _Frozen.machine_clock + timedelta(seconds=step)
        service.tick()


def test_a_clock_an_hour_behind_still_shows_lunch_at_the_servers_1330(monkeypatch):
    # This machine says 12:30; the server (IST) says 13:30. Offset +3600.
    service, shown, *_rest, clock = make(LUNCH - timedelta(hours=1), 3600.0, monkeypatch)
    service.tick()
    settle(service, clock)
    assert "wellbeing:lunch" in shown


def test_without_the_offset_the_same_machine_shows_nothing_at_that_moment(monkeypatch):
    """The control: it is the offset that moves the reminder, not the harness."""
    service, shown, *_rest, clock = make(LUNCH - timedelta(hours=1), 0.0, monkeypatch)
    service.tick()
    settle(service, clock)
    assert "wellbeing:lunch" not in shown


def test_a_clock_an_hour_ahead_does_not_show_lunch_early(monkeypatch):
    # This machine says 13:30; the server says 12:30. Lunch is not due yet.
    service, shown, *_rest, clock = make(LUNCH, -3600.0, monkeypatch)
    service.tick()
    settle(service, clock)
    assert "wellbeing:lunch" not in shown


def test_a_wrong_time_zone_thirteen_and_a_half_hours_out_is_corrected(monkeypatch):
    """A PC in India with its time zone set to UTC-8 and the displayed time adjusted by hand: the
    real instant is 13 h 30 m out, so every daily reminder fired half a day away."""
    service, shown, *_rest, clock = make(LUNCH - timedelta(hours=13, minutes=30), 13.5 * 3600, monkeypatch)
    service.tick()
    settle(service, clock)
    assert "wellbeing:lunch" in shown


def test_a_clock_so_late_that_lunch_was_beyond_its_grace_still_shows_it_on_time(monkeypatch):
    """Twenty minutes behind: by the machine's clock lunch would only be due in twenty minutes (and by a
    clock twenty minutes *ahead* it would already be 'missed', beyond the ten-minute grace)."""
    service, shown, *_rest, clock = make(LUNCH + timedelta(minutes=20), -1200.0, monkeypatch)
    service.tick()
    settle(service, clock)
    # It is 13:30 by the server: due now, so shown -- not recorded as missed.
    assert "wellbeing:lunch" in shown


def test_the_offset_is_read_on_every_tick_so_a_new_measurement_applies_at_once(monkeypatch):
    service, shown, source, clock = make(LUNCH - timedelta(hours=1), 0.0, monkeypatch)
    service.tick()
    settle(service, clock, seconds=30)
    assert shown == []

    source.clock_offset_seconds = 3600.0                      # the next answer measured the skew
    settle(service, clock, seconds=30)
    assert "wellbeing:lunch" in shown


@pytest.mark.parametrize("value", [None, "3600", True, object(), float("nan"), float("inf"), float("-inf")])
def test_a_missing_or_unusable_offset_is_the_machines_own_clock(monkeypatch, value):
    service, *_rest = make(LUNCH, value, monkeypatch)
    assert abs((service._now_ist() - LUNCH).total_seconds()) < 1.0


def test_a_schedule_source_that_has_no_offset_at_all_is_the_machines_own_clock(monkeypatch):
    service, shown, source, clock = make(LUNCH, 0.0, monkeypatch)
    del source.clock_offset_seconds                           # an older runtime object, a test double
    assert abs((service._now_ist() - LUNCH).total_seconds()) < 1.0


def test_nothing_is_shown_while_the_schedule_service_is_still_waiting_for_its_first_answer(monkeypatch):
    """A fresh start with nothing persisted: lunch is inside its grace window, but the schedule that may have
    moved or switched it off has not arrived, so the scheduler waits for it rather than guess."""
    service, shown, source, clock = make(LUNCH + timedelta(minutes=2), 0.0, monkeypatch, ready=False)
    service.tick()
    settle(service, clock, seconds=40)
    assert shown == []

    source.ready = True                                       # the answer arrived (or the bounded wait ended)
    settle(service, clock, seconds=30)
    assert "wellbeing:lunch" in shown

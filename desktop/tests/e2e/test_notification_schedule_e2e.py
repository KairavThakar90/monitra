"""
The administrator's notification schedule, end to end: web API -> database -> desktop -> screen.

Nothing here is mocked. A real backend process serves the real API against the development
database; an administrator principal sets the schedule through the real routes; the real desktop
runtime (schedule service, wellbeing scheduler, notification service) is signed in as an employee
and shows what it was told, when it was told to. Assertions are made against what the notification
service was asked to show and what its card actually says.

Opt-in, like the other end-to-end modules:

    MONITRA_E2E=1 python -m pytest tests/e2e/test_notification_schedule_e2e.py -q -s   (from desktop/)

The headless platform has no tray, so the notification service draws its card -- the same words the
platform toast carries. `tests/e2e/test_native_toast_e2e.py` is the one that puts a real toast on a
real Windows desktop and photographs it.

The schedule is one row for the whole deployment (`system_settings`, key `desktop_notifications`),
so the row is photographed before and put back exactly afterwards, administrator audit rows included
(they go with the principal). Every notification these create is titled "E2E" so that anybody whose
desktop happens to be pointed at the development backend for those few minutes can see what it is.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime, timedelta

import httpx
import pytest

from core.time_format import IST
from tests.e2e.test_timing_lifecycle_e2e import (  # noqa: F401  (fixtures)
    BACKEND_ROOT, _pump, api, backend, db, desktop, principal, pytestmark,
)

SETTING_KEY = "desktop_notifications"


@pytest.fixture(scope="module")
def admin():
    helper = BACKEND_ROOT / "tests" / "e2e_support.py"
    out = subprocess.run(
        [sys.executable, str(helper), "provision", "administrator"], cwd=str(BACKEND_ROOT),
        capture_output=True, text=True, check=True,
    ).stdout.strip().splitlines()[-1]
    fixture = json.loads(out)
    try:
        yield fixture
    finally:
        subprocess.run(
            [sys.executable, str(helper), "cleanup", json.dumps(fixture)],
            cwd=str(BACKEND_ROOT), capture_output=True, text=True, check=False,
        )


@pytest.fixture
def admin_api(backend, admin):
    with httpx.Client(base_url=backend, timeout=30, headers={"Authorization": f"Bearer {admin['token']}"}) as client:
        yield client


@pytest.fixture
def schedule_restored(db, admin):
    """The deployment's notification schedule is put back exactly as it was found."""
    from sqlalchemy import text

    with db.connect() as conn:
        before = conn.execute(
            text("SELECT value, updated_at, updated_by_user_id, updated_by_username "
                 "FROM system_settings WHERE key = :k"), {"k": SETTING_KEY},
        ).mappings().first()
    yield before
    with db.begin() as conn:
        if before is None:
            conn.execute(text("DELETE FROM system_settings WHERE key = :k"), {"k": SETTING_KEY})
        else:
            conn.execute(
                text("UPDATE system_settings SET value = CAST(:v AS jsonb), updated_at = :at, "
                     "updated_by_user_id = :uid, updated_by_username = :uname WHERE key = :k"),
                {"v": json.dumps(before["value"]), "at": before["updated_at"],
                 "uid": before["updated_by_user_id"], "uname": before["updated_by_username"], "k": SETTING_KEY},
            )


class Spy:
    """Records what the notification service is asked to show, then lets it show it."""

    def __init__(self, notifications):
        self.calls = []
        self._real = notifications.notify

        def notify(message, level="info", title=None, key=None, link=None, native=None):
            self.calls.append({"message": message, "title": title, "key": key, "native": native, "at": time.monotonic()})
            return self._real(message, level, title=title, key=key, link=link, native=native)

        notifications.notify = notify

    def keys(self):
        return [call["key"] for call in self.calls]

    def one(self, key):
        found = [call for call in self.calls if call["key"] == key]
        return found[0] if found else None


def _next_minute(seconds_ahead: int, extra_minutes: int = 0) -> datetime:
    """The first whole IST minute at least `seconds_ahead` from now, plus `extra_minutes`."""
    moment = datetime.now(IST) + timedelta(seconds=seconds_ahead)
    target = moment.replace(second=0, microsecond=0)
    if moment > target:
        target += timedelta(minutes=1)
    return target + timedelta(minutes=extra_minutes)


def _hhmm(moment: datetime) -> str:
    return moment.strftime("%H:%M")


def _expect(response, status=200):
    assert response.status_code == status, f"{response.request.method} {response.request.url} -> {response.status_code}: {response.text}"
    return response


@pytest.mark.usefixtures("schedule_restored")
def test_a_time_the_administrator_sets_is_the_time_the_desktop_shows_it(qapp, desktop, admin_api, api):
    """A custom notification and a moved built-in reminder arrive at their minute, in order; one the
    administrator switched off is never sent; and the desktop's own clock agrees with the server's."""
    spy = Spy(desktop.notifications)
    custom_at = _next_minute(65)
    lunch_at = custom_at + timedelta(minutes=1)
    weekdays = list(range(7))

    created = _expect(admin_api.post("/desktop-notifications/custom", json={
        "title": "E2E standup", "message": "E2E: daily standup in five minutes.",
        "time": _hhmm(custom_at), "weekdays": weekdays, "enabled": True,
    }), 201).json()
    custom_id = [c["id"] for c in created["custom"]
                 if c["title"] == "E2E standup" and c["time"] == _hhmm(custom_at)][0]
    _expect(admin_api.put("/desktop-notifications/builtin/lunch", json={"time": _hhmm(lunch_at), "weekdays": weekdays}))
    # Switched off, and given the very minute the custom one is due: it must not appear.
    _expect(admin_api.put("/desktop-notifications/builtin/tea_morning", json={
        "enabled": False, "time": _hhmm(custom_at), "weekdays": weekdays,
    }))

    # What the desktop is told -- as the server's own answer to the desktop's own request.
    told = _expect(api.get("/desktop-notifications/schedule")).json()
    assert [c["time"] for c in told["custom"] if c["id"] == custom_id] == [_hhmm(custom_at)]
    assert {b["key"]: b for b in told["builtin"]}["lunch"]["time"] == _hhmm(lunch_at)
    assert {b["key"]: b for b in told["builtin"]}["tea_morning"]["enabled"] is False

    # The desktop hears it (a stream event, or the poll) and applies it.
    schedule_service = desktop.notification_schedule
    _pump(qapp, lambda: schedule_service.schedule is not None
          and schedule_service.schedule.version == told["version"], 60, "the desktop to apply the schedule")
    assert schedule_service.ready
    assert abs(schedule_service.clock_offset_seconds) < 3.0, (
        f"this machine's clock disagrees with the server's by {schedule_service.clock_offset_seconds:.1f}s"
    )
    # It is also kept, so a start while offline still has it.
    assert desktop.cache.load_app_state("notifications.schedule")["version"] == told["version"]

    seconds_to_custom = (custom_at - datetime.now(IST)).total_seconds()
    assert seconds_to_custom > 5, "the schedule reached the desktop too late for this test to be meaningful"

    # The custom notification, at its minute (grace: shown within ~a few seconds, never before it).
    _pump(qapp, lambda: spy.one(f"wellbeing:custom:{custom_id}") is not None, seconds_to_custom + 60,
          "the custom notification to be shown")
    shown_at = datetime.now(IST)
    assert shown_at >= custom_at - timedelta(seconds=2), f"shown at {shown_at:%H:%M:%S}, before its time {custom_at:%H:%M}"
    assert shown_at - custom_at < timedelta(seconds=45), f"shown {shown_at - custom_at} after its time"
    custom = spy.one(f"wellbeing:custom:{custom_id}")
    assert (custom["title"], custom["message"], custom["native"]) == (
        "E2E standup", "E2E: daily standup in five minutes.", True)

    # What is on screen is whole: the card (the platform toast's twin on a machine with no tray) carries
    # every word the administrator wrote. Checked now, while it is up -- a card goes after thirty seconds.
    _pump(qapp, lambda: any(entry.card._message.text() == "E2E: daily standup in five minutes."
                            for entry in desktop.notifications._cards), 10, "the card to be on screen")
    assert {entry.card._title.text() for entry in desktop.notifications._cards} >= {"E2E standup"}

    # The moved built-in reminder, a minute later, with the application's own wording.
    _pump(qapp, lambda: spy.one("wellbeing:lunch") is not None, 150, "the moved lunch reminder to be shown")
    lunch_shown_at = datetime.now(IST)
    assert lunch_shown_at >= lunch_at - timedelta(seconds=2)
    assert lunch_shown_at - lunch_at < timedelta(seconds=45)
    assert spy.one("wellbeing:lunch")["title"] == "🧘 Take a Lunch Break"

    # In that order, once each, and the one switched off never.
    assert spy.keys().index(f"wellbeing:custom:{custom_id}") < spy.keys().index("wellbeing:lunch")
    assert spy.keys().count(f"wellbeing:custom:{custom_id}") == 1
    assert spy.keys().count("wellbeing:lunch") == 1
    assert "wellbeing:tea_morning" not in spy.keys(), "a notification the administrator switched off was sent"


@pytest.mark.usefixtures("schedule_restored")
def test_a_pushed_message_reaches_a_signed_in_desktop_within_seconds(qapp, desktop, admin_api):
    spy = Spy(desktop.notifications)
    schedule_service = desktop.notification_schedule
    _pump(qapp, lambda: schedule_service.ready and schedule_service.schedule is not None, 60,
          "the desktop to have its schedule")

    pushed = _expect(admin_api.post("/desktop-notifications/push", json={
        "title": "E2E notice", "message": "E2E: please ignore this test message.",
    })).json()
    push_id = pushed["pushes"][-1]["id"] if "pushes" in pushed else None

    _pump(qapp, lambda: any(call["key"].startswith("wellbeing:push:") for call in spy.calls), 60,
          "the pushed message to be shown")
    call = next(call for call in spy.calls if call["key"].startswith("wellbeing:push:"))
    assert (call["title"], call["message"]) == ("E2E notice", "E2E: please ignore this test message.")
    if push_id:
        assert call["key"] == f"wellbeing:push:{push_id}"
    assert sum(1 for c in spy.calls if c["key"].startswith("wellbeing:push:")) == 1

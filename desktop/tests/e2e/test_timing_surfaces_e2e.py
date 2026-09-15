"""
Every surface reads one netted number, and two clients converge on one entry.

Idle time, unwanted-activity deductions and reassignment all write signed
`time_entry_adjustments`. The entry list (what the desktop sums), the today
summary (the desktop's TODAY card), the dashboard (web KPI) and the
time-tracking detail (web day list) must all report the same seconds after
each action. Same opt-in and fixtures as `test_timing_lifecycle_e2e.py`.
"""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone

import pytest

from tests.e2e.test_timing_lifecycle_e2e import (  # noqa: F401  (fixtures)
    _dashboard_seconds, _ist_today, _pump, _row, _running_rows, _time_tracking_seconds,
    api, backend, clean_slate, db, desktop, principal, pytestmark,
)

UTC = timezone.utc


def _day_bounds(day: str):
    from core.time_format import ist_day_bounds_utc

    start, end = ist_day_bounds_utc(date.fromisoformat(day))
    return start.isoformat(), end.isoformat()


def _surfaces(api, principal, day: str) -> dict:
    start, end = _day_bounds(day)
    entries = api.get("/time-entries", params={
        "user_id": principal["user_id"], "limit": 1000, "start_date": start, "end_date": end,
    }).json()
    finished = [e for e in entries if e["end_time"] is not None]
    today = api.get("/time-entry-activities/today").json()["data"]
    return {
        "entries_net": sum(e["net_seconds"] for e in finished),
        "entries_raw": sum(e["total_seconds"] for e in finished),
        "today": today["tracked_seconds"],
        "dashboard": _dashboard_seconds(api, principal, day),
        "time_tracking": _time_tracking_seconds(api, principal, day),
    }


def _assert_agree(surfaces: dict, label: str) -> None:
    values = {k: v for k, v in surfaces.items() if k != "entries_raw"}
    assert len(set(values.values())) == 1, f"{label}: surfaces disagree: {surfaces}"


#: The principal's idle threshold is the default five minutes, and the backend
#: refuses an idle period shorter than it. Rather than wait it out, the entry
#: is started with an *age* (the same mechanism a replayed offline start uses)
#: so a six-minute idle period fits inside it.
ENTRY_AGE_SECONDS = 8 * 60
IDLE_AGE_SECONDS = 6 * 60


def _start(api, principal, key: str, age_seconds: int = 0) -> int:
    body = {
        "project_id": principal["project_id"], "task_id": principal["task_id"],
        "client_op": f"timer:e2e:{key}:{int(time.time() * 1000)}",
    }
    if age_seconds:
        now = datetime.now(UTC)
        body["started_at"] = (now - timedelta(seconds=age_seconds)).isoformat()
        body["client_time"] = now.isoformat()
    response = api.post("/time-entries/start", json=body)
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _report_idle(api, entry_id: int) -> dict:
    reported = api.post("/idle-periods", json={
        "time_entry_id": entry_id,
        "idle_started_at": (datetime.now(UTC) - timedelta(seconds=IDLE_AGE_SECONDS)).isoformat(),
        "idle_detected_at": datetime.now(UTC).isoformat(),
        "client_event_id": f"e2e-idle-{entry_id}",
    })
    assert reported.status_code == 201, reported.text
    return reported.json()


def _stop(api, entry_id: int) -> dict:
    response = api.post(f"/time-entries/{entry_id}/stop", json={})
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.usefixtures("clean_slate")
@pytest.mark.parametrize("keep, action", [(False, "resume"), (True, "resume"), (False, "stop")])
def test_idle_actions_change_every_surface_the_same_way(api, db, principal, keep, action):
    day = _ist_today()
    before = _surfaces(api, principal, day)
    _assert_agree(before, "before")

    entry_id = _start(api, principal, f"idle-{keep}-{action}", age_seconds=ENTRY_AGE_SECONDS)
    period_id = _report_idle(api, entry_id)["id"]
    time.sleep(1)
    resolved = api.post(f"/idle-periods/{period_id}/resolve",
                        json={"keep_idle_time": keep, "action": action})
    assert resolved.status_code == 200, resolved.text
    if action == "resume":
        time.sleep(1)
        _stop(api, entry_id)
    row = _row(db, entry_id)
    assert row["status"] == "stopped", "a stop action must stop the entry on the server"

    read = api.get(f"/time-entries/{entry_id}").json()
    idle_seconds = resolved.json()["idle_duration_seconds"]
    if keep and action == "resume":
        assert read["adjustment_seconds"] == 0
    else:
        assert read["adjustment_seconds"] == -idle_seconds, read
        assert IDLE_AGE_SECONDS - 5 <= idle_seconds <= IDLE_AGE_SECONDS + 10
    assert read["net_seconds"] == max(0, read["total_seconds"] + read["adjustment_seconds"])

    after = _surfaces(api, principal, day)
    _assert_agree(after, f"after keep={keep} action={action}")
    assert after["entries_net"] - before["entries_net"] == read["net_seconds"]


@pytest.mark.usefixtures("clean_slate")
def test_an_unwanted_activity_deduction_is_applied_once_everywhere(api, db, principal):
    day = _ist_today()
    before = _surfaces(api, principal, day)
    entry_id = _start(api, principal, "deduct")
    time.sleep(2)
    _stop(api, entry_id)
    body = {"adjustment_seconds": -600, "reason": "e2e: CTRL threshold",
            "source_activity_type": "keyboard", "source_key_or_action": "CTRL",
            "client_event_id": f"e2e-adj-{entry_id}"}
    first = api.post(f"/time-entries/{entry_id}/adjustments", json=body)
    assert first.status_code in (200, 201), first.text
    retry = api.post(f"/time-entries/{entry_id}/adjustments", json=body)
    assert retry.status_code in (200, 201), retry.text
    read = api.get(f"/time-entries/{entry_id}").json()
    assert read["adjustment_seconds"] == -600, "the retried deduction was applied twice"
    assert read["net_seconds"] == 0
    after = _surfaces(api, principal, day)
    _assert_agree(after, "after deduction")
    assert after["entries_net"] == before["entries_net"]


@pytest.mark.usefixtures("clean_slate")
def test_reassigning_idle_time_keeps_every_surface_in_agreement(api, db, principal):
    day = _ist_today()
    entry_id = _start(api, principal, "reassign", age_seconds=ENTRY_AGE_SECONDS)
    period = _report_idle(api, entry_id)
    time.sleep(1)
    moved = api.post(f"/idle-periods/{period['id']}/reassign",
                     json={"project_id": principal["project_id"], "task_id": principal["task_id"]})
    assert moved.status_code == 200, moved.text
    api.post(f"/idle-periods/{period['id']}/resolve", json={"keep_idle_time": False, "action": "resume"})
    time.sleep(1)
    _stop(api, entry_id)
    after = _surfaces(api, principal, day)
    _assert_agree(after, "after reassignment")
    original = api.get(f"/time-entries/{entry_id}").json()
    assert original["adjustment_seconds"] < 0, "the original entry must carry the deduction"


@pytest.mark.usefixtures("clean_slate")
def test_a_stop_from_the_web_is_adopted_by_the_desktop(qapp, desktop, api, db, principal):
    """Started on the desktop, stopped from the web: the desktop's later stop
    must not extend the entry, and both clients converge on one record."""
    timer = desktop.timer
    finalized = []
    timer.timer_finalized.connect(finalized.append)
    timer.start_tracking(principal["project_id"], principal["task_id"], "E2E task")
    _pump(qapp, lambda: timer.entry_id is not None, 30, "start")
    entry_id = timer.entry_id
    assert api.get("/time-entries/active").json()["entry"]["id"] == entry_id
    time.sleep(2)
    web_stop = _stop(api, entry_id)
    time.sleep(2)
    timer.stop_tracking()
    _pump(qapp, lambda: bool(finalized), 30, "stop")
    entry = finalized[0]["entry"]
    assert entry["end_time"] == web_stop["end_time"]
    assert entry["total_seconds"] == web_stop["total_seconds"]
    row = _row(db, entry_id)
    assert row["total_seconds"] == int(row["derived"]) == web_stop["total_seconds"]
    assert len(_running_rows(db, principal["user_id"])) == 0

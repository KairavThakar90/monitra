"""
Break In / Break Out against the real backend and the real database.

Same opt-in and fixtures as `test_timing_lifecycle_e2e.py`:

    MONITRA_E2E=1 python -m pytest tests/e2e/test_break_lifecycle_e2e.py -q -s

What is verified, at every layer, is the one accounting rule the feature
exists for: the break is a gap between two ordinary entries, never time
inside one. And the one safety rule: a task that can no longer be started
is not resumed, and nothing else is started in its place.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

import pytest

from background_services.public_api import BreakStatus
from tests.e2e.test_timing_lifecycle_e2e import (  # noqa: F401  (fixtures)
    DURATION_TOLERANCE_SECONDS, _all_rows_today, _dashboard_seconds, _ist_today, _pump,
    _row, _running_rows, api, backend, clean_slate, db, desktop, principal, pytestmark,
)

UTC = timezone.utc


def _set_task_status(db, task_id: int, status: str) -> None:
    from sqlalchemy import text

    with db.begin() as conn:
        conn.execute(text("UPDATE tasks SET status = :s WHERE id = :id"), {"s": status, "id": task_id})


@pytest.mark.usefixtures("clean_slate")
def test_break_in_and_out_produce_two_entries_with_the_break_between_them(
    qapp, desktop, api, db, principal
):
    timer = desktop.timer
    finalized, breaks = [], []
    timer.timer_finalized.connect(finalized.append)
    timer.break_state_changed.connect(breaks.append)
    work_seconds, break_seconds = 6, 5

    # Work.
    timer.start_tracking(principal["project_id"], principal["task_id"], "E2E task")
    _pump(qapp, lambda: timer.entry_id is not None, 30, "the start to be bound")
    first_id = timer.entry_id
    time.sleep(work_seconds)

    # Break In: the ordinary stop, finalized by the backend through the queue.
    pressed_break_in = datetime.now(UTC)
    assert timer.break_in() is True
    assert not timer.is_running()
    assert timer.break_status == BreakStatus.ON_BREAK
    _pump(qapp, lambda: bool(finalized), 30, "the break-in stop to be finalized")
    assert finalized[0]["entry"]["id"] == first_id
    assert api.get("/time-entries/active").json()["entry"] is None, "nothing runs during a break"
    assert len(_running_rows(db, principal["user_id"])) == 0
    time.sleep(break_seconds)

    # Break Out: checked against the backend, then the ordinary start.
    pressed_break_out = datetime.now(UTC)
    assert timer.break_out() is True
    _pump(qapp, lambda: timer.is_running(), 30, "the resume to start")
    assert timer.task_id == principal["task_id"]
    assert timer.break_status == BreakStatus.NONE and timer.pre_break_task() is None
    _pump(qapp, lambda: timer.entry_id is not None and timer.entry_id != first_id, 30,
          "the resumed start to be bound")
    second_id = timer.entry_id
    assert second_id != first_id, "a resume is a new entry, not the old one reopened"
    active = api.get("/time-entries/active").json()["entry"]
    assert active["id"] == second_id and active["task_id"] == principal["task_id"]
    time.sleep(work_seconds)

    # The ordinary Stop.
    pressed_stop = datetime.now(UTC)
    timer.stop_tracking()
    _pump(qapp, lambda: len(finalized) == 2, 30, "the final stop to be finalized")
    assert finalized[1]["entry"]["id"] == second_id

    # Database: two stopped rows, each the length of its work stretch, and
    # the second starting no earlier than Break Out was pressed -- so the
    # break lies between them, not inside either.
    first, second = _row(db, first_id), _row(db, second_id)
    assert first["status"] == "stopped" and second["status"] == "stopped"
    assert abs(first["total_seconds"] - work_seconds) <= DURATION_TOLERANCE_SECONDS, first
    assert abs(second["total_seconds"] - work_seconds) <= DURATION_TOLERANCE_SECONDS, second
    gap = (second["start_time"] - first["end_time"]).total_seconds()
    assert gap >= break_seconds - DURATION_TOLERANCE_SECONDS, (first, second)
    assert abs((second["start_time"] - pressed_break_out).total_seconds()) <= DURATION_TOLERANCE_SECONDS
    assert abs((first["end_time"] - pressed_break_in).total_seconds()) <= DURATION_TOLERANCE_SECONDS
    assert len(_running_rows(db, principal["user_id"])) == 0

    # What the web renders: the two stretches of work, nothing for the break.
    day = _ist_today()
    total_today = sum(_row(db, r[0])["total_seconds"] for r in _all_rows_today(db, principal["user_id"]))
    assert _dashboard_seconds(api, principal, day) == total_today
    assert total_today < (pressed_stop - first["start_time"].astimezone(UTC)).total_seconds() - (
        break_seconds - DURATION_TOLERANCE_SECONDS
    ), "the break counted as work"
    assert breaks == [BreakStatus.ON_BREAK, BreakStatus.RESUMING, BreakStatus.NONE]

    print(
        f"\n[e2e break] first={first['total_seconds']}s break_gap={gap:.1f}s "
        f"second={second['total_seconds']}s dashboard={_dashboard_seconds(api, principal, day)}"
    )


@pytest.mark.usefixtures("clean_slate")
def test_a_task_archived_during_the_break_is_not_resumed_and_nothing_else_starts(
    qapp, desktop, api, db, principal
):
    timer = desktop.timer
    finalized, errors = [], []
    timer.timer_finalized.connect(finalized.append)
    timer.timer_error.connect(errors.append)

    timer.start_tracking(principal["project_id"], principal["task_id"], "E2E task")
    _pump(qapp, lambda: timer.entry_id is not None, 30, "the start to be bound")
    timer.break_in()
    _pump(qapp, lambda: bool(finalized), 30, "the break-in stop to be finalized")

    _set_task_status(db, principal["task_id"], "archived")
    try:
        assert api.get(f"/api/v1/projects/{principal['project_id']}/tasks").json() == [], (
            "precondition: the backend no longer lists the task"
        )
        assert timer.break_out() is True
        _pump(qapp, lambda: timer.break_status == BreakStatus.NONE, 30, "the refusal")

        assert not timer.is_running()
        assert timer.pre_break_task() is None
        assert errors and "E2E task" in errors[0] and "no longer" in errors[0], errors
        # A second of the event loop: anything the refusal might wrongly
        # have scheduled would land now.
        quiet_until = time.monotonic() + 1.0
        while time.monotonic() < quiet_until:
            qapp.processEvents()
            time.sleep(0.02)
        assert api.get("/time-entries/active").json()["entry"] is None
        assert len(_running_rows(db, principal["user_id"])) == 0
        assert desktop.cache.get_pending_count() == 0, "no start was queued for a gone task"
    finally:
        _set_task_status(db, principal["task_id"], "todo")

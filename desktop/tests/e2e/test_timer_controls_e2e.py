"""
The reworked timer controls against the real backend and the real database.

Same opt-in and fixtures as `test_timing_lifecycle_e2e.py`:

    MONITRA_E2E=1 python -m pytest tests/e2e/test_timer_controls_e2e.py -q -s

Where the lifecycle suites drive `TimerService` directly, this one drives the
*widgets*: a real `DashboardWindow` on the real runtime, signed in as the
disposable principal, with the sidebar's circular Play / Pause, a task row's
Start, and the ACTIVE TASK card's Break In / Break Out all clicked the way a
user clicks them. Every assertion is then made against the row in Postgres
and the API's own answer -- one entry per session, none during a break, and
the same task resumed -- as well as against what each control is showing.

Set `MONITRA_E2E_SHOTS=<dir>` to save a screenshot of the window at each
step (with `QT_QPA_PLATFORM=windows` these are the real display's pixels).
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from background_services.public_api import BreakStatus
from tests.e2e.test_timing_lifecycle_e2e import (  # noqa: F401  (fixtures)
    DURATION_TOLERANCE_SECONDS, _pump, _row, _running_rows, api, backend,
    clean_slate, db, desktop, principal, pytestmark,
)
from ui.dashboard_window import BREAK_ENDED_MESSAGE, BREAK_STARTED_MESSAGE
from ui.timer_control import CAPTION_ON_BREAK, CAPTION_SELECT_TASK

SHOTS = os.environ.get("MONITRA_E2E_SHOTS")


def _drain(qapp, rounds: int = 8) -> None:
    for _ in range(rounds):
        qapp.processEvents()


def _shot(window, name: str) -> None:
    if not SHOTS:
        return
    folder = Path(SHOTS)
    folder.mkdir(parents=True, exist_ok=True)
    window.grab().save(str(folder / f"{name}.png"))


@pytest.fixture
def window(qapp, desktop, principal):
    """The real dashboard on the real runtime, signed in as the principal."""
    from PySide6.QtCore import QTimer

    from ui.dashboard_window import DashboardWindow
    from ui.styles import APP_QSS

    runtime = desktop
    qapp.setStyleSheet(APP_QSS)
    # The client already carries the principal's token (see `desktop`); the
    # window reads the profile from the real /auth/me, as login does.
    runtime.api_client.access_token = principal["token"]
    widget = DashboardWindow(
        runtime=runtime,
        session_manager=runtime.session_manager,
        project_service=runtime.project_service,
        task_service=runtime.task_service,
        time_entry_service=runtime.time_entry_service,
        api_client=runtime.api_client,
    )
    widget.resize(1400, 860)
    widget.show()
    me = api_me(runtime)
    widget.on_login(me)
    # The project list and the task list come from the real backend.
    _pump(qapp, lambda: any(p.get("id") == principal["project_id"] for p in widget._projects),
          30, "the principal's project to load")
    project = next(p for p in widget._projects if p.get("id") == principal["project_id"])
    widget._on_project_selected(project)
    _pump(qapp, lambda: any(r.task.get("id") == principal["task_id"]
                            for r in widget._task_section._task_rows),
          30, "the principal's task row to render")
    yield widget
    widget.reset_state()
    _pump(qapp, lambda: runtime.health_report().get("tasks_in_flight", 0) == 0, 10,
          "the dashboard's background work to finish")
    widget.hide()
    widget.deleteLater()
    _drain(qapp)
    QTimer.singleShot(0, lambda: None)


def api_me(runtime) -> dict:
    response = runtime.api_client.get("/auth/me")
    data = response.json()
    return data.get("user", data) if isinstance(data, dict) else {}


def _task_row(window, task_id):
    return next(r for r in window._task_section._task_rows if r.task.get("id") == task_id)


def _circle(window):
    return window._sidebar._timer_control.button


def _click(qapp, button, what: str) -> None:
    _pump(qapp, lambda: button.isEnabled(), 10, f"{what} to become clickable")
    button.click()
    _drain(qapp)


def _click_row_body(qapp, row) -> None:
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    QTest.mouseClick(row._name_label, Qt.MouseButton.LeftButton)
    _drain(qapp)


def _active_entry(api):
    """The backend's running entry for the principal, or None.

    Retried once on a transport error: the test client keeps a pooled
    keep-alive connection across the sleeps between steps, and the server
    may have closed it meanwhile (WinError 10053 on Windows). That is the
    harness's own connection, not the desktop's, so a fresh request is the
    honest answer rather than a failure.
    """
    import httpx

    try:
        return api.get("/time-entries/active").json()["entry"]
    except httpx.TransportError:
        return api.get("/time-entries/active").json()["entry"]


@pytest.mark.usefixtures("clean_slate")
def test_play_pause_break_in_and_break_out_through_the_widgets(
    qapp, window, desktop, api, db, principal
):
    timer = desktop.timer
    finalized = []
    timer.timer_finalized.connect(finalized.append)
    task_id = principal["task_id"]
    sidebar, cards = window._sidebar, window._stat_cards
    work_seconds, break_seconds = 4, 3

    # A fresh window: nothing running, nothing on break, Play waits for a task.
    assert not timer.is_running() and _active_entry(api) is None
    assert sidebar.timer_control_state() == {
        "running": False, "enabled": False, "caption": CAPTION_SELECT_TASK,
    }
    assert sidebar._status_text.text() == "Idle"
    assert cards.active_card._value.full_text() == "No active task"
    _shot(window, "01_fresh")

    # Test A: select the task, press the circular Play.
    _click_row_body(qapp, _task_row(window, task_id))
    assert _task_row(window, task_id).is_selected
    _click(qapp, _circle(window), "Play")
    assert timer.is_running() and timer.task_id == task_id
    _pump(qapp, lambda: timer.entry_id is not None, 30, "the start to be bound")
    first_id = timer.entry_id
    active = _active_entry(api)
    assert active is not None and active["id"] == first_id and active["task_id"] == task_id
    assert len(_running_rows(db, principal["user_id"])) == 1
    assert sidebar.timer_control_state()["running"] is True
    assert sidebar._status_text.text() == "Active"
    assert _task_row(window, task_id)._is_running
    assert cards.active_card._value.full_text() == _task_row(window, task_id).task["name"]
    assert cards.break_button.text() == "Break In"
    _pump(qapp, lambda: cards.break_button.isEnabled(), 5, "Break In to become clickable")
    _shot(window, "02_running_from_play")
    time.sleep(work_seconds)

    # Test C: Break In from the card.
    _click(qapp, cards.break_button, "Break In")
    assert not timer.is_running() and timer.break_status == BreakStatus.ON_BREAK
    assert cards.break_button.text() == "Break Out"
    assert sidebar.timer_control_state() == {
        "running": False, "enabled": False, "caption": CAPTION_ON_BREAK,
    }
    assert window._action_banner.message == BREAK_STARTED_MESSAGE
    assert not window._action_banner.isHidden()
    assert cards.active_card._sub.full_text().startswith("On break")
    _pump(qapp, lambda: len(finalized) == 1, 30, "the break-in stop to be finalized")
    assert finalized[0]["entry"]["id"] == first_id
    assert _active_entry(api) is None, "nothing runs during a break"
    assert len(_running_rows(db, principal["user_id"])) == 0
    _shot(window, "03_on_break")
    # The circle cannot start anything during the break.
    _circle(window).click()
    window._on_play_requested()
    _drain(qapp)
    assert not timer.is_running() and _active_entry(api) is None
    time.sleep(break_seconds)

    # Test E: select something else during the break -- the held task wins.
    other_rows = [r for r in window._task_section._task_rows if r.task.get("id") != task_id]
    if other_rows:
        _click_row_body(qapp, other_rows[0])
    assert timer.pre_break_task()["task_id"] == task_id

    # Test D: Break Out from the card.
    _click(qapp, cards.break_button, "Break Out")
    _pump(qapp, lambda: timer.is_running(), 30, "the resume to start")
    assert timer.task_id == task_id and timer.break_status == BreakStatus.NONE
    _pump(qapp, lambda: timer.entry_id is not None and timer.entry_id != first_id, 30,
          "the resumed start to be bound")
    second_id = timer.entry_id
    active = _active_entry(api)
    assert active["id"] == second_id and active["task_id"] == task_id
    _drain(qapp)
    assert window._action_banner.message == BREAK_ENDED_MESSAGE
    assert cards.break_button.text() == "Break In"
    assert sidebar.timer_control_state()["running"] is True
    _pump(qapp, lambda: sidebar.timer_control_state()["enabled"], 5, "Pause to become clickable")
    _shot(window, "04_resumed")
    time.sleep(work_seconds)

    # Test B: the circular Pause.
    _click(qapp, _circle(window), "Pause")
    assert not timer.is_running()
    _pump(qapp, lambda: len(finalized) == 2, 30, "the pause's stop to be finalized")
    assert finalized[1]["entry"]["id"] == second_id
    assert _active_entry(api) is None
    assert sidebar.timer_control_state()["running"] is False
    assert sidebar._status_text.text() == "Idle"
    assert cards.active_card._value.full_text() == "No active task"
    _shot(window, "05_paused")

    # Test G: the task row's own Start still works, and the circle follows.
    _click(qapp, _task_row(window, task_id)._timer_btn, "the row's Start")
    assert timer.is_running() and timer.task_id == task_id
    _pump(qapp, lambda: timer.entry_id not in (None, first_id, second_id), 30,
          "the row's start to be bound")
    third_id = timer.entry_id
    assert sidebar.timer_control_state()["running"] is True
    time.sleep(2)
    _click(qapp, _circle(window), "Pause")
    _pump(qapp, lambda: len(finalized) == 3, 30, "the final stop to be finalized")
    assert _active_entry(api) is None

    # The database: three stopped rows, one per session, the break between
    # the first two, and never more than one running at once.
    first, second, third = _row(db, first_id), _row(db, second_id), _row(db, third_id)
    for row in (first, second, third):
        assert row["status"] == "stopped" and row["task_id"] == task_id, row
    assert abs(first["total_seconds"] - work_seconds) <= DURATION_TOLERANCE_SECONDS, first
    assert abs(second["total_seconds"] - work_seconds) <= DURATION_TOLERANCE_SECONDS, second
    gap = (second["start_time"] - first["end_time"]).total_seconds()
    assert gap >= break_seconds - DURATION_TOLERANCE_SECONDS, (first, second)
    assert len(_running_rows(db, principal["user_id"])) == 0
    assert len({first_id, second_id, third_id}) == 3, "three sessions, three entries"
    print(
        f"\n[e2e controls] first={first['total_seconds']}s break_gap={gap:.1f}s "
        f"second={second['total_seconds']}s third={third['total_seconds']}s"
    )


@pytest.mark.usefixtures("clean_slate")
def test_rapid_clicks_on_the_circle_create_one_session(qapp, window, desktop, api, db, principal):
    timer = desktop.timer
    finalized = []
    timer.timer_finalized.connect(finalized.append)
    task_id = principal["task_id"]
    _click_row_body(qapp, _task_row(window, task_id))

    button = _circle(window)
    _pump(qapp, lambda: button.isEnabled(), 5, "Play to become clickable")
    for _ in range(6):
        button.click()
    _drain(qapp)
    assert timer.is_running() and timer.task_id == task_id
    _pump(qapp, lambda: timer.entry_id is not None, 30, "the start to be bound")
    entry_id = timer.entry_id
    time.sleep(2)
    assert _active_entry(api)["id"] == entry_id
    assert len(_running_rows(db, principal["user_id"])) == 1

    _pump(qapp, lambda: button.isEnabled(), 5, "Pause to become clickable")
    for _ in range(6):
        button.click()
    _drain(qapp)
    assert not timer.is_running()
    _pump(qapp, lambda: len(finalized) == 1, 30, "the stop to be finalized")
    assert finalized[0]["entry"]["id"] == entry_id
    assert _active_entry(api) is None
    assert len(_running_rows(db, principal["user_id"])) == 0
    # One session for the whole burst.
    from sqlalchemy import text

    with db.connect() as conn:
        count = conn.execute(
            text("SELECT count(*) FROM time_entries WHERE user_id = :u AND id >= :first"),
            {"u": principal["user_id"], "first": entry_id},
        ).scalar_one()
    assert count == 1, f"{count} entries were created by a burst of clicks"

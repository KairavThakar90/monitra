"""
Project/task synchronisation, end to end: web -> HTTP -> database -> desktop,
and desktop -> HTTP -> database -> web.

Nothing here is mocked. A real backend process serves the real API against
the development database; the real desktop runtime and the real
`DashboardWindow` (offscreen) load, probe and reconcile against it; and
"the web" is a second, administrator principal driving the same endpoints
the web client uses. Every assertion is made against what the desktop
actually holds, what the API answers, or the row in Postgres.

The one thing the tests do that a user would not is call the dashboard's
probe slot directly, instead of waiting out its 30-second timer -- the timer
fires exactly that slot, and a suite that sleeps half a minute per step
would not be run. Nothing calls `refresh_data()`; that would be pressing
Refresh, which is the thing being proved unnecessary.

Opt-in, because it needs the development database and a free port:

    MONITRA_E2E=1 python -m pytest tests/e2e/test_sync_lifecycle_e2e.py -q -s   (from desktop/)

The principals are disposable and provisioned by
`backend/tests/e2e_sync_support.py`, which refuses any database that is not
the one `DATABASE_URL_DEV` names and removes everything it created.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

import httpx
import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("MONITRA_E2E") != "1",
    reason="real-backend E2E; set MONITRA_E2E=1 to run",
)

DESKTOP_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = DESKTOP_ROOT.parent / "backend"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _pump(qapp, until: Callable[[], bool], timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if until():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


# ── the real backend ─────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def backend():
    port = _free_port()
    env = dict(os.environ, ENV="development", PYTHONUNBUFFERED="1")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
         "--port", str(port), "--log-level", "warning"],
        cwd=str(BACKEND_ROOT), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 90
        while True:
            try:
                if httpx.get(f"{base}/health", timeout=2).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if proc.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError("the backend did not come up")
            time.sleep(0.25)
        yield base
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.fixture(scope="module")
def fixture():
    helper = BACKEND_ROOT / "tests" / "e2e_sync_support.py"
    out = subprocess.run(
        [sys.executable, str(helper), "provision"], cwd=str(BACKEND_ROOT),
        capture_output=True, text=True, check=True,
    ).stdout.strip().splitlines()[-1]
    data = json.loads(out)
    try:
        yield data
    finally:
        subprocess.run(
            [sys.executable, str(helper), "cleanup", json.dumps(data)],
            cwd=str(BACKEND_ROOT), capture_output=True, text=True, check=False,
        )


@pytest.fixture(scope="module")
def db(fixture):
    from sqlalchemy import create_engine

    engine = create_engine(fixture["database_url"], pool_pre_ping=True)
    yield engine
    engine.dispose()


@pytest.fixture
def web(backend, fixture):
    """The administrator, driving the API the way the web client does."""
    return httpx.Client(base_url=backend, timeout=30,
                        headers={"Authorization": f"Bearer {fixture['admin']['token']}"})


@pytest.fixture
def employee_api(backend, fixture):
    return httpx.Client(base_url=backend, timeout=30,
                        headers={"Authorization": f"Bearer {fixture['employee']['token']}"})


def _runtime(tmp_path, base_url, token, monkeypatch):
    monkeypatch.setenv("SMS_API_BASE_URL", base_url)
    monkeypatch.setenv("MONITRA_ENV", "development")
    from core.runtime import ApplicationRuntime
    from storage.manager import StorageManager

    manager = StorageManager(str(tmp_path / "e2e-cache.db"))
    runtime = ApplicationRuntime(storage=manager)
    runtime.api_client.base_url = base_url
    runtime.api_client.access_token = token
    return runtime


@pytest.fixture
def desktop(qapp, tmp_path, backend, fixture, monkeypatch):
    """The real desktop runtime and dashboard, signed in as the employee."""
    from ui.dashboard_window import DashboardWindow

    runtime = _runtime(tmp_path, backend, fixture["employee"]["token"], monkeypatch)
    runtime.start_services()
    window = DashboardWindow(
        runtime=runtime, session_manager=runtime.session_manager,
        project_service=runtime.project_service, task_service=runtime.task_service,
        time_entry_service=runtime.time_entry_service, api_client=runtime.api_client,
    )
    yield runtime, window
    window.reset_state()
    window.deleteLater()
    runtime.shutdown(timeout_ms=5000)


def _sign_in(qapp, runtime, window, fixture, project_id):
    """Open the dashboard as the app does on launch and wait for the
    automatic startup synchronisation to land."""
    window.on_login(dict(fixture["employee"]["profile"]))
    _pump(qapp, lambda: any(p["id"] == project_id for p in window._projects), 30,
          "the startup refresh to load projects")
    _pump(qapp, lambda: window._refresh_outstanding == 0, 30, "the startup refresh round")
    # The app opens the newest project (the list is newest-first) or the one
    # the user was last in. The tests work in project A, so click it.
    if not window._current_project or window._current_project["id"] != project_id:
        window._on_project_selected(next(p for p in window._projects if p["id"] == project_id))
    _pump(qapp, lambda: window._current_project is not None
          and window._current_project["id"] == project_id
          and window._task_section._has_loaded_tasks
          and not runtime.tasks.in_flight, 30, "the selected project's tasks")
    # Baseline the change probe, as the first timer tick would.
    window._probe_sync_revision()
    _pump(qapp, lambda: window._sync_revision is not None, 30, "the probe baseline")


def _probe_until(qapp, window, until, what, attempts=10):
    """Fire the probe slot as its timer would, until `until()` holds."""
    for _ in range(attempts):
        window._probe_sync_revision()
        try:
            _pump(qapp, until, 15, what)
            return
        except AssertionError:
            continue
    raise AssertionError(f"timed out waiting for {what}")


def _project_ids(window):
    return [p["id"] for p in window._projects]


def _task_names(window):
    return [t.get("name") for t in window._project_tasks]


def _time_entry(db, entry_id):
    from sqlalchemy import text

    with db.connect() as conn:
        return dict(conn.execute(
            text("SELECT id, user_id, project_id, task_id, start_time, end_time, "
                 "total_seconds, status, client_op FROM time_entries WHERE id = :id"),
            {"id": entry_id},
        ).mappings().one())


# ── E2E 1: startup synchronisation ───────────────────────────────────────────

def test_opening_the_desktop_loads_the_backends_projects_and_tasks(qapp, desktop, fixture):
    runtime, window = desktop
    _sign_in(qapp, runtime, window, fixture, fixture["project_id"])

    assert fixture["project_id"] in _project_ids(window)
    assert any(t["id"] == fixture["task_id"] for t in window._project_tasks)
    assert runtime.sync.last_synced_at is not None, "the startup round counts as a sync"
    # The probe knows this backend and has relaxed the full-refresh cadence.
    assert window._sync_probe_supported is True


# ── E2E 2/3/D/E: changes made on the web reach the open desktop ─────────────

def test_a_project_created_on_the_web_appears_without_refresh(qapp, desktop, fixture, web):
    runtime, window = desktop
    _sign_in(qapp, runtime, window, fixture, fixture["project_id"])

    created = web.post("/api/v1/projects", json={
        "project_name": f"E2E sync B {fixture['stamp']}", "status_id": fixture["active_status_id"],
        "leader_id": fixture["admin"]["user_id"], "employee_ids": [fixture["employee"]["user_id"]],
        "deadline": "2030-01-01", "billing_type": "free",
    })
    assert created.status_code == 201, created.text
    project_b = created.json()["id"]

    _probe_until(qapp, window, lambda: project_b in _project_ids(window), "project B on the desktop")
    assert window._current_project["id"] == fixture["project_id"], "the selection is not disturbed"


def test_a_task_created_and_then_edited_on_the_web_appears_without_refresh(qapp, desktop, fixture, web):
    runtime, window = desktop
    _sign_in(qapp, runtime, window, fixture, fixture["project_id"])
    name = f"E2E web task {fixture['stamp']}"

    created = web.post(f"/api/v1/projects/{fixture['project_id']}/tasks", json={
        "name": name, "status_id": fixture["todo_status_id"],
        "assignee_id": fixture["employee"]["user_id"],
    })
    assert created.status_code == 201, created.text
    task_id = created.json()["id"]
    _probe_until(qapp, window, lambda: name in _task_names(window), "the web-created task")

    renamed = web.patch(f"/api/v1/projects/{fixture['project_id']}/tasks/{task_id}",
                        json={"name": name + " (renamed)"})
    assert renamed.status_code == 200, renamed.text
    _probe_until(qapp, window, lambda: name + " (renamed)" in _task_names(window), "the rename")
    assert name not in _task_names(window)


def test_removing_the_member_on_the_web_makes_the_project_disappear(qapp, desktop, fixture, web):
    runtime, window = desktop
    created = web.post("/api/v1/projects", json={
        "project_name": f"E2E sync C {fixture['stamp']}", "status_id": fixture["active_status_id"],
        "leader_id": fixture["admin"]["user_id"], "employee_ids": [fixture["employee"]["user_id"]],
        "deadline": "2030-01-01", "billing_type": "free",
    })
    assert created.status_code == 201, created.text
    project_c = created.json()["id"]
    _sign_in(qapp, runtime, window, fixture, fixture["project_id"])
    assert project_c in _project_ids(window)
    # The user is working in project C when their membership is removed.
    window._on_project_selected(next(p for p in window._projects if p["id"] == project_c))
    _pump(qapp, lambda: window._task_section._has_loaded_tasks, 30, "project C's tasks")

    removed = web.delete(f"/api/v1/projects/{project_c}/members/{fixture['employee']['user_id']}")
    assert removed.status_code in (200, 204), removed.text

    _probe_until(qapp, window, lambda: project_c not in _project_ids(window), "project C to disappear")
    assert window._current_project is not None and window._current_project["id"] != project_c
    assert runtime.cache.get_cached_tasks(project_c) is None, "its cached tasks are dropped"


# ── E2E 4/6: desktop mutations reach the web, and a retry does not duplicate ─

def test_a_desktop_created_task_reaches_the_web_and_a_retried_create_does_not_duplicate(
    qapp, desktop, fixture, web,
):
    runtime, window = desktop
    _sign_in(qapp, runtime, window, fixture, fixture["project_id"])
    section = window._task_section
    name = f"E2E desktop task {fixture['stamp']}"
    project_id = fixture["project_id"]
    client_op = section._client_op_for_create(project_id, name)

    # The same submission the Add Task dialog makes.
    section._run_task_mutation(
        lambda: runtime.task_service.create_task(
            project_id, name, fixture["employee"]["user_id"], client_op=client_op),
        success_message="Task created successfully.", key=f"create-task:{project_id}:{name}",
        kind="created", project_id=project_id,
        after_success=lambda: section._pending_create_ops.pop((project_id, name), None),
    )
    _pump(qapp, lambda: name in _task_names(window), 30, "the created task on screen")
    _pump(qapp, lambda: not runtime.tasks.in_flight, 30, "the reconciling reload")
    assert name in _task_names(window), "the reconciling read must not remove it"

    # The web sees it, once.
    listed = web.get(f"/api/v1/projects/{project_id}/tasks").json()
    matching = [t for t in listed if t["name"] == name]
    assert len(matching) == 1
    task_id = matching[0]["id"]

    # The reply was lost; the client retries with the same key.
    replay = runtime.task_service.create_task(
        project_id, name, fixture["employee"]["user_id"], client_op=client_op)
    assert replay["id"] == task_id
    listed = web.get(f"/api/v1/projects/{project_id}/tasks").json()
    assert len([t for t in listed if t["name"] == name]) == 1, "no duplicate task"


# ── E2E 7: overlapping synchronisation, latest state wins ────────────────────

def test_a_list_in_flight_during_a_local_change_cannot_undo_it(qapp, desktop, fixture, web):
    runtime, window = desktop
    _sign_in(qapp, runtime, window, fixture, fixture["project_id"])
    project_id = fixture["project_id"]
    name = f"E2E overlap task {fixture['stamp']}"

    # A list read goes out (real HTTP, in flight on the pool)...
    assert window._load_tasks(project_id)
    # ...and the user's create lands on screen before it answers.
    created = runtime.task_service.create_task(project_id, name, fixture["employee"]["user_id"])
    window._on_task_mutated("created", project_id, created)
    assert name in _task_names(window)

    _pump(qapp, lambda: not runtime.tasks.in_flight, 30, "every read to settle")
    assert name in _task_names(window), "the list that predated the create must not paint it away"
    assert name in [t["name"] for t in web.get(f"/api/v1/projects/{project_id}/tasks").json()]


# ── E2E 8: the timer is untouched by synchronisation ─────────────────────────

def test_a_running_timer_survives_synchronisation_and_stops_against_the_right_task(
    qapp, desktop, fixture, web, db,
):
    runtime, window = desktop
    _sign_in(qapp, runtime, window, fixture, fixture["project_id"])
    timer = runtime.timer

    timer.start_tracking(fixture["project_id"], fixture["task_id"], "A1")
    _pump(qapp, lambda: timer.entry_id is not None, 30, "the start to be bound")
    entry_id = timer.entry_id

    # The web changes things while the timer runs, and the desktop notices.
    created = web.post(f"/api/v1/projects/{fixture['project_id']}/tasks", json={
        "name": f"E2E mid-timer task {fixture['stamp']}", "status_id": fixture["todo_status_id"],
        "assignee_id": fixture["employee"]["user_id"],
    })
    assert created.status_code == 201, created.text
    _probe_until(qapp, window, lambda: created.json()["name"] in _task_names(window), "the new task")
    _pump(qapp, lambda: window._refresh_outstanding == 0, 30, "the triggered refresh")

    assert timer.is_running() and timer.entry_id == entry_id
    assert timer.task_id == fixture["task_id"]
    time.sleep(3)
    elapsed = timer.elapsed_seconds()
    timer.stop_tracking()
    _pump(qapp, lambda: not runtime.tasks.in_flight and runtime.cache.get_pending_count() == 0,
          30, "the stop to reach the backend")

    row = _time_entry(db, entry_id)
    assert row["end_time"] is not None and row["status"] != "running"
    assert row["project_id"] == fixture["project_id"] and row["task_id"] == fixture["task_id"]
    assert row["user_id"] == fixture["employee"]["user_id"]
    assert abs(row["total_seconds"] - elapsed) <= 2


# ── E2E 5: offline -> restart -> reconnect -> the queue replays exactly once ─

def test_a_session_tracked_offline_reaches_the_database_once_after_restart_and_reconnect(
    qapp, tmp_path, backend, fixture, db, monkeypatch,
):
    dead = f"http://127.0.0.1:{_free_port()}"
    first = _runtime(tmp_path, dead, fixture["employee"]["token"], monkeypatch)
    first.start_services()
    try:
        first.timer.start_tracking(fixture["project_id"], fixture["task_id"], "A1")
        session = first.timer.active_session()
        _pump(qapp, lambda: first.cache.get_pending_count() >= 1, 30, "the start to be queued")
        time.sleep(2)
        first.timer.stop_tracking()
        _pump(qapp, lambda: first.cache.get_pending_count() >= 2, 30, "the stop to be queued")
    finally:
        first.shutdown(timeout_ms=5000)

    # Restart, now with the backend reachable.
    second = _runtime(tmp_path, backend, fixture["employee"]["token"], monkeypatch)
    second.start_services()
    try:
        assert second.cache.get_pending_count() >= 2, "the queue survived the restart"
        _pump(qapp, lambda: second.cache.get_pending_count() == 0, 60, "the queue to drain")
    finally:
        second.shutdown(timeout_ms=5000)

    from sqlalchemy import text

    with db.connect() as conn:
        rows = conn.execute(
            text("SELECT id, end_time, total_seconds FROM time_entries "
                 "WHERE user_id = :u AND client_op = :op"),
            {"u": fixture["employee"]["user_id"], "op": session["client_op"]},
        ).fetchall()
    assert len(rows) == 1, "replaying the queue produced exactly one entry"
    assert rows[0].end_time is not None
    assert rows[0].total_seconds >= 2


# ── E2E 9: waking from sleep resumes synchronisation ─────────────────────────

def test_waking_from_sleep_resynchronises_without_waiting_out_the_timers(qapp, desktop, fixture, web):
    runtime, window = desktop
    _sign_in(qapp, runtime, window, fixture, fixture["project_id"])
    before = runtime.sync.last_synced_at
    name = f"E2E sleep task {fixture['stamp']}"
    created = web.post(f"/api/v1/projects/{fixture['project_id']}/tasks", json={
        "name": name, "status_id": fixture["todo_status_id"],
        "assignee_id": fixture["employee"]["user_id"],
    })
    assert created.status_code == 201, created.text

    # What the recovery heartbeat emits when it finds itself an hour late.
    runtime.recovery.system_resumed.emit(3600.0)

    _pump(qapp, lambda: name in _task_names(window), 30, "the post-sleep refresh")
    _pump(qapp, lambda: runtime.sync.last_synced_at != before, 30, "last-sync to advance")

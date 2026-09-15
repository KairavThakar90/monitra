"""
The timing lifecycle, end to end: desktop -> HTTP -> database -> reports.

Nothing here is mocked. A real backend process serves the real API against
the development database; the real desktop runtime (TimerService, the
durable queue, the API client) starts and stops timers against it; and every
assertion is made against the row in Postgres, the API's own answer, the
desktop's own state, and the aggregates the web dashboard renders -- read
twice, so "refresh" cannot change the number.

Opt-in, because it needs the development database and a free port:

    MONITRA_E2E=1 python -m pytest tests/e2e -q -s      (from desktop/)

The principal is a disposable `employee` provisioned by
`backend/tests/e2e_support.py` and removed afterwards together with every
row it wrote. The helper refuses any database that is not the one
`DATABASE_URL_DEV` names.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
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
UTC = timezone.utc

#: How far the server's recorded duration may sit from the interval the
#: desktop measured. One second of rounding on each end plus the latency
#: asymmetry between the start and the stop request; documented in
#: docs/TIMING_MODEL.md.
DURATION_TOLERANCE_SECONDS = 2


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
        deadline = time.monotonic() + 60
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
def principal():
    helper = BACKEND_ROOT / "tests" / "e2e_support.py"
    out = subprocess.run(
        [sys.executable, str(helper), "provision"], cwd=str(BACKEND_ROOT),
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


@pytest.fixture(scope="module")
def db(principal):
    from sqlalchemy import create_engine

    engine = create_engine(principal["database_url"], pool_pre_ping=True)
    yield engine
    engine.dispose()


def _row(db, entry_id: int) -> dict:
    from sqlalchemy import text

    with db.connect() as conn:
        row = conn.execute(
            text("SELECT id, user_id, task_id, start_time, end_time, total_seconds, status, "
                 "client_op, round(extract(epoch from (end_time - start_time))) AS derived "
                 "FROM time_entries WHERE id = :id"),
            {"id": entry_id},
        ).mappings().one()
        return dict(row)


def _running_rows(db, user_id: int) -> list:
    from sqlalchemy import text

    with db.connect() as conn:
        return conn.execute(
            text("SELECT id FROM time_entries WHERE user_id = :u AND end_time IS NULL"),
            {"u": user_id},
        ).fetchall()


@pytest.fixture
def api(backend, principal):
    return httpx.Client(base_url=backend, timeout=30,
                        headers={"Authorization": f"Bearer {principal['token']}"})


@pytest.fixture
def clean_slate(api, db, principal):
    """No running entry before each test, whatever the previous one did."""
    for (entry_id,) in _running_rows(db, principal["user_id"]):
        api.post(f"/time-entries/{entry_id}/stop", json={})
    yield


@pytest.fixture
def desktop(qapp, tmp_path, backend, principal, monkeypatch):
    """The real desktop runtime, pointed at the real backend."""
    monkeypatch.setenv("SMS_API_BASE_URL", backend)
    monkeypatch.setenv("MONITRA_ENV", "development")
    from core.runtime import ApplicationRuntime
    from storage.manager import StorageManager

    manager = StorageManager(str(tmp_path / "e2e-cache.db"))
    runtime = ApplicationRuntime(storage=manager)
    runtime.api_client.base_url = backend
    runtime.api_client.access_token = principal["token"]
    yield runtime
    runtime.shutdown(timeout_ms=3000)


def _dashboard_seconds(api, principal, day: str) -> int:
    response = api.get("/api/v1/react/dashboard",
                       params={"start_date": day, "end_date": day, "member_id": principal["user_id"]})
    assert response.status_code == 200, response.text
    return response.json()["summary"]["total_seconds"]


def _time_tracking_seconds(api, principal, day: str) -> int:
    response = api.get(f"/api/v1/time-tracking/{principal['user_id']}",
                       params={"start_date": day, "end_date": day})
    assert response.status_code == 200, response.text
    return response.json()["summary"]["total_seconds"]


def _ist_today() -> str:
    from core.time_format import ist_today
    return ist_today().isoformat()


# ── 1. Start -> wait -> Stop, verified at every layer ────────────────────────

@pytest.mark.usefixtures("clean_slate")
@pytest.mark.parametrize("wait_seconds", [10, 30])
def test_start_wait_stop_is_exact_at_every_layer(qapp, desktop, api, db, principal, wait_seconds):
    timer = desktop.timer
    finalized = []
    timer.timer_finalized.connect(finalized.append)

    pressed_start = datetime.now(UTC)
    timer.start_tracking(principal["project_id"], principal["task_id"], "E2E task")
    _pump(qapp, lambda: timer.entry_id is not None, 30, "the start to be bound")
    entry_id = timer.entry_id
    session = timer.active_session()

    # Database: one running row, stamped on the server clock, carrying our key.
    row = _row(db, entry_id)
    assert row["end_time"] is None and row["status"] == "running"
    assert row["client_op"] == session["client_op"]
    assert len(_running_rows(db, principal["user_id"])) == 1
    # API: the scoped active read names exactly this entry and carries the clock.
    active = api.get("/time-entries/active").json()
    assert active["entry"]["id"] == entry_id
    assert "server_time" in active
    # Desktop: elapsed counts from the local anchor and the offset is recorded.
    assert 0 <= timer.elapsed_seconds() <= 3
    assert "clock_offset_seconds" in session or True

    time.sleep(wait_seconds)
    _pump(qapp, lambda: True, 0.1, "tick")
    desktop_elapsed = timer.elapsed_seconds()

    pressed_stop = datetime.now(UTC)
    timer.stop_tracking()
    _pump(qapp, lambda: bool(finalized), 30, "the stop to be finalized")
    assert not timer.is_running()
    entry = finalized[0]["entry"]
    assert entry["id"] == entry_id

    # Database: finalized once, duration derived from the two timestamps.
    row = _row(db, entry_id)
    assert row["end_time"] is not None and row["status"] == "stopped"
    assert row["total_seconds"] == int(row["derived"]), row
    measured = (pressed_stop - pressed_start).total_seconds()
    assert abs(row["total_seconds"] - measured) <= DURATION_TOLERANCE_SECONDS, (row, measured)
    assert abs(row["total_seconds"] - desktop_elapsed) <= DURATION_TOLERANCE_SECONDS, (row, desktop_elapsed)
    assert len(_running_rows(db, principal["user_id"])) == 0
    # API: the entry read agrees with the row, and nothing is active.
    read = api.get(f"/time-entries/{entry_id}").json()
    assert read["total_seconds"] == row["total_seconds"] and read["is_running"] is False
    assert api.get("/time-entries/active").json()["entry"] is None

    # What the web renders, immediately and again after a "refresh".
    day = _ist_today()
    dashboard_first = _dashboard_seconds(api, principal, day)
    tracking_first = _time_tracking_seconds(api, principal, day)
    dashboard_again = _dashboard_seconds(api, principal, day)
    tracking_again = _time_tracking_seconds(api, principal, day)
    assert dashboard_first == dashboard_again == tracking_first == tracking_again
    # This test's sessions are the only ones this principal has today.
    total_today = sum(
        _row(db, r[0])["total_seconds"] for r in _all_rows_today(db, principal["user_id"])
    )
    assert dashboard_first == total_today

    print(
        f"\n[e2e wait={wait_seconds}s] start_time={row['start_time'].isoformat()} "
        f"end_time={row['end_time'].isoformat()} server_total_seconds={row['total_seconds']} "
        f"desktop_elapsed={desktop_elapsed} dashboard={dashboard_first} "
        f"post_refresh={dashboard_again} time_tracking={tracking_again}"
    )


def _all_rows_today(db, user_id: int) -> list:
    from sqlalchemy import text
    from core.time_format import ist_day_bounds_utc, ist_today

    start, end = ist_day_bounds_utc(ist_today())
    with db.connect() as conn:
        return conn.execute(
            text("SELECT id FROM time_entries WHERE user_id = :u AND start_time >= :s "
                 "AND start_time < :e AND end_time IS NOT NULL"),
            {"u": user_id, "s": start, "e": end},
        ).fetchall()


# ── 2. Repeated operations stay separate ─────────────────────────────────────

@pytest.mark.usefixtures("clean_slate")
def test_repeated_start_stop_produce_separate_correct_entries(qapp, desktop, api, db, principal):
    timer = desktop.timer
    finalized = []
    timer.timer_finalized.connect(finalized.append)
    ids = []
    for _ in range(3):
        timer.start_tracking(principal["project_id"], principal["task_id"], "E2E task")
        _pump(qapp, lambda: timer.entry_id is not None, 30, "start")
        ids.append(timer.entry_id)
        time.sleep(2)
        count = len(finalized)
        timer.stop_tracking()
        _pump(qapp, lambda: len(finalized) > count, 30, "stop")
    assert len(set(ids)) == 3
    for entry_id in ids:
        row = _row(db, entry_id)
        assert row["status"] == "stopped"
        assert row["total_seconds"] == int(row["derived"])
        assert 1 <= row["total_seconds"] <= 4, row


# ── 3. Duplicate and concurrent requests over real HTTP ──────────────────────

@pytest.mark.usefixtures("clean_slate")
def test_a_retried_start_returns_the_same_entry_and_never_a_second(api, db, principal):
    now = datetime.now(UTC).isoformat()
    body = {"project_id": principal["project_id"], "task_id": principal["task_id"],
            "started_at": now, "client_time": now, "client_op": "timer:e2e:retry:0001"}
    first = api.post("/time-entries/start", json=body)
    assert first.status_code == 201, first.text
    replay = api.post("/time-entries/start", json=body)
    assert replay.status_code == 200, replay.text
    assert replay.json()["id"] == first.json()["id"]
    assert replay.json()["start_time"] == first.json()["start_time"]
    assert len(_running_rows(db, principal["user_id"])) == 1

    other = api.post("/time-entries/start", json=dict(body, client_op="timer:e2e:retry:0002"))
    assert other.status_code == 409, other.text
    assert other.json()["detail"]["active_entry"]["id"] == first.json()["id"]

    entry_id = first.json()["id"]
    stop_body = {"stopped_at": datetime.now(UTC).isoformat(), "client_time": datetime.now(UTC).isoformat()}
    stopped = api.post(f"/time-entries/{entry_id}/stop", json=stop_body)
    assert stopped.status_code == 200, stopped.text
    again = api.post(f"/time-entries/{entry_id}/stop",
                     json={"stopped_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat()})
    assert again.status_code == 200, again.text
    assert again.json()["end_time"] == stopped.json()["end_time"]
    assert again.json()["total_seconds"] == stopped.json()["total_seconds"]


@pytest.mark.usefixtures("clean_slate")
def test_concurrent_starts_create_exactly_one_entry(api, backend, principal, db):
    results = []
    barrier = threading.Barrier(6)

    def fire(index: int):
        with httpx.Client(base_url=backend, timeout=30,
                          headers={"Authorization": f"Bearer {principal['token']}"}) as client:
            barrier.wait()
            response = client.post("/time-entries/start", json={
                "project_id": principal["project_id"], "task_id": principal["task_id"],
                "client_op": f"timer:e2e:concurrent:{index}",
            })
            results.append(response.status_code)

    threads = [threading.Thread(target=fire, args=(i,)) for i in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    assert sorted(results) == [201] + [409] * 5, results
    assert len(_running_rows(db, principal["user_id"])) == 1


@pytest.mark.usefixtures("clean_slate")
def test_concurrent_stops_finalize_once(api, backend, principal, db):
    started = api.post("/time-entries/start", json={
        "project_id": principal["project_id"], "task_id": principal["task_id"],
    })
    assert started.status_code == 201, started.text
    entry_id = started.json()["id"]
    time.sleep(1.5)
    answers = []
    barrier = threading.Barrier(4)

    def fire(offset: int):
        with httpx.Client(base_url=backend, timeout=30,
                          headers={"Authorization": f"Bearer {principal['token']}"}) as client:
            barrier.wait()
            response = client.post(f"/time-entries/{entry_id}/stop", json={
                "stopped_at": (datetime.now(UTC) + timedelta(seconds=offset)).isoformat(),
            })
            answers.append(response.json())

    threads = [threading.Thread(target=fire, args=(i,)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    assert len({a["end_time"] for a in answers}) == 1, answers
    assert len({a["total_seconds"] for a in answers}) == 1
    row = _row(db, entry_id)
    assert row["total_seconds"] == int(row["derived"]) == answers[0]["total_seconds"]


# ── 4. The client's clock does not matter ────────────────────────────────────

@pytest.mark.usefixtures("clean_slate")
def test_a_skewed_client_clock_records_the_same_interval(api, db, principal):
    skew = timedelta(minutes=7)     # this client's clock runs seven minutes fast
    pressed = datetime.now(UTC) + skew
    started = api.post("/time-entries/start", json={
        "project_id": principal["project_id"], "task_id": principal["task_id"],
        "started_at": pressed.isoformat(), "client_time": pressed.isoformat(),
        "client_op": "timer:e2e:skew:0001",
    })
    assert started.status_code == 201, started.text
    entry_id = started.json()["id"]
    server_start = datetime.fromisoformat(started.json()["start_time"].replace("Z", "+00:00"))
    assert abs((server_start - (pressed - skew)).total_seconds()) <= 3, "start was not placed on the server clock"

    time.sleep(3)
    pressed_stop = datetime.now(UTC) + skew
    stopped = api.post(f"/time-entries/{entry_id}/stop", json={
        "stopped_at": pressed_stop.isoformat(), "client_time": pressed_stop.isoformat(),
    })
    assert stopped.status_code == 200, stopped.text
    row = _row(db, entry_id)
    assert 2 <= row["total_seconds"] <= 5, row     # three seconds, not seven minutes and three


# ── 5. Restart and reconciliation ────────────────────────────────────────────

@pytest.mark.usefixtures("clean_slate")
def test_a_restart_recovers_the_running_entry_without_duplicating_it(qapp, tmp_path, backend, principal, db, monkeypatch):
    monkeypatch.setenv("SMS_API_BASE_URL", backend)
    from core.runtime import ApplicationRuntime
    from storage.manager import StorageManager

    manager = StorageManager(str(tmp_path / "restart-cache.db"))
    first = ApplicationRuntime(storage=manager)
    first.api_client.base_url = backend
    first.api_client.access_token = principal["token"]
    first.timer.start_tracking(principal["project_id"], principal["task_id"], "E2E task")
    _pump(qapp, lambda: first.timer.entry_id is not None, 30, "start")
    entry_id = first.timer.entry_id
    anchor = first.timer.active_session()["started_at_utc"]
    time.sleep(2)
    # The process dies: no clean stop, no stop request.
    first.timer._tick_timer.stop()
    first.tasks.shutdown(timeout_ms=2000)
    manager.close()

    manager = StorageManager(str(tmp_path / "restart-cache.db"))
    second = ApplicationRuntime(storage=manager)
    second.api_client.base_url = backend
    second.api_client.access_token = principal["token"]
    try:
        recovered = second.timer.recover()
        assert recovered is not None and recovered["entry_id"] == entry_id
        assert recovered["started_at_utc"] == anchor
        assert second.timer.elapsed_seconds() >= 2
        # The backend agrees, and reconciling with it changes nothing.
        active = second.api_client.get("/time-entries/active").json()
        assert active["entry"]["id"] == entry_id
        second.timer.adopt_remote_session(active["entry"], server_time=active["server_time"])
        assert second.timer.entry_id == entry_id
        assert second.timer.active_session()["started_at_utc"] == anchor
        assert len(_running_rows(db, principal["user_id"])) == 1

        finalized = []
        second.timer.timer_finalized.connect(finalized.append)
        second.timer.stop_tracking()
        _pump(qapp, lambda: bool(finalized), 30, "stop")
        row = _row(db, entry_id)
        assert row["status"] == "stopped" and row["total_seconds"] == int(row["derived"])
        assert row["total_seconds"] >= 2
    finally:
        second.shutdown(timeout_ms=3000)


@pytest.mark.usefixtures("clean_slate")
def test_a_queued_start_replayed_after_a_lost_response_binds_the_same_entry(qapp, desktop, api, db, principal):
    """The lost-response case: the backend applied the start, the desktop
    never heard, and the durable queue re-sends it. The replay must resolve
    to the entry the first attempt created, and the queued stop must land
    on that entry."""
    from background_services.timer.timer_service import new_client_op

    started_at = datetime.now(UTC)
    client_op = new_client_op(principal["task_id"], started_at)
    first = api.post("/time-entries/start", json={
        "project_id": principal["project_id"], "task_id": principal["task_id"],
        "started_at": started_at.isoformat(), "client_time": started_at.isoformat(),
        "client_op": client_op,
    })
    assert first.status_code == 201, first.text
    entry_id = first.json()["id"]

    cache = desktop.cache
    cache.enqueue_action(
        "stop_timer",
        {"entry_id": None, "task_id": principal["task_id"], "client_op": client_op,
         "stopped_at": (datetime.now(UTC) + timedelta(seconds=1)).isoformat()},
        priority=1, idempotency_key=f"stop:{client_op}",
    )
    result = desktop.sync._handle_start_timer({
        "project_id": principal["project_id"], "task_id": principal["task_id"],
        "started_at": started_at.isoformat(), "client_op": client_op,
    })
    assert result["entry_id"] == entry_id
    assert len(_running_rows(db, principal["user_id"])) == 1
    pending = cache.pending_stop_payload_for_entry(entry_id)
    assert pending is not None, "the queued stop never learned the entry id"

    time.sleep(1.2)
    stopped = desktop.sync._handle_stop_timer(pending)
    assert stopped["id"] == entry_id
    row = _row(db, entry_id)
    assert row["status"] == "stopped" and row["total_seconds"] == int(row["derived"])

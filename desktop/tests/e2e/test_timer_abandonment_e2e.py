"""
Abandoned timers, end to end: desktop -> HTTP -> database -> reports.

Nothing is mocked. A real backend process (with the reconcile job's token set)
serves the real API against a real PostgreSQL; the real desktop runtime starts
a timer and heartbeats against it; time is *moved* by rewriting the disposable
principal's own rows (a weekend cannot be waited out), and every assertion is
made against the row in Postgres, the API's answer, the desktop's own state
and the aggregates the web renders.

The incident being reproduced: a timer started Friday at 17:23 IST was still
`running` on Monday and Active Users read 65:32:33 (docs/TIMING_MODEL.md
section 9).

Opt-in (needs a database and a free port):

    MONITRA_E2E=1 python -m pytest tests/e2e/test_timer_abandonment_e2e.py -q -s

The principal is a disposable `employee` provisioned and removed by
`backend/tests/e2e_support.py`, which refuses any database that is not the one
`DATABASE_URL_DEV` names.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from tests.e2e.test_timing_lifecycle_e2e import (  # noqa: F401  (fixtures and helpers)
    BACKEND_ROOT, _free_port, _pump, _row, _running_rows, api, clean_slate, db, desktop, principal,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("MONITRA_E2E") != "1",
    reason="real-backend E2E; set MONITRA_E2E=1 to run",
)

UTC = timezone.utc
JOB_TOKEN = "e2e-reconcile-token"
#: The second secret that closing an entry by hand needs; the scheduler never has it.
OPERATOR_TOKEN = "e2e-operator-token"


@pytest.fixture(scope="module")
def backend():
    """The real backend, with the scheduler token set so the reconcile endpoint
    can be called the way the scheduled job calls it."""
    port = _free_port()
    env = dict(os.environ, ENV="development", PYTHONUNBUFFERED="1", EMAIL_DISPATCH_TOKEN=JOB_TOKEN,
               TIMER_OPERATOR_TOKEN=OPERATOR_TOKEN, TIMER_RECONCILER_ENABLED="false")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
         "--port", str(port), "--log-level", "warning"],
        cwd=str(BACKEND_ROOT), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
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


def _job(backend: str, query: str = "") -> dict:
    """Call the reconcile endpoint. A plain call is the scheduler's; one that applies by hand
    (`dry_run=false`) also presents the operator's secret, as an approved correction does."""
    headers = {"X-Email-Dispatch-Token": JOB_TOKEN}
    if "dry_run=false" in query:
        headers["X-Timer-Operator-Token"] = OPERATOR_TOKEN
    response = httpx.post(f"{backend}/internal/time-entries/reconcile{query}", headers=headers, timeout=60)
    assert response.status_code == 200, response.text
    return response.json()


def _move_in_time(db, entry_id: int, *, start: datetime, last_seen: datetime | None) -> None:
    """Put a running entry where it would be after a weekend: started `start`,
    last heard from `last_seen`. Rewrites only this disposable principal's row."""
    from sqlalchemy import text

    with db.begin() as conn:
        conn.execute(
            text("UPDATE time_entries SET start_time = :s, last_seen_at = :l WHERE id = :id AND end_time IS NULL"),
            {"s": start, "l": last_seen, "id": entry_id},
        )


def _seen(db, entry_id: int):
    from sqlalchemy import text

    with db.connect() as conn:
        return conn.execute(text("SELECT last_seen_at FROM time_entries WHERE id = :id"), {"id": entry_id}).scalar()


def _audit(db, entry_id: int):
    from sqlalchemy import text

    with db.connect() as conn:
        return conn.execute(
            text("SELECT reason, decided_by, ended_at, total_seconds, last_seen_at "
                 "FROM time_entry_reconciliations WHERE time_entry_id = :id"),
            {"id": entry_id},
        ).mappings().all()


def _friday():
    """Friday 17:23:21 IST, and the instants around it."""
    start = datetime(2026, 10, 9, 11, 53, 21, tzinfo=UTC)
    return start, start + timedelta(minutes=27)


def _silence_the_heartbeat(desktop):
    """Call this BEFORE the timer starts. The desktop's first beat goes out the moment
    the entry is bound, and one that landed after the test had moved the row in time
    would rewrite `last_seen_at` to now and undo the move -- a race that holding the
    beat only after the bind was observed could lose. These tests are about what
    happens when the desktop does not speak."""
    desktop.sync._heartbeat_unsupported_until = time.monotonic() + 3600


def _let_the_heartbeat_speak(desktop):
    desktop.sync._heartbeat_unsupported_until = 0.0
    desktop.sync.request_heartbeat()            # what a resume from sleep does


# ── 1. a running timer reports in ────────────────────────────────────────────

@pytest.mark.usefixtures("clean_slate")
def test_a_running_timer_is_alive_on_the_server_and_says_so_everywhere(qapp, desktop, api, db, principal):
    timer = desktop.timer
    timer.start_tracking(principal["project_id"], principal["task_id"], "E2E task")
    _pump(qapp, lambda: timer.entry_id is not None, 30, "the start to be bound")
    entry_id = timer.entry_id

    # The start promised liveness, so the clock began at creation...
    assert _seen(db, entry_id) is not None
    first_seen = _seen(db, entry_id)
    # ...and the desktop's first heartbeat (sent as soon as the entry was bound) moves it.
    _pump(qapp, lambda: _seen(db, entry_id) > first_seen, 20, "the first heartbeat")

    entry = api.get("/time-entries/active").json()["entry"]
    assert entry["liveness"] == "live"
    assert entry["last_seen_at"] is not None
    assert entry["is_running"] is True

    # The scheduled run has nothing to do for a timer that is reporting in.
    report = _job(str(api.base_url).rstrip("/"))
    assert report["candidates"] == 0 and report["dry_run"] is True
    timer.stop_tracking()
    _pump(qapp, lambda: not _running_rows(db, principal["user_id"]), 30, "the stop to land")


# ── 2. the weekend ───────────────────────────────────────────────────────────

@pytest.mark.usefixtures("clean_slate")
def test_friday_start_monday_morning_the_job_reports_then_closes_at_last_signal_plus_grace(
    qapp, desktop, api, db, principal, backend
):
    """Report first (nothing written), then apply: 42 minutes of Friday, not
    65 hours, in the row, the API and every report."""
    timer = desktop.timer
    _silence_the_heartbeat(desktop)
    timer.start_tracking(principal["project_id"], principal["task_id"], "E2E task")
    _pump(qapp, lambda: timer.entry_id is not None, 30, "the start to be bound")
    entry_id = timer.entry_id
    start, seen = _friday()
    _move_in_time(db, entry_id, start=start, last_seen=seen)

    # The figure every consumer shows is already the frozen one -- before anything is closed.
    shown = api.get("/time-entries/active").json()["entry"]
    assert shown["liveness"] == "stale"
    assert shown["elapsed_seconds"] == 42 * 60, shown
    friday_total = api.get(
        f"/api/v1/time-tracking/{principal['user_id']}", params={"start_date": "2026-10-09", "end_date": "2026-10-09"},
    ).json()["summary"]["total_seconds"]
    assert friday_total == 42 * 60, friday_total
    the_weekend = api.get(
        f"/api/v1/time-tracking/{principal['user_id']}", params={"start_date": "2026-10-10", "end_date": "2026-10-11"},
    ).json()["summary"]["total_seconds"]
    assert the_weekend == 0, "nothing is booked for Saturday and Sunday"

    report = _job(backend)                      # the flag is off: report only
    assert report["dry_run"] is True and report["candidates"] == 1 and report["closed"] == 0
    item = report["items"][0]
    assert item["time_entry_id"] == entry_id and item["reason"] == "no_signal"
    assert item["would_total_seconds"] == 42 * 60
    assert _row(db, entry_id)["end_time"] is None, "a report-only run wrote nothing"

    applied = _job(backend, "?dry_run=false")
    assert applied["closed"] == 1 and applied["dry_run"] is False
    row = _row(db, entry_id)
    assert row["end_time"] == seen + timedelta(minutes=15)
    assert row["total_seconds"] == 42 * 60 == int(row["derived"])
    assert row["status"] == "stopped" and row["start_time"] == start
    audit = _audit(db, entry_id)
    assert len(audit) == 1 and audit[0]["reason"] == "no_signal" and audit[0]["decided_by"] == "operator"
    assert api.get("/time-entries/active").json()["entry"] is None

    again = _job(backend, "?dry_run=false")     # idempotent
    assert again["closed"] == 0 and again["candidates"] == 0
    assert len(_audit(db, entry_id)) == 1


@pytest.mark.usefixtures("clean_slate")
def test_the_desktop_learns_its_entry_was_closed_and_says_why(qapp, desktop, api, db, principal, backend):
    """The laptop wakes on Monday with a local session still counting 65
    hours. Its first heartbeat finds the entry closed, ends the session at the
    backend's figure and tells the person."""
    timer = desktop.timer
    told, finalized = [], []
    timer.timer_closed_by_server.connect(told.append)
    timer.timer_finalized.connect(finalized.append)
    _silence_the_heartbeat(desktop)
    timer.start_tracking(principal["project_id"], principal["task_id"], "E2E task")
    _pump(qapp, lambda: timer.entry_id is not None, 30, "the start to be bound")
    entry_id = timer.entry_id
    start, seen = _friday()
    _move_in_time(db, entry_id, start=start, last_seen=seen)
    assert _job(backend, "?dry_run=false")["closed"] == 1

    _let_the_heartbeat_speak(desktop)
    _pump(qapp, lambda: bool(told), 30, "the heartbeat to find the closed entry")

    assert not timer.is_running(), "the local session ended"
    assert told[0]["reason"] == "no_signal"
    assert told[0]["total_seconds"] == 42 * 60
    assert finalized and finalized[0]["entry"]["id"] == entry_id
    assert desktop.cache.load_app_state("timer_state") is None
    assert not _running_rows(db, principal["user_id"])


@pytest.mark.usefixtures("clean_slate")
def test_a_new_start_replaces_the_silent_entry_instead_of_adopting_it(qapp, desktop, api, db, principal):
    timer = desktop.timer
    timer.start_tracking(principal["project_id"], principal["task_id"], "E2E task")
    _pump(qapp, lambda: timer.entry_id is not None, 30, "the start to be bound")
    old_id = timer.entry_id
    timer.stop_tracking()
    _pump(qapp, lambda: not _running_rows(db, principal["user_id"]), 30, "the first stop to land")

    # A Friday entry that nobody stopped, started by an API client (a second machine).
    now = datetime.now(UTC)
    friday = api.post("/time-entries/start", json={
        "project_id": principal["project_id"], "task_id": principal["task_id"],
        "started_at": now.isoformat(), "client_time": now.isoformat(),
        "client_op": "timer:e2e:friday:0001", "reports_liveness": True,
    })
    assert friday.status_code == 201, friday.text
    friday_id = friday.json()["id"]
    start, seen = _friday()
    _move_in_time(db, friday_id, start=start, last_seen=seen)

    timer.start_tracking(principal["project_id"], principal["task_id"], "Monday task")
    _pump(qapp, lambda: timer.entry_id not in (None, old_id, friday_id), 30, "the Monday start to bind")

    assert timer.is_running() and timer.entry_id != friday_id, "Friday's timer was not handed back"
    friday_row = _row(db, friday_id)
    assert friday_row["end_time"] == seen + timedelta(minutes=15)
    assert _audit(db, friday_id)[0]["reason"] == "replaced_by_start"
    assert len(_running_rows(db, principal["user_id"])) == 1, "exactly one running entry"
    timer.stop_tracking()
    _pump(qapp, lambda: not _running_rows(db, principal["user_id"]), 30, "the stop to land")


# ── 3. entries that never reported ───────────────────────────────────────────

@pytest.mark.usefixtures("clean_slate")
def test_an_entry_that_never_reported_is_reported_but_closed_only_when_named(api, db, principal, backend):
    now = datetime.now(UTC)
    started = api.post("/time-entries/start", json={
        "project_id": principal["project_id"], "task_id": principal["task_id"],
        "started_at": now.isoformat(), "client_time": now.isoformat(), "client_op": "timer:e2e:legacy:0001",
    })                                                                          # an older client: no liveness
    assert started.status_code == 201, started.text
    entry_id = started.json()["id"]
    assert started.json()["liveness"] == "unknown"
    start, _ = _friday()
    _move_in_time(db, entry_id, start=start, last_seen=None)

    scheduled = _job(backend, "?dry_run=false")
    assert scheduled["unreported_running"] >= 1 and scheduled["closed"] == 0
    assert _row(db, entry_id)["end_time"] is None, "silence from a client that never spoke proves nothing"

    refused = httpx.post(
        f"{backend}/internal/time-entries/reconcile?dry_run=false&include_unreported=true",
        headers={"X-Email-Dispatch-Token": JOB_TOKEN}, timeout=60,
    )
    assert refused.status_code == 400, "an operator closure must name its entries"

    report = _job(backend, f"?dry_run=true&include_unreported=true&entry_id={entry_id}")
    assert report["candidates"] == 1 and report["items"][0]["reason"] == "operator"
    assert _row(db, entry_id)["end_time"] is None

    done = _job(backend, f"?dry_run=false&include_unreported=true&entry_id={entry_id}")
    assert done["closed"] == 1
    assert _audit(db, entry_id)[0]["decided_by"] == "operator"
    assert _row(db, entry_id)["end_time"] == start + timedelta(minutes=15), "no upload names it: its start + grace"


# ── 4. a stop is honoured however old it is ──────────────────────────────────

@pytest.mark.usefixtures("clean_slate")
def test_a_stop_pressed_nine_days_ago_ends_the_entry_nine_days_ago(api, db, principal):
    """Delivered after eight days offline: recorded when it was pressed, not at
    arrival time (the seven-day ceiling was replacing it with 'now')."""
    now = datetime.now(UTC)
    started = api.post("/time-entries/start", json={
        "project_id": principal["project_id"], "task_id": principal["task_id"],
        "started_at": now.isoformat(), "client_time": now.isoformat(), "client_op": "timer:e2e:old:0001",
    })
    entry_id = started.json()["id"]
    start = now - timedelta(days=10)
    _move_in_time(db, entry_id, start=start, last_seen=None)
    pressed = now - timedelta(days=9)

    stopped = api.post(f"/time-entries/{entry_id}/stop", json={
        "stopped_at": pressed.isoformat(), "client_time": now.isoformat(),
    })
    assert stopped.status_code == 200, stopped.text
    row = _row(db, entry_id)
    assert abs((row["end_time"] - pressed).total_seconds()) < 2, row
    assert row["total_seconds"] == int(row["derived"]) == pytest.approx(86400, abs=2)

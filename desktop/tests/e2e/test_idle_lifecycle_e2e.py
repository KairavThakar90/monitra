"""
Idle time, end to end: the detector, the backend's verdict, and every display.

Nothing here is mocked except one reading. A real backend process serves the
real API against the development database; the real desktop runtime runs its
real services -- `IdleService` detects on its own thread, reports over HTTP,
and applies the answer; the timer, the durable queue and the sync consumer
are the ones the application ships. Every assertion is made against the
desktop's own displayed figure, the API's rows, and the aggregates the web
client renders.

The one simulated input is *inactivity*: `ActivityService.idle_seconds()` is
replaced with a value, because waiting out a real five-minute threshold four
times is not something a check can do. The threshold comparison, the report,
the popup state machine, the resolution, the deduction and every figure that
follows are genuine.

What is proven, for the four answers the idle alert offers:

    1. No, discard  + Resume  -> the desktop's running clock drops by the idle
                                 time at once, and the web's figure for the
                                 running entry equals it
    2. No, discard  + Stop    -> the figure banked on the desktop, the stored
                                 entry and every web surface are all net of it
    3. Yes, keep    + Stop    -> same as 2 (stopping always discards)
    4. Yes, keep    + Resume  -> nothing is deducted anywhere; the clock is
                                 unchanged

plus: a reassignment drops the clock by the moved seconds, and a deduction
survives a restart of the desktop.

Same opt-in and fixtures as `test_timing_lifecycle_e2e.py`:

    MONITRA_E2E=1 python -m pytest tests/e2e/test_idle_lifecycle_e2e.py -q -s
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest

from tests.e2e.test_timing_lifecycle_e2e import (  # noqa: F401  (fixtures)
    _dashboard_seconds, _ist_today, _pump, _row, _running_rows, _time_tracking_seconds,
    api, backend, clean_slate, db, desktop, principal, pytestmark,
)
from tests.e2e.test_timing_surfaces_e2e import _day_bounds

UTC = timezone.utc

#: The principal's threshold is the default five minutes and the backend
#: refuses a shorter idle period. The entry is started with an *age* -- the
#: mechanism a replayed offline start uses -- so a six-minute idle stretch
#: fits inside it, and the desktop adopts it exactly as it adopts a running
#: entry after a restart.
ENTRY_AGE_SECONDS = 12 * 60
IDLE_SECONDS = 6 * 60
#: Two clocks read a second apart, plus a second of rounding on each side.
TOLERANCE_SECONDS = 3


# ── helpers ──────────────────────────────────────────────────────────────────

@pytest.fixture
def idle_reading(desktop, monkeypatch):
    """The one simulated input: how long the user has been inactive."""
    fake = {"idle": 0.0}
    monkeypatch.setattr(desktop.activity, "idle_seconds", lambda: fake["idle"])
    return fake


def _day_entries(api, principal, day: str) -> list:
    start, end = _day_bounds(day)
    return api.get("/time-entries", params={
        "user_id": principal["user_id"], "limit": 1000, "start_date": start, "end_date": end,
    }).json()


def _finished_net(api, principal, day: str) -> int:
    return sum(e["net_seconds"] for e in _day_entries(api, principal, day) if e["end_time"] is not None)


def _start_aged(qapp, desktop, api, principal, key: str) -> int:
    """A running entry that began ENTRY_AGE_SECONDS ago, tracked by the desktop."""
    now = datetime.now(UTC)
    response = api.post("/time-entries/start", json={
        "project_id": principal["project_id"], "task_id": principal["task_id"],
        "client_op": f"timer:e2e:idle:{key}:{int(time.time() * 1000)}",
        "started_at": (now - timedelta(seconds=ENTRY_AGE_SECONDS)).isoformat(),
        "client_time": now.isoformat(),
    })
    assert response.status_code == 201, response.text
    entry = response.json()
    desktop.timer.adopt_remote_session(entry)
    timer = desktop.timer
    _pump(qapp, lambda: timer.is_running() and timer.entry_id == entry["id"], 10, "adoption")
    assert abs(timer.measured_seconds() - ENTRY_AGE_SECONDS) <= TOLERANCE_SECONDS
    assert timer.adjustment_seconds() == 0
    return entry["id"]


def _go_idle(qapp, desktop, idle_reading) -> dict:
    """Report the user idle and let the detector's own thread open the period."""
    idle = desktop.idle
    # Monitoring began before the inactivity did -- the detector refuses to
    # claim idle time from before the window it is watching.
    idle._monitoring_since = time.monotonic() - (IDLE_SECONDS + 60)
    idle_reading["idle"] = float(IDLE_SECONDS)
    idle.wake()
    _pump(qapp, lambda: idle.pending_period() is not None, 30, "the idle period to open")
    idle_reading["idle"] = 0.0
    period = idle.pending_period()
    assert period["status"] == "pending"
    return period


def _settle(qapp, seconds: float) -> None:
    """Run the event loop for a fixed time (a display tick is one second)."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.02)


def _web(api, principal, day: str) -> dict:
    return {
        "dashboard": _dashboard_seconds(api, principal, day),
        "time_tracking": _time_tracking_seconds(api, principal, day),
    }


# ── 1-4. The four answers, at every layer ────────────────────────────────────

@pytest.mark.usefixtures("clean_slate")
@pytest.mark.parametrize(
    "keep, action",
    [(False, "resume"), (False, "stop"), (True, "stop"), (True, "resume")],
    ids=["discard+resume", "discard+stop", "keep+stop", "keep+resume"],
)
def test_the_popup_answer_reaches_every_display(
    qapp, desktop, idle_reading, api, db, principal, keep, action
):
    timer, idle = desktop.timer, desktop.idle
    ticks, stopped, finalized, resolved = [], [], [], []
    timer.timer_tick.connect(ticks.append)
    timer.timer_stopped.connect(stopped.append)
    timer.timer_finalized.connect(finalized.append)
    idle.resolve_succeeded.connect(resolved.append)

    day = _ist_today()
    finished_before = _finished_net(api, principal, day)
    entry_id = _start_aged(qapp, desktop, api, principal, f"{keep}-{action}")
    period = _go_idle(qapp, desktop, idle_reading)
    measured_before = timer.measured_seconds()
    assert timer.elapsed_seconds() == measured_before, "nothing may be deducted before an answer"

    # ── the answer ──
    idle.resolve(keep, action)
    _pump(qapp, lambda: bool(resolved), 30, "the answer to be accepted")
    _pump(qapp, lambda: idle.pending_period() is None, 5, "the popup state to clear")

    counted = keep and action == "resume"
    period_row = api.get(f"/idle-periods/{period['id']}").json()
    assert period_row["status"] == "resolved"
    assert period_row["counted"] is counted
    assert (period_row["keep_idle_time"], period_row["action"]) == (keep, action)
    idle_seconds = period_row["idle_duration_seconds"]
    assert IDLE_SECONDS - 5 <= idle_seconds <= IDLE_SECONDS + 20, idle_seconds
    deduction = 0 if counted else idle_seconds

    # The verdict the desktop received is the entry's net adjustment.
    assert resolved[0]["time_entry_adjustment_seconds"] == -deduction
    read = api.get(f"/time-entries/{entry_id}").json()
    assert read["adjustment_seconds"] == -deduction

    if action == "resume":
        # ── the running clock, on the desktop ──
        assert timer.is_running()
        assert timer.adjustment_seconds() == -deduction
        expected = timer.measured_seconds() - deduction
        assert abs(timer.elapsed_seconds() - expected) <= 1, (timer.elapsed_seconds(), expected)
        _settle(qapp, 1.2)  # let one display tick fire
        assert ticks and abs(ticks[-1] - timer.elapsed_seconds()) <= 2, "the tick shows another figure"

        # ── the running entry, on the web ──
        assert abs(read["net_seconds"] - timer.elapsed_seconds()) <= TOLERANCE_SECONDS
        web = _web(api, principal, day)
        live = finished_before + timer.elapsed_seconds()
        assert abs(web["time_tracking"] - live) <= TOLERANCE_SECONDS, (web, live)
        assert abs(web["dashboard"] - live) <= TOLERANCE_SECONDS, (web, live)

        # ── then the user stops from the task row ──
        display_at_stop = timer.elapsed_seconds()
        timer.stop_tracking()
        _pump(qapp, lambda: bool(finalized), 30, "the stop to be finalized")
        entry = finalized[0]["entry"]
        assert entry["id"] == entry_id
        assert entry["adjustment_seconds"] == -deduction
        assert entry["net_seconds"] == max(0, entry["total_seconds"] - deduction)
        assert abs(entry["net_seconds"] - display_at_stop) <= TOLERANCE_SECONDS
        assert abs(stopped[0]["elapsed_seconds"] - entry["net_seconds"]) <= TOLERANCE_SECONDS
    else:
        # ── stopped from the popup: the desktop banks the netted figure ──
        assert not timer.is_running()
        assert stopped, "timer_stopped never fired"
        banked = stopped[0]["elapsed_seconds"]
        assert abs(banked - (measured_before - deduction)) <= TOLERANCE_SECONDS, (banked, measured_before, deduction)
        assert finalized and finalized[0]["entry"] is None, "the idle path stops through the backend"
        assert read["end_time"] is not None
        assert read["net_seconds"] == max(0, read["total_seconds"] - deduction)
        assert abs(read["net_seconds"] - banked) <= TOLERANCE_SECONDS
        assert len(_running_rows(db, principal["user_id"])) == 0

    # ── finished: the stored entry, the desktop and every web surface agree ──
    final = api.get(f"/time-entries/{entry_id}").json()
    assert final["end_time"] is not None
    assert final["total_seconds"] - final["net_seconds"] == deduction, final
    row = _row(db, entry_id)
    assert row["total_seconds"] == int(row["derived"]), "total_seconds is never edited"
    finished = _finished_net(api, principal, day)
    web = _web(api, principal, day)
    assert web["dashboard"] == web["time_tracking"] == finished, (web, finished)
    assert finished - finished_before == final["net_seconds"]
    print(
        f"\n[idle e2e keep={keep} action={action}] idle={idle_seconds}s deduction={deduction}s "
        f"measured={final['total_seconds']}s net={final['net_seconds']}s "
        f"dashboard={web['dashboard']} time_tracking={web['time_tracking']}"
    )


# ── Reassignment moves seconds off the clock too ─────────────────────────────

@pytest.mark.usefixtures("clean_slate")
def test_reassigned_idle_time_leaves_the_running_clock(qapp, desktop, idle_reading, api, db, principal):
    timer, idle = desktop.timer, desktop.idle
    moved_events, resolved, finalized = [], [], []
    idle.reassign_succeeded.connect(moved_events.append)
    idle.resolve_succeeded.connect(resolved.append)
    timer.timer_finalized.connect(finalized.append)

    day = _ist_today()
    finished_before = _finished_net(api, principal, day)
    entry_id = _start_aged(qapp, desktop, api, principal, "reassign")
    period = _go_idle(qapp, desktop, idle_reading)

    idle.reassign(principal["project_id"], principal["task_id"])
    _pump(qapp, lambda: bool(moved_events), 30, "the reassignment")
    moved = moved_events[0]["reassigned_seconds"]
    assert moved >= IDLE_SECONDS - 5
    # Still pending -- the user must answer the popup -- and the clock has
    # already dropped by the seconds that now belong to the destination.
    assert idle.pending_period() is not None and idle.pending_period()["reassigned"] is True
    assert timer.is_running()
    assert timer.adjustment_seconds() == -moved
    assert abs(timer.elapsed_seconds() - (timer.measured_seconds() - moved)) <= 1

    # Discard the residual and resume: the deduction becomes the whole idle
    # period, never the moved seconds twice.
    idle.resolve(False, "resume")
    _pump(qapp, lambda: bool(resolved), 30, "the answer")
    period_row = api.get(f"/idle-periods/{period['id']}").json()
    idle_seconds = period_row["idle_duration_seconds"]
    assert period_row["reassigned_seconds"] == moved
    assert resolved[0]["time_entry_adjustment_seconds"] == -idle_seconds
    assert timer.adjustment_seconds() == -idle_seconds
    read = api.get(f"/time-entries/{entry_id}").json()
    assert read["adjustment_seconds"] == -idle_seconds
    assert abs(read["net_seconds"] - timer.elapsed_seconds()) <= TOLERANCE_SECONDS

    timer.stop_tracking()
    _pump(qapp, lambda: bool(finalized), 30, "the stop")
    entry = finalized[0]["entry"]
    assert entry["net_seconds"] == max(0, entry["total_seconds"] - idle_seconds)
    destination = api.get(f"/time-entries/{period_row['reassigned_time_entry_id']}").json()
    assert destination["total_seconds"] == moved and destination["net_seconds"] == moved
    finished = _finished_net(api, principal, day)
    web = _web(api, principal, day)
    assert web["dashboard"] == web["time_tracking"] == finished, (web, finished)
    # The seconds are counted exactly once across the two entries.
    assert finished - finished_before == entry["net_seconds"] + moved


# ── A deduction survives a restart ───────────────────────────────────────────

@pytest.mark.usefixtures("clean_slate")
def test_a_discarded_idle_period_survives_a_restart(
    qapp, tmp_path, backend, principal, api, db, monkeypatch
):
    """Discard + Resume, then Monitra restarts (an update restart: the
    session record is kept). The recovered clock must still be net of the
    idle time, and reconciling with the backend must not undo it."""
    monkeypatch.setenv("SMS_API_BASE_URL", backend)
    from core.runtime import ApplicationRuntime
    from storage.manager import StorageManager

    manager = StorageManager(str(tmp_path / "idle-restart-cache.db"))
    first = ApplicationRuntime(storage=manager)
    first.api_client.base_url = backend
    first.api_client.access_token = principal["token"]
    fake = {"idle": 0.0}
    monkeypatch.setattr(first.activity, "idle_seconds", lambda: fake["idle"])
    first.start_services()
    try:
        resolved = []
        first.idle.resolve_succeeded.connect(resolved.append)
        entry_id = _start_aged(qapp, first, api, principal, "restart")
        _go_idle(qapp, first, fake)
        first.idle.resolve(False, "resume")
        _pump(qapp, lambda: bool(resolved), 30, "the answer")
        deduction = -resolved[0]["time_entry_adjustment_seconds"]
        assert deduction >= IDLE_SECONDS - 5
        assert first.timer.adjustment_seconds() == -deduction
        anchor = first.timer.active_session()["started_at_utc"]
    finally:
        # Not a quit: the timer is left running and its record kept, exactly
        # as an update restart or an OS shutdown leaves it.
        first.shutdown(timeout_ms=3000)

    manager = StorageManager(str(tmp_path / "idle-restart-cache.db"))
    second = ApplicationRuntime(storage=manager)
    second.api_client.base_url = backend
    second.api_client.access_token = principal["token"]
    try:
        recovered = second.timer.recover()
        assert recovered is not None and recovered["entry_id"] == entry_id
        assert recovered["started_at_utc"] == anchor
        assert second.timer.adjustment_seconds() == -deduction
        assert second.timer.elapsed_seconds() == max(0, second.timer.measured_seconds() - deduction)

        active = api.get("/time-entries/active").json()
        assert active["entry"]["id"] == entry_id
        assert active["entry"]["adjustment_seconds"] == -deduction
        second.timer.adopt_remote_session(active["entry"], server_time=active["server_time"])
        assert second.timer.adjustment_seconds() == -deduction

        second.start_services()
        finalized = []
        second.timer.timer_finalized.connect(finalized.append)
        display_at_stop = second.timer.elapsed_seconds()
        second.timer.stop_tracking()
        _pump(qapp, lambda: bool(finalized), 30, "the stop")
        entry = finalized[0]["entry"]
        assert entry["adjustment_seconds"] == -deduction
        assert entry["net_seconds"] == max(0, entry["total_seconds"] - deduction)
        assert abs(entry["net_seconds"] - display_at_stop) <= TOLERANCE_SECONDS
    finally:
        second.shutdown(timeout_ms=3000)

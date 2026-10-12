"""
A laptop closed on Friday and opened on Monday, end to end: desktop -> HTTP -> database -> reports.

Nothing is mocked. A real backend process serves the real API against a real PostgreSQL; the real
desktop runtime (TimerService, the durable queue, the sync consumer, the API client) runs a
timer against it; and every assertion is made against the rows in Postgres, the API's own
answer and the desktop's own state. Time is moved the way the other E2E modules move it: the
desktop's clock is a settable one (`timer_service._utc_now`), and the principal's own rows are
rewritten to where they would be after a weekend -- a weekend cannot be waited out.

What it pins is the rule in docs/TIMING_MODEL.md section 9.6: a machine that sleeps through
midnight does not book the night or the weekend, however the server had treated the entry, with the
backend unreachable, with a person acting at once, with the queue held for another account, and with
the wake and reconnect events repeated.

Opt-in (needs a database and a free port):

    MONITRA_E2E=1 python -m pytest tests/e2e/test_weekend_wake_e2e.py -q -s
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone

import pytest

from background_services.timer import timer_service as timer_module
from background_services.timer.timer_service import RECOVERY_CAP_SECONDS
from core.time_format import IST, ist_day_bounds_utc, to_ist
from tests.e2e.test_timer_abandonment_e2e import (  # noqa: F401  (fixtures and helpers)
    _job, _move_in_time, _silence_the_heartbeat, backend,
)
from tests.e2e.test_timing_lifecycle_e2e import (  # noqa: F401  (fixtures and helpers)
    _pump, _row, _running_rows, api, clean_slate, db, desktop, principal,
)
from tests.test_timer_lifecycle_reliability import FakeClock

pytestmark = pytest.mark.skipif(
    os.environ.get("MONITRA_E2E") != "1",
    reason="real-backend E2E; set MONITRA_E2E=1 to run",
)

UTC = timezone.utc
CAP = timedelta(seconds=RECOVERY_CAP_SECONDS)


class Weekend:
    """One Friday-to-Monday story. 'Monday' is the real now, so the server's own clock agrees."""

    def __init__(self, qapp, desktop, db, principal, monkeypatch, *, start_hours_before_wake=73):
        from sqlalchemy import text

        self.qapp, self.desktop, self.db, self.principal = qapp, desktop, db, principal
        self.monday = datetime.now(UTC)
        self.friday_start = (self.monday - timedelta(hours=start_hours_before_wake)).replace(microsecond=0)
        self.friday_quiet = self.friday_start + timedelta(minutes=37)          # the lid closes
        self.clock = FakeClock(self.friday_start)
        monkeypatch.setattr(timer_module, "_utc_now", self.clock)
        # Each story starts from an empty history for its (disposable) principal: the guard that
        # keeps a start out of time the user already has would otherwise move this story's
        # backdated starts to the end of the previous story's entries.
        with db.begin() as conn:
            conn.execute(text("DELETE FROM time_entries WHERE user_id = :u"), {"u": principal["user_id"]})
        with db.connect() as conn:
            self.baseline = conn.execute(text("SELECT coalesce(max(id), 0) FROM time_entries")).scalar()
        # The queued stop names its account, as it does in the real application.
        desktop.session_manager._user_info = {"id": principal["user_id"], "name": "e2e"}
        self.timer = desktop.timer
        self.entry_id = None

    # ── the story ────────────────────────────────────────────────────────────

    def start_friday(self, *, silence_heartbeat=True):
        if silence_heartbeat:
            _silence_the_heartbeat(self.desktop)
        self.timer.start_tracking(self.principal["project_id"], self.principal["task_id"], "E2E task")
        _pump(self.qapp, lambda: self.timer.entry_id is not None, 30, "the start to be bound")
        self.entry_id = self.timer.entry_id
        # The server's row where the desktop's anchor is, and last heard from when the lid closed.
        _move_in_time(self.db, self.entry_id, start=self.friday_start, last_seen=self.friday_quiet)
        self.clock.now = self.friday_quiet
        self.timer._emit_tick()                                                  # awake until now
        return self.entry_id

    def server_closes_it(self):
        report = _job(self.backend_url, "?dry_run=false")
        assert report["closed"] >= 1, report

    def wake(self):
        self.clock.now = datetime.now(UTC)

    # ── what the database holds ──────────────────────────────────────────────

    def entries(self):
        from sqlalchemy import text

        with self.db.connect() as conn:
            return [dict(r) for r in conn.execute(text(
                "SELECT id, start_time, end_time, total_seconds, status, client_op FROM time_entries "
                "WHERE user_id = :u AND id > :b ORDER BY start_time, id"
            ), {"u": self.principal["user_id"], "b": self.baseline}).mappings().all()]

    def audit_rows(self):
        from sqlalchemy import text

        with self.db.connect() as conn:
            return conn.execute(text(
                "SELECT reason, decided_by FROM time_entry_reconciliations r JOIN time_entries t "
                "ON t.id = r.time_entry_id WHERE t.user_id = :u AND t.id > :b"
            ), {"u": self.principal["user_id"], "b": self.baseline}).fetchall()

    def queue_drained(self):
        return self.desktop.cache.get_pending_count() == 0


@pytest.fixture
def weekend(qapp, desktop, db, principal, backend, monkeypatch, clean_slate):
    story = Weekend(qapp, desktop, db, principal, monkeypatch)
    story.backend_url = backend
    return story


def _close_to(actual, expected, seconds=3):
    return abs((actual - expected).total_seconds()) <= seconds


# ── 1. the server closed the original while the desktop slept ─────────────────

def test_the_server_closed_the_original_while_the_desktop_slept(weekend, api, principal):
    entry_id = weekend.start_friday()
    weekend.server_closes_it()
    closed = _row(weekend.db, entry_id)
    assert closed["end_time"] == weekend.friday_quiet + CAP

    weekend.wake()
    _pump(weekend.qapp, lambda: not weekend.timer.is_running(), 20, "the desktop to end its session")
    _pump(weekend.qapp, weekend.queue_drained, 30, "the queue to drain")

    entries = weekend.entries()
    assert len(entries) == 1, f"nothing was booked for the days in between: {entries}"
    assert entries[0]["end_time"] == weekend.friday_quiet + CAP, "the server's figure stands: the replay changed nothing"
    assert entries[0]["total_seconds"] == int((weekend.friday_quiet + CAP - weekend.friday_start).total_seconds())
    assert len(weekend.audit_rows()) == 1, "one closure, recorded once"
    assert not _running_rows(weekend.db, principal["user_id"])
    assert api.get("/time-entries/active").json()["entry"] is None
    assert weekend.desktop.cache.load_app_state("timer_state") is None


# ── 2. the server did not close it (the job is off) ───────────────────────────

def test_the_server_did_not_close_it_so_the_desktop_ends_it_at_the_last_awake_instant_plus_the_cap(weekend, principal):
    entry_id = weekend.start_friday()
    assert _row(weekend.db, entry_id)["end_time"] is None, "the job has not run"

    weekend.wake()
    _pump(weekend.qapp, lambda: _row(weekend.db, entry_id)["end_time"] is not None, 30, "the stop to land")

    entries = weekend.entries()
    assert len(entries) == 1, f"no entry for any slept-through day: {entries}"
    assert _close_to(entries[0]["end_time"], weekend.friday_quiet + CAP), entries[0]
    assert entries[0]["total_seconds"] == pytest.approx(52 * 60, abs=3)
    assert not _running_rows(weekend.db, principal["user_id"])
    assert weekend.audit_rows() == [], "closed by the desktop's own stop, not by the reconciler"


# ── 3. the desktop was offline for the whole weekend ──────────────────────────

def test_offline_the_whole_weekend_the_stop_waits_durably_and_lands_with_the_right_instant(weekend, principal):
    entry_id = weekend.start_friday()
    weekend.desktop.sync._awaiting_auth = True                 # no route to the backend

    weekend.wake()
    _pump(weekend.qapp, lambda: not weekend.timer.is_running(), 20, "the session to end")

    assert weekend.desktop.cache.has_pending_stop_for_entry(entry_id), "the stop is in the durable queue"
    assert _row(weekend.db, entry_id)["end_time"] is None, "and the backend has heard nothing"
    assert len(weekend.entries()) == 1

    weekend.desktop.sync._awaiting_auth = False                # connectivity returns
    weekend.desktop.sync.resume_after_auth()
    _pump(weekend.qapp, lambda: _row(weekend.db, entry_id)["end_time"] is not None, 30, "the stop to land")
    assert _close_to(_row(weekend.db, entry_id)["end_time"], weekend.friday_quiet + CAP)
    assert len(weekend.entries()) == 1


# ── 4. the desktop wakes with several pending midnight rollover events ────────

def test_pending_rollover_events_from_work_through_several_midnights_replay_in_order(weekend, principal):
    """Genuine work, offline: the machine stayed awake across three midnights (a tick every ten
    minutes), so each midnight queued a stop and a start. Reconnected, they replay against the
    real backend into contiguous entries that begin and end exactly on the IST midnights."""
    entry_id = weekend.start_friday(silence_heartbeat=False)
    weekend.desktop.sync._awaiting_auth = True

    moment, wake = weekend.friday_quiet, weekend.monday
    while moment < wake:
        moment += timedelta(minutes=10)
        weekend.clock.now = moment
        weekend.timer._emit_tick()
    weekend.clock.now = wake
    weekend.timer._emit_tick()

    midnights = []
    cursor = ist_day_bounds_utc(to_ist(weekend.friday_start).date())[1]
    while cursor <= wake:
        midnights.append(cursor)
        cursor += timedelta(days=1)
    assert len(midnights) >= 2, "the story crosses several midnights"
    assert weekend.timer.is_running(), "an awake machine keeps its session"
    assert weekend.desktop.cache.get_pending_count() >= 2 * len(midnights)

    weekend.desktop.sync._awaiting_auth = False
    weekend.desktop.sync.resume_after_auth()
    _pump(weekend.qapp, weekend.queue_drained, 90, "the replay to finish")
    _pump(weekend.qapp, lambda: weekend.timer.entry_id is not None, 30, "the last start to be bound")

    entries = weekend.entries()
    assert len(entries) == len(midnights) + 1, entries
    starts = [e["start_time"] for e in entries]
    assert _close_to(starts[0], weekend.friday_start)
    for expected, entry in zip(midnights, entries[1:]):
        assert _close_to(entry["start_time"], expected, 2), (expected, entry)
    for earlier, later in zip(entries, entries[1:]):
        assert earlier["end_time"] is not None
        assert abs((later["start_time"] - earlier["end_time"]).total_seconds()) <= 2, "contiguous, no gap or overlap"
        assert later["start_time"] >= earlier["end_time"], "and never an overlap"
    assert [e["end_time"] is None for e in entries] == [False] * len(midnights) + [True], "one running, the last"
    assert len(_running_rows(weekend.db, principal["user_id"])) == 1
    weekend.timer.stop_tracking()
    _pump(weekend.qapp, lambda: not _running_rows(weekend.db, principal["user_id"]), 30, "the final stop to land")


# ── 5. the user switches projects right after waking ──────────────────────────

def test_switching_project_right_after_waking_books_a_quarter_hour_and_starts_the_new_task_now(weekend, principal):
    entry_id = weekend.start_friday()
    weekend.wake()
    woke = weekend.clock.now
    weekend.timer.switch_tracking(principal["project_id"], principal["task_id"], "Another task")
    _pump(weekend.qapp, lambda: _row(weekend.db, entry_id)["end_time"] is not None, 30, "the old entry to close")
    _pump(weekend.qapp, lambda: weekend.timer.entry_id not in (None, entry_id), 30, "the new start to be bound")

    entries = weekend.entries()
    assert len(entries) == 2, entries
    old, new = entries
    assert _close_to(old["end_time"], weekend.friday_quiet + CAP)
    assert old["total_seconds"] == pytest.approx(52 * 60, abs=3)
    assert _close_to(new["start_time"], woke, 5), "the new task begins when the person did"
    assert new["end_time"] is None and len(_running_rows(weekend.db, principal["user_id"])) == 1
    weekend.timer.stop_tracking()
    _pump(weekend.qapp, lambda: not _running_rows(weekend.db, principal["user_id"]), 30, "the stop to land")


# ── 6. the user logs out before the queue replays ─────────────────────────────

def test_a_logout_before_the_replay_holds_the_stop_for_its_owner(weekend, principal):
    entry_id = weekend.start_friday()
    weekend.wake()
    _pump(weekend.qapp, lambda: not weekend.timer.is_running(), 20, "the session to end")
    weekend.desktop.sync._awaiting_auth = False

    # Signed out, then somebody else signed in: the stop is neither sent nor lost.
    for who in (None, {"id": principal["user_id"] + 100000, "name": "someone else"}):
        weekend.desktop.session_manager._user_info = who
        weekend.desktop.sync.wake()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            weekend.qapp.processEvents()
            time.sleep(0.05)
        assert _row(weekend.db, entry_id)["end_time"] is None, f"went out while {who} was signed in"
        assert weekend.desktop.cache.has_pending_stop_for_entry(entry_id)

    weekend.desktop.session_manager._user_info = {"id": principal["user_id"], "name": "e2e"}
    weekend.desktop.sync.resume_after_auth()
    _pump(weekend.qapp, lambda: _row(weekend.db, entry_id)["end_time"] is not None, 30, "the owner's stop to land")
    assert _close_to(_row(weekend.db, entry_id)["end_time"], weekend.friday_quiet + CAP)


# ── 7. duplicate wake and reconnect events ────────────────────────────────────

def test_repeated_wake_and_reconnect_events_change_nothing(weekend, principal):
    entry_id = weekend.start_friday()
    weekend.wake()
    for _ in range(3):
        weekend.desktop._on_system_resumed(250000.0)
        weekend.desktop.sync.request_heartbeat()
        weekend.timer._emit_tick()
        weekend.qapp.processEvents()
    _pump(weekend.qapp, lambda: _row(weekend.db, entry_id)["end_time"] is not None, 30, "the stop to land")
    _pump(weekend.qapp, weekend.queue_drained, 30, "the queue to drain")
    for _ in range(3):
        weekend.desktop._on_system_resumed(250000.0)
        weekend.desktop.sync.request_heartbeat()
        weekend.timer._emit_tick()
        weekend.qapp.processEvents()
    time.sleep(0.5)
    weekend.qapp.processEvents()

    entries = weekend.entries()
    assert len(entries) == 1 and not _running_rows(weekend.db, principal["user_id"]), entries
    assert _close_to(entries[0]["end_time"], weekend.friday_quiet + CAP)
    assert weekend.queue_drained()


# ── 8. an older queued operation arrives after the server stopped the original ─

def test_the_backend_never_reopens_and_never_double_books_for_an_obsolete_queued_operation(weekend, api, principal):
    """What an OLD desktop (1.0.0 to 1.3.2) sends when it wakes: the replay of a stop, the start
    of the next 'rolled-over' entry, and later the replay of that start. The server had already
    closed the original."""
    entry_id = weekend.start_friday()
    weekend.server_closes_it()
    closed_at = _row(weekend.db, entry_id)["end_time"]
    saturday = ist_day_bounds_utc(to_ist(weekend.friday_start).date())[1]
    now = datetime.now(UTC).isoformat()

    # (a) the obsolete stop, honoured however old, cannot lengthen or alter the closed entry
    stop = api.post(f"/time-entries/{entry_id}/stop", json={"stopped_at": saturday.isoformat(), "client_time": now})
    assert stop.status_code == 200, stop.text
    assert _row(weekend.db, entry_id)["end_time"] == closed_at, "the first stop's instant stands"

    # (b) the 'rolled-over' start of an OLD desktop is a fresh session and is accepted, backdated:
    #     the limitation -- the backend cannot tell a slept-through day from a worked one.
    start = api.post("/time-entries/start", json={
        "project_id": principal["project_id"], "task_id": principal["task_id"],
        "started_at": saturday.isoformat(), "client_time": now, "client_op": "timer:old:saturday",
    })
    assert start.status_code in (200, 201), start.text
    second = start.json()["id"]
    assert _close_to(_row(weekend.db, second)["start_time"], saturday, 3)
    api.post(f"/time-entries/{second}/stop", json={"stopped_at": (saturday + timedelta(hours=24)).isoformat(),
                                                    "client_time": now})

    # (c) the same start replayed after the entry was stopped is answered with that entry, unchanged
    row = _row(weekend.db, second)
    replay = api.post("/time-entries/start", json={
        "project_id": principal["project_id"], "task_id": principal["task_id"],
        "started_at": saturday.isoformat(), "client_time": now, "client_op": "timer:old:saturday",
    })
    assert replay.status_code in (200, 201) and replay.json()["id"] == second
    assert _row(weekend.db, second)["end_time"] == row["end_time"], "never reopened"
    assert not _running_rows(weekend.db, principal["user_id"])

    # (d) a start backdated INTO time the user already has is moved to the end of it
    inside = api.post("/time-entries/start", json={
        "project_id": principal["project_id"], "task_id": principal["task_id"],
        "started_at": (weekend.friday_start + timedelta(minutes=20)).isoformat(), "client_time": now,
        "client_op": "timer:old:inside",
    })
    assert inside.status_code in (200, 201), inside.text
    assert _row(weekend.db, inside.json()["id"])["start_time"] >= closed_at, "no half hour booked twice"
    api.post(f"/time-entries/{inside.json()['id']}/stop", json={})

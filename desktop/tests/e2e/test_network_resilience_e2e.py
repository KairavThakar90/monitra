"""
Network faults against the real backend: reads recover, writes never duplicate.

A fault-injecting proxy (the one `test_idle_resilience_e2e.py` uses) sits between
the real desktop runtime and the real backend, and the database is read back
after every fault.

    GET projects, connection reset once          -> loaded, after one transparent retry
    GET projects, 503 twice                      -> loaded; 503 three times -> a classified failure
    healthy GET                                  -> exactly one request, no added delay
    timer START, reply lost after the server applied it -> ONE entry, bound to the timer
    timer STOP,  reply lost after the server applied it -> stopped ONCE, exact duration
    timer START while the backend refuses everything     -> queued; ONE entry once it returns

    MONITRA_E2E=1 python -m pytest tests/e2e/test_network_resilience_e2e.py -q -s
"""
from __future__ import annotations

import time

import pytest

from app.api.exceptions import ApiError, FailureCode, describe_failure
from tests.e2e.test_idle_resilience_e2e import (  # noqa: F401  (fixtures)
    desktop, proxy, _stop_quietly,
)
from tests.e2e.test_timing_lifecycle_e2e import (  # noqa: F401
    _pump, _row, _running_rows, api, backend, clean_slate, db, principal, pytestmark,
)


def _entries_for(db, user_id: int, client_op: str) -> list:
    from sqlalchemy import text

    with db.connect() as conn:
        return conn.execute(
            text("SELECT id, end_time, total_seconds FROM time_entries WHERE user_id = :u AND client_op = :op"),
            {"u": user_id, "op": client_op},
        ).fetchall()


# ── reads ────────────────────────────────────────────────────────────────────

def test_a_healthy_read_is_one_request_with_no_added_delay(desktop, proxy, principal):
    proxy.clear()
    started = time.monotonic()
    projects = desktop.project_service.get_projects()
    elapsed = time.monotonic() - started
    assert isinstance(projects, list)
    assert proxy.count("GET", "/api/v1/projects") == 1, "a healthy load must cost exactly one request"
    assert elapsed < 5


def test_a_reset_connection_on_a_read_is_retried_transparently(desktop, proxy, principal):
    proxy.clear()
    proxy.fault("/api/v1/projects", ("drop_after",), times=1, method="GET")
    projects = desktop.project_service.get_projects()
    assert isinstance(projects, list)
    assert proxy.count("GET", "/api/v1/projects") == 2


def test_two_gateway_errors_are_ridden_out(desktop, proxy, principal):
    proxy.clear()
    proxy.fault("/api/v1/projects", ("status", 503), times=2, method="GET")
    assert isinstance(desktop.project_service.get_projects(), list)
    assert proxy.count("GET", "/api/v1/projects") == 3


def test_a_persistent_outage_fails_after_three_attempts_with_a_diagnosis(desktop, proxy, principal):
    proxy.clear()
    proxy.fault("/api/v1/projects", ("status", 503), times=None, method="GET")
    with pytest.raises(ApiError) as caught:
        desktop.project_service.get_projects()
    assert proxy.count("GET", "/api/v1/projects") == 3, "bounded: never a retry storm"
    info = describe_failure(caught.value)
    assert info["code"] == "http_503" and info["attempts"] == 3 and info["request_id"]
    proxy.clear()
    assert isinstance(desktop.project_service.get_projects(), list), "and it recovers when the backend does"


def test_a_session_the_server_refused_is_not_retried_as_a_network_problem(desktop, proxy, principal):
    proxy.clear()
    proxy.fault("/api/v1/projects", ("status", 401), times=None, method="GET")
    with pytest.raises(ApiError):
        desktop.project_service.get_projects()
    # one request, plus the silent refresh attempt's own traffic -- never three of the same
    assert proxy.count("GET", "/api/v1/projects") <= 2


# ── writes: the timer ────────────────────────────────────────────────────────

@pytest.mark.usefixtures("clean_slate")
def test_a_start_whose_reply_was_lost_produces_one_entry(qapp, desktop, proxy, api, db, principal):
    proxy.clear()
    timer = desktop.timer
    proxy.fault("/time-entries/start", ("drop_after",), times=1, method="POST")
    timer.start_tracking(principal["project_id"], principal["task_id"], "E2E lost start")
    session = timer.active_session()
    client_op = session["client_op"]
    _pump(qapp, lambda: timer.entry_id is not None, 90, "the start to be confirmed through the queue")

    entries = _entries_for(db, principal["user_id"], client_op)
    assert len(entries) == 1, f"the retry created {len(entries)} entries"
    assert timer.entry_id == entries[0].id
    assert len(_running_rows(db, principal["user_id"])) == 1
    _stop_quietly(qapp, desktop)


@pytest.mark.usefixtures("clean_slate")
def test_a_stop_whose_reply_was_lost_stops_the_entry_once_with_the_right_duration(qapp, desktop, proxy, api, db, principal):
    proxy.clear()
    timer = desktop.timer
    finalized = []
    timer.timer_finalized.connect(finalized.append)
    timer.start_tracking(principal["project_id"], principal["task_id"], "E2E lost stop")
    _pump(qapp, lambda: timer.entry_id is not None, 30, "the start")
    entry_id = timer.entry_id
    time.sleep(3)

    proxy.fault("/stop", ("drop_after",), times=1, method="POST")
    timer.stop_tracking()
    _pump(qapp, lambda: bool(finalized), 90, "the stop to be confirmed")

    row = _row(db, entry_id)
    assert row["status"] == "stopped" and row["end_time"] is not None
    assert row["total_seconds"] == int(row["derived"]), "duration is derived from the two timestamps"
    assert 3 <= row["total_seconds"] <= 12, row
    assert len(_running_rows(db, principal["user_id"])) == 0
    assert len(_entries_for(db, principal["user_id"], row["client_op"])) == 1


@pytest.mark.usefixtures("clean_slate")
def test_a_start_during_a_total_outage_is_queued_and_lands_once_when_the_backend_returns(qapp, desktop, proxy, api, db, principal):
    proxy.clear()
    timer = desktop.timer
    proxy.fault("", ("status", 503), times=None)          # everything is refused
    timer.start_tracking(principal["project_id"], principal["task_id"], "E2E outage start")
    client_op = timer.active_session()["client_op"]
    assert timer.is_running(), "the timer runs locally; the user is not blocked"
    time.sleep(2)
    assert _entries_for(db, principal["user_id"], client_op) == [], "nothing can have been written yet"

    proxy.clear()                                         # the backend comes back
    _pump(qapp, lambda: timer.entry_id is not None, 120, "the queued start to be delivered")
    entries = _entries_for(db, principal["user_id"], client_op)
    assert len(entries) == 1
    assert len(_running_rows(db, principal["user_id"])) == 1
    _stop_quietly(qapp, desktop)

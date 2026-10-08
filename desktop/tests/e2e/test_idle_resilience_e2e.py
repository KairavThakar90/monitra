"""
Idle handling under a bad network and a bad clock, against the real backend.

The happy path is `test_idle_lifecycle_e2e.py`. This module is the other half:
a fault-injecting HTTP proxy sits between the real desktop runtime and the real
backend, and the database is read back after every fault.

    reply lost after the server applied it   -> a retry is the same answer, applied once
    reply held past the client's timeout     -> the popup recovers; applied once
    backend 500 / 503                        -> nothing applied; retry works; no admin needed
    report refused for a while               -> the stretch is held and still reported
    reassign reply lost                      -> found on the server, not reported as a failure
    client clock 90 s fast                   -> accepted (it was a permanent 400 before)
    older client, no client_time, 30 s fast  -> accepted
    the real dialog, clicked, through a lost reply -> closes on the second press

Same opt-in and fixtures as the other E2E suites:

    MONITRA_E2E=1 python -m pytest tests/e2e/test_idle_resilience_e2e.py -q -s
"""
from __future__ import annotations

import socket
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from tests.e2e.test_idle_lifecycle_e2e import (  # noqa: F401  (fixtures and helpers)
    ENTRY_AGE_SECONDS, IDLE_SECONDS, TOLERANCE_SECONDS, _go_idle, _settle, _start_aged,
    idle_reading,
)
from tests.e2e.test_timing_lifecycle_e2e import (  # noqa: F401
    _free_port, _pump, _running_rows, api, backend, clean_slate, db, principal, pytestmark,
)

UTC = timezone.utc


# ── the fault-injecting proxy ────────────────────────────────────────────────

class FaultProxy:
    """Forwards to the backend; applies the rules in `rules` to matching requests.

    A rule is ``{"match": "/resolve", "action": ..., "times": n}`` where action is

    * ``("drop_after",)``   forward the request (the server applies it), then hang up
    * ``("hold", secs)``    forward the request, then wait `secs` before replying
    * ``("status", code)``  answer `code` without forwarding (nothing applied)

    ``exact`` matches the whole path (the report is POST /idle-periods; a
    substring would also catch every /idle-periods/{id}/resolve).
    """

    def __init__(self, target: str) -> None:
        self.target = target
        self.rules: list = []
        self.seen: list = []
        self._lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_a):  # quiet
                pass

            def _serve(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else None
                path = self.path
                with outer._lock:
                    outer.seen.append((self.command, path))
                    rule = next((r for r in outer.rules
                                 if (r["match"] == path if r["exact"] else r["match"] in path)
                                 and (r["method"] in (None, self.command))
                                 and r["times"] != 0), None)
                    if rule is not None and rule["times"] is not None:
                        rule["times"] -= 1
                action = rule["action"] if rule else None
                if action and action[0] == "status":
                    payload = b'{"detail":"injected fault"}'
                    self.send_response(action[1])
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                headers = {k: v for k, v in self.headers.items()
                           if k.lower() not in ("host", "content-length", "connection")}
                response = httpx.request(
                    self.command, outer.target + path, content=body, headers=headers, timeout=60,
                )
                if action and action[0] == "drop_after":
                    self.close_connection = True
                    try:
                        self.request.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    self.request.close()
                    return
                if action and action[0] == "hold":
                    time.sleep(action[1])
                self.send_response(response.status_code)
                for key, value in response.headers.items():
                    if key.lower() in ("transfer-encoding", "connection", "content-length",
                                       "content-encoding"):
                        continue
                    self.send_header(key, value)
                content = response.content
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                try:
                    self.wfile.write(content)
                except OSError:
                    pass

            do_GET = do_POST = do_PATCH = do_PUT = do_DELETE = _serve

        self.port = _free_port()
        self.server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def fault(self, match: str, action: tuple, times: int | None = 1, *,
              exact: bool = False, method: str | None = None) -> None:
        with self._lock:
            self.rules.append({"match": match, "action": action, "times": times,
                               "exact": exact, "method": method})

    def clear(self) -> None:
        with self._lock:
            self.rules.clear()
            self.seen.clear()

    def count(self, method: str, path: str) -> int:
        """Requests seen for exactly `path` (query string ignored)."""
        with self._lock:
            return sum(1 for m, p in self.seen if m == method and p.split("?")[0] == path)

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture(scope="module")
def proxy(backend):
    p = FaultProxy(backend)
    yield p
    p.close()


@pytest.fixture
def desktop(qapp, tmp_path, proxy, principal, monkeypatch):
    """The real desktop runtime, talking to the backend *through the proxy*."""
    proxy.clear()
    monkeypatch.setenv("SMS_API_BASE_URL", proxy.url)
    monkeypatch.setenv("MONITRA_ENV", "development")
    # A held reply must exceed the client's timeout without the test waiting 15 s.
    import app.idle.service as idle_api_module
    monkeypatch.setattr(idle_api_module, "TIMEOUT_DECISION", 2.0)
    from core.runtime import ApplicationRuntime
    from storage.manager import StorageManager

    runtime = ApplicationRuntime(storage=StorageManager(str(tmp_path / "e2e-cache.db")))
    runtime.api_client.base_url = proxy.url
    runtime.api_client.access_token = principal["token"]
    runtime.start_services()
    yield runtime
    runtime.shutdown(timeout_ms=3000)


def _adjustments(db, entry_id: int) -> tuple:
    from sqlalchemy import text

    with db.connect() as conn:
        row = conn.execute(
            text("SELECT count(*) AS n, coalesce(sum(adjustment_seconds), 0) AS total "
                 "FROM time_entry_adjustments WHERE time_entry_id = :id"),
            {"id": entry_id},
        ).one()
        return int(row.n), int(row.total)


def _stop_quietly(qapp, desktop):
    if desktop.timer.is_running():
        finalized = []
        desktop.timer.timer_finalized.connect(finalized.append)
        desktop.timer.stop_tracking()
        _pump(qapp, lambda: bool(finalized), 30, "cleanup stop")


# ── 1. The server applied it and the reply never came ────────────────────────

@pytest.mark.usefixtures("clean_slate")
def test_a_resolve_whose_reply_was_lost_is_applied_once(qapp, desktop, proxy, idle_reading, api, db, principal):
    timer, idle = desktop.timer, desktop.idle
    failures, resolved = [], []
    idle.resolve_failed.connect(failures.append)
    idle.resolve_succeeded.connect(resolved.append)
    entry_id = _start_aged(qapp, desktop, api, principal, "lost-resolve")
    period = _go_idle(qapp, desktop, idle_reading)

    proxy.fault("/resolve", ("drop_after",), times=1)
    idle.resolve(False, "resume")
    _pump(qapp, lambda: bool(failures), 30, "the lost reply to surface as a failure")
    assert idle.pending_period() is not None, "the period was abandoned"
    # The server did apply it -- one deduction, even though the client was told nothing.
    count, total = _adjustments(db, entry_id)
    assert count == 1 and total < 0

    # Not stuck: the user presses the same button again, and the same answer is confirmed.
    idle.resolve(False, "resume")
    _pump(qapp, lambda: bool(resolved), 30, "the repeated answer to be confirmed")
    assert idle.pending_period() is None and idle.idle_state == "MONITORING"
    assert _adjustments(db, entry_id) == (count, total), "the deduction was applied twice"
    assert timer.adjustment_seconds() == total
    assert api.get(f"/time-entries/{entry_id}").json()["adjustment_seconds"] == total
    assert api.get(f"/idle-periods/{period['id']}").json()["status"] == "resolved"
    _stop_quietly(qapp, desktop)


@pytest.mark.usefixtures("clean_slate")
def test_a_reply_held_past_the_timeout_recovers_without_a_second_deduction(qapp, desktop, proxy, idle_reading, api, db, principal):
    timer, idle = desktop.timer, desktop.idle
    failures, resolved = [], []
    idle.resolve_failed.connect(failures.append)
    idle.resolve_succeeded.connect(resolved.append)
    entry_id = _start_aged(qapp, desktop, api, principal, "held-resolve")
    _go_idle(qapp, desktop, idle_reading)

    proxy.fault("/resolve", ("hold", 5.0), times=1)
    started = time.monotonic()
    idle.resolve(True, "resume")
    _pump(qapp, lambda: bool(failures), 30, "the timeout to surface")
    waited = time.monotonic() - started
    assert waited < 12, f"the client waited {waited:.0f}s: the timeout is not finite enough"
    assert idle.idle_state == "PENDING", "the popup would be stuck"

    proxy.clear()
    idle.resolve(True, "resume")
    _pump(qapp, lambda: bool(resolved), 30, "the retry")
    assert idle.pending_period() is None
    assert _adjustments(db, entry_id)[0] == 0, "keep + resume deducts nothing"
    assert timer.adjustment_seconds() == 0
    time.sleep(5)                                    # let the held handler finish cleanly
    _stop_quietly(qapp, desktop)


@pytest.mark.usefixtures("clean_slate")
def test_a_backend_error_applies_nothing_and_a_retry_works(qapp, desktop, proxy, idle_reading, api, db, principal):
    timer, idle = desktop.timer, desktop.idle
    failures, resolved = [], []
    idle.resolve_failed.connect(failures.append)
    idle.resolve_succeeded.connect(resolved.append)
    entry_id = _start_aged(qapp, desktop, api, principal, "500-resolve")
    _go_idle(qapp, desktop, idle_reading)

    proxy.fault("/resolve", ("status", 503), times=2)
    idle.resolve(False, "resume")
    _pump(qapp, lambda: len(failures) == 1, 30, "first 503")
    assert _adjustments(db, entry_id) == (0, 0), "a refused request must write nothing"
    idle.resolve(False, "resume")
    _pump(qapp, lambda: len(failures) == 2, 30, "second 503")
    assert idle.pending_period() is not None and idle.idle_state == "PENDING"
    idle.resolve(False, "resume")
    _pump(qapp, lambda: bool(resolved), 30, "the third try")
    count, total = _adjustments(db, entry_id)
    assert count == 1 and total < 0
    assert timer.adjustment_seconds() == total
    _stop_quietly(qapp, desktop)


# ── 2. Reassign ──────────────────────────────────────────────────────────────

@pytest.mark.usefixtures("clean_slate")
def test_a_reassign_whose_reply_was_lost_is_found_on_the_server(qapp, desktop, proxy, idle_reading, api, db, principal):
    timer, idle = desktop.timer, desktop.idle
    failures, moved = [], []
    idle.reassign_failed.connect(failures.append)
    idle.reassign_succeeded.connect(moved.append)
    entry_id = _start_aged(qapp, desktop, api, principal, "lost-reassign")
    _go_idle(qapp, desktop, idle_reading)

    proxy.fault("/reassign", ("drop_after",), times=1)
    idle.reassign(principal["project_id"], principal["task_id"])
    _pump(qapp, lambda: bool(moved) or bool(failures), 30, "the reassignment to settle")
    assert failures == [], f"reported a failure for something the server had done: {failures}"
    assert moved and moved[0]["reassigned_seconds"] >= IDLE_SECONDS - 5
    count, total = _adjustments(db, entry_id)
    assert count == 1, "the reassignment was applied more than once"
    assert timer.adjustment_seconds() == total
    assert idle.idle_state == "PENDING" and idle.pending_period()["reassigned"] is True
    # And the popup can still be answered.
    resolved = []
    idle.resolve_succeeded.connect(resolved.append)
    idle.resolve(True, "resume")
    _pump(qapp, lambda: bool(resolved), 30, "the final answer")
    _stop_quietly(qapp, desktop)


# ── 3. The report ────────────────────────────────────────────────────────────

@pytest.mark.usefixtures("clean_slate")
def test_a_report_refused_for_a_while_is_held_and_still_reaches_the_user(qapp, desktop, proxy, idle_reading, api, db, principal):
    idle = desktop.idle
    _start_aged(qapp, desktop, api, principal, "report-503")
    proxy.fault("/idle-periods", ("status", 503), times=3, exact=True, method="POST")

    idle._monitoring_since = time.monotonic() - (IDLE_SECONDS + 60)
    idle_reading["idle"] = float(IDLE_SECONDS)
    idle.wake()
    _pump(qapp, lambda: proxy.count("POST", "/idle-periods") >= 1, 30, "the first report")
    idle_reading["idle"] = 0.0                       # the user is back at the keyboard
    assert idle.pending_period() is None
    _pump(qapp, lambda: idle.pending_period() is not None, 60, "the held stretch to be reported")
    assert proxy.count("POST", "/idle-periods") == 4, "three refusals and the report that landed"
    period = idle.pending_period()
    # The stretch is the one the user was idle for, not "idle since they came back".
    started = datetime.fromisoformat(period["idle_started_at"].replace("Z", "+00:00"))
    assert abs((datetime.now(UTC) - started).total_seconds() - IDLE_SECONDS) < 90
    resolved = []
    idle.resolve_succeeded.connect(resolved.append)
    idle.resolve(True, "resume")
    _pump(qapp, lambda: bool(resolved), 30, "answer")
    _stop_quietly(qapp, desktop)


# ── 4. Clock skew ────────────────────────────────────────────────────────────

class _FastClock(datetime):
    skew = timedelta(seconds=90)

    @classmethod
    def now(cls, tz=None):
        return super().now(tz) + cls.skew


@pytest.mark.usefixtures("clean_slate")
def test_a_desktop_clock_ninety_seconds_fast_still_gets_its_popup(qapp, desktop, idle_reading, api, db, principal, monkeypatch):
    from background_services.idle import idle_service as module

    monkeypatch.setattr(module, "datetime", _FastClock)
    idle = desktop.idle
    _start_aged(qapp, desktop, api, principal, "skew")
    period = _go_idle(qapp, desktop, idle_reading)
    assert period["status"] == "pending"
    # Placed on the server's clock: the stretch began ~IDLE_SECONDS ago there too.
    started = datetime.fromisoformat(period["idle_started_at"].replace("Z", "+00:00"))
    server_age = (datetime.now(UTC) - started).total_seconds()
    assert IDLE_SECONDS - 10 <= server_age <= IDLE_SECONDS + 40, server_age
    resolved = []
    idle.resolve_succeeded.connect(resolved.append)
    idle.resolve(True, "resume")
    _pump(qapp, lambda: bool(resolved), 30, "answer")
    _stop_quietly(qapp, desktop)


@pytest.mark.usefixtures("clean_slate")
def test_an_older_desktop_without_client_time_thirty_seconds_fast_is_not_refused(qapp, desktop, idle_reading, api, db, principal, monkeypatch):
    from background_services.idle import idle_service as module

    _FastClock.skew = timedelta(seconds=30)
    monkeypatch.setattr(module, "datetime", _FastClock)
    original = desktop.idle_api.report_idle_period
    monkeypatch.setattr(
        desktop.idle_api, "report_idle_period",
        lambda *a, **kw: original(*a, **{**kw, "client_time": None}),
    )
    try:
        idle = desktop.idle
        _start_aged(qapp, desktop, api, principal, "skew-legacy")
        period = _go_idle(qapp, desktop, idle_reading)
        assert period["status"] == "pending"
        resolved = []
        idle.resolve_succeeded.connect(resolved.append)
        idle.resolve(True, "resume")
        _pump(qapp, lambda: bool(resolved), 30, "answer")
        _stop_quietly(qapp, desktop)
    finally:
        _FastClock.skew = timedelta(seconds=90)


# ── 5. The real dialog, clicked ──────────────────────────────────────────────

@pytest.mark.usefixtures("clean_slate")
def test_the_real_popup_survives_a_lost_reply_and_closes_on_the_second_press(qapp, desktop, proxy, idle_reading, api, db, principal):
    from background_services.public_api import BackgroundApi
    from ui.idle_alert_dialog import IdleAlertDialog

    idle, timer = desktop.idle, desktop.timer
    entry_id = _start_aged(qapp, desktop, api, principal, "dialog")
    period = _go_idle(qapp, desktop, idle_reading)
    dialog = IdleAlertDialog(BackgroundApi(desktop), period)
    dialog.show()
    _settle(qapp, 0.2)
    try:
        dialog.discard_radio.setChecked(True)
        proxy.fault("/resolve", ("drop_after",), times=1)
        dialog.resume_btn.click()
        assert not dialog.resume_btn.isEnabled(), "no feedback that the request is in flight"
        _pump(qapp, lambda: dialog.resume_btn.isEnabled(), 30, "the buttons to come back")
        assert dialog.isVisible(), "the popup vanished on a failure"
        assert "retry" in dialog.status_label.text().lower() or "again" in dialog.status_label.text().lower()
        assert dialog.discard_radio.isChecked(), "the user's choice was lost"

        dialog.resume_btn.click()
        _pump(qapp, lambda: dialog.is_done(), 30, "the popup to close")
        _settle(qapp, 0.3)
        assert not dialog.isVisible()
        count, total = _adjustments(db, entry_id)
        assert count == 1 and total < 0
        assert timer.adjustment_seconds() == total
        assert len(_running_rows(db, principal["user_id"])) == 1, "resume must leave the timer running"
    finally:
        dialog.force_close()
        dialog.deleteLater()
        _stop_quietly(qapp, desktop)

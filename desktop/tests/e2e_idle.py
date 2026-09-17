"""
End-to-end check of the desktop idle module against the real backend.

Everything here is real: the ApplicationRuntime and its services, the HTTP
client, the backend endpoints, and the Neon database rows are read back
directly to confirm what actually landed. The detector's own loop thread does
the detecting.

Exactly one thing is simulated — the *inactivity reading*. Waiting out a real
five-minute idle threshold four times over is not something a check can do, so
`ActivityService.idle_seconds()` is overridden to report the user as idle. The
threshold comparison, the report, the popup contract, the resolution and the
reassignment are all genuine.

Usage, from desktop/ with the backend running on http://localhost:8000:

    python tests/e2e_idle.py
    python tests/e2e_idle.py --cleanup     # remove the rows it created

Every time entry it creates is tagged [IDLE-E2E] in its description so
--cleanup can find them and nothing else is touched.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timedelta, timezone

DESKTOP_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
BACKEND_ROOT = os.path.abspath(os.path.join(DESKTOP_ROOT, "..", "backend"))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("MONITRA_LOG_LEVEL", "WARNING")

# The backend package is imported first for database access and for minting a
# token, then removed from sys.path so `app.*` resolves to the desktop package.
sys.path.insert(0, BACKEND_ROOT)
from app.core.database import get_session_local  # noqa: E402
from app.core.security import create_access_token  # noqa: E402
from sqlalchemy import text  # noqa: E402

# Build the sessionmaker NOW, while `app` still resolves to the backend
# package. get_database_url() imports app.core.config lazily, on first use --
# and by then `app` is the desktop package, so a deferred first call fails
# with ModuleNotFoundError: No module named 'app.core'.
_SessionFactory = get_session_local()

sys.path.remove(BACKEND_ROOT)
for module in [name for name in sys.modules if name == "app" or name.startswith("app.")]:
    del sys.modules[module]

sys.path.insert(0, DESKTOP_ROOT)
from PySide6.QtWidgets import QApplication  # noqa: E402

from background_services.idle.idle_service import IdleState  # noqa: E402
from core.runtime import ApplicationRuntime  # noqa: E402
from storage.manager import StorageManager  # noqa: E402
from ui.idle_alert_dialog import IdleAlertDialog  # noqa: E402

TAG = "[IDLE-E2E]"
IDLE_SECONDS = 20 * 60  # what the fake reading reports

results = []


def heading(text_):
    print(f"\n{'=' * 70}\n{text_}\n{'=' * 70}")


def check(label, actual, expected):
    ok = actual == expected
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got {actual!r}, expected {expected!r}")
    results.append(ok)
    return ok


def record(name, ok):
    print(f"  -> {name}: {'PASS' if ok else 'FAIL'}")


# ── Database read-back ────────────────────────────────────────────────────────

def db():
    return _SessionFactory()


def idle_row(session, period_id):
    return session.execute(text(
        "SELECT status, counted, keep_idle_time, action, idle_duration_seconds, "
        "reassigned, reassigned_seconds, reassigned_project_id, reassigned_task_id, "
        "reassigned_time_entry_id "
        "FROM time_entry_idle_periods WHERE id = :i"
    ), {"i": period_id}).mappings().first()


def adjustments_for(session, entry_id):
    return session.execute(text(
        "SELECT COALESCE(SUM(adjustment_seconds), 0) FROM time_entry_adjustments "
        "WHERE time_entry_id = :e"
    ), {"e": entry_id}).scalar() or 0


def entry_row(session, entry_id):
    return session.execute(text(
        "SELECT total_seconds, end_time, status, project_id, task_id "
        "FROM time_entries WHERE id = :e"
    ), {"e": entry_id}).mappings().first()


# ── Harness ───────────────────────────────────────────────────────────────────

class Harness:
    def __init__(self, tmp_db):
        self.app = QApplication.instance() or QApplication([])
        self.storage = StorageManager(tmp_db)
        self.runtime = ApplicationRuntime(storage=self.storage)
        self.session = db()

        self.actor = self._pick_actor()
        if not self.actor:
            raise SystemExit(
                "No user found who can reach two projects that have tasks. "
                "Seed data first with backend/scripts/seed_dummy_time_tracking.py."
            )
        self.runtime.api_client.access_token = create_access_token(
            {"user_id": self.actor.id}
        )
        print(f"acting as user {self.actor.id} ({self.actor.name}), "
              f"idle_enabled={self.actor.idle_enabled} "
              f"idle_minutes={self.actor.idle_minutes}")

        # The one simulated input. Everything downstream of it is real.
        self._fake_idle = 0.0
        self.runtime.activity.idle_seconds = lambda: self._fake_idle

        self.runtime.start_services()
        self.idle = self.runtime.idle
        self.timer = self.runtime.timer
        self.destinations = self._pick_projects()

    def _pick_actor(self):
        return self.session.execute(text("""
            SELECT u.id, u.name, u.idle_enabled, u.idle_minutes
            FROM users u
            WHERE u.is_active AND u.idle_enabled
              AND EXISTS (
                  SELECT 1 FROM project_members pm
                  JOIN projects p ON p.id = pm.project_id AND p.status <> 'archived'
                  JOIN tasks t ON t.project_id = p.id AND t.status <> 'archived'
                  WHERE pm.user_id = u.id
                  GROUP BY pm.user_id HAVING COUNT(DISTINCT p.id) >= 2
              )
            ORDER BY u.id LIMIT 1
        """)).first()

    def _pick_projects(self):
        rows = self.session.execute(text("""
            SELECT DISTINCT ON (p.id) p.id AS project_id, p.project_name, t.id AS task_id, t.task_name
            FROM project_members pm
            JOIN projects p ON p.id = pm.project_id AND p.status <> 'archived'
            JOIN tasks t ON t.project_id = p.id AND t.status <> 'archived'
            WHERE pm.user_id = :u
            ORDER BY p.id LIMIT 2
        """), {"u": self.actor.id}).mappings().all()
        if len(rows) < 2:
            raise SystemExit("Need two authorised projects with tasks.")
        return list(rows)

    # ── Event-loop helpers ───────────────────────────────────────────────────

    def wait_until(self, predicate, timeout=20.0, label="condition"):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return True
            time.sleep(0.05)
        self.app.processEvents()
        if predicate():
            return True
        print(f"  [FAIL] timed out waiting for {label}")
        results.append(False)
        return False

    def settle(self, seconds=1.0):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.02)

    # ── Flow ─────────────────────────────────────────────────────────────────

    def stop_timer_and_wait(self):
        """Stop, and wait for the BACKEND to agree the entry is closed.

        The desktop's stop is deliberately optimistic: local state clears
        immediately and the request follows. `is_running()` therefore goes
        false long before the entry is closed server-side, and starting the
        next timer in that gap earns a legitimate 409 ("User already has an
        active timer"). Real usage never does this; a scripted check does it
        constantly, so it waits for the server rather than for local state.
        """
        if self.timer.is_running():
            self.timer.stop_tracking()
            self.wait_until(lambda: not self.timer.is_running(), label="local stop")
        self.wait_until(self._no_running_entry, label="the backend to close the entry")

    def _no_running_entry(self):
        self.app.processEvents()
        self.session.commit()  # a fresh read, not this transaction's snapshot
        return self.session.execute(text(
            "SELECT COUNT(*) FROM time_entries WHERE user_id = :u AND end_time IS NULL"
        ), {"u": self.actor.id}).scalar() == 0

    def start_timer(self):
        """A real timer on the first project/task, bound to a backend entry."""
        self.stop_timer_and_wait()
        dest = self.destinations[0]
        self.timer.start_tracking(dest["project_id"], dest["task_id"], dest["task_name"])
        self.wait_until(lambda: self.timer.entry_id, label="a backend entry id")
        entry_id = self.timer.entry_id
        # Backdate the entry so a 20-minute idle window fits inside it, and
        # tag it so --cleanup can find it.
        self.session.execute(text(
            "UPDATE time_entries SET start_time = now() - interval '40 minutes', "
            "description = :d WHERE id = :e"
        ), {"d": f"{TAG} tracked work", "e": entry_id})
        self.session.commit()
        return entry_id

    def go_idle(self):
        """Report the user as idle and let the detector's own thread act."""
        self.idle._monitoring_since = time.monotonic() - (IDLE_SECONDS + 60)
        self._fake_idle = IDLE_SECONDS
        self.idle.wake()
        self.wait_until(lambda: self.idle.pending_period() is not None,
                        label="a pending idle period")
        self._fake_idle = 0.0
        return self.idle.pending_period()

    def resolve(self, keep, action):
        self.idle.resolve(keep, action)
        self.wait_until(lambda: self.idle.pending_period() is None, label="resolution")
        self.settle(0.5)
        self.session.commit()  # see the API's committed rows

    def close(self):
        self.runtime.shutdown(timeout_ms=3000)
        self.session.close()


# ── Scenarios ─────────────────────────────────────────────────────────────────

def scenario_condition(h, name, keep, action, expect_counted):
    heading(name)
    entry_id = h.start_timer()
    period = h.go_idle()
    if not period:
        record(name, False)
        return
    print(f"  entry={entry_id} idle_period={period['id']} status={period['status']}")

    h.resolve(keep, action)
    row = idle_row(h.session, period["id"])
    before = len(results)

    check("idle period resolved", row["status"], "resolved")
    check("server's counted decision", row["counted"], expect_counted)
    check("keep/action recorded", (row["keep_idle_time"], row["action"]), (keep, action))
    check("deduction on the entry", adjustments_for(h.session, entry_id),
          0 if expect_counted else -row["idle_duration_seconds"])
    check("local timer stopped?", not h.timer.is_running(), action == "stop")
    check("entry stopped on the backend?",
          entry_row(h.session, entry_id)["end_time"] is not None, action == "stop")

    if action == "resume":
        h.stop_timer_and_wait()
    record(name, all(results[before:]))


def scenario_reassign(h):
    heading("REASSIGN: idle time moves to another project/task")
    entry_id = h.start_timer()
    period = h.go_idle()
    if not period:
        record("REASSIGN", False)
        return
    before = len(results)
    dest = h.destinations[1]
    origin = h.destinations[0]

    # A task from another project must be refused by the backend.
    failures = []
    h.idle.reassign_failed.connect(failures.append)
    h.idle.reassign(dest["project_id"], origin["task_id"])
    h.wait_until(lambda: failures, label="rejection of a cross-project task")
    check("task from another project rejected", bool(failures), True)

    successes = []
    h.idle.reassign_succeeded.connect(successes.append)
    h.idle.reassign(dest["project_id"], dest["task_id"])
    h.wait_until(lambda: successes, label="reassignment")
    h.settle(0.5)
    h.session.commit()

    row = idle_row(h.session, period["id"])
    moved = row["reassigned_seconds"]
    destination = entry_row(h.session, row["reassigned_time_entry_id"])
    original = entry_row(h.session, entry_id)

    check("period marked reassigned", row["reassigned"], True)
    check("destination project", destination["project_id"], dest["project_id"])
    check("destination task", destination["task_id"], dest["task_id"])
    check("destination carries the idle seconds", destination["total_seconds"], moved)
    check("same seconds deducted from the original",
          adjustments_for(h.session, entry_id), -moved)
    check("original entry NOT re-pointed",
          (original["project_id"], original["task_id"]),
          (origin["project_id"], origin["task_id"]))
    check("period still pending for the main popup", row["status"], "pending")
    check("timer still running", h.timer.is_running(), True)
    check("local state agrees it is pending", h.idle.idle_state, IdleState.PENDING)

    # Duplicate reassignment must be refused.
    failures.clear()
    h.idle.reassign(dest["project_id"], dest["task_id"])
    h.wait_until(lambda: failures, label="rejection of a duplicate reassignment")
    check("duplicate reassignment rejected", bool(failures), True)

    # Finish the popup: keep + resume. The reassigned seconds must not be
    # deducted a second time.
    h.resolve(True, "resume")
    check("no second deduction after resolution",
          adjustments_for(h.session, entry_id), -moved)

    h.stop_timer_and_wait()
    record("REASSIGN", all(results[before:]))


def scenario_duplicates(h):
    heading("DUPLICATES: one period, one popup, one request")
    entry_id = h.start_timer()
    period = h.go_idle()
    if not period:
        record("DUPLICATES", False)
        return
    before = len(results)

    # Keep reporting idle: no second period may be opened.
    h._fake_idle = IDLE_SECONDS + 120
    for _ in range(5):
        h.idle.wake()
        h.settle(0.3)
    h.session.commit()
    count = h.session.execute(text(
        "SELECT COUNT(*) FROM time_entry_idle_periods WHERE time_entry_id = :e"
    ), {"e": entry_id}).scalar()
    check("exactly one idle period for the entry", count, 1)
    check("still the same pending period", h.idle.pending_period()["id"], period["id"])

    # Double-clicked Resume: the second call is refused while the first is in
    # flight, so the deduction cannot be applied twice.
    h.idle.resolve(False, "resume")
    h.idle.resolve(False, "resume")
    h.wait_until(lambda: h.idle.pending_period() is None, label="resolution")
    h.settle(0.5)
    h.session.commit()
    row = idle_row(h.session, period["id"])
    check("deducted exactly once", adjustments_for(h.session, entry_id),
          -row["idle_duration_seconds"])

    h.stop_timer_and_wait()
    record("DUPLICATES", all(results[before:]))


def scenario_stop_while_pending(h):
    heading("STOP while pending: unresolved idle time is never banked")
    entry_id = h.start_timer()
    period = h.go_idle()
    if not period:
        record("STOP-WHILE-PENDING", False)
        return
    before = len(results)

    h.stop_timer_and_wait()  # straight to Stop; the popup is never answered
    h.settle(1.5)
    h.session.commit()

    row = idle_row(h.session, period["id"])
    check("backend auto-resolved the pending period", row["status"], "resolved")
    check("resolved as discarded", row["counted"], False)
    check("recorded as a stop", row["action"], "stop")
    check("idle seconds deducted", adjustments_for(h.session, entry_id),
          -row["idle_duration_seconds"])
    check("local pending state cleared", h.idle.pending_period(), None)
    record("STOP-WHILE-PENDING", all(results[before:]))


def scenario_recovery(h):
    heading("RECOVERY: a pending period survives a restart")
    entry_id = h.start_timer()
    period = h.go_idle()
    if not period:
        record("RECOVERY", False)
        return
    before = len(results)

    # Simulate a restart: drop every trace of the period from local state,
    # exactly as a fresh process would have.
    h.idle.reset_session()
    check("nothing pending locally", h.idle.pending_period(), None)

    opened = []
    h.idle.idle_period_opened.connect(opened.append)
    h.idle.wake()
    h.wait_until(lambda: h.idle.pending_period() is not None,
                 label="the period to be recovered from the backend")
    check("recovered the same period", h.idle.pending_period()["id"], period["id"])
    check("the popup is raised again", len(opened), 1)
    h.session.commit()
    count = h.session.execute(text(
        "SELECT COUNT(*) FROM time_entry_idle_periods WHERE time_entry_id = :e"
    ), {"e": entry_id}).scalar()
    check("recovery did not open a second period", count, 1)

    h.resolve(False, "stop")
    record("RECOVERY", all(results[before:]))


def scenario_popup_contract(h):
    """The popup is a real widget here, not a mock: its dismissal guards are
    exercised against Qt itself."""
    heading("POPUP: mandatory, non-dismissible until resolved")
    before = len(results)
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent

    period = {
        "id": 1, "time_entry_id": 1, "status": "pending", "reassigned": False,
        "idle_started_at": (datetime.now(timezone.utc) - timedelta(minutes=7)).isoformat(),
        "idle_detected_at": (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat(),
        "original_project_id": h.destinations[0]["project_id"],
    }
    from background_services.public_api import BackgroundApi

    dialog = IdleAlertDialog(BackgroundApi(h.runtime), period)
    dialog.show()
    h.settle(0.3)

    check("no system close button",
          bool(dialog.windowFlags() & Qt.WindowType.FramelessWindowHint), True)
    check("application-modal", dialog.isModal(), True)
    check("always on top",
          bool(dialog.windowFlags() & Qt.WindowType.WindowStaysOnTopHint), True)

    dialog.keyPressEvent(QKeyEvent(
        QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier
    ))
    h.settle(0.2)
    check("Escape does not dismiss", dialog.isVisible(), True)

    dialog.reject()
    h.settle(0.2)
    check("reject() does not dismiss", dialog.isVisible(), True)

    dialog.close()
    h.settle(0.2)
    check("close() does not dismiss", dialog.isVisible(), True)

    check("the live figure reads the real elapsed idle time",
          dialog.duration_label.text(), "7 minutes")
    check("default selection is 'Yes, keep idle time'",
          dialog.keep_radio.isChecked(), True)

    dialog.force_close()  # the shutdown/logout path
    h.settle(0.2)
    check("force_close() tears it down", dialog.isVisible(), False)
    record("POPUP-CONTRACT", all(results[before:]))


# ── Cleanup ───────────────────────────────────────────────────────────────────

def cleanup():
    session = db()
    try:
        ids = [r[0] for r in session.execute(text(
            "SELECT id FROM time_entries WHERE description LIKE :tag"
        ), {"tag": f"%{TAG}%"}).all()]
        reassigned = [r[0] for r in session.execute(text(
            "SELECT reassigned_time_entry_id FROM time_entry_idle_periods "
            "WHERE time_entry_id = ANY(:ids) AND reassigned_time_entry_id IS NOT NULL"
        ), {"ids": ids or [0]}).all()]
        all_ids = list({*ids, *reassigned})
        if not all_ids:
            print("Nothing tagged to clean up.")
            return
        session.execute(text("DELETE FROM time_entries WHERE id = ANY(:ids)"),
                        {"ids": all_ids})
        session.commit()
        print(f"Deleted {len(all_ids)} tagged time entries "
              f"(idle periods and adjustments cascade).")
    finally:
        session.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cleanup", action="store_true")
    parser.add_argument("--db", default=None, help="local cache path for the run")
    args = parser.parse_args()

    if args.cleanup:
        cleanup()
        return 0

    tmp_db = args.db or os.path.join(
        os.environ.get("TEMP", "/tmp"), f"monitra-idle-e2e-{os.getpid()}.db"
    )
    harness = Harness(tmp_db)
    try:
        scenario_condition(harness, "CONDITION 1: No, discard + Stop   -> NOT counted",
                           False, "stop", False)
        scenario_condition(harness, "CONDITION 2: No, discard + Resume -> NOT counted",
                           False, "resume", False)
        scenario_condition(harness, "CONDITION 3: Yes, keep + Stop     -> COUNTED",
                           True, "stop", True)
        scenario_condition(harness, "CONDITION 4: Yes, keep + Resume   -> COUNTED",
                           True, "resume", True)
        scenario_reassign(harness)
        scenario_duplicates(harness)
        scenario_stop_while_pending(harness)
        scenario_recovery(harness)
        scenario_popup_contract(harness)
    finally:
        harness.close()

    heading("SUMMARY")
    failed = results.count(False)
    print(f"  {len(results) - failed}/{len(results)} assertions passed")
    print("  Run with --cleanup to delete the rows this check created.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

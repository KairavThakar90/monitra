"""
idle_service — Detects inactivity, and owns every idle-period operation.

Division of responsibility
--------------------------
The desktop detects that the user stopped touching the machine. The **backend
decides everything else**: how long the idle period actually was, whether the
time counts, and where reassigned time lands. Nothing here computes tracked
time, and nothing here edits it.

Why the detection lives in a service rather than in the dialog
--------------------------------------------------------------
Idle detection has to keep working while the main window is minimised or
hidden in the tray, and the popup that results is transient. A transient
widget owning a long-running detector is the exact pattern that destabilised
this application before (see DO_NOT_DO.md). So the runtime owns the detector
and the API calls; the dialog is a view that renders this service's state and
calls two methods on it.

Where the inactivity reading comes from
---------------------------------------
`ActivityService.idle_seconds()` — the Windows `GetLastInputInfo` reading that
already drives the activity percentage. No new global listener is installed:
there is still exactly one place in the process that observes system input.
On a platform where that reading is unavailable the service reports itself
unsupported and never fires, rather than guessing.

Duplicate prevention
--------------------
An explicit state machine, plus the backend's own idempotency:

    MONITORING → REPORTING → PENDING → RESOLVING → MONITORING
                                    ↘ REASSIGNING ↗

Only `MONITORING` can open a period, only `PENDING` can be resolved or
reassigned, and every transition happens on the GUI thread. A retried report
carries a `client_event_id`, and a second report while one is pending returns
the period that already exists, so neither a network retry nor a race can
produce two popups or two idle periods.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from PySide6.QtCore import Signal, Slot

from app.api.exceptions import ApiError
from core.service import LoopService
from core.time_format import parse_utc as _parse_utc

#: How often inactivity is evaluated. The reading itself is two syscalls, so
#: this is deliberately unhurried — a threshold measured in minutes does not
#: need a sub-second poll, and the tick must stay cheap enough to be invisible.
POLL_INTERVAL_MS = 2000

#: How often the user's idle configuration is re-read from the backend. It is
#: seeded from `/auth/me` at login, so this is a slow correction for a value
#: an administrator changed mid-session — not a poll the feature depends on.
CONFIG_REFRESH_SECONDS = 15 * 60

#: Idle detection must not fire sooner than the user's own threshold, but the
#: backend applies a few seconds of tolerance for poll scheduling. Reporting a
#: hair early would be rejected, so the client rounds up rather than down.
DETECTION_MARGIN_SECONDS = 1


class IdleState:
    """Where this service is in the idle lifecycle. One value at a time."""

    #: Idle detection is off for this user, or cannot be measured here.
    DISABLED = "DISABLED"
    #: Watching for inactivity. The only state that may open an idle period.
    MONITORING = "MONITORING"
    #: A report is in flight. Blocks a second report for the same stretch.
    REPORTING = "REPORTING"
    #: The backend holds an unresolved period; the popup is up.
    PENDING = "PENDING"
    #: The user's answer is in flight. Blocks a double-clicked button.
    RESOLVING = "RESOLVING"
    #: A reassignment is in flight. Blocks a double-clicked Reassign.
    REASSIGNING = "REASSIGNING"


class IdleService(LoopService):
    """
    Owns idle detection and every idle-period call to the backend.

    Signals (all delivered on the GUI thread):
        idle_period_opened(dict)   — a pending period exists; show the popup
        idle_period_cleared()      — it is gone; close the popup
        resolve_succeeded(dict)    — the backend accepted the user's answer
        resolve_failed(str)        — it did not; the period is still pending
        reassign_succeeded(dict)   — idle time moved to another project/task
        reassign_failed(str)       — it did not; nothing was written
        config_changed(bool, int)  — idle_enabled, idle_minutes
    """

    name = "idle"

    idle_period_opened = Signal(dict)
    idle_period_cleared = Signal()
    resolve_succeeded = Signal(dict)
    resolve_failed = Signal(str)
    reassign_succeeded = Signal(dict)
    reassign_failed = Signal(str)
    config_changed = Signal(bool, int)

    #: Internal, worker-thread → GUI-thread hand-offs. `tick()` runs off the
    #: GUI thread, so it decides *that* something should happen and emits;
    #: the slot that actually calls the backend runs on the GUI thread and
    #: submits through the shared task pool.
    _threshold_reached = Signal(float)
    _interruption_due = Signal(int)
    _entry_observed = Signal(int)
    _config_refresh_due = Signal()

    interval_ms = POLL_INTERVAL_MS

    def __init__(self, runtime, idle_api, parent=None) -> None:
        super().__init__(runtime, parent)
        self._api = idle_api

        # ── Configuration (authoritative source: the users table) ────────────
        self._idle_enabled = True
        self._idle_minutes = 5
        self._config_loaded = False
        self._config_read_at = time.monotonic()

        # ── State machine ────────────────────────────────────────────────────
        self._state = IdleState.MONITORING
        self._pending: Optional[Dict[str, Any]] = None
        self._pending_entry_id: Optional[int] = None

        #: Monotonic instant from which inactivity may be claimed. Set when
        #: tracking starts and again whenever a period resolves with Resume,
        #: so the seconds before the timer began — and the seconds the user
        #: spent answering the last popup — can never be reported as a new
        #: idle period.
        self._monitoring_since = time.monotonic()
        #: Entry ids whose pending period has already been checked with the
        #: backend, so recovery runs once per entry rather than every tick.
        self._recovery_checked: set = set()
        self._last_entry_id: Optional[int] = None
        self._unsupported_logged = False
        #: An interruption gap (power cut, crash, kill, hang) that reached the
        #: user's threshold and has not yet been reported as an idle period.
        #: Set by `_on_tracking_recovered`, reported once the entry id is
        #: known and the backend is reachable, cleared when the report lands
        #: or is definitively refused. See `_on_interruption_due`.
        self._interruption: Optional[Dict[str, Any]] = None

        self._threshold_reached.connect(self._on_threshold_reached)
        self._interruption_due.connect(self._on_interruption_due)
        self._entry_observed.connect(self._on_entry_observed)
        self._config_refresh_due.connect(self._refresh_config)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def on_start(self) -> None:
        # The user's threshold is known locally before any request answers:
        # the restored session holds the last `/auth/me` profile. Seeding
        # from it here is what lets a recovered session's interruption gap
        # be judged against the user's own threshold rather than the
        # default -- recovery runs before session verification has answered.
        session_manager = getattr(self.runtime, "session_manager", None)
        profile = getattr(session_manager, "user_info", None)
        if isinstance(profile, dict):
            self.apply_user_profile(profile)
        timer = self.runtime.timer
        timer.timer_started.connect(self._on_tracking_started)
        timer.timer_recovered.connect(self._on_tracking_started)
        timer.timer_recovered.connect(self._on_tracking_recovered)
        timer.timer_stopped.connect(self._on_tracking_stopped)
        super().on_start()

    def on_stop(self, timeout_ms: int) -> bool:
        # Nothing durable to flush: a pending idle period lives on the server,
        # which is exactly why a crash or a restart cannot lose it.
        return super().on_stop(timeout_ms)

    # ── Configuration ─────────────────────────────────────────────────────────

    @property
    def idle_enabled(self) -> bool:
        return self._idle_enabled

    @property
    def idle_minutes(self) -> int:
        return self._idle_minutes

    def idle_config(self) -> Dict[str, Any]:
        return {"idle_enabled": self._idle_enabled, "idle_minutes": self._idle_minutes}

    def apply_user_profile(self, user_data: Optional[Dict[str, Any]]) -> None:
        """Seed the configuration from a `/auth/me` payload.

        Login and session verification already fetch the whole profile, and it
        carries `idle_enabled` and `idle_minutes`. Reading them from what was
        fetched anyway is why this feature adds no request to the sign-in path.
        """
        if not isinstance(user_data, dict):
            return
        user = user_data.get("user") if isinstance(user_data.get("user"), dict) else user_data
        if "idle_enabled" not in user and "idle_minutes" not in user:
            return
        self._set_config(
            user.get("idle_enabled", self._idle_enabled),
            user.get("idle_minutes", self._idle_minutes),
        )
        self._config_loaded = True
        self._config_read_at = time.monotonic()

    def _set_config(self, enabled: Any, minutes: Any) -> None:
        try:
            minutes_value = int(minutes)
        except (TypeError, ValueError):
            minutes_value = self._idle_minutes
        # A zero or negative threshold would make every poll look idle. The
        # backend rejects such a value on write; if one reaches us anyway,
        # keep the last usable one rather than firing continuously.
        if minutes_value <= 0:
            self.log.warning("ignoring non-positive idle_minutes=%r", minutes)
            minutes_value = self._idle_minutes
        enabled_value = bool(enabled)
        if enabled_value == self._idle_enabled and minutes_value == self._idle_minutes:
            return
        self._idle_enabled = enabled_value
        self._idle_minutes = minutes_value
        self.log.info("idle configuration: enabled=%s minutes=%d", enabled_value, minutes_value)
        self.config_changed.emit(enabled_value, minutes_value)

    @Slot()
    def _refresh_config(self) -> None:
        self._config_read_at = time.monotonic()

        def on_success(config: Dict[str, Any]) -> None:
            if isinstance(config, dict):
                self._set_config(
                    config.get("idle_enabled", self._idle_enabled),
                    config.get("idle_minutes", self._idle_minutes),
                )
                self._config_loaded = True

        self.runtime.tasks.submit(
            self._api.get_config,
            on_success=on_success,
            # A failed refresh keeps the last known configuration. Blanking a
            # valid local value because the network blipped is a regression,
            # not error handling.
            on_error=lambda exc: self.log.info("idle config refresh failed: %s", exc),
            key="idle-config",
        )

    # ── Detection (worker thread) ─────────────────────────────────────────────

    def tick(self) -> Optional[int]:
        """Evaluate inactivity. Runs off the GUI thread; touches no widget.

        Everything here is a cheap read: two syscalls for the idle reading and
        a dict copy for the session. Anything that needs the network is handed
        to the GUI thread by signal and submitted to the shared task pool.
        """
        self.heartbeat()

        if time.monotonic() - self._config_read_at > CONFIG_REFRESH_SECONDS:
            self._config_read_at = time.monotonic()  # claim it before emitting
            self._config_refresh_due.emit()

        if not self._idle_enabled:
            return POLL_INTERVAL_MS
        if self._state != IdleState.MONITORING:
            return POLL_INTERVAL_MS
        if not self.runtime.timer.is_running():
            return POLL_INTERVAL_MS

        session = self.runtime.timer.active_session() or {}
        entry_id = session.get("entry_id")
        if entry_id != self._last_entry_id:
            self._last_entry_id = entry_id
            if entry_id:
                self._entry_observed.emit(int(entry_id))
        if not entry_id:
            # The start has not reached the backend yet, so there is no entry
            # to attach an idle period to. Detection resumes as soon as the
            # id arrives; no local-only idle period is invented.
            return POLL_INTERVAL_MS

        if self._interruption is not None:
            # An interruption gap is waiting to be reported. It takes
            # precedence over fresh inactivity: it is older, and the backend
            # holds at most one pending period per entry anyway.
            self._interruption_due.emit(int(entry_id))
            return POLL_INTERVAL_MS

        idle = self._effective_idle_seconds()
        if idle is None:
            return POLL_INTERVAL_MS
        if idle + DETECTION_MARGIN_SECONDS >= self._idle_minutes * 60:
            self._threshold_reached.emit(idle)
        return POLL_INTERVAL_MS

    def _effective_idle_seconds(self) -> Optional[float]:
        """How long the user has been idle *within this monitoring window*.

        The raw reading counts inactivity from the last input, which may
        predate the timer starting or the last popup being answered. Bounding
        it by `_monitoring_since` is what stops a freshly resumed timer from
        immediately reporting the idle stretch the user just dealt with.
        """
        raw = self.runtime.activity.idle_seconds()
        if raw is None:
            if not self._unsupported_logged:
                self._unsupported_logged = True
                self.log.warning(
                    "inactivity cannot be measured on this platform; idle "
                    "detection is inactive (no value is guessed)"
                )
            return None
        return min(float(raw), time.monotonic() - self._monitoring_since)

    @property
    def supported(self) -> bool:
        return self.runtime.activity.idle_seconds() is not None

    # ── Reporting (GUI thread) ────────────────────────────────────────────────

    @Slot(float)
    def _on_threshold_reached(self, idle_seconds: float) -> None:
        if self._state != IdleState.MONITORING or not self._idle_enabled:
            return
        session = self.runtime.timer.active_session() or {}
        entry_id = session.get("entry_id")
        if not entry_id or not self.runtime.timer.is_running():
            return

        now = datetime.now(timezone.utc)
        started_at = now - timedelta(seconds=idle_seconds)
        idle_started_at = started_at.isoformat()
        # Stable for this stretch of inactivity, so a retry of the same report
        # returns the period that already exists rather than opening a second.
        client_event_id = f"idle:{entry_id}:{idle_started_at}"

        self._state = IdleState.REPORTING
        self.log.info(
            "idle threshold reached: %.0fs idle on entry %s (threshold %dm)",
            idle_seconds, entry_id, self._idle_minutes,
        )

        def call():
            return self._api.report_idle_period(
                time_entry_id=int(entry_id),
                idle_started_at=idle_started_at,
                idle_detected_at=now.isoformat(),
                client_event_id=client_event_id,
            )

        self.runtime.tasks.submit(
            call,
            on_success=lambda period: self._adopt_pending(period, int(entry_id)),
            on_error=self._on_report_failed,
            key=f"idle-report:{client_event_id}",
        )

    def _on_report_failed(self, exc: BaseException) -> None:
        """A report that did not land leaves nothing behind.

        The state returns to MONITORING, so the next tick tries again while
        the user is still idle. No popup is shown for an idle period the
        backend does not hold: the popup's whole contract is that the server
        has something pending to resolve.
        """
        self.log.warning("could not report idle period: %s", exc)
        if self._state == IdleState.REPORTING:
            self._state = IdleState.MONITORING

    def _adopt_pending(self, period: Optional[Dict[str, Any]], entry_id: int) -> None:
        """Take ownership of a pending period and raise the popup — once."""
        if not isinstance(period, dict) or not period.get("id"):
            if self._state == IdleState.REPORTING:
                self._state = IdleState.MONITORING
            return
        if self._pending and self._pending.get("id") == period.get("id"):
            self._pending = period  # refreshed copy; the popup is already up
            return
        self._pending = period
        self._pending_entry_id = entry_id
        self._state = IdleState.PENDING
        self.log.info("idle period %s pending on entry %s", period.get("id"), entry_id)
        self.idle_period_opened.emit(dict(period))

    def pending_period(self) -> Optional[Dict[str, Any]]:
        return dict(self._pending) if self._pending else None

    @property
    def idle_state(self) -> str:
        """Where this service is in the idle lifecycle (an `IdleState`).

        Deliberately NOT called `state`: `BaseService.state` is the *service*
        lifecycle that ServiceManager reads, and shadowing it is a mistake
        this codebase has already paid for -- `NetworkService.state` once hid
        connectivity behind the name the manager used for RUNNING/STOPPED.
        The domain property carries a domain-qualified name, exactly as
        `network_state` does.
        """
        return self._state

    # ── Recovery ──────────────────────────────────────────────────────────────

    @Slot(int)
    def _on_entry_observed(self, entry_id: int) -> None:
        """Ask the backend whether this entry already has an unresolved period.

        Runs once per entry id. This is what makes a crash or a restart safe:
        the pending period lives on the server, so the popup comes back
        instead of the idle time being silently counted or silently dropped.
        """
        if entry_id in self._recovery_checked:
            return
        self._recovery_checked.add(entry_id)
        if not self._idle_enabled or self._state != IdleState.MONITORING:
            return

        self.runtime.tasks.submit(
            lambda: self._api.get_pending_idle_period(entry_id),
            on_success=lambda period: self._adopt_recovered(period, entry_id),
            on_error=lambda exc: self.log.info(
                "could not check for a pending idle period on entry %s: %s", entry_id, exc
            ),
            key=f"idle-pending:{entry_id}",
        )

    def _adopt_recovered(self, period: Optional[Dict[str, Any]], entry_id: int) -> None:
        if not period:
            return
        self.log.info("recovered pending idle period %s from the backend", period.get("id"))
        self._adopt_pending(period, entry_id)

    # ── Interruption gaps ─────────────────────────────────────────────────────
    #
    # The business rule for an unexpected interruption -- power cut, cable
    # pulled, battery exhausted, hard power-off, crash, kill, hang -- is the
    # idle rule. A powered-off machine is not evidence of work, and a session
    # recovered after one must not silently count the gap; but the session
    # itself is preserved, so the user, not the client, decides. The gap runs
    # from the previous process's last durable heartbeat to the recovery
    # instant. When it reaches the user's own `idle_minutes` it is reported
    # through the same `POST /idle-periods` an ordinary idle stretch uses, and
    # the same popup, the same keep/discard/resume/stop answer and the same
    # backend accounting apply. Below the threshold nothing is reported, as
    # for any shorter pause. No new threshold, no new maximum.
    #
    # Idempotent by three layers: a client event id keyed on the session and
    # the interruption instant; the backend's "one unresolved period per
    # entry", which answers a second report with the period already pending;
    # and the pending lookup every entry id already gets at recovery, which
    # brings back a period the user has not yet answered.

    @Slot(dict)
    def _on_tracking_recovered(self, session: dict) -> None:
        interrupted_at = _parse_utc(session.get("interrupted_at_utc"))
        recovered_at = _parse_utc(session.get("recovered_at_utc"))
        client_op = session.get("client_op")
        if interrupted_at is None or recovered_at is None or not client_op:
            self._interruption = None
            return
        gap = (recovered_at - interrupted_at).total_seconds()
        # The threshold is applied when the report is about to be sent, not
        # here: the user's configuration may not have been read yet at the
        # instant of recovery, and judging the gap against the default was
        # measured to drop a real 85-second outage for a one-minute user.
        self._interruption = {
            "client_op": client_op,
            "gap_seconds": gap,
            "idle_started_at": interrupted_at.isoformat(),
            "idle_detected_at": recovered_at.isoformat(),
            "client_event_id": f"interruption:{client_op}:{interrupted_at.isoformat()}",
        }
        self.log.info(
            "recovered session was interrupted for %.0fs; it will be reported as an "
            "idle period if it reaches the user's idle threshold", gap,
        )

    def _network_usable(self) -> bool:
        network = getattr(self.runtime, "network", None)
        if network is None:
            return True
        state = getattr(network, "network_state", None)
        if state is None:
            return True
        from background_services.network import NetworkState
        return state in NetworkState.USABLE or state == NetworkState.UNKNOWN

    @Slot(int)
    def _on_interruption_due(self, entry_id: int) -> None:
        """Report the recorded interruption gap as an idle period, once."""
        interruption = self._interruption
        if interruption is None or not self._idle_enabled:
            return
        if self._state != IdleState.MONITORING or not self.runtime.timer.is_running():
            return
        session = self.runtime.timer.active_session() or {}
        if session.get("client_op") != interruption["client_op"]:
            # The session the gap belonged to is gone; nothing to reconcile.
            self._interruption = None
            return
        if not self._config_loaded:
            return  # the user's threshold is not known yet; judged next tick
        if interruption["gap_seconds"] + DETECTION_MARGIN_SECONDS < self._idle_minutes * 60:
            self.log.info(
                "recovered session was interrupted for %.0fs, under the user's %dm idle "
                "threshold; nothing to reconcile",
                interruption["gap_seconds"], self._idle_minutes,
            )
            self._interruption = None
            return
        if not self._network_usable():
            return  # kept; the next tick tries again once the network is back

        self._state = IdleState.REPORTING
        self.log.info(
            "reporting interruption %s -> %s (%.0fs, threshold %dm) on entry %s",
            interruption["idle_started_at"], interruption["idle_detected_at"],
            interruption["gap_seconds"], self._idle_minutes, entry_id,
        )

        def call():
            return self._api.report_idle_period(
                time_entry_id=int(entry_id),
                idle_started_at=interruption["idle_started_at"],
                idle_detected_at=interruption["idle_detected_at"],
                client_event_id=interruption["client_event_id"],
            )

        def on_success(period) -> None:
            self._interruption = None
            self._adopt_pending(period, int(entry_id))

        self.runtime.tasks.submit(
            call,
            on_success=on_success,
            on_error=self._on_interruption_report_failed,
            key=f"idle-report:{interruption['client_event_id']}",
        )

    def _on_interruption_report_failed(self, exc: BaseException) -> None:
        """A definitive refusal ends the attempt; anything else is retried.

        400 (the gap is under the user's threshold on the server's clock),
        404 (the entry is gone) and 409 (the entry has already stopped, or
        idle detection is off for this user) are answers, and the backend
        is authoritative: the gap is then not reconciled through a popup.
        A connection error, a 5xx or a timeout is not an answer: the gap is
        kept and reported at the next tick once the backend is reachable, so
        an interruption that ends with the network still down is reconciled
        as soon as it returns.
        """
        status_code = getattr(exc, "status_code", None)
        if status_code in (400, 404, 409):
            self.log.warning("interruption gap not accepted by the backend (%s); dropping it", exc)
            self._interruption = None
        else:
            self.log.warning("could not report the interruption gap yet: %s", exc)
        if self._state == IdleState.REPORTING:
            self._state = IdleState.MONITORING

    # ── Tracking lifecycle ────────────────────────────────────────────────────

    def _on_tracking_started(self, session: dict) -> None:
        """A new (or recovered) tracking session begins a fresh idle window."""
        self._monitoring_since = time.monotonic()
        self._last_entry_id = None
        if self._state in (IdleState.MONITORING, IdleState.REPORTING):
            self._state = IdleState.MONITORING

    def _on_tracking_stopped(self, _payload: dict) -> None:
        """The timer stopped, so any pending period is no longer this popup's.

        The backend resolves a still-pending period as discarded when the
        entry stops — the same outcome as the user pressing Stop — so the
        popup is dismissed rather than left demanding an answer about a timer
        that is no longer running.
        """
        self._monitoring_since = time.monotonic()
        self._last_entry_id = None
        self._interruption = None
        if self._pending is not None:
            self.log.info(
                "timer stopped with idle period %s pending; the backend "
                "resolves it as discarded",
                self._pending.get("id"),
            )
        self._clear_pending()

    def _clear_pending(self) -> None:
        had_pending = self._pending is not None
        self._pending = None
        self._pending_entry_id = None
        self._state = IdleState.MONITORING if self._idle_enabled else IdleState.DISABLED
        if had_pending:
            self.idle_period_cleared.emit()

    def reset_session(self) -> None:
        """Drop all session-scoped state. Called on logout."""
        self._recovery_checked.clear()
        self._last_entry_id = None
        self._interruption = None
        self._config_loaded = False
        self._config_read_at = time.monotonic()
        self._monitoring_since = time.monotonic()
        self._clear_pending()

    # ── Resolution (GUI thread; called by the popup) ──────────────────────────

    def resolve(self, keep_idle_time: bool, action: str) -> None:
        """Send the user's answer to the backend.

        The server decides the outcome: idle time counts only for keep +
        resume. `action="stop"` stops the time entry through the backend's own
        stop path, so there is no second stop implementation here.

        Guarded by state, so a double-clicked button sends one request.
        """
        if action not in ("stop", "resume"):
            self.resolve_failed.emit("Unsupported action.")
            return
        if self._state == IdleState.RESOLVING:
            return  # already in flight
        if self._state != IdleState.PENDING or not self._pending:
            self.resolve_failed.emit("This idle period is no longer pending.")
            return

        period_id = int(self._pending["id"])
        resolved_at = datetime.now(timezone.utc).isoformat()
        self._state = IdleState.RESOLVING

        def call():
            return self._api.resolve_idle_period(
                period_id, bool(keep_idle_time), action, resolved_at
            )

        self.runtime.tasks.submit(
            call,
            on_success=lambda result: self._on_resolved(result, action),
            on_error=lambda exc: self._on_resolve_error(exc, action),
            key=f"idle-resolve:{period_id}",
        )

    def _on_resolved(self, result: Dict[str, Any], action: str) -> None:
        counted = bool(result.get("counted")) if isinstance(result, dict) else False
        self.log.info(
            "idle period %s resolved: action=%s counted=%s duration=%ss "
            "entry_adjustment=%ss",
            (result or {}).get("id"), action, counted,
            (result or {}).get("idle_duration_seconds"),
            (result or {}).get("time_entry_adjustment_seconds"),
        )
        # The verdict first, then the local consequences: for "stop" the
        # timer folds its final figure into the day the moment it stops, and
        # that figure has to be the netted one.
        self._apply_entry_adjustment(result, action)
        self._finish_resolution(action)
        self.resolve_succeeded.emit(result if isinstance(result, dict) else {})

    def _apply_entry_adjustment(self, result: Any, action: str) -> None:
        """Carry the backend's deduction for the entry into the live timer.

        The resolve and reassign responses carry `time_entry_adjustment_seconds`,
        the entry's net signed adjustment after the operation. The timer
        stores it beside its start anchor and shows `measured + adjustment`,
        so "No, discard idle time" is visible on the running clock at once,
        and "Yes, keep idle time" + Resume leaves it exactly as it was. The
        client applies the number; it never works out what it should be.

        A backend that predates the field answers without it. The entry's
        own record (`GET /time-entries/active`, which carries
        `adjustment_seconds`) is then read instead -- still the server's
        figure, one round trip later.
        """
        if not isinstance(result, dict):
            return
        entry_id = result.get("time_entry_id") or self._pending_entry_id
        if not entry_id:
            return
        value = result.get("time_entry_adjustment_seconds")
        if value is not None:
            self.runtime.timer.apply_entry_adjustment(int(entry_id), value)
            return
        if action == "stop":
            return  # the session ends now; the finalized entry is re-read anyway
        service = getattr(self.runtime, "time_entry_service", None)
        if service is None or not hasattr(service, "get_active_time_entry"):
            self.log.info("no active-entry read available to refresh the adjustment")
            return

        def on_success(data: Any) -> None:
            entry = data.get("entry") if isinstance(data, dict) else None
            if isinstance(entry, dict) and "adjustment_seconds" in entry:
                self.runtime.timer.apply_entry_adjustment(
                    entry.get("id"), entry.get("adjustment_seconds")
                )

        self.runtime.tasks.submit(
            service.get_active_time_entry,
            on_success=on_success,
            on_error=lambda exc: self.log.info(
                "could not refresh the entry's adjustment after the idle answer: %s", exc
            ),
            key=f"idle-adjustment:{entry_id}",
        )

    def _on_resolve_error(self, exc: BaseException, action: str) -> None:
        status = getattr(exc, "status_code", None)
        if status == 409:
            # The server has already resolved this period — its state is the
            # authoritative one, and retrying can only conflict again. Clear
            # it so the mandatory popup cannot become unclosable, and say so
            # rather than pretending the user's answer was applied.
            self.log.warning("idle period already resolved on the backend: %s", exc)
            self._finish_resolution(action)
            self.resolve_succeeded.emit({"conflict": True})
            self.runtime.notifications.notify(
                "That idle period had already been resolved, so your answer was "
                "not applied.",
                "warning", key="idle-conflict",
            )
            return
        self.log.warning("could not resolve idle period: %s", exc)
        # Keep it pending so the user can retry. Losing the pending state here
        # would silently abandon idle time the backend still holds.
        self._state = IdleState.PENDING
        self.resolve_failed.emit(_message(exc, "Could not save your answer."))

    def _finish_resolution(self, action: str) -> None:
        """Bring local state in line with the resolution the backend applied."""
        self._pending = None
        self._pending_entry_id = None
        self._state = IdleState.MONITORING if self._idle_enabled else IdleState.DISABLED
        # A fresh idle window either way: the user has just interacted, and
        # the stretch they answered about must not be reported again.
        self._monitoring_since = time.monotonic()
        if action == "stop":
            # The backend has already stopped the entry through its own stop
            # path, so the local timer is brought down without issuing a
            # second stop that the server would only answer with a 409.
            self.runtime.timer.stop_tracking(notify_backend=False)

    # ── Reassignment (GUI thread; called by the popup) ────────────────────────

    def reassign(self, project_id: int, task_id: int) -> None:
        """Move this idle period's elapsed time to another project/task.

        The backend validates the destination against what this user is
        authorised for, writes the destination entry and the offsetting
        deduction atomically, and leaves the period **pending** — the user
        still has to answer the main popup, which is why the state returns to
        PENDING rather than clearing.
        """
        if self._state == IdleState.REASSIGNING:
            return  # already in flight
        if self._state != IdleState.PENDING or not self._pending:
            self.reassign_failed.emit("This idle period is no longer pending.")
            return
        if not project_id or not task_id:
            self.reassign_failed.emit("Select both a project and a task.")
            return

        period_id = int(self._pending["id"])
        self._state = IdleState.REASSIGNING

        self.runtime.tasks.submit(
            lambda: self._api.reassign_idle_period(period_id, int(project_id), int(task_id)),
            on_success=self._on_reassigned,
            on_error=self._on_reassign_error,
            key=f"idle-reassign:{period_id}",
        )

    def _on_reassigned(self, result: Dict[str, Any]) -> None:
        if isinstance(result, dict) and result.get("id"):
            self._pending = result
        self._state = IdleState.PENDING
        # The reassigned seconds have already been deducted from the
        # original entry, so the running clock drops by them now.
        self._apply_entry_adjustment(result, "resume")
        self.log.info(
            "idle period %s reassigned: %ss to project %s / task %s",
            (result or {}).get("id"), (result or {}).get("reassigned_seconds"),
            (result or {}).get("reassigned_project_id"),
            (result or {}).get("reassigned_task_id"),
        )
        self.reassign_succeeded.emit(result if isinstance(result, dict) else {})

    def _on_reassign_error(self, exc: BaseException) -> None:
        self.log.warning("could not reassign idle time: %s", exc)
        # The backend's reassignment is one transaction: a failure wrote
        # nothing, so the period is simply still pending and unreassigned.
        self._state = IdleState.PENDING
        self.reassign_failed.emit(_message(exc, "Could not reassign this time."))


def _message(exc: BaseException, fallback: str) -> str:
    """The backend's own explanation when there is one, else `fallback`."""
    if isinstance(exc, ApiError) and str(exc).strip():
        return str(exc)
    return fallback

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
unsupported and never fires, rather than guessing -- and says so, with the
reason, in the log and in the diagnostics it reports to the backend.

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

Nothing here can wait for ever
------------------------------
Every state that waits on the network has a deadline, and the deadline is
enforced by this service rather than trusted to the transport:

* every request has a finite timeout, and `REQUEST_DEADLINE_SECONDS` bounds a
  state (REPORTING / RESOLVING / REASSIGNING) whatever the transport does;
  a request that outlives it is abandoned -- its late success is still
  honoured, its late failure is ignored;
* a report that fails is kept and retried with jittered exponential backoff,
  never every tick, and a stretch of inactivity that could not be reported
  while the user was away is *held* and reported when it can be, instead of
  being lost when the user comes back;
* the provisional popup shown for a crash/sleep gap says what it is waiting
  for, can be told to retry now, and after `CONFIRM_DEFER_AFTER_SECONDS` can
  be put away -- the gap stays held and the popup returns once the backend
  confirms it;
* a popup the dashboard did not acknowledge is raised again.

None of this adds a retry queue or a timer: the retries are driven by this
service's own tick, which is supervised (see `core.service.ServiceManager`).
"""
from __future__ import annotations

import random
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from PySide6.QtCore import Signal, Slot

from app.api.exceptions import ApiError
from core.logging_setup import session_generation
from core.service import LoopService
from core.time_format import parse_utc as _parse_utc

#: How often inactivity is evaluated. The reading itself is two syscalls, so
#: this is deliberately unhurried — a threshold measured in minutes does not
#: need a sub-second poll, and the tick must stay cheap enough to be invisible.
POLL_INTERVAL_MS = 2000

#: How often the user's idle configuration is re-read from the backend. It is
#: seeded from `/auth/me` at login, so this is a slow correction for a value
#: an administrator changed mid-session — not a poll the feature depends on.
CONFIG_REFRESH_SECONDS = 3 * 60

#: Idle detection must not fire sooner than the user's own threshold, but the
#: backend applies a few seconds of tolerance for poll scheduling. Reporting a
#: hair early would be rejected, so the client rounds up rather than down.
DETECTION_MARGIN_SECONDS = 1

#: A state that waits on a request is abandoned after this long, whatever the
#: HTTP client is doing. The transport's timeouts are per phase (connect,
#: read, write, pool), so a server that dribbles bytes can exceed their sum;
#: this is the total, and the one the user is protected by.
REQUEST_DEADLINE_SECONDS = 45.0

#: Report retries: 2 s, 4 s, 8 s ... capped, each scaled by 50-150% so a
#: fleet that lost the backend together does not return together.
RETRY_BASE_SECONDS = 2.0
RETRY_CAP_SECONDS = 60.0

#: After a definitive refusal of an ordinary report (the backend said no, not
#: "I could not hear you"), wait this long before asking again.
REFUSED_COOLDOWN_SECONDS = 60.0

#: While the network service says the backend is unreachable, the report is
#: not attempted -- except that one attempt is made this often regardless. The
#: verdict is a probe's, and a probe can be wrong about what the real request
#: would do; a popup that waits for a verdict nothing can overturn is stuck.
UNREACHABLE_PROBE_SECONDS = 30.0

#: How long a gap waits for the user's configuration before it is reported
#: anyway. The backend holds the real threshold and refuses a report under it,
#: so reporting without knowing the local one is safe; waiting for ever is not.
CONFIG_WAIT_SECONDS = 20.0

#: What the provisional popup is told. After the first the user may press
#: "Retry now"; after the second it may be put away until the backend answers.
CONFIRM_RETRY_NOW_AFTER_SECONDS = 5.0
CONFIRM_DEFER_AFTER_SECONDS = 60.0

#: The dashboard must acknowledge a popup within this long (doubling each
#: time) or it is raised again, at most `POPUP_REPEAT_LIMIT` times, after
#: which the user is notified by the tray as well.
POPUP_ACK_GRACE_SECONDS = 4.0
POPUP_REPEAT_LIMIT = 4

#: A tick this much later than scheduled means the process was not running:
#: the machine slept, or hibernated, or was frozen.
SUSPEND_STALL_SECONDS = 60.0

#: How long an inactivity reading may be unavailable before it is reported
#: to the backend, and how often it is logged again while it stays so.
READING_REPORT_AFTER_SECONDS = 60.0
READING_RELOG_SECONDS = 300.0

#: A line in the log summarising the monitor, and a report to the backend,
#: while a timer runs.
HEALTH_LOG_SECONDS = 10 * 60
HEALTH_REPORT_SECONDS = 6 * 3600
#: The same event is not reported to the backend more often than this.
DIAGNOSTIC_MIN_INTERVAL_SECONDS = 600.0


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


class FailureKind:
    """What a failed call to the backend means for what to do next."""

    #: The backend heard the request and said no (400/403/404/409/422). Asking
    #: again with the same request cannot change the answer.
    REFUSED = "refused"
    #: 401: the session needs renewing. Not the request's fault; the token
    #: refresh and the session-expiry handling live elsewhere.
    AUTH = "auth"
    #: A timeout, a refused or dropped connection: nobody heard anything.
    NETWORK = "network"
    #: 5xx, 408, 425, 429: the backend heard and could not answer just now.
    SERVER = "server"

    TRANSIENT = frozenset({AUTH, NETWORK, SERVER})


def classify_failure(exc: BaseException) -> tuple:
    """`(FailureKind, http status or None)` for an exception from the API."""
    status = getattr(exc, "status_code", None)
    if status is None:
        return FailureKind.NETWORK, None
    if status == 401:
        return FailureKind.AUTH, status
    if status in (408, 425, 429) or status >= 500:
        return FailureKind.SERVER, status
    return FailureKind.REFUSED, status


class IdleService(LoopService):
    """
    Owns idle detection and every idle-period call to the backend.

    Signals (all delivered on the GUI thread):
        idle_period_opened(dict)     — a pending period exists; show the popup
        idle_period_cleared()        — it is gone; close the popup
        interruption_pending(dict)   — a crash-recovery or wake-from-sleep gap
                                        was detected, before the backend has
                                        issued a real idle-period id; the popup
                                        may open immediately, showing the live
                                        gap, but must not let the user act
                                        until `idle_period_opened` confirms it
        interruption_status(dict)    — what that provisional popup is waiting
                                        for (phase, message, attempt, timings)
        interruption_withdrawn()     — a gap shown via `interruption_pending`
                                        was never accepted by the backend
                                        (under threshold, entry gone, or idle
                                        detection off); close the popup
        resolve_succeeded(dict)      — the backend accepted the user's answer
        resolve_failed(str)          — it did not; the period is still pending
        reassign_succeeded(dict)     — idle time moved to another project/task
        reassign_failed(str)         — it did not; nothing was written
        config_changed(bool, int)    — idle_enabled, idle_minutes
    """

    name = "idle"

    #: Silent death of this service is a product defect (no popup, ever), so
    #: the ServiceManager checks on it. See `LoopService.check_liveness`.
    supervised = True

    idle_period_opened = Signal(dict)
    idle_period_cleared = Signal()
    interruption_pending = Signal(dict)
    interruption_status = Signal(dict)
    interruption_withdrawn = Signal()
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
    _inflight_stuck = Signal()
    _popup_unacked = Signal()
    _suspend_detected = Signal(float, str, str)
    _diagnostic_due = Signal(str, str)
    _health_due = Signal()

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
        #: A stretch of inactivity that has not been reported as an idle
        #: period yet: a power cut / crash / kill / hang gap, a sleep, or an
        #: ordinary stretch whose report could not be delivered. Set by
        #: `_on_tracking_recovered`, `_on_suspend_detected` or a failed
        #: `_on_threshold_reached`; reported once the entry id is known and
        #: the backend answers; cleared when the report lands or is
        #: definitively refused. See `_on_interruption_due`.
        self._interruption: Optional[Dict[str, Any]] = None

        # ── Request supervision ──────────────────────────────────────────────
        #: Bumped by `reset_session`: a callback from before it is ignored.
        self._epoch = 0
        #: Bumped per submission: only the newest attempt's failure counts.
        self._attempt_id = 0
        self._inflight_since: Optional[float] = None
        self._report_ctx: Optional[Dict[str, Any]] = None
        self._report_failures = 0
        self._report_next_at = 0.0
        self._last_attempt_at = 0.0
        self._last_status_key: Optional[tuple] = None
        #: The user's answer, kept so a retry after a failure repeats the same
        #: `resolved_at` and so the backend recognises it as the same answer.
        self._answer: Optional[Dict[str, Any]] = None

        # ── Popup acknowledgement ────────────────────────────────────────────
        self._popup_acked = True
        self._popup_emitted_at = 0.0
        self._popup_repeats = 0

        # ── Diagnostics ──────────────────────────────────────────────────────
        self._ticks = 0
        self._last_tick_mono: Optional[float] = None
        self._prev_tick_mono: Optional[float] = None
        self._prev_tick_wall: Optional[datetime] = None
        self._resumes = 0
        self._last_resume_at: Optional[datetime] = None
        self._loop_restarts = 0
        self._reading_failures = 0
        self._reading_failed_since: Optional[float] = None
        self._reading_logged_at = 0.0
        self._reading_reported = False
        self._last_raw_reading: Optional[float] = None
        self._longest_idle = 0.0
        self._last_input_wall: Optional[datetime] = None
        self._last_error: Optional[Dict[str, Any]] = None
        self._diag_sent: Dict[str, float] = {}
        self._health_logged_at = time.monotonic()
        self._health_reported_at = time.monotonic()

        self._threshold_reached.connect(self._on_threshold_reached)
        self._interruption_due.connect(self._on_interruption_due)
        self._entry_observed.connect(self._on_entry_observed)
        self._config_refresh_due.connect(self._refresh_config)
        self._inflight_stuck.connect(self._on_inflight_stuck)
        self._popup_unacked.connect(self._on_popup_unacked)
        self._suspend_detected.connect(self._on_suspend_detected)
        self._diagnostic_due.connect(self._send_diagnostic)
        self._health_due.connect(self._on_health_due)

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
        if not getattr(self, "_subscribed", False):
            timer.timer_started.connect(self._on_tracking_started)
            timer.timer_recovered.connect(self._on_tracking_started)
            timer.timer_recovered.connect(self._on_tracking_recovered)
            timer.timer_stopped.connect(self._on_tracking_stopped)
            self._subscribed = True
        # A fresh loop begins a fresh tick history: the gap since the last
        # tick of a loop that died is not a sleep.
        self._prev_tick_mono = None
        self._prev_tick_wall = None
        super().on_start()

    def on_stop(self, timeout_ms: int) -> bool:
        # Nothing durable to flush: a pending idle period lives on the server,
        # which is exactly why a crash or a restart cannot lose it.
        return super().on_stop(timeout_ms)

    def on_loop_restarted(self, reason: str) -> None:
        """The supervisor replaced a dead or blocked loop (GUI thread)."""
        self._loop_restarts += 1
        self.log.error(
            "IDLE_MONITOR_RESTARTED reason=%s restarts=%d state=%s", reason,
            self._loop_restarts, self._state,
        )
        self._send_diagnostic("monitor_restarted", reason)

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
        if not enabled_value:
            # Whatever was waiting to be reported is no longer wanted, and a
            # popup that was opened for it must not wait for ever on a report
            # that will now never be sent.
            self._withdraw_interruption("idle_disabled")
        self.config_changed.emit(enabled_value, minutes_value)

    @Slot()
    def _refresh_config(self) -> None:
        self._config_read_at = time.monotonic()
        epoch = self._epoch

        def on_success(config: Dict[str, Any]) -> None:
            if epoch != self._epoch:
                return
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
            guard_generation=False,
        )

    # ── Detection (worker thread) ─────────────────────────────────────────────

    def tick(self) -> Optional[int]:
        """Evaluate inactivity. Runs off the GUI thread; touches no widget.

        Everything here is a cheap read: two syscalls for the idle reading and
        a dict copy for the session. Anything that needs the network is handed
        to the GUI thread by signal and submitted to the shared task pool.
        """
        now = time.monotonic()
        self.heartbeat()
        self._ticks += 1
        self._last_tick_mono = now

        resumed = self._check_suspend(now)

        if now - self._config_read_at > CONFIG_REFRESH_SECONDS:
            self._config_read_at = now  # claim it before emitting
            self._config_refresh_due.emit()

        self._supervise(now)

        timer = self.runtime.timer
        if not timer.is_running():
            return POLL_INTERVAL_MS

        if now - self._health_logged_at > HEALTH_LOG_SECONDS:
            self._health_logged_at = now
            self._health_due.emit()

        session = timer.active_session() or {}
        entry_id = session.get("entry_id")

        # Before anything is reported: has the backend already got a pending
        # period for this entry? (A crash or a restart must bring its popup
        # back, and must never open a second.) Asked once per entry id.
        if entry_id != self._last_entry_id and self._state == IdleState.MONITORING:
            self._last_entry_id = entry_id
            if entry_id:
                self._entry_observed.emit(int(entry_id))

        if self._interruption is not None:
            # A held stretch takes precedence over fresh inactivity: it is
            # older, and the backend holds at most one pending period per
            # entry anyway. It is also handed over when idle detection has
            # since been switched off, so the slot can withdraw it rather
            # than leave a popup waiting on a report that will not be sent.
            if self._state == IdleState.MONITORING and now >= self._report_next_at:
                self._interruption_due.emit(int(entry_id or 0))
            return POLL_INTERVAL_MS

        if not self._idle_enabled:
            return POLL_INTERVAL_MS
        if self._state != IdleState.MONITORING:
            return POLL_INTERVAL_MS

        if not entry_id:
            # The start has not reached the backend yet, so there is no entry
            # to attach an idle period to. Detection resumes as soon as the
            # id arrives; no local-only idle period is invented.
            return POLL_INTERVAL_MS

        if resumed:
            return POLL_INTERVAL_MS  # the wake is handled as a gap, not a reading

        idle = self._effective_idle_seconds()
        if idle is None:
            return POLL_INTERVAL_MS
        if now < self._report_next_at:
            return POLL_INTERVAL_MS  # backing off after a failed report
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
        now = time.monotonic()
        raw = self.runtime.activity.idle_seconds()
        if raw is None:
            self._note_reading_failure(now)
            return None
        self._note_reading_ok(raw)
        return min(float(raw), now - self._monitoring_since)

    def _probe_diagnostics(self) -> Dict[str, Any]:
        probe = getattr(self.runtime.activity, "probe_diagnostics", None)
        try:
            return dict(probe()) if probe else {}
        except Exception:  # noqa: BLE001
            return {}

    def _note_reading_failure(self, now: float) -> None:
        self._reading_failures += 1
        if self._reading_failed_since is None:
            self._reading_failed_since = now
        info = self._probe_diagnostics()
        reason = info.get("failure_reason") or "unknown"
        if not self._unsupported_logged or now - self._reading_logged_at >= READING_RELOG_SECONDS:
            self._unsupported_logged = True
            self._reading_logged_at = now
            self.log.warning(
                "IDLE_READING_UNAVAILABLE reason=%s platform=%s failures=%d for=%.0fs; "
                "idle detection is inactive until the OS can answer (no value is guessed)",
                reason, info.get("platform", sys.platform), self._reading_failures,
                now - self._reading_failed_since,
            )
        if not self._reading_reported and now - self._reading_failed_since >= READING_REPORT_AFTER_SECONDS:
            self._reading_reported = True
            self._diagnostic_due.emit("reading_unavailable", str(reason)[:200])

    def _note_reading_ok(self, raw: float) -> None:
        if self._reading_failed_since is not None:
            self.log.info(
                "IDLE_READING_RECOVERED after %d failed reading(s)", self._reading_failures
            )
            if self._reading_reported:
                self._diagnostic_due.emit("reading_recovered", "")
            self._reading_failed_since = None
            self._reading_reported = False
            self._unsupported_logged = False
            self._reading_failures = 0
        previous = self._last_raw_reading
        self._last_raw_reading = raw
        self._longest_idle = max(self._longest_idle, raw)
        # The reading dropping means input arrived. When it did is what an
        # investigation needs to tell "the user was active" from "the monitor
        # was not looking".
        if previous is None or raw < previous - 1.0:
            self._last_input_wall = datetime.now(timezone.utc) - timedelta(seconds=raw)

    @property
    def supported(self) -> bool:
        return self.runtime.activity.idle_seconds() is not None

    def detection_status(self) -> Dict[str, Any]:
        """Whether idle detection is working right now, and if not, why.

        For the diagnostics report; never used to decide anything.
        """
        if not self._idle_enabled:
            return {"status": "disabled", "reason": "idle detection is switched off for this user"}
        if self._reading_failed_since is not None:
            return {"status": "unavailable",
                    "reason": self._probe_diagnostics().get("failure_reason") or "no reading"}
        return {"status": "ok", "reason": None}

    # ── Sleep / resume ────────────────────────────────────────────────────────

    def _check_suspend(self, now: float) -> bool:
        """Notice that this process was not running, and hand the gap over.

        A machine that sleeps freezes every timer in the process, this one
        included. When the first tick afterwards arrives, the time since the
        previous tick is the sleep. The documented rule is that sleep is
        inactivity (docs/TIMING_MODEL.md): it is reported as an idle period
        from the last moment this monitor was looking. That cannot be left to
        the inactivity reading alone -- the key or lid that wakes a machine
        often counts as input, so the first reading afterwards is a few
        seconds, and a night's sleep would be counted as work.

        The monotonic clock advances through a suspend on Windows; elsewhere
        it does not, so the wall clock is read as well there. (A wall clock
        set forward by hand looks like a sleep on those platforms; the user
        is then asked about a gap that did not happen, and says "yes".)
        """
        wall = datetime.now(timezone.utc)
        prev_mono, prev_wall = self._prev_tick_mono, self._prev_tick_wall
        self._prev_tick_mono, self._prev_tick_wall = now, wall
        if prev_mono is None or prev_wall is None:
            return False
        gap = now - prev_mono
        if sys.platform != "win32":
            gap = max(gap, (wall - prev_wall).total_seconds())
        if gap - POLL_INTERVAL_MS / 1000.0 < SUSPEND_STALL_SECONDS:
            return False
        self._resumes += 1
        self._last_resume_at = wall
        self.log.info(
            "IDLE_SYSTEM_RESUMED the monitor did not run for %.0fs (resume #%d)",
            gap, self._resumes,
        )
        self._suspend_detected.emit(gap, prev_wall.isoformat(), wall.isoformat())
        return True

    @Slot(float, str, str)
    def _on_suspend_detected(self, gap: float, started_iso: str, detected_iso: str) -> None:
        # Whatever the user's configuration was before the sleep may have
        # changed while the machine was off.
        self._config_refresh_due.emit()
        if not self._idle_enabled or self._interruption is not None:
            return
        if self._state != IdleState.MONITORING or not self.runtime.timer.is_running():
            return
        session = self.runtime.timer.active_session() or {}
        if self._config_loaded and gap + DETECTION_MARGIN_SECONDS < self._idle_minutes * 60:
            self.log.info(
                "IDLE_SUSPEND_UNDER_THRESHOLD gap=%.0fs threshold=%dm; nothing to reconcile",
                gap, self._idle_minutes,
            )
            return
        key = self._session_key(session)
        self._interruption = self._new_interruption(
            kind="suspend", session_key=key, client_op=session.get("client_op"),
            gap_seconds=gap, started_iso=started_iso, detected_iso=detected_iso,
            event_id=f"suspend:{key}:{started_iso}", provisional=True,
        )
        self.log.info(
            "IDLE_SUSPEND_GAP gap=%.0fs from %s; it will be reported as an idle period",
            gap, started_iso,
        )
        self._send_diagnostic("system_resumed", f"gap={int(gap)}s")
        self.interruption_pending.emit(dict(self._interruption))

    # ── Reporting (GUI thread) ────────────────────────────────────────────────

    @staticmethod
    def _session_key(session: Dict[str, Any]) -> Any:
        return session.get("client_op") or session.get("entry_id")

    def _new_interruption(
        self, *, kind: str, session_key: Any, client_op: Any, gap_seconds: float,
        started_iso: str, detected_iso: str, event_id: str, provisional: bool,
    ) -> Dict[str, Any]:
        self._last_status_key = None
        self._report_failures = 0
        self._report_next_at = 0.0
        # The clock for "how long since the backend was last asked" starts
        # now, so an unreachable-backend verdict is honoured for its first
        # `UNREACHABLE_PROBE_SECONDS` rather than overridden at once.
        self._last_attempt_at = time.monotonic()
        return {
            "kind": kind,
            "client_op": client_op,
            "session_key": session_key,
            "gap_seconds": gap_seconds,
            "idle_started_at": started_iso,
            "idle_detected_at": detected_iso,
            "client_event_id": event_id,
            "provisional": provisional,
            "recorded_mono": time.monotonic(),
            "recorded_epoch": time.time(),
        }

    def _begin_inflight(self) -> int:
        self._attempt_id += 1
        self._inflight_since = time.monotonic()
        self._last_attempt_at = self._inflight_since
        return self._attempt_id

    def _stale(self, ctx: Dict[str, Any]) -> bool:
        """True when a callback belongs to a session that no longer exists.

        The task runner would silently drop such a callback (its own
        generation guard), which leaves a state machine waiting for an answer
        that will never be delivered. This service decides instead, so the
        state it abandons is always put right.
        """
        if ctx.get("epoch") != self._epoch:
            return True
        if ctx.get("generation") != session_generation():
            self.log.info("IDLE_CALLBACK_STALE the session changed while the call was in flight")
            self._abandon_inflight()
            return True
        return False

    def _abandon_inflight(self) -> None:
        self._inflight_since = None
        if self._state in (IdleState.REPORTING, IdleState.RESOLVING, IdleState.REASSIGNING):
            self._state = IdleState.PENDING if self._pending else IdleState.MONITORING

    def _ctx(self, **extra) -> Dict[str, Any]:
        ctx = {"epoch": self._epoch, "generation": session_generation()}
        ctx.update(extra)
        return ctx

    @Slot(float)
    def _on_threshold_reached(self, idle_seconds: float) -> None:
        if self._state != IdleState.MONITORING or not self._idle_enabled:
            return
        if self._interruption is not None:
            return  # an older stretch is already waiting to be reported
        session = self.runtime.timer.active_session() or {}
        entry_id = session.get("entry_id")
        if not entry_id or not self.runtime.timer.is_running():
            return

        now = datetime.now(timezone.utc)
        started_at = (now - timedelta(seconds=idle_seconds)).replace(microsecond=0)
        idle_started_at = started_at.isoformat()
        # Stable for this stretch of inactivity (to the second), so a retry of
        # the same report returns the period that already exists rather than
        # opening a second.
        client_event_id = f"idle:{entry_id}:{idle_started_at}"
        self.log.info(
            "idle threshold reached: %.0fs idle on entry %s (threshold %dm)",
            idle_seconds, entry_id, self._idle_minutes,
        )
        self._submit_report(
            int(entry_id), idle_started_at, now.isoformat(), client_event_id,
            origin="threshold", session_key=self._session_key(session),
            client_op=session.get("client_op"),
        )

    def _submit_report(
        self, entry_id: int, started_iso: str, detected_iso: str, event_id: str,
        *, origin: str, session_key: Any, client_op: Any,
    ) -> None:
        self._state = IdleState.REPORTING
        attempt = self._begin_inflight()
        ctx = self._ctx(
            origin=origin, entry_id=entry_id, started=started_iso,
            detected=detected_iso, event_id=event_id, attempt=attempt,
            session_key=session_key, client_op=client_op,
        )
        self._report_ctx = ctx

        def call():
            return self._api.report_idle_period(
                time_entry_id=entry_id,
                idle_started_at=started_iso,
                idle_detected_at=detected_iso,
                client_event_id=event_id,
                # The client's clock as it leaves, so the backend can place
                # both instants by age and a skewed clock is harmless.
                client_time=datetime.now(timezone.utc).isoformat(),
            )

        self.runtime.tasks.submit(
            call,
            on_success=lambda period: self._on_report_ok(period, ctx),
            on_error=lambda exc: self._on_report_failed(exc, ctx),
            key=f"idle-report:{event_id}:{attempt}",
            guard_generation=False,
        )

    def _on_report_ok(self, period: Optional[Dict[str, Any]], ctx: Dict[str, Any]) -> None:
        if self._stale(ctx):
            return
        entry_id = ctx["entry_id"]
        # The period belongs to an entry that is still the one running. A
        # timer stopped (or switched) while the report was in flight has had
        # its pending period resolved by the backend already; a popup for it
        # would ask about a timer that no longer exists.
        session = (self.runtime.timer.active_session() or {}) if self.runtime.timer.is_running() else {}
        if session.get("entry_id") != entry_id:
            self.log.info(
                "IDLE_REPORT_DISCARDED entry %s is no longer the running entry", entry_id
            )
            if self._state == IdleState.REPORTING:
                self._state = IdleState.MONITORING
            self._inflight_since = None
            return
        self._inflight_since = None
        self._report_failures = 0
        self._report_next_at = 0.0
        self._last_error = None
        if ctx["origin"] != "threshold" and self._interruption is not None \
                and self._interruption.get("client_event_id") == ctx["event_id"]:
            self._interruption = None
            self._last_status_key = None
        if isinstance(period, dict) and period.get("status") not in (None, "pending"):
            # The backend answered a repeat of a report with the period it
            # already holds -- and that one has been answered. There is
            # nothing to ask the user; a popup for it would only be refused.
            self.log.info(
                "IDLE_REPORT_ALREADY_RESOLVED period %s is %s; no popup",
                period.get("id"), period.get("status"),
            )
            if self._state == IdleState.REPORTING:
                self._state = IdleState.MONITORING
            if self._interruption is None:
                self.interruption_withdrawn.emit()
            return
        self._adopt_pending(period, entry_id)

    def _on_report_failed(self, exc: BaseException, ctx: Dict[str, Any]) -> None:
        """A report that did not land leaves nothing behind -- except the
        stretch itself, which is held and reported when the backend can hear.

        No popup is shown for an idle period the backend does not hold: the
        popup's whole contract is that the server has something pending to
        resolve.
        """
        if self._stale(ctx):
            return
        if ctx.get("attempt") != self._attempt_id:
            return  # a newer attempt owns the state now
        self._inflight_since = None
        kind, status = classify_failure(exc)
        self._record_error(kind, status, exc)
        if self._state == IdleState.REPORTING:
            self._state = IdleState.MONITORING
        origin = ctx["origin"]

        if kind == FailureKind.REFUSED:
            self.log.warning(
                "IDLE_REPORT_REFUSED origin=%s status=%s entry=%s: %s",
                origin, status, ctx["entry_id"], exc,
            )
            # A refusal may mean the local threshold or enabled flag is stale.
            self._config_refresh_due.emit()
            if origin == "threshold":
                self._report_next_at = time.monotonic() + REFUSED_COOLDOWN_SECONDS
            else:
                self._withdraw_interruption(f"refused_{status}")
            return

        self._report_failures += 1
        delay = self._backoff(self._report_failures)
        self._report_next_at = time.monotonic() + delay
        self.log.warning(
            "IDLE_REPORT_FAILED origin=%s kind=%s status=%s attempt=%d retry_in=%.0fs: %s",
            origin, kind, status, self._report_failures, delay, exc,
        )
        if self._report_failures in (3, 10, 30):
            self._send_diagnostic("report_failed", f"{kind} status={status} attempts={self._report_failures}")
        if origin == "threshold" and self._interruption is None:
            # The user is (or was) idle and the backend could not be told.
            # Hold the stretch, with the identity it already has, so that
            # coming back to the keyboard does not make it vanish.
            self._interruption = self._new_interruption(
                kind="held", session_key=ctx["session_key"], client_op=ctx.get("client_op"),
                gap_seconds=(_parse_utc(ctx["detected"]) - _parse_utc(ctx["started"])).total_seconds(),
                started_iso=ctx["started"], detected_iso=ctx["detected"],
                event_id=ctx["event_id"], provisional=False,
            )
            self._report_failures = 1
            self._report_next_at = time.monotonic() + delay
            self.log.info("IDLE_STRETCH_HELD kept for the next attempt (%s)", ctx["event_id"])
        self._publish_status(
            "auth" if kind == FailureKind.AUTH else "retrying",
            self._retry_message(kind, delay), attempt=self._report_failures,
        )

    @staticmethod
    def _retry_message(kind: str, delay: float) -> str:
        if kind == FailureKind.AUTH:
            return (
                "Your sign-in needs to be renewed. Monitra is trying again; "
                "if this stays, sign in again."
            )
        return f"Couldn't reach the server. Trying again in {int(round(delay))}s…"

    @staticmethod
    def _backoff(failures: int) -> float:
        base = min(RETRY_CAP_SECONDS, RETRY_BASE_SECONDS * (2 ** max(0, failures - 1)))
        return base * random.uniform(0.5, 1.5)

    def _record_error(self, kind: str, status: Optional[int], exc: BaseException) -> None:
        self._last_error = {
            "kind": kind, "status": status, "at": time.time(),
            "message": str(exc)[:200],
        }

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
        self._popup_acked = False
        self._popup_emitted_at = time.monotonic()
        self._popup_repeats = 0
        self.log.info("idle period %s pending on entry %s", period.get("id"), entry_id)
        self.idle_period_opened.emit(dict(period))

    def popup_shown(self, period_id: Optional[int] = None) -> None:
        """The dashboard has put the popup on screen (or unlocked it).

        Until it says so, the service does not assume the popup exists: a
        dialog whose construction raised, or that Windows hid, is otherwise
        indistinguishable from one the user is looking at.
        """
        if self._pending and (period_id is None or self._pending.get("id") == period_id):
            self._popup_acked = True

    @Slot()
    def _on_popup_unacked(self) -> None:
        if self._state != IdleState.PENDING or not self._pending or self._popup_acked:
            return
        waited = time.monotonic() - self._popup_emitted_at
        if waited < POPUP_ACK_GRACE_SECONDS * (2 ** self._popup_repeats):
            return
        self._popup_repeats += 1
        self._popup_emitted_at = time.monotonic()
        self.log.error(
            "IDLE_POPUP_NOT_ACKNOWLEDGED period=%s waited=%.0fs attempt=%d/%d; raising it again",
            self._pending.get("id"), waited, self._popup_repeats, POPUP_REPEAT_LIMIT,
        )
        if self._popup_repeats == 1:
            self._send_diagnostic("popup_failed", f"period={self._pending.get('id')}")
        notifications = getattr(self.runtime, "notifications", None)
        if self._popup_repeats >= 2 and notifications is not None:
            # The popup itself is not getting through; the tray is a second,
            # independent channel.
            notifications.notify(
                "You have been idle. Open Monitra to say whether to keep that time.",
                "warning", key="idle-alert-fallback",
            )
        if self._popup_repeats <= POPUP_REPEAT_LIMIT:
            self.idle_period_opened.emit(dict(self._pending))
        else:
            self._popup_acked = True  # stop; the log and the tray have said it

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

    # ── In-flight supervision ─────────────────────────────────────────────────

    def _supervise(self, now: float) -> None:
        """Worker-thread watch over every state that waits (cheap reads only)."""
        state = self._state
        if state in (IdleState.REPORTING, IdleState.RESOLVING, IdleState.REASSIGNING):
            since = self._inflight_since
            if since is not None and now - since > REQUEST_DEADLINE_SECONDS:
                self._inflight_stuck.emit()
        elif state == IdleState.PENDING and not self._popup_acked:
            if now - self._popup_emitted_at >= POPUP_ACK_GRACE_SECONDS * (2 ** self._popup_repeats):
                self._popup_unacked.emit()

    @Slot()
    def _on_inflight_stuck(self) -> None:
        since = self._inflight_since
        state = self._state
        if since is None or state not in (
            IdleState.REPORTING, IdleState.RESOLVING, IdleState.REASSIGNING,
        ):
            return
        waited = time.monotonic() - since
        if waited <= REQUEST_DEADLINE_SECONDS:
            return
        self.log.error(
            "IDLE_INFLIGHT_TIMEOUT state=%s waited=%.0fs; abandoning the request and recovering",
            state, waited,
        )
        self._send_diagnostic("inflight_stuck", f"{state} {int(waited)}s")
        # Supersede: the abandoned request's late *failure* is ignored, while
        # its late *success* is still honoured -- the backend did the work.
        self._attempt_id += 1
        self._inflight_since = None
        timeout = ApiError("The server did not answer in time.")
        self._record_error(FailureKind.NETWORK, None, timeout)
        if state == IdleState.REPORTING:
            ctx = self._report_ctx or {}
            self._state = IdleState.MONITORING
            if ctx and ctx.get("epoch") == self._epoch:
                self._on_report_failed_after_timeout(ctx, timeout)
        elif state == IdleState.RESOLVING:
            self._state = IdleState.PENDING
            self.resolve_failed.emit("The server did not answer in time.")
        else:
            self._state = IdleState.PENDING
            self.reassign_failed.emit("The server did not answer in time.")

    def recover_inflight(self) -> None:
        """Give up on whatever request is in flight now (GUI thread).

        For the popup's own last-resort timer: it has been showing "Resuming…"
        for longer than any request can legitimately take.
        """
        if self._inflight_since is not None:
            self._inflight_since = time.monotonic() - REQUEST_DEADLINE_SECONDS - 1.0
            self._on_inflight_stuck()

    def _on_report_failed_after_timeout(self, ctx: Dict[str, Any], exc: BaseException) -> None:
        # Same handling as any transient failure, for the attempt just abandoned.
        ctx = dict(ctx)
        ctx["attempt"] = self._attempt_id
        self._on_report_failed(exc, ctx)

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
        epoch = self._epoch

        def on_error(exc: BaseException) -> None:
            self.log.info(
                "could not check for a pending idle period on entry %s: %s", entry_id, exc
            )
            # Not an answer: look again at the next opportunity rather than
            # never. (A pending period the client never hears about is a
            # popup that never appears.)
            if epoch == self._epoch:
                self._recovery_checked.discard(entry_id)

        self.runtime.tasks.submit(
            lambda: self._api.get_pending_idle_period(entry_id),
            on_success=lambda period: self._adopt_recovered(period, entry_id, epoch),
            on_error=on_error,
            key=f"idle-pending:{entry_id}",
            guard_generation=False,
        )

    def _adopt_recovered(self, period: Optional[Dict[str, Any]], entry_id: int, epoch: Optional[int] = None) -> None:
        if epoch is not None and epoch != self._epoch:
            return
        if not period:
            return
        if self._state != IdleState.MONITORING:
            return  # something else already owns the state
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
    # A sleep (`_on_suspend_detected`) and an ordinary stretch whose report
    # could not be delivered are held in exactly the same place and reported
    # the same way.
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
        if not self._idle_enabled:
            # No popup for a user whose idle detection is off. (Before this
            # one was opened and could never be confirmed or withdrawn: the
            # gap is only ever reported for an enabled user.)
            self.log.info(
                "recovered session was interrupted for %.0fs; idle detection is off "
                "for this user, nothing to reconcile", gap,
            )
            self._interruption = None
            return
        # The threshold is applied when the report is about to be sent, not
        # here: the user's configuration may not have been read yet at the
        # instant of recovery, and judging the gap against the default was
        # measured to drop a real 85-second outage for a one-minute user.
        self._interruption = self._new_interruption(
            kind="interruption", session_key=client_op, client_op=client_op,
            gap_seconds=gap, started_iso=interrupted_at.isoformat(),
            detected_iso=recovered_at.isoformat(),
            event_id=f"interruption:{client_op}:{interrupted_at.isoformat()}",
            provisional=True,
        )
        self.log.info(
            "recovered session was interrupted for %.0fs; it will be reported as an "
            "idle period if it reaches the user's idle threshold", gap,
        )
        # Shown instantly, before the backend has confirmed anything: the
        # popup may open now with the live gap, but nothing here asserts
        # this is the final number -- only `idle_period_opened`, once the
        # backend answers, may be acted on.
        self.interruption_pending.emit(dict(self._interruption))

    def _network_usable(self) -> bool:
        network = getattr(self.runtime, "network", None)
        if network is None:
            return True
        state = getattr(network, "network_state", None)
        if state is None:
            return True
        from background_services.network import NetworkState
        return state in NetworkState.USABLE or state == NetworkState.UNKNOWN

    def _no_route(self) -> bool:
        network = getattr(self.runtime, "network", None)
        state = getattr(network, "network_state", None)
        return state == "NO_NETWORK"

    def _publish_status(self, phase: str, message: str, *, attempt: int = 0) -> None:
        """Tell the provisional popup what it is waiting for -- on change only."""
        intr = self._interruption
        if intr is None or not intr.get("provisional"):
            return
        key = (phase, attempt)
        if key == self._last_status_key:
            return
        self._last_status_key = key
        self.interruption_status.emit({
            "phase": phase,
            "message": message,
            "attempt": attempt,
            "since_epoch": intr.get("recorded_epoch", time.time()),
            "retry_after": CONFIRM_RETRY_NOW_AFTER_SECONDS,
            "defer_after": CONFIRM_DEFER_AFTER_SECONDS,
        })

    def _withdraw_interruption(self, reason: str) -> None:
        """The one way a held stretch ends without being reported.

        Every silent clear that used to be scattered through this module left
        the provisional popup locked on "Confirming with the server…" for
        ever; routing them here makes the popup's closure and the log line
        impossible to forget.
        """
        intr, self._interruption = self._interruption, None
        if intr is None:
            return
        self.log.info(
            "IDLE_INTERRUPTION_WITHDRAWN reason=%s kind=%s gap=%.0fs",
            reason, intr.get("kind", "?"), float(intr.get("gap_seconds") or 0),
        )
        self._last_status_key = None
        if intr.get("provisional", True):
            self.interruption_withdrawn.emit()

    def retry_interruption_now(self) -> None:
        """The user asked the provisional popup to try again immediately."""
        if self._interruption is None:
            return
        self.log.info("IDLE_RETRY_NOW requested for the held stretch")
        self._report_next_at = 0.0
        self._last_attempt_at = 0.0
        self._last_status_key = None
        self.wake()

    def defer_interruption(self) -> None:
        """Put the provisional popup away; keep the stretch and keep trying.

        Nothing is counted or discarded by this: the entry keeps running as
        it was, the gap stays held, and when the backend accepts the report
        the popup comes back as an ordinary confirmed one. It exists so that
        a backend that cannot be reached does not make Monitra unusable.
        """
        intr = self._interruption
        if intr is None:
            return
        intr["provisional"] = False
        self.log.warning(
            "IDLE_INTERRUPTION_DEFERRED by the user after %.0fs; the stretch stays held",
            time.monotonic() - float(intr.get("recorded_mono", time.monotonic())),
        )

    @Slot(int)
    def _on_interruption_due(self, entry_id: int) -> None:
        """Report the held stretch as an idle period, once."""
        interruption = self._interruption
        if interruption is None:
            return
        if not self.runtime.timer.is_running():
            return  # `_on_tracking_stopped` withdraws it
        session = self.runtime.timer.active_session() or {}
        key = interruption.get("session_key", interruption.get("client_op"))
        if key is not None and self._session_key(session) != key:
            # The session the gap belonged to is gone; nothing to reconcile.
            self._withdraw_interruption("session_changed")
            return
        if not self._idle_enabled:
            self._withdraw_interruption("idle_disabled")
            return
        if self._state != IdleState.MONITORING:
            return  # a request is in flight; its outcome decides what next
        now = time.monotonic()
        if now < self._report_next_at:
            return

        recorded = float(interruption.get("recorded_mono", now))
        kind = interruption.get("kind", "interruption")
        entry_id = session.get("entry_id")
        if not entry_id:
            self._publish_status(
                "waiting_entry", "Waiting for your timer to finish syncing…",
            )
            return
        if kind != "held" and not self._config_loaded:
            if now - recorded < CONFIG_WAIT_SECONDS:
                self._publish_status("waiting_config", "Checking your idle settings…")
                if now - self._config_read_at > 5.0:
                    self._config_refresh_due.emit()
                return
            # Reported without a local threshold: the backend holds the real
            # one and answers 400 for a gap under it.
        elif kind != "held" and (
            float(interruption["gap_seconds"]) + DETECTION_MARGIN_SECONDS < self._idle_minutes * 60
        ):
            self.log.info(
                "recovered session was interrupted for %.0fs, under the user's %dm idle "
                "threshold; nothing to reconcile",
                interruption["gap_seconds"], self._idle_minutes,
            )
            self._withdraw_interruption("under_threshold")
            return
        if not self._network_usable() and now - self._last_attempt_at < UNREACHABLE_PROBE_SECONDS:
            self._publish_status(
                "waiting_network", "Waiting for the connection to come back…",
            )
            return
        if self._no_route() and now - self._last_attempt_at < UNREACHABLE_PROBE_SECONDS:
            self._publish_status(
                "waiting_network", "Waiting for the connection to come back…",
            )
            return

        self.log.info(
            "reporting %s %s -> %s (%.0fs, threshold %dm) on entry %s",
            kind, interruption["idle_started_at"], interruption["idle_detected_at"],
            interruption["gap_seconds"], self._idle_minutes, entry_id,
        )
        self._publish_status("sending", "Confirming with the server…", attempt=self._report_failures)
        self._submit_report(
            int(entry_id), interruption["idle_started_at"], interruption["idle_detected_at"],
            interruption["client_event_id"], origin=kind,
            session_key=self._session_key(session), client_op=session.get("client_op"),
        )

    # ── Tracking lifecycle ────────────────────────────────────────────────────

    def _on_tracking_started(self, session: dict) -> None:
        """A new (or recovered) tracking session begins a fresh idle window."""
        self._monitoring_since = time.monotonic()
        self._last_entry_id = None
        self._report_failures = 0
        self._report_next_at = 0.0
        if self._state in (IdleState.MONITORING, IdleState.REPORTING):
            self._state = IdleState.MONITORING
            self._inflight_since = None

    def _on_tracking_stopped(self, _payload: dict) -> None:
        """The timer stopped, so any pending period is no longer this popup's.

        The backend resolves a still-pending period as discarded when the
        entry stops — the same outcome as the user pressing Stop — so the
        popup is dismissed rather than left demanding an answer about a timer
        that is no longer running.
        """
        self._monitoring_since = time.monotonic()
        self._last_entry_id = None
        self._withdraw_interruption("timer_stopped")
        if self._pending is not None:
            self.log.info(
                "timer stopped with idle period %s pending; the backend "
                "resolves it as discarded",
                self._pending.get("id"),
            )
        self._answer = None
        self._inflight_since = None
        self._clear_pending()

    def _clear_pending(self) -> None:
        had_pending = self._pending is not None
        self._pending = None
        self._pending_entry_id = None
        self._popup_acked = True
        self._state = IdleState.MONITORING if self._idle_enabled else IdleState.DISABLED
        if had_pending:
            self.idle_period_cleared.emit()

    def reset_session(self) -> None:
        """Drop all session-scoped state. Called on logout."""
        self._epoch += 1
        self._recovery_checked.clear()
        self._last_entry_id = None
        self._withdraw_interruption("session_reset")
        self._answer = None
        self._inflight_since = None
        self._report_ctx = None
        self._report_failures = 0
        self._report_next_at = 0.0
        self._config_loaded = False
        self._config_read_at = time.monotonic()
        self._monitoring_since = time.monotonic()
        self._last_error = None
        self._clear_pending()

    # ── Resolution (GUI thread; called by the popup) ──────────────────────────

    def resolve(self, keep_idle_time: bool, action: str) -> None:
        """Send the user's answer to the backend.

        The server decides the outcome: idle time counts exactly when the
        user chose to keep it, for either action. `action="stop"` stops the
        time entry through the backend's own stop path, so there is no second
        stop implementation here.

        Guarded by state, so a double-clicked button sends one request. A
        failed request leaves the period pending and the answer re-sendable:
        the backend treats a repeat of the same answer as the same answer, so
        a request that was in fact processed (and whose reply was lost) is
        confirmed by sending it again, never applied twice.
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
        answer = (bool(keep_idle_time), action)
        if not self._answer or self._answer.get("period_id") != period_id or self._answer.get("answer") != answer:
            self._answer = {
                "period_id": period_id, "answer": answer,
                "resolved_at": datetime.now(timezone.utc).isoformat(),
            }
        resolved_at = self._answer["resolved_at"]
        self._state = IdleState.RESOLVING
        attempt = self._begin_inflight()
        ctx = self._ctx(attempt=attempt, period_id=period_id)

        def call():
            return self._api.resolve_idle_period(
                period_id, bool(keep_idle_time), action, resolved_at
            )

        self.runtime.tasks.submit(
            call,
            on_success=lambda result: self._on_resolve_ok(result, action, ctx),
            on_error=lambda exc: self._on_resolve_error(exc, action, ctx),
            key=f"idle-resolve:{period_id}:{attempt}",
            guard_generation=False,
        )

    def _on_resolve_ok(self, result: Dict[str, Any], action: str, ctx: Dict[str, Any]) -> None:
        if self._stale(ctx):
            return
        if not self._pending or int(self._pending["id"]) != ctx["period_id"]:
            # Late success for a period this session no longer holds (the
            # timer stopped first, or the user signed out): nothing to finish.
            self.log.info("IDLE_RESOLVE_LATE_SUCCESS ignored: period %s is not pending", ctx["period_id"])
            return
        self._on_resolved(result, action)

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
            guard_generation=False,
        )

    def _on_resolve_error(self, exc: BaseException, action: str, ctx: Optional[Dict[str, Any]] = None) -> None:
        if ctx is not None:
            if self._stale(ctx):
                return
            if ctx.get("attempt") != self._attempt_id:
                return  # abandoned by the deadline; a newer state owns this
        self._inflight_since = None
        kind, status = classify_failure(exc)
        self._record_error(kind, status, exc)
        if status == 409:
            # The server has already resolved this period — its state is the
            # authoritative one, and retrying can only conflict again. Clear
            # it so the mandatory popup cannot become unclosable, and say so
            # rather than pretending the user's answer was applied.
            self.log.warning("idle period already resolved on the backend: %s", exc)
            entry_id = self._pending_entry_id
            self._finish_resolution(action)
            if entry_id:
                # The figure on the running clock may predate whatever the
                # other resolution deducted; take the backend's.
                self._apply_entry_adjustment({"time_entry_id": entry_id}, "resume")
            self.resolve_succeeded.emit({"conflict": True})
            self.runtime.notifications.notify(
                "That idle period had already been resolved, so your answer was "
                "not applied.",
                "warning", key="idle-conflict",
            )
            return
        self.log.warning(
            "IDLE_RESOLVE_FAILED kind=%s status=%s: %s", kind, status, exc
        )
        # Keep it pending so the user can retry. Losing the pending state here
        # would silently abandon idle time the backend still holds.
        self._state = IdleState.PENDING
        self.resolve_failed.emit(_message(exc, "Could not save your answer."))

    def _finish_resolution(self, action: str) -> None:
        """Bring local state in line with the resolution the backend applied."""
        self._pending = None
        self._pending_entry_id = None
        self._answer = None
        self._inflight_since = None
        self._popup_acked = True
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

        A reassignment has no idempotency key of its own: repeating it after
        a reply was lost is answered 409 "already reassigned". So an
        uncertain failure is not reported as a failure -- the period is read
        back, and if the backend did apply it the result is taken from there.
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
        attempt = self._begin_inflight()
        ctx = self._ctx(attempt=attempt, period_id=period_id)

        self.runtime.tasks.submit(
            lambda: self._api.reassign_idle_period(period_id, int(project_id), int(task_id)),
            on_success=lambda result: self._on_reassigned_ok(result, ctx),
            on_error=lambda exc: self._on_reassign_error(exc, ctx),
            key=f"idle-reassign:{period_id}:{attempt}",
            guard_generation=False,
        )

    def _on_reassigned_ok(self, result: Dict[str, Any], ctx: Dict[str, Any]) -> None:
        if self._stale(ctx):
            return
        if not self._pending or int(self._pending["id"]) != ctx["period_id"]:
            return
        self._inflight_since = None
        self._on_reassigned(result)

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

    def _on_reassign_error(self, exc: BaseException, ctx: Optional[Dict[str, Any]] = None) -> None:
        if ctx is not None:
            if self._stale(ctx):
                return
            if ctx.get("attempt") != self._attempt_id:
                return
        kind, status = classify_failure(exc)
        self._record_error(kind, status, exc)
        self.log.warning("IDLE_REASSIGN_FAILED kind=%s status=%s: %s", kind, status, exc)
        entry_id = self._pending_entry_id
        # Uncertain: nothing heard (the request may have been applied), or 409
        # (a repeat of one that was). Ask the backend what it holds.
        if ctx is not None and entry_id and (kind != FailureKind.REFUSED or status == 409):
            self._reconcile_reassign(exc, ctx, int(entry_id))
            return
        # The backend's reassignment is one transaction: a refusal wrote
        # nothing, so the period is simply still pending and unreassigned.
        self._inflight_since = None
        self._state = IdleState.PENDING
        self.reassign_failed.emit(_message(exc, "Could not reassign this time."))

    def _reconcile_reassign(self, exc: BaseException, ctx: Dict[str, Any], entry_id: int) -> None:
        period_id = ctx["period_id"]
        attempt = self._begin_inflight()
        recon = self._ctx(attempt=attempt, period_id=period_id)

        def on_success(period: Optional[Dict[str, Any]]) -> None:
            if self._stale(recon) or recon["attempt"] != self._attempt_id:
                return
            self._inflight_since = None
            if isinstance(period, dict) and period.get("id") == period_id and period.get("reassigned"):
                self.log.info("IDLE_REASSIGN_RECONCILED the backend had applied it")
                self._on_reassigned(period)
                return
            self._state = IdleState.PENDING
            self.reassign_failed.emit(_message(exc, "Could not reassign this time."))

        def on_error(_exc: BaseException) -> None:
            if self._stale(recon) or recon["attempt"] != self._attempt_id:
                return
            self._inflight_since = None
            self._state = IdleState.PENDING
            self.reassign_failed.emit(_message(exc, "Could not reassign this time."))

        self.runtime.tasks.submit(
            lambda: self._api.get_pending_idle_period(entry_id),
            on_success=on_success, on_error=on_error,
            key=f"idle-reassign-check:{period_id}:{attempt}",
            guard_generation=False,
        )

    # ── Diagnostics ───────────────────────────────────────────────────────────

    def diagnostics(self) -> Dict[str, Any]:
        """A snapshot of the monitor for the log and the backend report.

        Counts, states and short reason codes only: no window title, URL,
        application name, token or any text a user wrote.
        """
        now = time.monotonic()
        info = self._probe_diagnostics()
        error = self._last_error or {}
        data: Dict[str, Any] = {
            "state": self._state,
            "service_state": self.health.state,
            "idle_enabled": self._idle_enabled,
            "idle_minutes": self._idle_minutes,
            "config_loaded": self._config_loaded,
            "platform": sys.platform,
            "reading_supported": bool(info.get("supported", True)),
            "reading_failure": info.get("failure_reason"),
            "reading_failures": int(info.get("total_failures", 0) or 0),
            "seconds_since_tick": round(now - self._last_tick_mono, 1) if self._last_tick_mono else None,
            "seconds_since_input": (
                round((datetime.now(timezone.utc) - self._last_input_wall).total_seconds(), 1)
                if self._last_input_wall else None
            ),
            "longest_idle_seconds": round(self._longest_idle, 1),
            "restarts": self._loop_restarts,
            "resumes": self._resumes,
            "report_failures": self._report_failures,
            "last_error_kind": error.get("kind"),
            "last_error_status": error.get("status"),
            "pending_period_id": (self._pending or {}).get("id"),
        }
        latency = getattr(self._api, "last_latency_ms", None)
        if latency is not None:
            data["last_api_latency_ms"] = int(latency)
        return data

    @Slot()
    def _on_health_due(self) -> None:
        self.log.info(
            "IDLE_HEALTH %s",
            " ".join(f"{k}={v}" for k, v in self.diagnostics().items() if v is not None),
        )
        now = time.monotonic()
        if now - self._health_reported_at >= HEALTH_REPORT_SECONDS:
            self._health_reported_at = now
            self._send_diagnostic("health", "")

    @Slot(str, str)
    def _send_diagnostic(self, event: str, detail: str = "") -> None:
        """Best-effort health report to the backend's log. Never retried, never
        queued, never allowed to matter: losing one costs nothing."""
        send = getattr(self._api, "send_diagnostics", None)
        if send is None:
            return
        now = time.monotonic()
        last = self._diag_sent.get(event)
        if last is not None and now - last < DIAGNOSTIC_MIN_INTERVAL_SECONDS:
            return
        self._diag_sent[event] = now
        payload = {k: v for k, v in self.diagnostics().items() if v is not None}
        payload["event"] = event
        if detail:
            payload["detail"] = detail[:200]
        try:
            from version import VERSION
            payload["app_version"] = VERSION
        except Exception:  # noqa: BLE001
            pass
        self.runtime.tasks.submit(
            lambda: send(payload),
            on_error=lambda exc: self.log.info("idle diagnostics not delivered: %s", exc),
            key=f"idle-diag:{event}",
            guard_generation=False,
        )


def _message(exc: BaseException, fallback: str) -> str:
    """The backend's own explanation when there is one, else `fallback`."""
    if isinstance(exc, ApiError) and str(exc).strip():
        return str(exc)
    return fallback

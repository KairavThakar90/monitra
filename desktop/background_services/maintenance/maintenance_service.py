"""
maintenance_service — tells the user when Monitra is under maintenance.

What this is, and what it is not
--------------------------------
An administrator can switch a product-wide *notice* on and off. While it is
on, the desktop shows a small, non-blocking card ("Monitra is under
maintenance … ● OFFLINE") and says so once through the tray. That is the whole
effect. This service is deliberately connected to nothing else in the
runtime: it does not read the timer, the trackers, the screenshot scheduler,
the sync consumer or the network service's state machine, and none of them
read it. A timer running at 10:00 when the notice appears is the same timer,
with the same `started_at_utc`, at 11:00 when it clears. Nothing is split,
paused, deducted or restarted -- there is no code path here that could.

Design constraints, all from DO_NOT_DO.md:

**No new mechanism.** A `LoopService` like every other periodic service,
registered with `ApplicationRuntime`, notifying through the
`NotificationService` that already owns notifications. No thread, timer,
cache or queue of its own.

**Edge-triggered, never level-triggered.** The backend answers "true" on every
poll for as long as the notice is on. `maintenance_changed` fires only when
the answer *changes* -- once when it goes on, once when it goes off -- so a
window cannot be re-shown or a toast re-sent on every poll.

**The backend decides.** "Under maintenance" is the administrator's switch as
the backend reports it. It is never inferred from an outage: the network
service owns "offline", and the two are different facts. A poll that fails
leaves the last known answer exactly where it was -- the notice is not
cleared because the backend could not be reached, and not raised because it
could not be reached either.

**It holds instead of failing.** Signed out, offline, or a backend without
the endpoint (an older deployment) are all reasons to wait quietly. A failed
check must never affect tracking.
"""
from __future__ import annotations

import random
from typing import Any, Dict, Optional

from PySide6.QtCore import Signal

from app.api.exceptions import ApiError
from app.maintenance.service import MaintenanceApiService
from background_services.network import NetworkState
from background_services.notifications import NotificationLevel
from core.service import LoopService, ServiceState

#: The notice, word for word, so the desktop card, the tray message and the
#: tests all say the same thing. Deliberately calm: it says the work is safe
#: and that Monitra is still running, and it names no failure.
MAINTENANCE_TITLE = "Monitra is under maintenance"
MAINTENANCE_BODY = (
    "We're currently performing maintenance. Your activity is being saved "
    "safely offline and will sync automatically when connectivity is available."
)
MAINTENANCE_STATUS_LABEL = "OFFLINE"
MAINTENANCE_CLEARED_MESSAGE = "Maintenance is complete. Monitra is back to normal."

#: Tray notification keys. One per edge, so the notification service's
#: de-duplication is a second guard against a repeat, never the first.
NOTIFY_KEY_ON = "maintenance-mode:on"
NOTIFY_KEY_OFF = "maintenance-mode:off"


class MaintenanceService(LoopService):
    """
    Periodically asks the backend whether the maintenance notice is on, and
    reports each *change* of that answer.

    Signals:
        maintenance_changed(bool) — the notice went on (True) or off (False).
            Emitted on a transition only, never on a poll that answered the
            same as the last one. The first answer of a session is emitted
            only if it is True (there is nothing to clear otherwise).
    """

    name = "maintenance"

    maintenance_changed = Signal(bool)

    #: The same cadence as the dashboard's sync probe: a change made by an
    #: administrator reaches an open desktop within about half a minute.
    CHECK_INTERVAL_MS = 30 * 1000
    #: While signed out, offline or after a failed check.
    HOLD_INTERVAL_MS = 30 * 1000
    #: After tick() raised (a bug, not a network failure): back off harder.
    error_interval_ms = 60 * 1000
    #: Room for startup to restore the session and run its first probe.
    FIRST_CHECK_DELAY_MS = 5 * 1000
    interval_ms = CHECK_INTERVAL_MS
    #: One TIMEOUT_FAST request is the whole blocking budget of a tick.
    stop_timeout_ms = 8000

    def __init__(self, runtime, maintenance_api: MaintenanceApiService, parent=None) -> None:
        super().__init__(runtime, parent)
        self._maintenance_api = maintenance_api
        #: The backend's last answer this session. None until it has answered.
        self._known: Optional[bool] = None
        self._first_tick_done = False

    # ── Read side ─────────────────────────────────────────────────────────

    @property
    def maintenance_mode(self) -> bool:
        """Whether the notice is currently on, as last reported. False until
        the backend has answered -- an unknown is never shown as a notice."""
        return bool(self._known)

    # ── Requests from the runtime ─────────────────────────────────────────

    def check_now(self) -> None:
        """Ask now rather than at the next interval (safe from any thread).

        Used on login and on the network's recovery edge, so a notice that
        went on or off while this client was signed out or unreachable is
        reflected as soon as the backend can be asked again.
        """
        self.wake()

    def reset_session(self) -> None:
        """Forget the last answer, on logout.

        If the notice was showing, it is cleared through the same edge the UI
        already handles -- there is no second "hide" path. The next sign-in
        starts from unknown, so a notice still on is shown again, once.
        """
        was_on = self._known
        self._known = None
        if was_on:
            self.maintenance_changed.emit(False)

    # ── The loop ──────────────────────────────────────────────────────────

    def _should_hold(self) -> Optional[str]:
        """A reason to skip this check, or None to proceed."""
        api_client = getattr(self.runtime, "api_client", None)
        if api_client is None or not api_client.access_token:
            return "not signed in"
        network = getattr(self.runtime, "network", None)
        if network is not None and network.network_state not in NetworkState.WORTH_TRYING:
            return f"network {network.network_state}"
        return None

    def tick(self) -> Optional[int]:
        if not self._first_tick_done:
            self._first_tick_done = True
            return self.FIRST_CHECK_DELAY_MS

        hold_reason = self._should_hold()
        if hold_reason:
            self.log.debug("maintenance check held: %s", hold_reason)
            return self.HOLD_INTERVAL_MS

        try:
            payload = self._maintenance_api.get_maintenance_status()
        except ApiError as exc:
            # Quietly. The last known answer stands: a notice is neither
            # cleared nor raised because the backend could not be asked.
            self.log.debug("maintenance check unavailable: %s", exc)
            self.heartbeat(success=False)
            return self._jittered(self.HOLD_INTERVAL_MS)

        self.heartbeat()
        if self.state == ServiceState.DEGRADED:
            self._set_state(ServiceState.RUNNING)
        self._apply(bool((payload or {}).get("maintenance_mode", False)))
        return self._jittered(self.CHECK_INTERVAL_MS)

    def _apply(self, active: bool) -> None:
        """Record the answer and emit only if it changed."""
        previous = self._known
        self._known = active
        if previous == active:
            # true -> true, false -> false: the level, not an edge. Nothing.
            return
        if previous is None and not active:
            # First answer of the session, and it is "off": nothing to show.
            return

        self.log.info("maintenance notice %s", "on" if active else "off")
        self.maintenance_changed.emit(active)

    @staticmethod
    def _jittered(interval_ms: int) -> int:
        """Spread a fleet's checks out so they do not arrive in lockstep."""
        return int(interval_ms * (0.85 + random.random() * 0.3))

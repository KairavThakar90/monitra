"""
activity_service — Captures, aggregates and persists user activity.
"""
from __future__ import annotations

import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from PySide6.QtCore import Signal, Slot

from background_services.activity import input_access
from background_services.activity.input_counter import InputEventCounter
from background_services.activity.input_probe import InputProbe
from background_services.activity.today_summary import ActivityTotals, totals_from_percent
from background_services.activity.unwanted_activity import UnwantedActivityMonitor
from background_services.notifications import NotificationLevel
from core.service import LoopService


# Configurable Thresholds & Weights for normalized Activity % calculation
MAX_KEYBOARD_STROKES_PER_INTERVAL = 120
MAX_MOUSE_CLICKS_PER_INTERVAL = 30
MAX_MOUSE_MOVEMENTS_PER_INTERVAL = 400

KEYBOARD_WEIGHT = 0.40
MOUSE_CLICK_WEIGHT = 0.30
MOUSE_MOVEMENT_WEIGHT = 0.30


def calculate_activity_percentage(
    keyboard_strokes: int,
    mouse_clicks: int,
    mouse_movements: int,
    active_seconds: int = 0,
    window_seconds: int = 60
) -> int:
    """
    Normalised activity (0-100) from the input actually counted in a window.

    Each counter is scored against the volume a fully engaged minute is
    expected to produce, capped at 1.0, and the three are combined with the
    weights above.

    **Why this is not "seconds the user was present".** It used to be. The
    first branch of this function read

        if window_seconds > 0 and active_seconds >= 0:
            return round(active_seconds / window_seconds * 100)

    and `active_seconds` is a count, so `>= 0` is always true: the weighted
    model below it was unreachable, and every keystroke, click and movement
    the client captured was stored, uploaded — and ignored. What the number
    actually reported was "in how many sampled seconds did Windows say input
    had occurred in the last 1.5 seconds", which saturates: anyone moving a
    mouse continuously scores 100%, so a whole day of screenshots carried an
    identical, uninformative 100%. Worse, presence comes from
    `GetLastInputInfo`, which sees input the client's hooks cannot (an
    elevated window, for one) — so windows were recorded at 100% with zero
    counted events, a number nothing in the captured data could support.

    Counting resolves both: it cannot exceed what was observed, and it
    distinguishes reading from typing. `active_seconds` is still recorded
    alongside, because presence is a real measurement and is worth keeping —
    it just is not this number.

    Thresholds are per minute, so a partial window (the tail of a session) is
    scaled to its real length rather than being scored as if it were a full
    one — otherwise every session's last window would read near zero.
    """
    if window_seconds <= 0:
        return 0

    scale = window_seconds / 60.0
    k_max = MAX_KEYBOARD_STROKES_PER_INTERVAL * scale
    c_max = MAX_MOUSE_CLICKS_PER_INTERVAL * scale
    m_max = MAX_MOUSE_MOVEMENTS_PER_INTERVAL * scale

    k_score = min(max(0, keyboard_strokes) / k_max, 1.0) if k_max > 0 else 0.0
    c_score = min(max(0, mouse_clicks) / c_max, 1.0) if c_max > 0 else 0.0
    m_score = min(max(0, mouse_movements) / m_max, 1.0) if m_max > 0 else 0.0

    total_score = (
        k_score * KEYBOARD_WEIGHT
        + c_score * MOUSE_CLICK_WEIGHT
        + m_score * MOUSE_MOVEMENT_WEIGHT
    )
    return max(0, min(100, round(total_score * 100)))


class ActivityService(LoopService):
    """
    Samples user input once per second while a timer is running and flushes an
    aggregated window to storage every `WINDOW_SECONDS`.

    Two capture mechanisms feed the same window, and they answer different
    questions — they are not two sources for one number:

    - InputProbe (presence): "was the user there this second, and did the
      pointer move". No counters; see its docstring for why it no longer has
      any.
    - InputEventCounter: the single source of keystroke/click/movement
      counts, for the backend's time_entry_activity columns, for the
      activity percentage, and for the watched-key tallies the
      unwanted-activity rules consume. Its listeners run ONLY between
      start_tracker() and stop_tracker(). On a platform where the probe is
      unsupported (macOS) but the counter works, a second with any counted
      event is treated as active, so the percentage works there too instead
      of reading unmeasured.

    Capture can be *blocked*: on macOS the Quartz tap needs the user's Input
    Monitoring permission, and without it the counter hears nothing. That
    state is named rather than left to look like idleness -- logged once as
    `ACTIVITY_CAPTURE_BLOCKED`, shown to the user once with the switch's
    location, exposed to the card through `capture_status()`, and retried
    every `CAPTURE_RETRY_SECONDS` while the timer runs so a permission
    granted mid-session is picked up without restarting the timer. The
    counter's own guards make the retry safe: a second `start()` on a running
    tap is a no-op, never a second tap.

    Signals:
        activity_window_recorded(dict)  — one completed window
        activity_percent_changed(int)   — current session percentage
        unwanted_activity_alert(str)    — user-facing warning to display
        capture_status_changed(str)     — "" when counting resumed, else the
                                          blocked state; edges only
        tracking_enabled_changed(bool)  — an administrator turned this member's
                                          activity tracking on or off; edges only

    **The member's switch.** An administrator can turn activity tracking off for a
    member in User Management. "Activity" here is everything this package measures
    of what the person does at the machine: keyboard and mouse activity (this
    service), and the applications and websites they use (`AppUsageService`,
    `UrlUsageService`, which follow this service's switch and do not read it
    themselves). Off means nothing is measured, nothing is recorded and nothing is
    uploaded for the session. It is deliberately *not* idle detection: that has its
    own switch (`idle_enabled`) and reads `idle_seconds()` below, which is a plain
    system call that works whether or not a session is being tracked here.
    """

    name = "activity"

    activity_window_recorded = Signal(dict)
    activity_percent_changed = Signal(int)
    unwanted_activity_alert = Signal(str)
    capture_status_changed = Signal(str)
    tracking_enabled_changed = Signal(bool)
    #: Emitted from the loop thread when the periodic refresh is due; the slot
    #: runs on the GUI thread, which is where the request is handed to the pool.
    _config_refresh_due = Signal()

    #: How often the member's switch is re-read from the backend. The same cadence
    #: as the idle and screenshot settings, so an administrator's change reaches a
    #: running desktop within the same few minutes for all of them. Seeded at
    #: sign-in from the profile (`apply_user_profile`); this is the correction.
    CONFIG_REFRESH_SECONDS = 3 * 60

    SAMPLE_INTERVAL_MS = 1000
    WINDOW_SECONDS = 60
    #: How often, in sampled seconds, a blocked counter is asked to start
    #: again while a timer runs. One minute: cheap (a refused tap returns at
    #: once) and quick enough that flipping the switch in System Settings is
    #: noticed without the user having to do anything else.
    CAPTURE_RETRY_SECONDS = 60
    #: A flushed window at least this long with zero counted events, while
    #: counting is on, is worth a diagnostic line: it is either a genuinely
    #: idle user or a listener that has gone deaf, and the line carries what
    #: is needed to tell which.
    SILENT_WINDOW_MIN_SECONDS = 30

    def __init__(self, runtime, cache, activity_api=None, parent=None) -> None:
        super().__init__(runtime, parent)
        self._cache = cache
        self._activity_api = activity_api
        #: Whether an administrator has this member's activity tracking on. True
        #: until the server says otherwise -- exactly what this service did before
        #: the switch existed -- and only an explicit `False` turns it off, so a
        #: missing or unreadable answer can never switch tracking off.
        self._tracking_enabled = True
        #: Whether a timer session is running, tracked or not. A session that
        #: starts while the switch is off must be picked up if it is turned on
        #: during it, so the session is remembered here even when nothing is measured.
        self._session_open = False
        self._config_read_at = time.monotonic()
        self._config_refresh_due.connect(self._refresh_config)
        self._probe = InputProbe()
        self._monitor = UnwantedActivityMonitor(
            on_event=self._on_unwanted_event,
            on_alert=self.unwanted_activity_alert.emit,
        )
        self._counter = InputEventCounter(watch_keys=self._monitor.watch_keys)
        self.interval_ms = self.SAMPLE_INTERVAL_MS

        self._entry_id: Optional[int] = None
        #: The timer session's own stable key. Windows sampled before the
        #: backend has issued an entry id are written against it and adopted
        #: later -- see `_flush_window` and `bind_entry_id`.
        self._client_op: Optional[str] = None
        self._tracking = False
        self._window_start: Optional[str] = None
        self._sampled = 0
        self._active = 0
        self._key_events = 0
        self._mouse_events = 0
        self._keyboard_strokes = 0
        self._mouse_clicks = 0
        self._mouse_movements = 0
        self._held_events: list = []
        #: The blocked state's name while counting is not running, else None.
        #: Plain attributes, read by the GUI thread on every tick.
        self._capture_blocked: Optional[str] = None
        self._capture_detail: str = ""
        self._capture_retry_in = 0
        #: Consecutive flushed windows with no counted event while counting
        #: was on. Drives the rate of `ACTIVITY_CAPTURE_SILENT` lines.
        self._silent_windows = 0

    @property
    def supported(self) -> bool:
        return self._probe.supported

    def capture_status(self) -> Dict[str, Any]:
        """
        Whether keyboard/mouse counting is running right now, and if not, why.

        `blocked` is None while counting, otherwise an `InputAccess` value
        ("denied", "restart_required", "unavailable"). Reads plain attributes,
        so the dashboard may call it on every tick.
        """
        return {
            "counting": self._capture_blocked is None,
            "blocked": self._capture_blocked,
            "detail": self._capture_detail,
            "tracking": self._tracking,
            # An administrator has turned activity tracking off for this member:
            # nothing is counted because nothing is meant to be, which is not the
            # same as a permission being refused.
            "disabled": not self._tracking_enabled,
        }

    def idle_seconds(self) -> Optional[float]:
        """Seconds since the last system-wide keyboard or mouse input.

        `None` when the platform cannot measure it. Exposed here because this
        service already owns the only input-observation path in the process:
        idle detection reads this instead of installing global listeners of
        its own, so there is still exactly one place system input is watched.
        """
        return self._probe.idle_seconds()

    def probe_diagnostics(self) -> Dict[str, Any]:
        """The inactivity probe's own health, for the idle monitor's report."""
        return self._probe.diagnostics()

    def current_percent(self) -> int:
        return calculate_activity_percentage(
            self._keyboard_strokes,
            self._mouse_clicks,
            self._mouse_movements,
            self._active,
            max(1, self._sampled)
        )

    def live_window_totals(self) -> ActivityTotals:
        """
        The window currently being sampled, as an addable weighted total.

        This is the only measurement that exists nowhere else: it has not been
        flushed to the local cache, so it cannot have been uploaded either.
        The dashboard adds it to the persisted totals to keep TODAY'S ACTIVITY
        moving between the once-a-minute window flushes, without any risk of
        counting the same seconds twice.

        Reads plain integer counters, so it is safe to call from the GUI
        thread on every tick.
        """
        if not self._tracking or self._sampled <= 0:
            return ActivityTotals()
        return totals_from_percent(self.current_percent(), self._sampled)

    def percent_for_entry(self, entry_id: int) -> int:
        try:
            return self._cache.get_activity_percent_for_entry(entry_id)
        except Exception:  # noqa: BLE001
            self.log.exception("could not read activity for entry %s", entry_id)
            return 0

    def _reset_window(self) -> None:
        self._window_start = datetime.now(timezone.utc).isoformat()
        self._sampled = 0
        self._active = 0
        self._key_events = 0
        self._mouse_events = 0
        self._keyboard_strokes = 0
        self._mouse_clicks = 0
        self._mouse_movements = 0

    def _flush_window(self) -> None:
        """Write the sampled window to the local queue and start a new one.

        A window with no entry id yet is still written, against this
        session's `client_op`; the adoption binds it once the backend issues
        the id. Holding it open instead is what used to happen, and it broke
        an offline session in two ways: `_sampled` grew past WINDOW_SECONDS
        for as long as the session stayed unattributed, so the whole session
        landed as ONE sample -- measured at `window_seconds = 7200` for two
        hours offline, which the backend refuses outright (its schema caps a
        window at 3600) so the session's activity was never stored at all --
        and everything measured so far lived only in memory, where a crash
        took it.

        Without a client_op there is nothing that could ever adopt the row,
        so the window is held rather than written somewhere it can never be
        attributed. That is the same rule `AppUsageService._flush_segment`
        applies, for the same reason.
        """
        if self._sampled <= 0 or self._window_start is None:
            self._reset_window()
            return

        if self._entry_id is None and not self._client_op:
            self.log.debug(
                "holding a %ds activity window: no entry id and no session key "
                "to attribute it with",
                self._sampled,
            )
            return

        act_percent = self.current_percent()
        record = {
            "time_entry_id": self._entry_id,
            "window_start": self._window_start,
            "window_seconds": self._sampled,
            "active_seconds": self._active,
            "key_events": self._key_events,
            "mouse_events": self._mouse_events,
            "keyboard_strokes": self._keyboard_strokes,
            "mouse_clicks": self._mouse_clicks,
            "mouse_movements": self._mouse_movements,
            "activity_percent": act_percent,
        }
        try:
            self._cache.save_activity_sample(
                time_entry_id=self._entry_id,
                window_start=self._window_start,
                window_seconds=self._sampled,
                active_seconds=self._active,
                key_events=self._key_events,
                mouse_events=self._mouse_events,
                keyboard_strokes=self._keyboard_strokes,
                mouse_clicks=self._mouse_clicks,
                mouse_movements=self._mouse_movements,
                activity_percent=act_percent,
                client_op=self._client_op,
            )
        except Exception:  # noqa: BLE001
            self.log.exception("could not persist activity window")
        else:
            self.log.info(
                "activity window: %d/%ds active (%d%%, keys=%d, clicks=%d, moves=%d) for entry %s",
                self._active, self._sampled, act_percent,
                self._keyboard_strokes, self._mouse_clicks, self._mouse_movements,
                self._entry_id,
            )
            self._diagnose_window()
            self.activity_window_recorded.emit(record)
        self._reset_window()

    def _diagnose_window(self) -> None:
        """
        Say so when a whole window passed with counting on and nothing heard.

        Zero counts have several causes that look identical in the data: a
        user who was away, a permission that is granted but not in effect,
        secure keyboard entry swallowing every key, a tap the system disabled.
        One line per silent streak (and every tenth window of it) carries the
        counter's own health and, where the OS exposes one, its idle clock --
        a small `os_idle_seconds` beside zero counts means the user was there
        and the listener was deaf. Logging only; nothing here changes what is
        recorded.
        """
        counted = self._keyboard_strokes + self._mouse_clicks + self._mouse_movements
        if counted > 0:
            if self._silent_windows:
                self.log.info(
                    "ACTIVITY_CAPTURE_HEARD input again after %d silent window(s)",
                    self._silent_windows,
                )
            self._silent_windows = 0
            return
        if self._capture_blocked is not None or self._sampled < self.SILENT_WINDOW_MIN_SECONDS:
            return
        self._silent_windows += 1
        if self._silent_windows != 1 and self._silent_windows % 10 != 0:
            return
        try:
            diag = dict(self._counter.diagnostics())
        except Exception:  # noqa: BLE001
            diag = {}
        idle = diag.pop("os_idle_seconds", None)
        if idle is None:
            idle = self._probe.idle_seconds()
        self.log.warning(
            "ACTIVITY_CAPTURE_SILENT windows=%d window_seconds=%d probe_active_seconds=%d "
            "os_idle_seconds=%s counter=%s; zero keyboard/mouse events were counted "
            "while counting is on (a small os_idle_seconds here means the user was "
            "active and the listener did not hear it)",
            self._silent_windows, self._sampled, self._active,
            "n/a" if idle is None else f"{idle:.0f}", diag,
        )

    # ── The member's switch (User Management) ──────────────────────────────────

    def tracking_enabled(self) -> bool:
        """Whether this member's activity may be measured. Safe from any thread."""
        return self._tracking_enabled

    def apply_user_profile(self, user_data: Optional[Dict[str, Any]]) -> None:
        """Seed the switch from a `/auth/me` payload (sign-in, session verification).

        The profile is already in hand, so the member's own setting is in effect
        before a session can start. A profile that does not carry the field (a
        backend older than the switch) means *on* -- not whatever the last person
        who signed in on this machine had.
        """
        if not isinstance(user_data, dict):
            return
        user = user_data.get("user") if isinstance(user_data.get("user"), dict) else user_data
        self.set_tracking_enabled(user.get("activity_tracking_enabled", True))
        self._config_read_at = time.monotonic()

    def set_tracking_enabled(self, value: Any) -> None:
        """Turn tracking on or off. Edge-triggered: only a change does anything.

        Only a real boolean is believed; anything else is a damaged answer and
        leaves the switch where it was. Turning it off mid-session writes out
        what was measured so far and stops measuring; turning it on mid-session
        starts measuring from that moment.
        """
        if not isinstance(value, bool) or value == self._tracking_enabled:
            return
        self._tracking_enabled = value
        self.log.info(
            "ACTIVITY_TRACKING_SWITCH enabled=%s; activity tracking is turned %s for this account",
            value, "on" if value else "off",
        )
        if self._session_open:
            if value:
                self._begin_tracking()
            else:
                self._end_tracking()
        self.tracking_enabled_changed.emit(value)

    @Slot()
    def _refresh_config(self):
        """Re-read the switch from the backend (GUI thread; the request is on the pool).
        Returns the pool handle, or None when nothing was submitted."""
        if self._activity_api is None:
            return None
        tasks = getattr(self.runtime, "tasks", None)
        if tasks is None:
            return None

        def on_success(config: Any) -> None:
            # A key the answer does not carry (a backend older than the switch)
            # says nothing and changes nothing.
            if isinstance(config, dict) and isinstance(config.get("activity_tracking_enabled"), bool):
                self.set_tracking_enabled(config["activity_tracking_enabled"])
                # Into the persisted profile too, so a restart while offline seeds
                # from what the administrator last set.
                session_manager = getattr(self.runtime, "session_manager", None)
                update = getattr(session_manager, "update_user_info", None)
                if update is not None:
                    try:
                        update(activity_tracking_enabled=config["activity_tracking_enabled"])
                    except Exception:  # noqa: BLE001
                        self.log.exception("could not remember the activity switch in the profile")

        def on_error(exc: BaseException) -> None:
            # A failed refresh keeps the last known setting. Blanking a valid
            # local value because the network blipped is a regression.
            self.log.info("activity config refresh failed: %s", exc)
            self._config_refresh_done()

        def on_success_then_again(config: Any) -> None:
            try:
                on_success(config)
            finally:
                self._config_refresh_done()

        return tasks.submit(
            self._activity_api.get_config,
            on_success=on_success_then_again,
            on_error=on_error,
            key="activity-config",
            guard_generation=False,
        )

    def _config_refresh_done(self) -> None:
        """A refresh completed (either way): make the one that was asked for meanwhile."""
        if getattr(self, "_config_refresh_again", False):
            self._config_refresh_again = False
            self._refresh_config()

    @Slot()
    def refresh_config_now(self) -> None:
        """Re-read the switch now (an administrator changed something, or the network
        came back). GUI thread; not while signed out."""
        api_client = getattr(self.runtime, "api_client", None)
        if api_client is None or not getattr(api_client, "access_token", None):
            return
        self._config_read_at = time.monotonic()
        # A request the pool de-duplicates (a refresh is in flight) is made again the
        # moment that refresh completes, never left to the next three-minute timer.
        if self._refresh_config() is None:
            self._config_refresh_again = True

    def _maybe_refresh_config(self) -> None:
        """Called from the loop's tick (its own thread): ask for a refresh when due."""
        if self._activity_api is None:
            return
        now = time.monotonic()
        if now - self._config_read_at < self.CONFIG_REFRESH_SECONDS:
            return
        # Not while signed out: there is no token to ask with, and a refused
        # request is not an answer about this member.
        api_client = getattr(self.runtime, "api_client", None)
        if api_client is None or not getattr(api_client, "access_token", None):
            return
        self._config_read_at = now  # claim it before emitting
        self._config_refresh_due.emit()

    def on_start(self) -> None:
        # The restored session already holds the last `/auth/me` profile, so the
        # member's setting is known locally before any request answers -- a member
        # who is switched off is not measured in the seconds before verification.
        session_manager = getattr(self.runtime, "session_manager", None)
        profile = getattr(session_manager, "user_info", None)
        if isinstance(profile, dict):
            self.apply_user_profile(profile)
        super().on_start()

    # ── Tracker contract (driven by TimerService) ──────────────────────────────

    def start_tracker(self, session: Dict[str, Any]) -> None:
        self._entry_id = session.get("entry_id")
        self._client_op = session.get("client_op")
        self._session_open = True
        if not self._tracking_enabled:
            self.log.info(
                "activity tracking is turned off for this account; nothing will be "
                "measured for entry %s", self._entry_id,
            )
            return
        self._begin_tracking()

    def _begin_tracking(self) -> None:
        """Start measuring: a fresh window, the counter and the rules engine."""
        self._tracking = True
        self._reset_window()
        self._held_events = []
        self._silent_windows = 0
        self._monitor.start_session()
        counting = self._counter.start()
        self._evaluate_capture(counting)
        self.log.info(
            "activity capture started for entry %s (probe supported=%s, "
            "counting=%s, blocked=%s, platform=%s)",
            self._entry_id, self._probe.supported, counting,
            self._capture_blocked, sys.platform,
        )

    # ── Capture state (permission, availability) ───────────────────────────────

    def _evaluate_capture(self, counting: bool) -> None:
        """Turn the counter's start answer into a named, user-visible state."""
        if counting:
            self._on_capture_restored()
            return
        status = input_access.classify(False, getattr(self._counter, "failure", None))
        self._on_capture_blocked(status)

    def _on_capture_blocked(self, status: input_access.InputAccessStatus) -> None:
        """Counting is not running. Said once on entering the state; retried
        quietly every `CAPTURE_RETRY_SECONDS` until it clears."""
        self._capture_retry_in = self.CAPTURE_RETRY_SECONDS
        if self._capture_blocked == status.state.value:
            return
        self._capture_blocked = status.state.value
        self._capture_detail = status.detail
        self.log.warning(
            "ACTIVITY_CAPTURE_BLOCKED state=%s detail=%s platform=%s; no keyboard/"
            "mouse activity is being measured and no window is recorded until "
            "counting starts (retried every %ds while the timer runs)",
            status.state.value, status.detail, sys.platform, self.CAPTURE_RETRY_SECONDS,
        )
        if status.state is input_access.InputAccess.DENIED:
            # The supported way to put Monitra in the Input Monitoring list
            # and in front of the user. Grants nothing by itself.
            input_access.request_input_access()
        title, message = input_access.guidance(status.state)
        notifications = getattr(self.runtime, "notifications", None)
        if notifications is not None:
            try:
                notifications.notify(
                    message, NotificationLevel.WARNING, title=title,
                    key=f"activity-capture:{status.state.value}",
                    link=(
                        input_access.SETTINGS_URL
                        if status.state is input_access.InputAccess.DENIED else None
                    ),
                )
            except Exception:  # noqa: BLE001
                self.log.exception("could not show the activity-capture notification")
        self.capture_status_changed.emit(self._capture_blocked)

    def _on_capture_restored(self) -> None:
        """Counting is running: any earlier blocked state is over."""
        self._capture_retry_in = 0
        if self._capture_blocked is None:
            return
        self.log.info(
            "ACTIVITY_CAPTURE_RESTORED after %s; keyboard/mouse counting has resumed",
            self._capture_blocked,
        )
        self._capture_blocked = None
        self._capture_detail = ""
        self.capture_status_changed.emit("")

    def _retry_capture(self) -> None:
        """One sampled second of a blocked session: start the counter again
        when the retry interval is up. `InputEventCounter.start()` is a no-op
        on a running listener, so this can never install a second one."""
        self._capture_retry_in -= 1
        if self._capture_retry_in > 0:
            return
        self._evaluate_capture(self._counter.start())

    def bind_entry_id(self, entry_id: int) -> None:
        """
        Attach a backend entry id that arrived after tracking began.

        Windows already written against this session's `client_op` belong to
        the same entry, so they are adopted here — the same treatment
        `AppUsageService.bind_entry_id` gives a segment measured before the
        entry existed. Unwanted-activity events detected in that gap are
        written now for the same reason.

        This covers a session that is still running. A start confirmed
        through the durable queue may land after the session has stopped, and
        `SyncService._adopt_session_telemetry` covers that; both are
        idempotent, and both are required.
        """
        self._entry_id = entry_id
        self._adopt_queued_windows(entry_id)
        self._persist_held_events()

    def _adopt_queued_windows(self, entry_id: int) -> None:
        if not self._client_op:
            return
        try:
            adopted = self._cache.bind_activity_samples_to_entry(
                self._client_op, entry_id
            )
        except Exception:  # noqa: BLE001
            self.log.exception(
                "could not bind buffered activity windows to entry %s", entry_id
            )
        else:
            if adopted:
                self.log.info(
                    "bound %d buffered activity window(s) to entry %s",
                    adopted, entry_id,
                )

    def stop_tracker(self) -> None:
        self._session_open = False
        self._flush_window()
        self._persist_held_events()
        self._counter.stop()
        self._monitor.stop_session()
        self._tracking = False
        self._entry_id = None
        self._client_op = None
        self._window_start = None
        self._held_events = []

    def _end_tracking(self) -> None:
        """Stop measuring because the switch went off, with the session still running.

        What was measured so far is written out, the counter and the rules engine
        stop (so no listener is left hearing the keyboard), and the session's
        identity is *kept*: if the switch comes back on during this session,
        measuring resumes against the same entry. A stale "permission needed"
        state is cleared too -- nothing is being counted, so nothing is blocked.
        """
        self._flush_window()
        self._persist_held_events()
        self._counter.stop()
        self._monitor.stop_session()
        self._tracking = False
        self._window_start = None
        self._held_events = []
        self._capture_retry_in = 0
        if self._capture_blocked is not None:
            self._capture_blocked = None
            self._capture_detail = ""
            self.capture_status_changed.emit("")

    # ── Unwanted activity ─────────────────────────────────────────────────────

    def _on_unwanted_event(self, record: dict) -> None:
        """One detection occurrence from the rule engine: queue it (and its
        deduction, on every deduct_after-th occurrence) for upload, or hold
        it until the backend has issued an entry id."""
        if self._entry_id is None:
            self._held_events.append(record)
            return
        self._persist_event(self._entry_id, record)

    def _persist_held_events(self) -> None:
        if not self._held_events or self._entry_id is None:
            return
        held, self._held_events = self._held_events, []
        for record in held:
            self._persist_event(self._entry_id, record)

    def _persist_event(self, entry_id: int, record: dict) -> None:
        try:
            self._cache.save_unwanted_activity(
                record_id=record["client_event_id"],
                time_entry_id=entry_id,
                activity_type=record["activity_type"],
                key_or_action=record["key_or_action"],
                occurrence_count=record["occurrence_count"],
                alerted=record["alerted"],
                alert_count=record["alert_count"],
                recorded_at=record["recorded_at"],
            )
            if record.get("deduction_seconds", 0) > 0:
                self._cache.save_adjustment(
                    record_id=f"adj-{record['client_event_id']}",
                    time_entry_id=entry_id,
                    adjustment_seconds=-int(record["deduction_seconds"]),
                    reason=(
                        f"Unwanted activity rule '{record['activity_type']}' "
                        f"({record['key_or_action']}) reached occurrence "
                        f"{record['occurrence_index']}"
                    ),
                    source_activity_type=record["activity_type"],
                    source_key_or_action=record["key_or_action"],
                    source_client_event_id=record["client_event_id"],
                    recorded_at=record["recorded_at"],
                )
        except Exception:  # noqa: BLE001
            self.log.exception("could not persist unwanted-activity event")

    def tick(self) -> Optional[int]:
        self._maybe_refresh_config()
        if not self._tracking:
            return self.SAMPLE_INTERVAL_MS

        if self._entry_id is None:
            session = self.runtime.timer.active_session() or {}
            entry_id = session.get("entry_id")
            if self._client_op is None:
                self._client_op = session.get("client_op")
            if entry_id is not None:
                # Adopt through the same path as bind_entry_id, so an id
                # noticed here rather than delivered to us still releases the
                # windows already queued against this session's client_op.
                self.bind_entry_id(entry_id)

        if not self._counter.supported:
            self._retry_capture()

        counts = self._counter.snapshot_and_reset()
        counted_any = (
            counts["keystrokes"] > 0 or counts["clicks"] > 0 or counts["movements"] > 0
        )
        self._keyboard_strokes += counts["keystrokes"]
        self._mouse_clicks += counts["clicks"]
        self._mouse_movements += counts["movements"]

        sample = self._probe.sample(self.SAMPLE_INTERVAL_MS / 1000.0 * 1.5)
        if sample is None:
            if not self._counter.supported:
                return self.SAMPLE_INTERVAL_MS
            sample = {"active": counted_any, "mouse": counts["movements"] > 0}

        self._sampled += 1
        if sample.get("active") or counted_any:
            self._active += 1
        # Which *kind* of input a second contained comes from the counter,
        # the only thing in this process that sees individual events. The
        # probe contributes presence and pointer movement and nothing else:
        # it used to report "present and the cursor did not move" as a
        # keyboard second, which measured nothing about the keyboard.
        if sample.get("mouse") or counts["clicks"] > 0 or counts["movements"] > 0:
            self._mouse_events += 1
        if counts["keystrokes"] > 0:
            self._key_events += 1

        self._monitor.feed(self._counter.drain_watched_presses())

        self.heartbeat()

        if self._sampled >= self.WINDOW_SECONDS:
            self._flush_window()
        else:
            # Every sampled second, so the card follows the user's own hands:
            # a burst of typing shows within the second it happened, not up
            # to five seconds later. The slot only re-renders from state
            # already held -- no request, no query -- so once a second costs
            # the same as the timer's own display tick.
            self.activity_percent_changed.emit(self.current_percent())

        return self.SAMPLE_INTERVAL_MS

    def on_stop(self, timeout_ms: int) -> bool:
        self._flush_window()
        self._persist_held_events()
        self._counter.stop()
        return super().on_stop(timeout_ms)

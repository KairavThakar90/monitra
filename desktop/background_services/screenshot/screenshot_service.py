"""
screenshot_service — Captures screenshots while, and only while, time is tracked.

Ownership
---------
Registered with `ApplicationRuntime` and driven by `TimerService` through the
same `start_tracker` / `bind_entry_id` / `stop_tracker` contract the activity,
app-usage and URL trackers already use. It runs no retry loop (the durable
queue and `SyncService` do that) and never touches the UI.

Unlike those trackers this is a `BaseService`, not a `LoopService`, and owns no
thread. They sample once a second and genuinely need a loop; this one acts
roughly once every ten minutes, so a dedicated OS thread would sit idle for
99.9% of its life. Episodic work belongs on the shared bounded pool — which is
what `TaskRunner` is for, and what DO_NOT_DO.md prescribes instead of a thread
per job. The pool never expires its threads, so this adds neither a permanent
thread nor an extra SQLite connection.

What the schedule does
----------------------
A single-shot `QTimer` on the GUI thread is armed for the next planned capture
instant — it schedules, it does not poll, and an idle window costs one wake-up
rather than a tick per second. When it fires, the capture, the encode and the
queue write are submitted to the task pool under one de-duplication key, so
none of that work can ever run on the GUI thread and two captures cannot
overlap.

The per-window budget survives a restart: how many captures a window has spent
is persisted in `app_state` under `SCREENSHOT_WINDOW_KEY`, so relaunching
Monitra four minutes into a window cannot take a second screenshot the window
has already paid for.

Nothing here uploads. A capture is processed, written to the daily cache and
registered in the durable queue; `SyncService` drains that queue and deletes
the local file only once the backend confirms storage.

What authorises a capture
-------------------------
Tracking, and nothing else. The application being open, the user being logged
in, the window being minimised to the tray and this service being started are
all irrelevant on their own — `start_tracker` is the only thing that arms the
schedule, and `TimerService` is the only caller of it. Authentication is not
tracking.

Because the schedule is armed on the GUI thread but the capture runs on the
pool, "tracking was live when this was scheduled" is not the same claim as
"tracking is live now": a stop can land in between. Every capture therefore
re-checks immediately before it touches the screen, against a generation token
that `start_tracker` and `stop_tracker` both advance. A callback left over from
a stopped timer, a previous task, or a previous tracking session cannot match
the current generation, so it aborts before capturing rather than capturing and
deleting afterwards — an image that is never taken cannot leak.

What happens when a capture does not work
-----------------------------------------
Every expected capture ends in exactly one of three places: an image in the
durable queue, a retry that is still within its window, or an *explicit
recorded outcome* for the window (`pending_screenshot_events`, uploaded by
`SyncService`). There is no fourth, silent, state. This replaced a scheduler in
which a failed grab returned `None`, the planned instant had already been spent,
and the window simply had no screenshot and no explanation anywhere.

* A failed grab, encode, disk write or queue write is retried inside the same
  window, with backoff and jitter, until the window has no time left.
* A window that ends unresolved -- every retry failed, the OS refused, a privacy
  rule held it back the whole time -- is reported once, with a reason code.
* One exception cannot end the schedule: `_on_due` always re-arms, and a
  watchdog re-arms a timer that somehow is not running.
* A capture that never returns is abandoned after `CAPTURE_STUCK_SECONDS` and
  the window retried. The de-duplication key can no longer hold every later
  capture hostage.
"""
from __future__ import annotations

import random
import threading
import time
import uuid
from datetime import datetime, timezone
from time import time as _wall_time
from typing import Any, Dict, List, Optional, Tuple

from PySide6.QtCore import QTimer, Signal

from background_services.notifications import NotificationLevel
from background_services.screenshot import (
    capture, config, health, image_processor, mac_diagnostics, scheduler, screen_access, store,
)
from core.service import BaseService
from sync.local_cache import CACHE_OWNER_KEY
from tracking.active_window import get_active_window_details
from tracking.browsers.manager import get_browser_manager

#: Durable record of the window budget already spent, so a restart inside a
#: window cannot exceed `SCREENSHOTS_PER_WINDOW`.
SCREENSHOT_WINDOW_KEY = "screenshot_window_state"

#: What "uploaded" means to the person looking at their own screen; written by
#: `SyncService` when the backend confirms a capture is in Drive.
SCREENSHOT_LAST_UPLOAD_KEY = config.LAST_UPLOAD_STATE_KEY


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


#: "Read the owner now": the default for a direct `_capture_now(...)` call, as the
#: end-to-end tests make. A scheduled capture always passes the owner it was
#: authorised for, so a sign-in that lands while it runs cannot re-stamp it.
_LIVE_OWNER = object()

#: The same suffixes `tracking/app_identity.py` strips when it builds a
#: process identity, so a catalogue entry entered *with* one (as admins are
#: asked to, e.g. "chrome.exe") still matches the always-suffix-stripped name
#: `get_active_window_details()` reports (e.g. "chrome").
_KNOWN_PROCESS_SUFFIXES = (".exe", ".app", ".bat", ".cmd")


def _strip_known_suffix(name: str) -> str:
    lowered = name.lower()
    for suffix in _KNOWN_PROCESS_SUFFIXES:
        if lowered.endswith(suffix):
            return name[: -len(suffix)]
    return name


class _WindowOutcome:
    """What has happened to one window's capture so far.

    A window is *resolved* once it holds an image (`captured`), or its
    unresolved outcome has been recorded for the backend (`reported`). Anything
    else at the moment the window ends is exactly the silent loss this class
    exists to make impossible, so `ScreenshotService._finalize_outcome` runs on
    every exit from a window.
    """

    __slots__ = (
        "index", "start", "captured", "taken", "failures", "reason", "detail", "held",
        "reported",
    )

    def __init__(self, index: int, start: float) -> None:
        self.index = index
        #: The window's start (epoch seconds), fixed when the window was first
        #: seen. Recomputed later from the *current* window length it would be
        #: wrong the moment an administrator changed `capture_frequency`
        #: mid-window -- and a window the server places decades ago is refused.
        self.start = start
        self.captured = False
        #: Images this process has taken for the window.
        self.taken = 0
        self.failures = 0
        self.reason: Optional[str] = None
        self.detail: Optional[str] = None
        #: Why the capture is being held back right now, while it is:
        #: "blocked", "excluded", "privacy_config", "unavailable".
        self.held: Optional[str] = None
        self.reported = False


#: How a held window is described to the backend: (event state, reason code).
_HELD_EVENTS = {
    "blocked": ("blocked", "screen_recording_blocked"),
    "excluded": ("excluded", "privacy_rule"),
    "privacy_config": ("blocked", "privacy_config_unavailable"),
    "unavailable": ("unavailable", "capture_unavailable"),
}


class ScreenshotService(BaseService):
    """
    Owns screenshot capture.

    Signals:
        screenshot_captured(dict) — one capture queued for upload
        capture_unavailable(str)  — this machine cannot take screenshots
        capture_blocked(str)      — the OS will not let the screen be read
                                    (macOS Screen Recording); the argument is
                                    a `screen_access.ScreenAccess` value. Fires
                                    on the transition into a blocked state, not
                                    on every retry.
    """

    name = "screenshot"

    screenshot_captured = Signal(dict)
    capture_unavailable = Signal(str)
    capture_blocked = Signal(str)
    #: The user-facing screenshot state changed (`health.ScreenshotStatus` as a
    #: dict). Edge-triggered: emitted when the state or its wording changes,
    #: never on a poll of an unchanged one.
    status_changed = Signal(dict)

    #: How soon a capture that the OS refused (macOS Screen Recording not
    #: granted) is attempted again. The refusal costs one cheap permission
    #: read, no window budget and no screenshot, so this can be short enough
    #: that granting the permission is noticed within the minute.
    PERMISSION_RETRY_SECONDS = 30.0

    #: Longest the schedule may sleep, so `stop_tracker` is noticed promptly
    #: and a system clock jump cannot park it for a whole window.
    MAX_SLEEP_MS = 30_000

    #: How often the user's capture-frequency configuration is re-read from
    #: the backend. Matches `IdleService.CONFIG_REFRESH_SECONDS`: an
    #: administrator's change should reach a running desktop within the same
    #: window idle already promises, for consistency between the two
    #: settings. Seeded immediately at login/session-verify (see
    #: `apply_user_profile`); this is the slow correction for a value an
    #: administrator changed mid-session.
    CONFIG_REFRESH_SECONDS = 3 * 60

    #: Delays before a failed capture is attempted again inside the same
    #: window. The last value repeats. Jittered, so a fleet whose screens all
    #: became unreadable at once (a lock screen, a remote-desktop reconnect)
    #: does not retry in step.
    CAPTURE_RETRY_DELAYS = (10.0, 30.0, 60.0, 120.0)

    #: Hard bound on attempts in one window. Six attempts at the delays above
    #: span about eight minutes of a ten-minute window.
    CAPTURE_MAX_ATTEMPTS = 6

    #: A retry is never scheduled closer than this to the window's end: it
    #: would be stamped into the next window and spend that window's budget.
    CAPTURE_RETRY_MARGIN_SECONDS = 5.0

    #: A capture that has not reported back after this long is abandoned. A
    #: grab and a WebP encode take about a second; ninety seconds is a wedged
    #: GDI/UI-Automation call, a display that never answered, or work that was
    #: interrupted by the machine sleeping.
    CAPTURE_STUCK_SECONDS = 90.0

    #: The most capture tasks that may be *running* on the shared pool at once --
    #: counted where they run, so it holds whether or not their callbacks are
    #: ever delivered. Normally one; a second only while an abandoned one is
    #: still wedged. A thread stuck in an OS call cannot be killed and the pool
    #: has four: one wedged capture per window would, in forty minutes, take every
    #: thread the application has and stop sync, config and every other
    #: background job with it. At the cap no new capture is started and the window
    #: is reported failed (`capture_stuck`), so the failure is explicit and the
    #: rest of the application keeps running.
    MAX_RUNNING_CAPTURES = 2

    #: How soon the schedule looks again when an overdue instant found a
    #: capture still running. The instant is kept, not dropped.
    INFLIGHT_RECHECK_SECONDS = 5.0

    #: Retry after the pool refused a submission (shutting down).
    REJECTED_RETRY_SECONDS = 10.0

    #: How often the watchdog runs while tracking. It is the thing that
    #: notices the schedule has stopped, so it must not depend on the schedule.
    HEALTH_INTERVAL_MS = 30_000

    #: The privacy configuration is fetched at login and at launch. If neither
    #: result has arrived, capture is held (an exclusion must never be
    #: skipped), the fetch is re-issued this often, and after
    #: `PRIVACY_PENDING_REPORT_SECONDS` the hold is reported so it is visible.
    PRIVACY_REFETCH_SECONDS = 30.0
    PRIVACY_PENDING_REPORT_SECONDS = 120.0

    def __init__(self, runtime, cache, screenshot_api=None, parent=None) -> None:
        super().__init__(runtime, parent)
        self._cache = cache
        self._screenshot_api = screenshot_api

        #: The user's own capture interval in minutes, once known. None
        #: until `apply_user_profile`/`_refresh_config` has run at least
        #: once, in which case `_window_seconds()` falls back to
        #: `config.window_seconds()` -- today's hardcoded/env default --
        #: exactly like `IdleService._idle_minutes` defaults to 5 pre-seed.
        self._capture_frequency_minutes: Optional[int] = None

        self._privacy_config: Dict[str, Any] = {}
        #: Whether `_refresh_privacy_config` has resolved at least once
        #: (success or failure) since this process started. `{}` alone
        #: cannot distinguish "no exclusions" from "not fetched yet" --
        #: without this, `_check_authorized`'s `if self._privacy_config:`
        #: guard was skipped entirely for whatever captures fell inside the
        #: real network round trip after login, which a short capture
        #: frequency (a 1-minute interval, or a machine started right before
        #: a scheduled window) could make the very *first* capture of a
        #: session -- exactly the one an admin's exclusion most needs to
        #: cover, uncovered.
        self._privacy_config_loaded = False

        self._config_refresh_timer = QTimer(self)
        self._config_refresh_timer.setInterval(self.CONFIG_REFRESH_SECONDS * 1000)
        self._config_refresh_timer.timeout.connect(self._refresh_config)

        #: Schedules only; every millisecond of real work happens on the pool.
        self._due_timer = QTimer(self)
        self._due_timer.setSingleShot(True)
        self._due_timer.timeout.connect(self._on_due)

        self._tracking = False
        self._entry_id: Optional[int] = None
        #: The tracking session's own stable key. Captures taken before the
        #: backend issues an entry id are held against it and adopted when it
        #: arrives. This replaced an earlier scheme that recorded only the
        #: window tracking began in and could therefore adopt only that one
        #: window — every later window of a queued start stayed unattributed
        #: and never uploaded at all.
        self._client_op: Optional[str] = None

        #: Capture authorisation, written on the GUI thread and read on a pool
        #: thread, so it is guarded rather than read raw. `_generation` counts
        #: tracking sessions: every start and every stop advances it, which is
        #: what lets a capture tell "the session I was scheduled for" from
        #: "the session running now" without consulting Qt objects it does not
        #: own from the wrong thread.
        self._auth_lock = threading.Lock()
        self._generation = 0
        self._authorized = False

        self._planned_index: Optional[int] = None
        self._planned_times: List[float] = []
        self._unavailable_reported = False
        #: The `ScreenAccess` value the OS last refused a capture with, or
        #: None while captures are permitted. What makes the user-facing
        #: notice edge-triggered: told once when the state is entered, not
        #: on every retry.
        self._access_blocked_state: Optional[str] = None

        #: Whose screen this is (`/auth/me` id), written on each capture row
        #: so an involuntary sign-out keeps the owner's queue and a different
        #: user's sign-in discards only another person's.
        self._owner_user_id: Optional[int] = None

        #: What has happened to the current window's capture; see
        #: `_WindowOutcome`. GUI thread only.
        self._outcome: Optional[_WindowOutcome] = None
        self._consecutive_failed_windows = 0
        self._last_failure_reason: Optional[str] = None

        #: The capture currently on the pool: `(token, started, window)`.
        #: `started` is wall-clock, so a machine that slept through it counts
        #: as having waited. Serialises captures and detects a wedged one.
        self._token_counter = 0
        self._inflight: Optional[Tuple[int, float, int]] = None
        #: Capture tasks currently executing on the pool (see
        #: `MAX_RUNNING_CAPTURES`). Guarded by `_auth_lock`.
        self._running_captures = 0

        self._privacy_pending_since: Optional[float] = None
        self._privacy_last_fetch = 0.0
        self._scheduler_errors = 0
        self._status: Optional[health.ScreenshotStatus] = None
        self._status_dirty = False

        #: The watchdog. Independent of `_due_timer` on purpose: it exists to
        #: notice that one has stopped.
        self._health_timer = QTimer(self)
        self._health_timer.setInterval(self.HEALTH_INTERVAL_MS)
        self._health_timer.timeout.connect(self._on_health_tick)

    # ── Tracker contract (driven by TimerService) ─────────────────────────────

    def start_tracker(self, session: Dict[str, Any]) -> None:
        """Begin capturing for a tracking session."""
        if not config.enabled():
            self.log.info("screenshot capture is disabled by configuration")
            return
        self._tracking = True
        self._planned_index = None
        self._planned_times = []
        self._outcome = None
        self._consecutive_failed_windows = 0
        self._last_failure_reason = None
        self._inflight = None
        self._privacy_pending_since = None
        self._owner_user_id = self._current_user_id()
        # Arming and authorising are the same act: nothing else in this class
        # may set `_authorized`, so there is no path from "the app is open" or
        # "the service started" to a capture.
        self._authorize(session.get("entry_id"), session.get("client_op"))
        self.log.info(
            "screenshot capture started for entry %s (%d per %ds window)",
            self._entry_id, config.screenshots_per_window(), self._window_seconds(),
        )
        # Each tracking session is told afresh if the screen cannot be read,
        # and is told at Start -- not minutes later, at a random instant in
        # the first window, when the first capture happens to come due.
        self._access_blocked_state = None
        self._probe_screen_access()
        # Plan and arm immediately, so a session that begins mid-window still
        # gets whatever the window has left rather than waiting for the next.
        self._health_timer.start()
        self._on_due()
        # Next turn of the event loop: the first status needs a queue read,
        # which belongs on the pool, and nothing here waits for it.
        QTimer.singleShot(0, self._refresh_status)

    def bind_entry_id(self, entry_id: int) -> None:
        """
        Attach the backend entry id once it arrives.

        Every capture this session has already queued is attributed to the
        entry, so a screenshot taken in the first seconds of tracking — or at
        any point during an offline start — is uploaded rather than stranded.

        This is the *live-session* half of the adoption. It cannot be the only
        half: a start that failed over to the durable queue is confirmed by
        `SyncService`, which may be minutes later and may land after the user
        has already stopped, when this service holds no session state at all.
        `SyncService._handle_start_timer` therefore performs the same adoption
        directly against the cache. Both are keyed on `client_op` and both are
        idempotent — binding rows that are already bound updates nothing.
        """
        # Same tracking session, so the generation is deliberately *not*
        # advanced — an offline start legitimately schedules captures before
        # the backend has issued an id, and bumping here would abort them.
        with self._auth_lock:
            self._entry_id = entry_id
            client_op = self._client_op
        if not client_op:
            return
        try:
            bound = self._cache.bind_screenshots_to_client_op(client_op, entry_id)
        except Exception:  # noqa: BLE001
            self.log.exception("could not bind queued screenshots to entry %s", entry_id)
            return
        if bound:
            self.log.info("attributed %d queued screenshot(s) to entry %s", bound, entry_id)

    def stop_tracker(self) -> None:
        """
        Stop capturing immediately. Queued screenshots still upload.

        Revoking authorisation is what actually stops capture; stopping the
        QTimer only stops *scheduling*. A capture already submitted to the pool
        is not cancellable, so it is stopped instead by the generation bump
        here, which it will fail to match. Screenshots already captured while
        tracking was valid keep their queue rows and upload normally — stopping
        the clock stops new captures, it does not discard legitimate ones.
        """
        if self._tracking:
            self.log.info("screenshot capture stopped for entry %s", self._entry_id)
            # The window being left may be unresolved. Record it now, while the
            # session's entry id is still known; after `_revoke` it is not.
            self._finalize_current_outcome()
        self._due_timer.stop()
        self._health_timer.stop()
        self._tracking = False
        self._planned_index = None
        self._planned_times = []
        self._inflight = None
        self._revoke()
        QTimer.singleShot(0, self._refresh_status)

    def on_system_resumed(self, gap_seconds: float) -> None:
        """The machine woke from sleep: look at the schedule now.

        Timers do not run while the machine sleeps, so the wake-up this
        service armed may be hours late, and a capture that was on the pool
        when the lid closed will never finish. Waiting for the next 30-second
        wake-up would be harmless; doing it now makes the recovery explicit
        and the log say so.
        """
        if not self._tracking:
            return
        self.log.info(
            "SCREENSHOT_RESUME gap=%ds; re-evaluating the schedule and any capture "
            "that was running", int(gap_seconds),
        )
        self._on_due()

    def _current_user_id(self) -> Optional[int]:
        """The signed-in user's id, if the runtime knows it."""
        try:
            info = getattr(getattr(self.runtime, "session_manager", None), "user_info", None)
            value = (info or {}).get("id")
            if value is None:
                # The profile may not be loaded yet; the cache records whose it
                # is at every sign-in and restore (`claim_cache_for`).
                value = self._cache.load_app_state(CACHE_OWNER_KEY)
            return int(value) if value is not None else None
        except Exception:  # noqa: BLE001
            return None

    def status(self) -> Dict[str, Any]:
        """The current user-facing screenshot state, for a status surface."""
        if self._status is None:
            return health.ScreenshotStatus(
                health.INACTIVE, health.SEVERITY_NONE, ""
            ).as_dict()
        return self._status.as_dict()

    # ── Configuration ─────────────────────────────────────────────────────────
    #
    # Mirrors IdleService's apply_user_profile/_refresh_config exactly
    # (background_services/idle/idle_service.py): the backend is
    # authoritative for how often this user's screen is captured, seeded
    # from the login profile and corrected periodically for a mid-session
    # admin change. Nothing here computes or guesses the interval.

    def apply_user_profile(self, user_data: Optional[Dict[str, Any]]) -> None:
        """Seed the capture interval from a `/auth/me` payload, and kick off
        an immediate privacy-config fetch.

        Called on login and on session verification, both of which already
        hold the profile -- so the user's own interval is in effect before
        tracking can start, without an extra request.

        Privacy exclusions are not part of `/auth/me` and have no equivalent
        seed, so this is also the earliest point with a guaranteed-valid
        access token to fetch them from -- `on_start()`'s own immediate
        refresh can run before login (a fresh sign-in has no token yet), so
        without this an admin's exclusion would not be enforced until the
        first periodic refresh, up to `CONFIG_REFRESH_SECONDS` later.
        """
        self._refresh_privacy_config()
        if not isinstance(user_data, dict):
            return
        user = user_data.get("user") if isinstance(user_data.get("user"), dict) else user_data
        if "capture_frequency" not in user:
            return
        self._set_capture_frequency(user.get("capture_frequency"))

    def _set_capture_frequency(self, minutes: Any) -> None:
        try:
            minutes_value = int(minutes)
        except (TypeError, ValueError):
            return
        # A zero or negative interval is not a valid interval. Ignored
        # rather than applied, so a bad read never blanks a good value --
        # the same defensive posture IdleService._set_config uses.
        if minutes_value <= 0:
            self.log.warning("ignoring non-positive capture_frequency=%r", minutes)
            return
        if minutes_value == self._capture_frequency_minutes:
            return
        self._capture_frequency_minutes = minutes_value
        self.log.info("screenshot capture frequency: %d minute(s)", minutes_value)

    def _window_seconds(self) -> int:
        """The effective window length in seconds: the user's own capture
        interval once known, else today's hardcoded/env default -- exactly
        like `IdleService._idle_minutes` defaults to 5 pre-seed."""
        if self._capture_frequency_minutes:
            return self._capture_frequency_minutes * 60
        return config.window_seconds()

    def _refresh_config(self) -> None:
        """Re-read the capture interval and privacy config from the backend."""
        if self._screenshot_api is None:
            return
        tasks = getattr(self.runtime, "tasks", None)
        if tasks is None:
            return

        def _fetch_configs():
            cfg = self._screenshot_api.get_config()
            privacy_cfg = None
            try:
                privacy_cfg = self._screenshot_api.get_privacy_config()
            except Exception as e:
                self.log.warning("screenshot privacy config refresh failed: %s", e)
            return cfg, privacy_cfg

        def on_success(results: Any) -> None:
            cfg, privacy_cfg = results
            if isinstance(cfg, dict):
                self._set_capture_frequency(cfg.get("capture_frequency"))
            if isinstance(privacy_cfg, dict):
                self._privacy_config = privacy_cfg
                # A privacy configuration that has been fetched is a
                # configuration that has been fetched, whichever request
                # fetched it. The gate used to open only for the one request
                # `apply_user_profile` made, so if *that* callback was dropped
                # every later successful refresh left capture held for good.
                self._privacy_config_loaded = True

        tasks.submit(
            _fetch_configs,
            on_success=on_success,
            # A failed refresh keeps the last known interval. Blanking a
            # valid local value because the network blipped is a
            # regression, not error handling.
            on_error=lambda exc: self.log.info("screenshot config refresh failed: %s", exc),
            key="screenshot-config",
        )

    def _refresh_privacy_config(self) -> None:
        """Fetch just the privacy exclusions, independent of the capture
        interval -- called from `apply_user_profile` at login, when the
        access token is first guaranteed valid, so an admin's exclusion is
        enforced from the first capture rather than only once the periodic
        `_refresh_config` timer first fires. Kept separate from
        `_refresh_config` so seeding privacy on login cannot also re-apply a
        stale `capture_frequency` snapshot over the value login just seeded.
        """
        if self._screenshot_api is None:
            return
        tasks = getattr(self.runtime, "tasks", None)
        if tasks is None:
            return

        def on_success(privacy_cfg: Any) -> None:
            if isinstance(privacy_cfg, dict):
                self._privacy_config = privacy_cfg
            self._privacy_config_loaded = True

        def on_error(exc: BaseException) -> None:
            self.log.warning("screenshot privacy config refresh failed: %s", exc)
            # A genuinely unreachable backend must not block every future
            # capture forever -- the same tolerance `_set_capture_frequency`
            # already has for a network blip. This only ever un-blocks a
            # capture that was waiting on the *first* attempt; a config that
            # loaded successfully once is never reverted by a later failure
            # (`on_success` above is the only place `_privacy_config` itself
            # is written).
            self._privacy_config_loaded = True

        tasks.submit(
            lambda: self._screenshot_api.get_privacy_config(),
            on_success=on_success,
            on_error=on_error,
            key="screenshot-privacy-config",
            # Not session-scoped. The result says what the organisation
            # excludes, and what it unblocks is *capture*. Under the default
            # generation guard a sign-in that landed while this request was in
            # flight dropped both callbacks, `_privacy_config_loaded` stayed
            # False, and every capture of the process was held -- retried every
            # two seconds -- until the app was restarted.
            guard_generation=False,
        )

    # ── Capture authorisation ─────────────────────────────────────────────────

    def _authorize(self, entry_id: Optional[int], client_op: Optional[str]) -> None:
        """Open a new tracking generation and permit captures in it."""
        with self._auth_lock:
            self._generation += 1
            self._authorized = True
            self._entry_id = entry_id
            self._client_op = client_op
            generation = self._generation
        self.log.info(
            "screenshot scheduler started: time_entry_id=%s client_op=%s generation=%d",
            entry_id, client_op, generation,
        )

    def _revoke(self) -> None:
        """Close the current generation. Any capture scheduled in it aborts."""
        with self._auth_lock:
            self._generation += 1
            self._authorized = False
            self._entry_id = None
            self._client_op = None

    def _current_generation(self) -> int:
        with self._auth_lock:
            return self._generation

    def _entry_id_for(self, generation: int, fallback: Optional[int]) -> Optional[int]:
        """The freshest entry id for `generation`, or `fallback`.

        Read at the moment a row is written, so an id that arrived while the
        capture was still being encoded reaches the row that capture produces.

        Only the *current* generation can answer: a stop and any task switch
        both advance it, so a match means this is still the same tracking
        session, and a capture can never be attributed to a task that was not
        the one running when it was taken.
        """
        with self._auth_lock:
            if generation == self._generation and self._entry_id is not None:
                return self._entry_id
        return fallback

    def _check_authorized(
        self, generation: int
    ) -> Tuple[bool, Optional[int], Optional[str], Optional[str]]:
        """
        Check whether a capture is permitted right now.

        Returns (allowed, entry_id, client_op, reason). The capture must
        be attributed to the returned session tokens rather than reading them
        live again afterwards.
        """
        with self._auth_lock:
            if generation != self._generation:
                return False, None, None, "stale_scheduler_generation"
            if not self._authorized:
                return False, None, None, "timer_stopped"
            entry_id = self._entry_id
            client_op = self._client_op

        # 3. Privacy configuration check. A real API service that has not
        # resolved its first fetch yet must hold captures rather than let
        # them through unchecked -- see `_privacy_config_loaded`'s own
        # docstring. `screenshot_api is None` (no backend configured at all,
        # as in most of this file's own tests) is a different case and is
        # not gated: there is no privacy config to ever arrive.
        if self._screenshot_api is not None and not self._privacy_config_loaded:
            return False, None, None, "privacy_config_pending"

        if self._privacy_config:
            app_name, window_title, _exe_path, _pid, hwnd = get_active_window_details()
            if app_name is not None:
                # Check if it's a browser
                browser_info = get_browser_manager().extract_browser_info(app_name, window_title, hwnd or 0)
                
                # We need to map `excluded_applications` back to the app `process_name`.
                apps_by_id = {app["id"]: app for app in self._privacy_config.get("applications", [])}
                urls_by_id = {url["id"]: url for url in self._privacy_config.get("urls", [])}
                
                # Check URLs first if it's a browser
                if browser_info and browser_info.url:
                    for excl in self._privacy_config.get("excluded_urls", []):
                        url_obj = urls_by_id.get(excl["url_id"])
                        if url_obj:
                            pattern = url_obj["url_pattern"].replace("*", "")
                            if pattern.lower() in browser_info.url.lower():
                                return False, None, None, f"url excluded by privacy config ({url_obj['domain']})"
                
                # Check Applications. `app_name` comes from
                # `get_active_window_details()`, which -- per
                # `tracking/active_window.py` and the module docstring of
                # `tracking/app_identity.py` -- always reports the
                # executable's base name with any `.exe`/`.app`/`.bat`/`.cmd`
                # suffix already stripped (e.g. "chrome", not "chrome.exe").
                # `screenshot_applications.process_name` is seeded and
                # entered by admins *with* that suffix (e.g. "chrome.exe" --
                # see `scripts/seed_screenshot_privacy.py` and the admin
                # form's own "e.g., slack.exe" placeholder), so both sides
                # are normalised the same way before comparing. Comparing the
                # raw strings (as this used to) never matched anything on
                # Windows, for any application, and no exclusion ever took
                # effect.
                for excl in self._privacy_config.get("excluded_applications", []):
                    app_obj = apps_by_id.get(excl["application_id"])
                    if app_obj and _strip_known_suffix(app_obj["process_name"]).lower() == app_name.lower():
                        return False, None, None, f"application excluded by privacy config ({app_name})"

        return True, entry_id, client_op, None

    # ── Window budget ─────────────────────────────────────────────────────────

    def _spent(self, index: int) -> int:
        """How many captures the given window has already taken."""
        try:
            record = self._cache.load_app_state(SCREENSHOT_WINDOW_KEY)
        except Exception:  # noqa: BLE001
            self.log.exception("could not read the screenshot window budget")
            return 0
        if not isinstance(record, dict) or record.get("index") != index:
            return 0
        return int(record.get("captured", 0))

    def _record_capture(self, index: int) -> None:
        try:
            self._cache.save_app_state(
                SCREENSHOT_WINDOW_KEY,
                {"index": index, "captured": self._spent(index) + 1},
            )
        except Exception:  # noqa: BLE001
            self.log.exception("could not persist the screenshot window budget")

    # ── Loop ──────────────────────────────────────────────────────────────────

    def _on_due(self) -> None:
        """The scheduled instant arrived. Runs on the GUI thread; does no work.

        Whatever happens inside, the schedule is armed again on the way out.
        This slot used to re-arm as its last statement, so any exception
        between the top and the bottom left the single-shot timer stopped and
        the service silent for the rest of the session -- while the timer, the
        activity tracker and everything else carried on looking healthy.
        """
        if not self._tracking:
            return
        try:
            self._run_due()
        except Exception:  # noqa: BLE001
            self._scheduler_errors += 1
            self.log.exception(
                "SCREENSHOT_SCHEDULER_ERROR errors=%d; the schedule is re-armed and "
                "will try again", self._scheduler_errors,
            )
        finally:
            self._arm_safely()

    def _run_due(self) -> None:
        now = time.time()
        window = self._window_seconds()
        index = scheduler.window_index(now, window)
        outcome = self._roll_window(index)
        self._check_stuck()

        if not self._capture_available():
            outcome.held = "unavailable"
            return
        if outcome.held == "unavailable":
            outcome.held = None

        self._plan(index, window, now)

        # Take everything that has come due. Normally one; a machine that was
        # suspended can wake with several past instants in the same window, and
        # capturing the same screen twice a second apart is pointless — so the
        # whole overdue set counts as a single capture.
        if any(t <= now for t in self._planned_times):
            future = [t for t in self._planned_times if t > now]
            if outcome.taken >= config.screenshots_per_window():
                # The window already holds what it is owed -- a capture
                # abandoned as stuck can still finish late, and its retry was
                # planned meanwhile. A second image is not a safer image.
                self._planned_times = future
            elif self._inflight is not None:
                # One is still running. The instant is not spent: look again
                # shortly rather than dropping it, which is how a slow capture
                # used to cost the window its screenshot.
                self._planned_times = [now + self.INFLIGHT_RECHECK_SECONDS] + future
            else:
                self._planned_times = future
                self._submit_capture(index)
                self.heartbeat()

    def _plan(self, index: int, window: int, now: float) -> None:
        """Plan a window's capture instants, once per window."""
        if index == self._planned_index:
            return
        self._planned_index = index
        self._planned_times = scheduler.plan_window(
            index, window, config.screenshots_per_window(), now,
            already_captured=self._spent(index),
        )
        if self._planned_times:
            self.log.info(
                "window %d: %d capture(s) planned at %s",
                index, len(self._planned_times),
                ", ".join(
                    datetime.fromtimestamp(t).strftime("%H:%M:%S")
                    for t in self._planned_times
                ),
            )

    def _arm(self) -> None:
        """Re-arm for the next planned capture, or for the next window."""
        if not self._tracking:
            return
        now = time.time()
        window = self._window_seconds()
        index = self._planned_index
        if index is None:
            index = scheduler.window_index(now, window)
        if self._planned_times:
            target = self._planned_times[0]
        else:
            # Nothing left in this window; wake at the next window's start to
            # plan it. Never a per-second poll.
            target = scheduler.window_bounds(index + 1, window)[0]
        self._due_timer.start(
            max(250, min(self.MAX_SLEEP_MS, int((target - now) * 1000)))
        )

    def _arm_safely(self) -> None:
        """`_arm`, with a fallback so arming itself can never end the schedule."""
        try:
            self._arm()
        except Exception:  # noqa: BLE001
            self.log.exception(
                "SCREENSHOT_SCHEDULER_ERROR could not compute the next wake-up; "
                "falling back to a plain %d ms wake-up", self.MAX_SLEEP_MS,
            )
            try:
                self._due_timer.start(self.MAX_SLEEP_MS)
            except Exception:  # noqa: BLE001
                self.log.exception("SCREENSHOT_SCHEDULER_ERROR the schedule timer would not start")

    def _on_health_tick(self) -> None:
        """The watchdog: the schedule must be running whenever tracking is."""
        if not self._tracking:
            return
        try:
            if not self._due_timer.isActive():
                self.log.error(
                    "SCREENSHOT_SCHEDULER_REVIVED the schedule timer was not running "
                    "while tracking; re-arming it"
                )
                self._on_due()
            else:
                self._check_stuck()
            self._refresh_status()
        except Exception:  # noqa: BLE001
            self.log.exception("SCREENSHOT_SCHEDULER_ERROR the watchdog failed")

    # ── Windows and their outcomes ────────────────────────────────────────────

    def _roll_window(self, index: int) -> _WindowOutcome:
        """The current window's outcome, closing the previous one if it ended."""
        outcome = self._outcome
        if outcome is not None and outcome.index == index:
            return outcome
        if outcome is not None:
            self._finalize_outcome(outcome)
        self._outcome = _WindowOutcome(
            index, scheduler.window_bounds(index, self._window_seconds())[0]
        )
        return self._outcome

    def _outcome_for(self, index: Optional[int]) -> Optional[_WindowOutcome]:
        """The live outcome for `index`; None for a window already left."""
        outcome = self._outcome
        if outcome is None:
            return None
        if index is None or outcome.index == index:
            return outcome
        return None

    def _finalize_current_outcome(self, direct: bool = False) -> None:
        outcome = self._outcome
        self._outcome = None
        if outcome is not None:
            self._finalize_outcome(outcome, direct=direct)

    def _finalize_outcome(self, outcome: _WindowOutcome, direct: bool = False) -> None:
        """Record an unresolved window, once. A window with an image needs nothing."""
        if outcome.captured or outcome.reported:
            return
        if outcome.held in _HELD_EVENTS:
            state, reason = _HELD_EVENTS[outcome.held]
            detail = None
        elif outcome.failures:
            state, reason, detail = "failed", outcome.reason, outcome.detail
        else:
            return
        outcome.reported = True
        if state == "failed":
            self._consecutive_failed_windows += 1
        self._emit_event(outcome, state, reason or "unknown", detail, direct=direct)

    def _emit_event(
        self, outcome: _WindowOutcome, state: str, reason: str, detail: Optional[str],
        direct: bool = False,
    ) -> None:
        """Queue one capture event for the backend. Never blocks the GUI thread.

        `direct` writes it here and now. Only the service's own shutdown uses
        it: the pool is already refusing work by then, and one small insert on
        the way out is the price of not losing the window's explanation.
        """
        start = outcome.start
        event = {
            "event_id": str(uuid.uuid4()),
            "event_state": state,
            "window_start": _iso(start),
            "occurred_at": _iso(time.time()),
            "reason": reason,
            "detail": detail,
            "attempts": outcome.failures,
            "time_entry_id": self._entry_id,
            "owner_user_id": self._owner_user_id,
        }
        self.log.warning(
            "SCREENSHOT_WINDOW_UNRESOLVED window=%s state=%s reason=%s attempts=%d "
            "entry=%s detail=%s",
            _iso(start), state, reason, outcome.failures, self._entry_id, detail,
        )

        def write() -> None:
            self._cache.save_screenshot_event(
                event["event_id"], state, event["window_start"], event["occurred_at"],
                reason=reason, detail=detail, attempts=outcome.failures,
                time_entry_id=event["time_entry_id"], owner_user_id=event["owner_user_id"],
            )

        def done(_result: Any = None) -> None:
            sync = getattr(self.runtime, "sync", None)
            if sync is not None:
                sync.wake()

        tasks = getattr(self.runtime, "tasks", None)
        if tasks is None or direct:
            try:
                write()
            except Exception:  # noqa: BLE001
                self.log.exception("SCREENSHOT_EVENT_LOST could not queue the event")
            else:
                done()
            return
        tasks.submit(
            write,
            on_success=done,
            on_error=lambda exc: self.log.error(
                "SCREENSHOT_EVENT_LOST could not queue the event: %s", exc
            ),
            key=f"screenshot-event:{event['event_id']}",
            # A local write that must happen whoever is signed in by the time
            # the pool reaches it; the row carries its owner.
            guard_generation=False,
        )

    # ── Submitting a capture ──────────────────────────────────────────────────

    def _check_stuck(self) -> None:
        """Abandon a capture that has not reported back, and retry the window."""
        inflight = self._inflight
        if inflight is None:
            return
        token, started, index = inflight
        age = _wall_time() - started
        if age < self.CAPTURE_STUCK_SECONDS:
            return
        self._inflight = None
        self.log.error(
            "SCREENSHOT_CAPTURE_STUCK token=%d window=%d age=%ds; abandoning it and "
            "capturing again", token, index, int(age),
        )
        self._on_capture_failed(index, "capture_stuck", f"no result after {int(age)}s")

    def _submit_capture(self, index: int) -> None:
        """Run one capture on the shared pool, never on the GUI thread.

        One at a time: `_inflight` serialises captures, so two can never
        overlap. The pool's own de-duplication by key is kept for the normal
        case, but it is not what this relies on -- a task that never returns
        holds its key for ever, and with it every later capture.
        """
        tasks = getattr(self.runtime, "tasks", None)
        if tasks is None:
            return
        with self._auth_lock:
            running = self._running_captures
        if running >= self.MAX_RUNNING_CAPTURES:
            self.log.error(
                "SCREENSHOT_CAPTURE_REFUSED window=%d: %d capture(s) are still running "
                "on the task pool; not starting another, so the pool keeps threads "
                "for everything else", index, running,
            )
            self._on_capture_failed(
                index, "capture_stuck", f"{running} earlier capture(s) have not returned",
            )
            return
        if self._entry_id is None:
            session = self.runtime.timer.active_session() or {}
            with self._auth_lock:
                self._entry_id = session.get("entry_id")

        # The generation this capture belongs to. If tracking stops or switches
        # before the pool gets to it, this will no longer be current and the
        # capture aborts untaken.
        generation = self._current_generation()

        self._token_counter += 1
        token = self._token_counter
        self._inflight = (token, _wall_time(), index)
        # Always its own key. `_inflight` is what serialises captures; the pool's
        # de-duplication by key must never be what decides whether one runs, or a
        # single wedged task -- or one still running across a stop and start --
        # makes every later submission quietly return None.
        key = f"screenshot-capture:{token}"
        owner = self._owner_user_id        # whose screen this is, as of now

        handle = tasks.submit(
            lambda: self._capture_now(index, generation, owner),
            on_success=lambda record, t=token: self._on_captured(record, t),
            on_error=lambda exc, t=token: self._on_capture_error(exc, index, t),
            key=key,
            # A capture belongs to the session that was tracking when it was
            # taken; if that session ended while it ran, there is nothing to
            # publish.
            guard_generation=True,
        )
        if handle is None and self._inflight is not None and self._inflight[0] == token:
            # Refused (the pool is shutting down). Not a failure of the screen;
            # try again shortly rather than spending the instant.
            self._inflight = None
            self._retry_capture_in(self.REJECTED_RETRY_SECONDS)

    def _clear_inflight(self, token: Optional[int]) -> None:
        if token is None:
            return
        if self._inflight is not None and self._inflight[0] == token:
            self._inflight = None

    def _on_capture_error(self, exc: BaseException, index: int, token: Optional[int]) -> None:
        """The capture task raised. Back on the GUI thread."""
        self._clear_inflight(token)
        self.log.error(
            "SCREENSHOT_CAPTURE_ERROR window=%s %s: %s",
            index, type(exc).__name__, exc,
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        self._on_capture_failed(index, "capture_exception", f"{type(exc).__name__}: {exc}")

    def _on_captured(self, record: Optional[Dict[str, Any]], token: Optional[int] = None) -> None:
        """Publish a completed capture. Back on the GUI thread."""
        self._clear_inflight(token)
        if not record:
            # Authorisation was withdrawn before the screen was read (the timer
            # stopped, the task changed). Nothing was expected, nothing failed.
            return

        index = record.get("index")

        if record.get("blocked"):
            outcome = self._outcome_for(index)
            if outcome is not None:
                outcome.held = "blocked"
            self._on_capture_blocked(record["blocked"], record.get("detail", ""))
            self._refresh_status()
            return

        # If the capture was skipped because of privacy controls, we want to
        # resume instantly when they leave the excluded app. We do this by
        # scheduling a retry 2 seconds from now.
        if record.get("excluded"):
            self._on_capture_held(index, record.get("reason"))
            return

        if record.get("failed"):
            self._on_capture_failed(index, record["failed"], record.get("detail"))
            return

        # A real capture went through, so any earlier refusal by the OS is over.
        outcome = self._outcome_for(index)
        if outcome is not None:
            outcome.captured = True
            outcome.taken += 1
            outcome.held = None
            if outcome.taken >= config.screenshots_per_window():
                # Done for the window: drop any retry planned while this capture
                # was thought lost.
                window_end = outcome.start + self._window_seconds()
                self._planned_times = [t for t in self._planned_times if t > window_end]
        self._consecutive_failed_windows = 0
        self._last_failure_reason = None
        self._privacy_pending_since = None
        self._on_capture_unblocked()
        self.screenshot_captured.emit(record)
        # Upload promptly rather than on the sync loop's idle cadence.
        sync = getattr(self.runtime, "sync", None)
        if sync is not None:
            sync.wake()
        self._refresh_status()

    def _on_capture_held(self, index: Optional[int], reason: Optional[str]) -> None:
        """The capture was withheld on purpose (a privacy rule, or the privacy
        configuration not being known yet). Retry soon; record it if it lasts."""
        outcome = self._outcome_for(index)
        held_before = outcome.held if outcome is not None else None
        delay = 2.0
        if reason == "privacy_config_pending":
            now = _wall_time()
            if self._privacy_pending_since is None:
                self._privacy_pending_since = now
            waited = now - self._privacy_pending_since
            if now - self._privacy_last_fetch >= self.PRIVACY_REFETCH_SECONDS:
                # The request that should have opened this gate may have been
                # dropped. Ask again rather than wait for a restart.
                self._privacy_last_fetch = now
                self._refresh_privacy_config()
            if waited >= self.PRIVACY_PENDING_REPORT_SECONDS and outcome is not None:
                outcome.held = "privacy_config"
            if waited >= 20.0:
                delay = 10.0
        else:
            self._privacy_pending_since = None
            if outcome is not None:
                outcome.held = "excluded"
        self._retry_capture_in(delay)
        # Retried every couple of seconds for as long as the user stays in an
        # excluded application; the status only needs recomputing when what is
        # being held (or why) has changed, not on every one of those retries.
        if (outcome.held if outcome is not None else None) != held_before:
            self._refresh_status()

    def _retry_delay(self, failures: int) -> float:
        delays = self.CAPTURE_RETRY_DELAYS
        base = delays[min(max(failures, 1) - 1, len(delays) - 1)]
        return base * (0.75 + 0.5 * random.random())

    def _on_capture_failed(
        self, index: Optional[int], reason: str, detail: Optional[str]
    ) -> None:
        """A capture attempt produced no image. Retry inside the window, or record it.

        This is the path that did not exist. A failure used to return `None`:
        the instant was already spent, the window's budget was not, nothing was
        scheduled and nothing was recorded, so the window ended with activity
        and no screenshot and no one -- on this machine or the server -- able to
        say why.
        """
        if index is None and self._outcome is not None:
            index = self._outcome.index
        outcome = self._outcome_for(index)
        late = outcome is None
        if late and index is None:
            # Nothing says which window this was, and a window the server would
            # place in 1970 is worse than none; it is logged, not recorded.
            self.log.error(
                "SCREENSHOT_CAPTURE_ATTEMPT_FAILED with no window reason=%s detail=%s",
                reason, detail,
            )
            return
        if late:
            # The window has already been left (a slow failure, an abandoned
            # capture reporting back). It can no longer be retried; record it.
            outcome = _WindowOutcome(
                index, scheduler.window_bounds(index, self._window_seconds())[0]
            )
        outcome.failures += 1
        outcome.reason = reason
        outcome.detail = detail
        self._last_failure_reason = reason
        self.log.warning(
            "SCREENSHOT_CAPTURE_ATTEMPT_FAILED window=%s attempt=%d reason=%s detail=%s",
            outcome.index, outcome.failures, reason, detail,
        )
        if late or not self._tracking:
            self._finalize_outcome(outcome)
            self._refresh_status()
            return

        delay = self._retry_delay(outcome.failures)
        window_end = scheduler.window_bounds(outcome.index, self._window_seconds())[1]
        if (
            outcome.failures < self.CAPTURE_MAX_ATTEMPTS
            and time.time() + delay <= window_end - self.CAPTURE_RETRY_MARGIN_SECONDS
        ):
            self.log.info(
                "SCREENSHOT_CAPTURE_RETRY window=%s attempt=%d next_in=%.0fs",
                outcome.index, outcome.failures + 1, delay,
            )
            self._retry_capture_in(delay)
        else:
            self._finalize_outcome(outcome)
        self._refresh_status()

    # ── Status ────────────────────────────────────────────────────────────────

    def _refresh_status(self) -> None:
        """Recompute the user-facing state off the GUI thread and publish a change."""
        tasks = getattr(self.runtime, "tasks", None)
        if tasks is None:
            return
        outcome = self._outcome
        held = None
        if outcome is not None and not outcome.captured:
            held = outcome.held if outcome.held in ("excluded", "privacy_config") else None
        inputs = {
            "tracking": self._tracking,
            "blocked_state": self._access_blocked_state,
            "consecutive_failed_windows": self._consecutive_failed_windows,
            "last_failure_reason": self._last_failure_reason,
            "held": held,
        }

        def compute() -> "health.ScreenshotStatus":
            queue = self._cache.screenshot_queue_summary()
            last = self._cache.load_app_state(SCREENSHOT_LAST_UPLOAD_KEY)
            at = last.get("at") if isinstance(last, dict) else None
            return health.derive_status(**inputs, queue=queue, last_upload_at=at)

        def failed(exc: BaseException) -> None:
            self.log.warning("screenshot status refresh failed: %s", exc)
            self._rerun_status_if_dirty()

        def applied(status: "health.ScreenshotStatus") -> None:
            self._apply_status(status)
            self._rerun_status_if_dirty()

        handle = tasks.submit(
            compute,
            on_success=applied,
            on_error=failed,
            key="screenshot-status",
            guard_generation=False,
        )
        if handle is None:
            # A refresh is already running and may have read the queue before
            # whatever prompted this one. Run again when it lands, or the line
            # can stay behind the truth until something else happens to move it.
            self._status_dirty = True

    def refresh_status(self) -> None:
        """Recompute the status now. Called when an upload has been confirmed."""
        self._refresh_status()

    def _rerun_status_if_dirty(self) -> None:
        if self._status_dirty:
            self._status_dirty = False
            self._refresh_status()

    def _apply_status(self, status: "health.ScreenshotStatus") -> None:
        previous = self._status
        if status.same_as(previous):
            self._status = status
            return
        self._status = status
        self.log.info(
            "SCREENSHOT_STATUS state=%s severity=%s headline=%r pending=%d",
            status.state, status.severity, status.headline, status.pending,
        )
        self.status_changed.emit(status.as_dict())
        # One notice on entering a failed state, never one per poll. Blocked is
        # announced by its own, more specific, notification.
        if status.state == health.FAILED and (previous is None or previous.state != health.FAILED):
            notifications = getattr(self.runtime, "notifications", None)
            if notifications is not None:
                notifications.notify(
                    status.detail or status.headline, NotificationLevel.WARNING,
                    title=status.headline, key="screenshot-failed",
                )

    # ── Screen access (macOS Screen Recording) ────────────────────────────────

    def _probe_screen_access(self) -> None:
        """Ask, on the pool, whether the screen can be read -- so a user who
        has not granted macOS Screen Recording hears so as they press Start.
        A no-op wherever the OS has no such gate."""
        if not screen_access.required():
            return
        tasks = getattr(self.runtime, "tasks", None)
        if tasks is None:
            return
        tasks.submit(
            screen_access.check_screen_access,
            on_success=self._on_access_probed,
            on_error=lambda exc: self.log.warning("screen access probe failed: %s", exc),
            key="screenshot-access-probe",
            guard_generation=True,
        )

    def _on_access_probed(self, status: Optional[screen_access.AccessStatus]) -> None:
        if status is None or status.allowed or not self._tracking:
            return
        self._on_capture_blocked(status.state.value, status.detail)

    def _on_capture_blocked(self, state_value: str, detail: str) -> None:
        """The OS will not let the screen be read. Back on the GUI thread.

        Said once on entering the state; retried quietly until it clears."""
        try:
            state = screen_access.ScreenAccess(state_value)
        except ValueError:
            state = screen_access.ScreenAccess.DENIED
        if self._access_blocked_state != state.value:
            self._access_blocked_state = state.value
            self.log.warning(
                "SCREENSHOT_PERMISSION_BLOCKED state=%s detail=%s; no screenshot "
                "will be recorded until macOS Screen Recording is in effect for "
                "Monitra", state.value, detail,
            )
            if state is screen_access.ScreenAccess.DENIED:
                # The supported way to put Monitra in the Screen Recording
                # list and in front of the user. Grants nothing by itself.
                screen_access.request_screen_access()
            title, message = screen_access.guidance(state)
            notifications = getattr(self.runtime, "notifications", None)
            if notifications is not None:
                notifications.notify(
                    message, NotificationLevel.WARNING, title=title,
                    key=f"screenshot-permission:{state.value}",
                    link=(
                        screen_access.SETTINGS_URL
                        if state is screen_access.ScreenAccess.DENIED else None
                    ),
                )
            self.capture_blocked.emit(state.value)
        self._retry_capture_in(self.PERMISSION_RETRY_SECONDS)

    def _on_capture_unblocked(self) -> None:
        """A capture went through: any earlier refusal is over."""
        if self._access_blocked_state is None:
            return
        self.log.info(
            "SCREENSHOT_PERMISSION_RESTORED after %s; screenshots have resumed",
            self._access_blocked_state,
        )
        self._access_blocked_state = None
        notifications = getattr(self.runtime, "notifications", None)
        if notifications is not None:
            notifications.notify(
                "Screen Recording is on, so screenshots have resumed.",
                NotificationLevel.SUCCESS, title="Screenshots resumed",
                key="screenshot-permission:restored",
            )

    def _retry_capture_in(self, seconds: float) -> None:
        """Put one more capture instant `seconds` ahead of the plan."""
        retry_time = time.time() + seconds
        if not self._planned_times or self._planned_times[0] > retry_time:
            self._planned_times.insert(0, retry_time)
        self._arm_safely()

    def _capture_available(self) -> bool:
        """Whether this machine can capture and process a screenshot at all."""
        if capture.supported() and image_processor.supported():
            return True
        if not self._unavailable_reported:
            self._unavailable_reported = True
            reason = (
                "screen capture is unavailable on this installation"
                if not capture.supported()
                else "image processing is unavailable on this installation"
            )
            self.log.warning("%s; no screenshots will be captured", reason)
            self.capture_unavailable.emit(reason)
        return False

    # ── Capture ───────────────────────────────────────────────────────────────

    def _capture_now(
        self, index: int, generation: int, owner_user_id: Any = _LIVE_OWNER
    ) -> Optional[Dict[str, Any]]:
        """Capture, process, persist and queue one screenshot.

        Runs on a pool thread. It touches no widgets and no Qt objects — the
        result is handed back through `on_success`, which the TaskRunner
        delivers on the GUI thread.

        The authorisation check is the first statement for a reason: it has to
        happen before the screen is read, not after. Capturing and then
        discarding would mean the user's screen was photographed at a moment
        they were not tracking, which is the thing this rule exists to prevent
        — deleting the file afterwards does not undo that.

        Returns None only when authorisation was withdrawn (nothing was
        expected). Every other outcome is a dict the GUI thread acts on:
        a queued capture, `{"blocked"}`, `{"excluded"}` or `{"failed"}` --
        never a bare None, which is what used to make a failed capture
        indistinguishable from one that was never due.
        """
        with self._auth_lock:
            self._running_captures += 1
        try:
            return self._capture_now_inner(index, generation, owner_user_id)
        except Exception as exc:  # noqa: BLE001
            self.log.exception("SCREENSHOT_CAPTURE_ERROR window=%s", index)
            return {
                "failed": "capture_exception", "index": index,
                "detail": f"{type(exc).__name__}: {exc}",
            }
        finally:
            with self._auth_lock:
                self._running_captures -= 1

    def _capture_now_inner(
        self, index: int, generation: int, owner_user_id: Any = _LIVE_OWNER
    ) -> Optional[Dict[str, Any]]:
        owner = self._owner_user_id if owner_user_id is _LIVE_OWNER else owner_user_id
        allowed, entry_id, client_op, reason = self._check_authorized(generation)
        if not allowed:
            self.log.info("screenshot capture aborted: reason=%s", reason)
            # Both retry soon rather than waiting out the full window: an
            # exclusion the user just left, and a privacy config that has
            # simply not finished its first load yet, are each transient --
            # unlike "timer_stopped" or a stale generation, which mean this
            # window's capture is not coming back at all.
            if reason and ("excluded by privacy config" in reason or reason == "privacy_config_pending"):
                # No window index: a held capture is retried against whatever window
                # is current when the retry runs, and the pinned shape of this
                # record (tests/test_screenshot_privacy_config_race.py) is exactly
                # these two keys.
                return {"excluded": True, "reason": reason}
            return None

        # The OS's permission is checked immediately before the screen is read,
        # because that is the only moment it can be trusted: macOS answers a
        # capture without Screen Recording with a valid image of the wallpaper
        # (and Monitra's own windows), not with an error, so an unchecked
        # capture is uploaded as the user's work. Nothing is read, queued or
        # counted against the window's budget when access is refused.
        access = screen_access.check_screen_access()
        mac_diagnostics.log_access(access)   # logging only; no-op off macOS
        if not access.allowed:
            self.log.info(
                "screenshot capture skipped: screen access %s (%s)",
                access.state.value, access.detail,
            )
            return {"blocked": access.state.value, "detail": access.detail}

        # One capture event reads every attached display and produces exactly
        # one image. The display count is metadata on that single event — it
        # never becomes a second capture, a second queue row or a second
        # upload, whatever the machine has plugged in.
        merged = capture.capture_all_displays()
        # The instant the screen was read, not the instant it finished encoding:
        # the backend files the capture into its window by this timestamp, and
        # the encode can take a second or more on a dense screen. Stamping after
        # the encode moved a capture taken in the last second of a window into
        # the next one, leaving the window it was taken for showing activity and
        # no screenshot.
        captured_at = datetime.now(timezone.utc)
        if merged is None:
            # Already logged. The window's budget is deliberately not spent, and
            # the caller retries inside the window.
            return {
                "failed": "screen_unreadable", "index": index,
                "detail": "no display could be read",
            }
        mac_diagnostics.log_capture(merged)  # logging only; no-op off macOS

        processed = image_processor.process_merged(merged)
        if processed is None or not processed.data:
            return {
                "failed": "encode_failed", "index": index,
                "detail": "the captured frame could not be encoded",
            }

        client_screenshot_id = str(uuid.uuid4())
        path = store.write_screenshot(client_screenshot_id, processed.data, captured_at)
        if path is None:
            return {
                "failed": "store_failed", "index": index,
                "detail": "the image could not be written to the local cache",
            }

        window_start = _iso(scheduler.window_bounds(index, self._window_seconds())[0])

        # Attribution is read again here rather than reused from the check at
        # the top. The grab and the encode take a second or more on a dense
        # screen, and the backend's entry id can land inside that second: the
        # `bind_entry_id` that ran when it arrived had no row to adopt yet, and
        # the row written afterwards would keep the stale `None` forever --
        # withheld by the uploader, adopted by nothing, never uploaded. That is
        # not theoretical; it was observed in a real desktop run, one second
        # after the id arrived.
        #
        # The generation must still match, so this can only ever pick up the id
        # of the session this capture was authorised for, never a later one.
        entry_id = self._entry_id_for(generation, entry_id)

        try:
            self._cache.save_screenshot(
                client_screenshot_id=client_screenshot_id,
                local_file_path=str(path),
                captured_at=captured_at.isoformat(),
                window_start=window_start,
                width=processed.width,
                height=processed.height,
                file_size_bytes=processed.size_bytes,
                time_entry_id=entry_id,
                monitor_number=merged.monitor_number,
                display_count=processed.display_count,
                client_op=client_op,
                owner_user_id=owner,
            )
        except Exception as exc:  # noqa: BLE001
            self.log.exception("could not queue screenshot %s", client_screenshot_id)
            store.delete_screenshot(str(path))
            return {
                "failed": "queue_failed", "index": index,
                "detail": f"{type(exc).__name__}: {exc}",
            }

        self._record_capture(index)
        self.log.info(
            "SCREENSHOT_QUEUED id=%s entry=%s display_count=%d displays_expected=%d "
            "size=%dx%d bytes=%d quality=%d primary_size=%d fallback_triggered=%s "
            "target_size=%d attempts=%d",
            client_screenshot_id, entry_id, processed.display_count,
            merged.displays_expected, processed.width, processed.height,
            processed.size_bytes, processed.quality,
            processed.primary_size_bytes, processed.fallback_applied,
            processed.fallback_target_bytes, processed.fallback_attempts,
        )
        return {
            "client_screenshot_id": client_screenshot_id,
            "time_entry_id": entry_id,
            "captured_at": captured_at.isoformat(),
            "window_start": window_start,
            "file_size_bytes": processed.size_bytes,
            "display_count": processed.display_count,
            "index": index,
        }

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def _warm_capture_backend(self) -> None:
        """
        Resolve `mss` and Pillow now, on the pool, rather than on the GUI
        thread at the moment the user presses Start.

        Both are imported lazily, the first time `_capture_available()` asks
        whether this machine can capture — and that question is asked from
        `_on_due()`, which runs on the GUI thread. So the first tracking
        session of every run paid for both imports inline: measured at ~50 ms
        on a warm disk here, and materially worse on a cold one or from inside
        a packaged one-file build, where the modules are unpacked before they
        are read.

        Fifty milliseconds is not a hang, but it lands on the single most
        latency-sensitive action in the application, and CLAUDE.md's rule is
        not about how long the block is: work that can take more than a few
        milliseconds does not belong on the UI thread. Doing it here costs
        nothing the run would not have spent anyway — the import happens once
        per process either way — and it means the availability check is a
        cached module read by the time Start is pressed.

        Both loaders cache their result in a module global and are safe to
        call again, so nothing changes if a capture beats this to it.
        """
        if not config.enabled():
            return
        tasks = getattr(self.runtime, "tasks", None)
        if tasks is None:
            return
        tasks.submit(
            self._probe_capture_backend,
            on_error=lambda exc: self.log.warning(
                "could not resolve the screenshot backend: %s", exc
            ),
            key="screenshot-warmup",
            # Not session-scoped: whether this machine owns a screen reader is
            # a property of the installation, not of who is signed in.
            guard_generation=False,
        )

    @staticmethod
    def _probe_capture_backend() -> bool:
        """Import both halves of the capture path. Runs on a pool thread."""
        return capture.supported() and image_processor.supported()

    def on_start(self) -> None:
        # Resolve the capture backend off the GUI thread before the first
        # tracking session can need it.
        self._warm_capture_backend()
        # Anything a previous process left claimed goes back to pending, so a
        # crash mid-upload resumes rather than stranding the file.
        try:
            recovered = self._cache.reset_uploading_screenshots()
            if recovered:
                self.log.info("recovered %d interrupted screenshot upload(s)", recovered)
        except Exception:  # noqa: BLE001
            self.log.exception("could not recover interrupted screenshot uploads")
        # Reclaim images whose queue row no longer exists — a process killed
        # between the file write and the insert, or a queue cleared at logout.
        # Nothing else removes them, so on a long-lived install they would
        # accumulate indefinitely.
        try:
            store.prune_orphans(self._cache.get_screenshot_backlog_paths())
        except Exception:  # noqa: BLE001
            self.log.exception("could not prune orphaned screenshot files")
        self._config_refresh_timer.start()
        # Privacy exclusions have no login-time seed the way capture_frequency
        # does (apply_user_profile only carries the interval): without this,
        # `_privacy_config` stayed `{}` -- and therefore unenforced, see
        # `_check_authorized` -- for the first CONFIG_REFRESH_SECONDS after
        # every app start, since `QTimer.start()` does not fire immediately.
        # An admin's exclusion must be in effect before the first capture can
        # happen, not up to three minutes after. Only the privacy half, not
        # the full config: a restored session already has a valid token here
        # (unlike a fresh login, where apply_user_profile is what seeds it),
        # but capture_frequency already has its own login-time seed and does
        # not need a second, redundant fetch on top of it.
        self._refresh_privacy_config()
        super().on_start()

    def on_stop(self, timeout_ms: int) -> bool:
        # Nothing to wind down but the schedule: any capture still running is
        # on the shared pool, which the runtime drains before it stops
        # services.
        if self._tracking:
            # An update restart or an OS shutdown never calls `stop_tracker` (the
            # timer is deliberately left running for recovery), so a window that
            # is unresolved right now would otherwise be lost with the process.
            try:
                self._finalize_current_outcome(direct=True)
            except Exception:  # noqa: BLE001
                self.log.exception("could not record the unresolved window at shutdown")
        self._due_timer.stop()
        self._health_timer.stop()
        self._config_refresh_timer.stop()
        self._tracking = False
        # A capture already on the pool must not take the screen during
        # shutdown either; the runtime drains the pool after this, so without
        # revoking here that drain could still photograph the screen.
        self._revoke()
        return True

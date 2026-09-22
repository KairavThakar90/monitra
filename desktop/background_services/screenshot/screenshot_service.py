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
"""
from __future__ import annotations

import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from PySide6.QtCore import QTimer, Signal

from background_services.screenshot import capture, config, image_processor, scheduler, store
from core.service import BaseService
from tracking.active_window import get_active_window_details
from tracking.browsers.manager import get_browser_manager

#: Durable record of the window budget already spent, so a restart inside a
#: window cannot exceed `SCREENSHOTS_PER_WINDOW`.
SCREENSHOT_WINDOW_KEY = "screenshot_window_state"


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


class ScreenshotService(BaseService):
    """
    Owns screenshot capture.

    Signals:
        screenshot_captured(dict) — one capture queued for upload
        capture_unavailable(str)  — this machine cannot take screenshots
    """

    name = "screenshot"

    screenshot_captured = Signal(dict)
    capture_unavailable = Signal(str)

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

    # ── Tracker contract (driven by TimerService) ─────────────────────────────

    def start_tracker(self, session: Dict[str, Any]) -> None:
        """Begin capturing for a tracking session."""
        if not config.enabled():
            self.log.info("screenshot capture is disabled by configuration")
            return
        self._tracking = True
        self._planned_index = None
        self._planned_times = []
        # Arming and authorising are the same act: nothing else in this class
        # may set `_authorized`, so there is no path from "the app is open" or
        # "the service started" to a capture.
        self._authorize(session.get("entry_id"), session.get("client_op"))
        self.log.info(
            "screenshot capture started for entry %s (%d per %ds window)",
            self._entry_id, config.screenshots_per_window(), self._window_seconds(),
        )
        # Plan and arm immediately, so a session that begins mid-window still
        # gets whatever the window has left rather than waiting for the next.
        self._on_due()

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
        self._due_timer.stop()
        self._tracking = False
        self._planned_index = None
        self._planned_times = []
        self._revoke()

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
            privacy_cfg = {}
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
                
                # Check Applications. `app_name` is the raw OS-reported
                # process name (e.g. "chrome.exe"), exactly matching how
                # `screenshot_applications.process_name` is seeded --
                # `resolve_application()` is not used here on purpose: its
                # `identity.process_name` strips the .exe/.app/.bat/.cmd
                # suffix (for the human-readable app-usage display, a
                # different feature), so comparing against it never matched
                # anything and no application exclusion could ever take
                # effect.
                for excl in self._privacy_config.get("excluded_applications", []):
                    app_obj = apps_by_id.get(excl["application_id"])
                    if app_obj and app_obj["process_name"].lower() == app_name.lower():
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
        """The scheduled instant arrived. Runs on the GUI thread; does no work."""
        if not self._tracking:
            return
        if not self._capture_available():
            self._arm()
            return

        now = time.time()
        window = self._window_seconds()
        index = scheduler.window_index(now, window)
        self._plan(index, window, now)

        # Take everything that has come due. Normally one; a machine that was
        # suspended can wake with several past instants in the same window, and
        # capturing the same screen twice a second apart is pointless — so the
        # whole overdue set counts as a single capture.
        if any(t <= now for t in self._planned_times):
            self._planned_times = [t for t in self._planned_times if t > now]
            self._submit_capture(index)
            self.heartbeat()

        self._arm()

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

    def _submit_capture(self, index: int) -> None:
        """Run one capture on the shared pool, never on the GUI thread.

        Keyed, so a capture that is somehow still running when the next instant
        arrives drops the new one rather than overlapping with it.
        """
        tasks = getattr(self.runtime, "tasks", None)
        if tasks is None:
            return
        if self._entry_id is None:
            session = self.runtime.timer.active_session() or {}
            with self._auth_lock:
                self._entry_id = session.get("entry_id")

        # The generation this capture belongs to. If tracking stops or switches
        # before the pool gets to it, this will no longer be current and the
        # capture aborts untaken.
        generation = self._current_generation()

        tasks.submit(
            lambda: self._capture_now(index, generation),
            on_success=self._on_captured,
            on_error=lambda exc: self.log.error("screenshot capture failed: %s", exc),
            key="screenshot-capture",
            # A capture belongs to the session that was tracking when it was
            # taken; if that session ended while it ran, there is nothing to
            # publish.
            guard_generation=True,
        )

    def _on_captured(self, record: Optional[Dict[str, Any]]) -> None:
        """Publish a completed capture. Back on the GUI thread."""
        if not record:
            return
        
        # If the capture was skipped because of privacy controls, we want to
        # resume instantly when they leave the excluded app. We do this by
        # scheduling a retry 2 seconds from now.
        if record.get("excluded"):
            import time
            retry_time = time.time() + 2.0
            # Insert at the front so it's the very next thing
            if not self._planned_times or self._planned_times[0] > retry_time:
                self._planned_times.insert(0, retry_time)
            self._arm()
            return
            
        self.screenshot_captured.emit(record)
        # Upload promptly rather than on the sync loop's idle cadence.
        sync = getattr(self.runtime, "sync", None)
        if sync is not None:
            sync.wake()

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

    def _capture_now(self, index: int, generation: int) -> Optional[Dict[str, Any]]:
        """Capture, process, persist and queue one screenshot.

        Runs on a pool thread. It touches no widgets and no Qt objects — the
        result is handed back through `on_success`, which the TaskRunner
        delivers on the GUI thread.

        The authorisation check is the first statement for a reason: it has to
        happen before the screen is read, not after. Capturing and then
        discarding would mean the user's screen was photographed at a moment
        they were not tracking, which is the thing this rule exists to prevent
        — deleting the file afterwards does not undo that.
        """
        allowed, entry_id, client_op, reason = self._check_authorized(generation)
        if not allowed:
            self.log.info("screenshot capture aborted: reason=%s", reason)
            # Both retry soon rather than waiting out the full window: an
            # exclusion the user just left, and a privacy config that has
            # simply not finished its first load yet, are each transient --
            # unlike "timer_stopped" or a stale generation, which mean this
            # window's capture is not coming back at all.
            if reason and ("excluded by privacy config" in reason or reason == "privacy_config_pending"):
                return {"excluded": True, "reason": reason}
            return None

        # One capture event reads every attached display and produces exactly
        # one image. The display count is metadata on that single event — it
        # never becomes a second capture, a second queue row or a second
        # upload, whatever the machine has plugged in.
        merged = capture.capture_all_displays()
        if merged is None:
            return None  # already logged; the window's budget is deliberately not spent

        processed = image_processor.process_merged(merged)
        if processed is None or not processed.data:
            return None

        client_screenshot_id = str(uuid.uuid4())
        captured_at = datetime.now(timezone.utc)
        path = store.write_screenshot(client_screenshot_id, processed.data, captured_at)
        if path is None:
            return None

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
            )
        except Exception:  # noqa: BLE001
            self.log.exception("could not queue screenshot %s", client_screenshot_id)
            store.delete_screenshot(str(path))
            return None

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
        self._due_timer.stop()
        self._config_refresh_timer.stop()
        self._tracking = False
        # A capture already on the pool must not take the screen during
        # shutdown either; the runtime drains the pool after this, so without
        # revoking here that drain could still photograph the screen.
        self._revoke()
        return True

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
    CONFIG_REFRESH_SECONDS = 15 * 60

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
        """Seed the capture interval from a `/auth/me` payload.

        Called on login and on session verification, both of which already
        hold the profile -- so the user's own interval is in effect before
        tracking can start, without an extra request.
        """
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
        """Re-read the capture interval from the backend. Off the GUI
        thread for the request; applied back on it via `on_success`."""
        if self._screenshot_api is None:
            return
        tasks = getattr(self.runtime, "tasks", None)
        if tasks is None:
            return

        def on_success(cfg: Dict[str, Any]) -> None:
            if isinstance(cfg, dict):
                self._set_capture_frequency(cfg.get("capture_frequency"))

        tasks.submit(
            self._screenshot_api.get_config,
            on_success=on_success,
            # A failed refresh keeps the last known interval. Blanking a
            # valid local value because the network blipped is a
            # regression, not error handling.
            on_error=lambda exc: self.log.info("screenshot config refresh failed: %s", exc),
            key="screenshot-config",
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
    ) -> Tuple[bool, Optional[int], Optional[str], str]:
        """
        Whether a capture scheduled in `generation` may still take the screen.

        Called on a pool thread immediately before the capture, which is the
        only check that means anything: the scheduler's check happened at an
        earlier instant and cannot speak for this one.

        The entry id is returned rather than compared, because within one
        generation it legitimately changes exactly once — from None to the
        backend's id, when an offline start is finally acknowledged. Comparing
        it to the value captured at schedule time would abort a perfectly valid
        screenshot. The generation is what identifies the session; the id
        returned here is what the screenshot is recorded against, so a capture
        can never be attributed to a task that is no longer the one running.

        The session key is returned alongside it for the same reason: a capture
        taken before the id arrives is stored against `client_op` so the
        adoption can find it later, and reading both under one lock is what
        guarantees the pair describes a single session rather than two halves
        of a switch that happened in between.

        :return: (allowed, current entry id, session key, reason when not allowed)
        """
        with self._auth_lock:
            if not self._authorized:
                return False, None, None, "timer_stopped"
            if generation != self._generation:
                return False, None, None, "stale_scheduler_generation"
            return True, self._entry_id, self._client_op, ""

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

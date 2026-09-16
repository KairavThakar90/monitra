"""
core.runtime — The single application runtime and ownership root.

Everything long-lived in Monitra hangs off one `ApplicationRuntime`: storage,
the HTTP client, the domain services, the bounded task pool and every
background service. Nothing else creates a thread, and nothing else decides
when a thread dies.

Why this exists
---------------
The audited `main()` did the following, in this order:

    local_cache = LocalCache()
    ...
    network_monitor.start()      # QThread started...
    sync_queue.start()           # ...before any QApplication exists
    app = QApplication(sys.argv) # <- created here

Starting QThreads before a `QCoreApplication` exists is undefined behaviour;
queued signal delivery has no event loop to target, so early emissions were
silently dropped and startup ordering varied run to run. Shutdown was worse:
`_shutdown_app()` waited 1000 ms for threads that were parked in a 30-second
wait, then closed the SQLite connection and the HTTP client out from under
them. The instrumented reproduction confirmed the process did not exit and had
to be killed.

Guarantees provided here
------------------------
* No background thread starts before the Qt event loop exists.
* The UI becomes usable before any remote call is awaited.
* Every service has exactly one owner and one lifecycle path.
* Shutdown is ordered, bounded, and always terminates the process:
  producers stop, then consumers, then shared resources — and shared resources
  are closed only after every owning thread is confirmed stopped.
* A service that refuses to stop is named in the log with its state, rather
  than hanging the application.
"""
from __future__ import annotations

import time
from typing import Callable, List, Optional

from PySide6.QtCore import QObject, QTimer, Signal

from app.api.client import ApiClient
from app.auth.service import AuthService
from app.auth.session import SessionManager
from app.idle.service import IdleApiService
from app.projects.service import ProjectService
from app.tasks.service import TaskService
from app.updates.service import UpdateApiService
from app.feedback.service import FeedbackApiService
from app.portal.service import PortalService
from app.time_entries.service import TimeEntryService
from background_services.activity import ActivityService
from background_services.activity.app_usage_service import AppUsageService
from background_services.activity.url_usage_service import UrlUsageService
from background_services.idle import IdleService
from background_services.network import NetworkService, NetworkState
from background_services.notifications import NotificationService
from background_services.recovery import RecoveryService
from background_services.screenshot import ScreenshotService
from background_services.sync import SyncService
from background_services.timer import TimerService
from background_services.update import UpdateService
from core.logging_setup import (
    bump_session_generation, configure_logging, get_logger,
    install_excepthook, session_generation,
)
from background_services.wellbeing import WellbeingService
from core.service import ServiceManager, ServiceState
from core.tasks import TaskRunner
from storage.manager import StorageManager, get_storage_manager
from sync.local_cache import LocalCache

log = get_logger("runtime")

#: How long an explicit quit waits for a queued stop to reach the backend
#: before exiting anyway. One stop is one request, and the API client gives
#: a Start/Stop request `TIMEOUT_FAST` (5 s) to complete, so this is exactly
#: one attempt's worth: long enough for a single round trip against a cold
#: serverless backend, short enough that Quit still behaves like Quit. The
#: stop is durable before the wait begins, so running out of budget loses
#: nothing -- the next launch delivers it with the instant the user pressed
#: Quit, and the backend records that instant as the end time.
EXIT_STOP_FLUSH_BUDGET_MS = 5_000


class RuntimePhase:
    """Phases the runtime passes through. Each has a terminal outcome."""
    CREATED = "CREATED"
    STORAGE_READY = "STORAGE_READY"
    SESSION_RESTORED = "SESSION_RESTORED"
    UI_READY = "UI_READY"
    SERVICES_RUNNING = "SERVICES_RUNNING"
    SHUTTING_DOWN = "SHUTTING_DOWN"
    STOPPED = "STOPPED"


class ApplicationRuntime(QObject):
    """
    Owns the application's non-UI lifetime.

    Construction is cheap and does no I/O beyond opening the local database.
    Background services are created but **not started** until
    `start_services()` is called, which the main window does only after the
    shell is on screen.
    """

    phase_changed = Signal(str)
    #: Emitted when a service reports a state change, for status surfaces.
    service_health_changed = Signal(str, str)  # service name, state

    def __init__(self, storage: Optional[StorageManager] = None) -> None:
        super().__init__()
        configure_logging()
        install_excepthook()

        self._phase = RuntimePhase.CREATED
        self._shutdown_started = False
        self._started_at = time.monotonic()
        #: Exit preparation (see `prepare_exit`): started, finished, who to
        #: call back, and the bounded wait.
        self._exit_prepared = False
        self._exit_done = False
        self._exit_callbacks: List[Callable[[], None]] = []
        self._exit_timer: Optional[QTimer] = None
        self._exit_connected = False

        #: Queued actions older than this generation are refused. Raised on
        #: logout so a previous user's pending work cannot execute as the next.
        self.queue_floor_generation = 0

        # ── Storage ───────────────────────────────────────────────────────────
        self.storage = storage or get_storage_manager()
        self.cache = LocalCache(storage=self.storage)
        self._set_phase(RuntimePhase.STORAGE_READY)

        # ── Domain services (no threads of their own) ─────────────────────────
        self.api_client = ApiClient()
        self.session_manager = SessionManager(local_cache=self.cache)
        self.auth_service = AuthService(self.api_client, self.session_manager)
        # Any request that comes back 401 renews the token once and retries,
        # instead of surfacing an expired access token to the user as a failure.
        # Installed here because the runtime is the only place that owns both
        # halves; the client itself knows nothing about sessions.
        self.api_client.set_refresh_hook(self.auth_service.refresh_session)
        self.project_service = ProjectService(self.api_client)
        self.task_service = TaskService(self.api_client)
        self.time_entry_service = TimeEntryService(self.api_client)
        self.idle_api = IdleApiService(self.api_client)
        self.update_api = UpdateApiService(self.api_client)
        self.feedback_service = FeedbackApiService(self.api_client)
        self.portal_service = PortalService(self.api_client)

        # ── Bounded background execution ──────────────────────────────────────
        self.tasks = TaskRunner(parent=self)

        # ── Background services ───────────────────────────────────────────────
        self.services = ServiceManager(self)

        # Registration order is start order; shutdown is the reverse. Producers
        # are registered after the consumers they feed, so producers stop first.
        self.recovery: RecoveryService = self.services.register(
            RecoveryService(self, self.cache)
        )
        self.notifications: NotificationService = self.services.register(
            NotificationService(self)
        )
        self.network: NetworkService = self.services.register(
            NetworkService(self, self.api_client)
        )
        # Announces a newer release; owns nothing the rest of the runtime
        # depends on, so it is registered after the services it reads
        # (notifications, network) and therefore stops before them.
        self.updates: UpdateService = self.services.register(
            UpdateService(self, self.update_api, self.cache)
        )
        # Wellbeing reminders. Like UpdateService it only reads -- the
        # notification service and the session -- and nothing in the runtime
        # depends on it, so it is registered after what it reads and therefore
        # stops before them.
        self.wellbeing: WellbeingService = self.services.register(
            WellbeingService(self, self.cache)
        )
        self.sync: SyncService = self.services.register(
            SyncService(self, self.cache, self.time_entry_service, self.task_service)
        )
        self.timer: TimerService = self.services.register(
            TimerService(self, self.time_entry_service, self.cache)
        )
        self.activity: ActivityService = self.services.register(
            ActivityService(self, self.cache)
        )
        self.app_usage: AppUsageService = self.services.register(
            AppUsageService(self, self.cache)
        )
        self.url_usage: UrlUsageService = self.services.register(
            UrlUsageService(self, self.cache)
        )
        # A producer for the sync queue, like the trackers above it: it writes
        # captures to the durable screenshot queue and never uploads them
        # itself. Registered after SyncService so it stops first, leaving the
        # consumer running while the last capture is being written.
        self.screenshot: ScreenshotService = self.services.register(
            ScreenshotService(self, self.cache)
        )
        # Registered last, so it is the first to stop. It observes the timer
        # and the activity probe and must not still be evaluating inactivity
        # while the services it reads are being torn down.
        self.idle: IdleService = self.services.register(
            IdleService(self, self.idle_api)
        )

        # The timer drives the sub-trackers; they never start themselves.
        self.timer.register_tracker(self.activity)
        self.timer.register_tracker(self.app_usage)
        self.timer.register_tracker(self.url_usage)
        self.timer.register_tracker(self.screenshot)

        for service in self.services.services:
            service.state_changed.connect(
                lambda state, name=service.name: self.service_health_changed.emit(name, state)
            )

        # Cross-service wiring, declared in one place rather than scattered
        # through widget constructors.
        self.network.network_state_changed.connect(self._on_network_state_changed)
        # Waking from sleep: every timer-driven cadence was paused with the
        # machine. Probe the network now rather than waiting out the healthy
        # 30s interval, and let the sync consumer find out from that probe
        # whether it can drain. (Queued: the emitter is on the recovery
        # thread, the slots belong here.)
        self.recovery.system_resumed.connect(self._on_system_resumed)

        log.info("runtime constructed in %.0fms", (time.monotonic() - self._started_at) * 1000)

    # ── Phase ─────────────────────────────────────────────────────────────────

    @property
    def phase(self) -> str:
        return self._phase

    def _set_phase(self, phase: str) -> None:
        if self._phase == phase:
            return
        log.info("phase %s -> %s", self._phase, phase)
        self._phase = phase
        self.phase_changed.emit(phase)

    # ── Startup ───────────────────────────────────────────────────────────────

    def restore_session(self) -> bool:
        """
        Restore a persisted session from local storage.

        Local only, and deliberately so: this must not wait on the network.
        The token is verified against the backend afterwards, in the
        background, while the UI is already usable.
        """
        restored = False
        try:
            restored = self.session_manager.restore_session()
        except Exception:  # noqa: BLE001
            log.exception("could not restore persisted session")
        if restored:
            bump_session_generation()
            self.queue_floor_generation = 0  # same user; keep their queued work
            log.info("restored persisted session (generation %d)", session_generation())
            # The restored token names a user; the caches may not be theirs.
            # Checked here rather than only at sign-in because this path
            # reaches the dashboard without passing through on_login at all.
            try:
                profile = self.session_manager.user_info or {}
                self.cache.claim_cache_for(profile.get("id"))
            except Exception:  # noqa: BLE001 — never block a restore on this
                log.exception("could not verify which user the local cache belongs to")
        self._set_phase(RuntimePhase.SESSION_RESTORED)
        return restored

    def inspect_previous_run(self) -> bool:
        """Determine whether the previous process exited cleanly."""
        return self.recovery.inspect_previous_run()

    def start_services(self) -> None:
        """
        Start every background service.

        Called only after the main window is visible, so no remote call can
        delay the shell appearing. By this point the Qt event loop exists,
        which is a precondition for starting any QThread.
        """
        if self._shutdown_started:
            return
        log.info("starting background services")
        self.services.start_all()
        self._set_phase(RuntimePhase.SERVICES_RUNNING)

        # Now that services exist, recover anything the previous run left.
        try:
            self.recovery.recover()
        except Exception:  # noqa: BLE001
            log.exception("recovery failed; continuing with a clean session")

    def mark_ui_ready(self) -> None:
        self._set_phase(RuntimePhase.UI_READY)

    # ── Session transitions ───────────────────────────────────────────────────

    def on_login(self, user_id: Optional[int] = None) -> None:
        """Advance the session generation for a newly authenticated user.

        `user_id` binds the read-through caches to whoever just signed in. A
        deliberate logout already clears them, so this is the backstop for
        every other way the account can change: a session that ended without
        one, a crash between the two, a token replaced underneath the client.
        Without it the dashboard paints the previous user's projects and tasks
        from cache before the first response arrives.
        """
        generation = bump_session_generation()
        log.info("login: session generation is now %d", generation)
        try:
            self.cache.claim_cache_for(user_id)
        except Exception:  # noqa: BLE001 — a cache check must not block signing in
            log.exception("could not verify which user the local cache belongs to")
        self.sync.resume_after_auth()
        # The check holds while signed out; a login is the moment it can work.
        self.updates.check_now()

    def on_logout(self) -> None:
        """
        Tear down session-scoped state.

        Raising the queue floor is what prevents user A's queued operations
        from later executing under user B's token.
        """
        generation = bump_session_generation()
        self.queue_floor_generation = generation
        log.info("logout: queue floor raised to generation %d", generation)

        self.tasks.cancel_all()
        # Before the timer stops: a pending idle period belongs to the session
        # that is ending, and its popup must not survive into the next login.
        self.idle.reset_session()
        # The next user is told about a release in their own session rather
        # than inheriting "already announced" from the previous one.
        self.updates.reset_session()
        if self.timer.is_running():
            self.timer.stop_tracking()
        # A break, and the task it holds, belong to the session that is
        # ending; the next user must not be offered Break Out into it.
        self.timer.reset_break()
        try:
            cancelled = self.cache.cancel_actions_for_generation(generation)
            if cancelled:
                log.info("cancelled %d queued action(s) from the previous session", cancelled)
            self.cache.clear_app_usage()
            self.cache.clear_activity_samples()
            # Screenshots are session-scoped captures like the activity
            # windows above, and unlike them they also own files on disk. The
            # backend would refuse them under the next user's token anyway
            # (a time entry is only writable by the user it belongs to), so
            # leaving them queued would only park images of one user's screen
            # on disk through another user's session.
            self._discard_queued_screenshots()
            self.cache.clear_app_state()
            # The read-through caches the dashboard paints from before the
            # network answers. They are not user-scoped, so leaving them
            # behind shows the next user to sign in the previous user's
            # projects and tasks.
            self.cache.clear_user_scoped_cache()
        except Exception:  # noqa: BLE001
            log.exception("could not fully clear session-scoped state")

    def _discard_queued_screenshots(self) -> None:
        """Drop the screenshot queue and the files it references."""
        from background_services.screenshot import store

        try:
            paths = self.cache.get_screenshot_backlog_paths()
        except Exception:  # noqa: BLE001
            log.exception("could not read the screenshot backlog at logout")
            return
        self.cache.clear_screenshots()
        for path in paths:
            store.delete_screenshot(path)
        try:
            store.prune_empty_day_folders()
        except Exception:  # noqa: BLE001
            log.exception("could not prune the screenshot cache at logout")
        if paths:
            log.info("discarded %d queued screenshot(s) belonging to the previous session", len(paths))

    # ── Cross-service reactions ───────────────────────────────────────────────

    def _on_network_state_changed(self, state: str) -> None:
        """Nudge the sync consumer as soon as the backend becomes usable again."""
        if state in NetworkState.USABLE:
            self.sync.wake()

    def _on_system_resumed(self, gap_seconds: float) -> None:
        """The machine was asleep for `gap_seconds`; re-establish connectivity."""
        log.info("resume after %.0fs: probing the backend and waking the sync consumer", gap_seconds)
        self.network.check_now()
        self.sync.wake()

    # ── Health ────────────────────────────────────────────────────────────────

    def health_report(self) -> dict:
        """Snapshot of runtime health, for diagnostics and tests."""
        return {
            "phase": self._phase,
            "uptime_seconds": round(time.monotonic() - self._started_at, 1),
            "session_generation": session_generation(),
            "network_state": self.network.network_state,
            "timer_status": self.timer.status,
            "timer_elapsed": self.timer.elapsed_seconds(),
            "queue_depth": self._safe_queue_depth(),
            "tasks_in_flight": self.tasks.in_flight,
            "tasks_active": self.tasks.active_count,
            "services": self.services.health_report(),
        }

    def _safe_queue_depth(self) -> int:
        try:
            return self.cache.get_pending_count()
        except Exception:  # noqa: BLE001
            return -1

    # ── Exit ──────────────────────────────────────────────────────────────────

    def prepare_exit(
        self,
        on_ready: Callable[[], None],
        *,
        stop_timer: bool = True,
        budget_ms: int = EXIT_STOP_FLUSH_BUDGET_MS,
    ) -> None:
        """Bring the timer down for an explicit quit, then call `on_ready`.

        The rule this enforces: **quitting stops the timer.** Every explicit
        exit -- the dialog's Quit, a remembered Quit, the tray menu, the X
        button once "quit" is the remembered choice -- ends the running
        session before the process goes, so no entry keeps running on the
        backend after the user chose to leave. A remembered choice decides
        only whether the dialog is shown; it never decides this.

        Two steps, neither of which blocks the GUI thread:

        1. `stop_tracking()` -- the stop is queued durably and the session
           record removed, in that order, so a kill at any instant from here
           on can neither lose the stop nor resurrect the session.
        2. A bounded wait for the queued stop to reach the backend, driven by
           the sync consumer's own completion signals and a single-shot
           timer. When the backend is reachable this is one round trip; when
           it is not (a measured outage, or the consumer holding for
           re-authentication) there is nothing to wait for, and when the
           budget runs out the stop is simply delivered by the next launch.

        `stop_timer=False` is for an exit that is a *restart*, not a quit:
        installing an update relaunches the application, and the session is
        recovered by the new process exactly as after any other interruption
        (see docs/TIMING_MODEL.md §7).

        Idempotent: a second call while the first is waiting joins the wait
        and is called back with it.
        """
        self._exit_callbacks.append(on_ready)
        if self._exit_prepared:
            return
        self._exit_prepared = True
        log.info("exit requested (stop_timer=%s)", stop_timer)

        # Subscribed *before* the stop is queued: the consumer runs on its own
        # thread and can complete the stop the instant it is enqueued, and a
        # completion emitted before this subscription exists is never
        # delivered to it -- the exit would then wait out its whole budget
        # for a stop that had already landed.
        self.sync.action_completed.connect(self._on_exit_sync_progress)
        self.sync.action_failed.connect(self._on_exit_sync_failed)
        self._exit_connected = True

        if stop_timer and self.timer.is_running():
            self.timer.stop_tracking()

        if not self._exit_wait_needed():
            self._finish_exit_preparation("nothing to wait for")
            return

        self._exit_timer = QTimer(self)
        self._exit_timer.setSingleShot(True)
        self._exit_timer.timeout.connect(
            lambda: self._finish_exit_preparation("budget exhausted; the stop stays queued")
        )
        self._exit_timer.start(budget_ms)
        log.info("waiting up to %dms for the queued stop to reach the backend", budget_ms)
        # A stop already waiting out a retry backoff would not be attempted
        # inside the budget at all; the user is waiting, so it is tried now.
        try:
            self.cache.make_timer_actions_ready()
        except Exception:  # noqa: BLE001
            log.exception("could not bring the queued stop forward")
        # The consumer may be idle: make sure it looks now rather than at its
        # next scheduled poll.
        self.sync.wake()

    def _exit_wait_needed(self) -> bool:
        try:
            pending = self.cache.pending_stop_count()
        except Exception:  # noqa: BLE001
            log.exception("could not count queued stops; not waiting")
            return False
        if pending == 0:
            return False
        if self.network.network_state not in NetworkState.USABLE \
                and self.network.network_state != NetworkState.UNKNOWN:
            log.info("network is %s; the stop stays queued for the next launch",
                     self.network.network_state)
            return False
        if self.sync.state == ServiceState.DEGRADED:
            log.info("sync consumer is holding (%s); the stop stays queued",
                     self.sync.health.last_error)
            return False
        return True

    def _on_exit_sync_progress(self, _action_id: str, action_type: str, _result: dict) -> None:
        if action_type == "stop_timer" and not self._exit_wait_needed():
            self._finish_exit_preparation("stop delivered")

    def _on_exit_sync_failed(
        self, _action_id: str, action_type: str, error: str, will_retry: bool
    ) -> None:
        if action_type == "stop_timer":
            # Retrying now would only make the user wait for a backoff that
            # is measured in seconds; the stop is durable either way.
            self._finish_exit_preparation(
                f"stop could not be delivered now ({error}); retry={will_retry}"
            )

    def _finish_exit_preparation(self, reason: str) -> None:
        if self._exit_done:
            return
        self._exit_done = True
        if self._exit_timer is not None:
            self._exit_timer.stop()
            self._exit_timer = None
        if self._exit_connected:
            self._exit_connected = False
            for signal, slot in (
                (self.sync.action_completed, self._on_exit_sync_progress),
                (self.sync.action_failed, self._on_exit_sync_failed),
            ):
                try:
                    signal.disconnect(slot)
                except (RuntimeError, TypeError):
                    pass
        log.info("exit preparation complete: %s", reason)
        callbacks, self._exit_callbacks = self._exit_callbacks, []
        for callback in callbacks:
            try:
                callback()
            except Exception:  # noqa: BLE001
                log.exception("exit callback raised")

    # ── Shutdown ──────────────────────────────────────────────────────────────

    def shutdown(self, timeout_ms: int = 3000) -> bool:
        """
        Stop the runtime deterministically.

        Idempotent — calling it twice is safe, which matters because both the
        window's close handler and the post-`exec()` safeguard invoke it.

        :return: True if everything stopped cleanly within its timeout.
        """
        if self._shutdown_started:
            return True
        self._shutdown_started = True
        self._set_phase(RuntimePhase.SHUTTING_DOWN)
        started = time.monotonic()
        log.info("shutdown requested")

        # 1. Record the intent while the database is still fully available.
        try:
            self.recovery.mark_clean_shutdown()
        except Exception:  # noqa: BLE001
            log.exception("could not record clean shutdown")

        # 2. Stop accepting new non-critical work and cancel what is in flight.
        #    This runs before service shutdown so nothing new is queued behind
        #    a service that is already stopping.
        drained = self.tasks.shutdown(timeout_ms=timeout_ms)

        # 3. Stop services in reverse registration order: producers first, then
        #    the consumers they feed, then the monitors.
        failed: List[str] = self.services.stop_all(timeout_ms=timeout_ms)

        # 4. Only now release shared resources. Closing these while a service
        #    thread was still running was the original defect: it produced
        #    NoneType errors inside workers, which were swallowed, which left
        #    threads alive and the process unkillable.
        try:
            self.api_client.close()
        except Exception:  # noqa: BLE001
            log.exception("error closing API client")
        try:
            self.storage.close()
        except Exception:  # noqa: BLE001
            log.exception("error closing storage")

        clean = drained and not failed
        elapsed_ms = (time.monotonic() - started) * 1000
        if clean:
            log.info("shutdown complete in %.0fms", elapsed_ms)
        else:
            log.error(
                "shutdown completed in %.0fms with problems (tasks drained=%s, "
                "services that did not stop cleanly=%s)",
                elapsed_ms, drained, failed or "none",
            )
            for service in self.services.services:
                if service.name in failed:
                    log.error("  %s: %s", service.name, service.health.as_dict())

        self._set_phase(RuntimePhase.STOPPED)
        return clean

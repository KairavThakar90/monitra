"""
background_services.public_api — The supported surface for UI and features.

UI and feature modules must import from **this module only**. They must not
import service implementations, and must never touch QThread, worker
lifecycles, sync-queue internals or network polling internals directly. That
boundary is enforced by `tools/check_architecture.py` in CI.

The rule exists because the audit traced several production failures to
feature code owning runtime concerns: transient widgets owning long-running
threads, a dashboard slot wired straight to a queue-internal signal (which
produced the two-threads-per-second storm), and per-widget elapsed-time
counters competing with the timer's own state.

Everything here is a thin, intention-revealing call onto the runtime.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Callable, Dict, Optional

from background_services.activity.app_usage import build_app_usage_summary
from background_services.activity.retention import (
    ACTIVITY_DESKTOP_DAYS, DateAvailability, SCREENSHOT_DESKTOP_DAYS,
    activity_availability, screenshot_availability,
)
from background_services.activity.today_summary import (
    ActivityTotals, TodaySnapshot, build_today_snapshot,
)
from background_services.activity.url_usage import build_url_usage_summary
from background_services.network import NetworkState
from background_services.notifications import NotificationLevel, create_app_icon, set_windows_app_identity
from background_services.timer import TimerStatus
from background_services.update import ReleaseInfo, UpdateState
from core.tasks import TaskHandle

__all__ = [
    "ACTIVITY_DESKTOP_DAYS", "ActivityTotals", "BackgroundApi",
    "DateAvailability", "NetworkState", "NotificationLevel", "ReleaseInfo",
    "SCREENSHOT_DESKTOP_DAYS", "TimerStatus", "TaskHandle", "TodaySnapshot",
    "UpdateState", "create_app_icon", "set_windows_app_identity",
]


class BackgroundApi:
    """
    Facade over the ApplicationRuntime, handed to UI components.

    Holds no state of its own; it exists so that UI code has a narrow,
    documented surface and cannot reach into service internals.
    """

    def __init__(self, runtime) -> None:
        self._runtime = runtime

    # ── Background work ───────────────────────────────────────────────────────

    def run_in_background(
        self,
        fn: Callable[[], Any],
        *,
        on_success: Optional[Callable[[Any], None]] = None,
        on_error: Optional[Callable[[BaseException], None]] = None,
        key: Optional[str] = None,
    ) -> Optional[TaskHandle]:
        """
        Run `fn` off the GUI thread on the shared bounded pool.

        Callbacks are delivered on the GUI thread. Passing `key` de-duplicates:
        if a task with that key is already running, this call is dropped and
        None is returned. UI code must use this instead of creating a QThread.
        """
        return self._runtime.tasks.submit(
            fn, on_success=on_success, on_error=on_error, key=key
        )

    def cancel(self, handle: Optional[TaskHandle]) -> None:
        self._runtime.tasks.cancel(handle)

    def cancel_key(self, key: str) -> None:
        self._runtime.tasks.cancel_key(key)

    def cancel_keys_with_prefix(self, prefix: str) -> int:
        """Cancel every in-flight task whose key starts with `prefix`."""
        return self._runtime.tasks.cancel_keys_with_prefix(prefix)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    @property
    def lifecycle(self):
        """The recovery service, for its lifecycle signals.

        `system_resumed(float)` fires once when the machine comes back from
        sleep or hibernation (the gap it slept for, in seconds). A view that
        shows server state should re-read it then: the ordinary refresh
        cadence is measured in wall-clock timers that were paused with the
        machine, and everything on screen is as old as the sleep.
        """
        return self._runtime.recovery

    # ── Timer ─────────────────────────────────────────────────────────────────

    @property
    def timer(self):
        """
        The authoritative timer service.

        Read `elapsed_seconds()` from it for display; never maintain a separate
        counter in a widget.
        """
        return self._runtime.timer

    def start_timer(
        self,
        project_id: int,
        task_id: int,
        task_name: Optional[str] = None,
        *,
        for_date: Optional[date] = None,
    ) -> None:
        """Start tracking a task.

        `for_date` is the calendar day the user is acting on — the date the
        header is showing. Tracked time only ever runs against today, so the
        service refuses anything else and says so through `timer_error`. Pass
        it from every user-facing path: the disabled button is the first layer
        of that rule, and this is the layer that holds when a queued signal, a
        shortcut or a later caller gets past it. Omit it only for
        system-initiated actions, which are not scoped to a browsed date.
        """
        self._runtime.timer.start_tracking(
            project_id, task_id, task_name, for_date=for_date
        )

    def stop_timer(
        self, notify_backend: bool = True, *, for_date: Optional[date] = None
    ) -> None:
        """Stop tracking.

        `notify_backend=False` is only for the case where the backend has
        already stopped the entry itself — resolving an idle period with
        "Stop timer" does exactly that — so a second stop request would
        merely conflict with the one already applied. That path is
        system-initiated and passes no `for_date`; see `start_timer` for what
        the argument means and when to pass it.
        """
        self._runtime.timer.stop_tracking(
            notify_backend=notify_backend, for_date=for_date
        )

    def switch_timer(
        self,
        project_id: int,
        task_id: int,
        task_name: Optional[str] = None,
        *,
        for_date: Optional[date] = None,
    ) -> None:
        """Stop whatever is tracking and start this task. Same date rule."""
        self._runtime.timer.switch_tracking(
            project_id, task_id, task_name, for_date=for_date
        )

    def request_exit(
        self, on_ready: Callable[[], None], *, stop_timer: bool = True
    ) -> None:
        """Prepare the runtime for the process to exit, then call `on_ready`.

        An explicit quit always stops the running timer first; `on_ready` is
        called once the stop has reached the backend, or once it is clear it
        cannot right now (it stays durably queued either way). Never blocks:
        the window ignores the close, waits for the callback, and only then
        quits the application. `stop_timer=False` is reserved for a restart
        (installing an update), where the session is meant to survive.
        """
        self._runtime.prepare_exit(on_ready, stop_timer=stop_timer)

    def timer_elapsed_seconds(self) -> int:
        return self._runtime.timer.elapsed_seconds()

    def is_timer_running(self) -> bool:
        return self._runtime.timer.is_running()

    def active_session(self) -> Optional[Dict[str, Any]]:
        return self._runtime.timer.active_session()

    # ── Sync ──────────────────────────────────────────────────────────────────

    def enqueue(self, action_type: str, payload: Dict[str, Any], **kwargs) -> str:
        """
        Durably enqueue an operation for background synchronisation.

        The only supported way to schedule sync work. Feature modules must not
        implement their own queues, retry loops or backoff.
        """
        return self._runtime.sync.enqueue(action_type, payload, **kwargs)

    @property
    def sync(self):
        """The sync service, for connecting to its public signals."""
        return self._runtime.sync

    def pending_count(self) -> int:
        return self._runtime.cache.get_pending_count()

    def last_synced_at(self):
        """UTC datetime of the last successful sync this session, or None."""
        return self._runtime.sync.last_synced_at

    def note_pull_succeeded(self) -> None:
        """Tell the sync service a data refresh completed successfully.

        Advances "Last sync" for a pull, the same way a completed upload
        does. Call it only once the refresh has actually succeeded.
        """
        self._runtime.sync.note_pull_succeeded()

    # ── Network ───────────────────────────────────────────────────────────────

    @property
    def network(self):
        """The authoritative network service. There is exactly one."""
        return self._runtime.network

    def network_state(self) -> str:
        return self._runtime.network.network_state

    def is_online(self) -> bool:
        return self._runtime.network.is_online

    # ── Activity ──────────────────────────────────────────────────────────────

    @property
    def activity(self):
        return self._runtime.activity

    def activity_supported(self) -> bool:
        """False when this platform cannot measure input activity."""
        return self._runtime.activity.supported

    def current_activity_percent(self) -> int:
        return self._runtime.activity.current_percent()

    def activity_percent_for_entry(self, entry_id: int) -> int:
        return self._runtime.activity.percent_for_entry(entry_id)

    def live_activity_totals(self) -> ActivityTotals:
        """
        The activity window currently being sampled, as addable totals.

        Cheap and non-blocking (it reads integer counters), so the dashboard
        may call it on every tick to keep TODAY'S ACTIVITY moving between
        window flushes.
        """
        return self._runtime.activity.live_window_totals()

    def today_activity_snapshot(self, day) -> TodaySnapshot:
        """
        Today's persisted activity: the backend's duration-weighted aggregate
        plus the windows still queued locally for upload.

        Blocking: call it through `run_in_background`, never on the GUI
        thread. Never raises — a failed request comes back with
        `remote_ok=False` so the caller can keep the last good value on
        screen instead of resetting a real percentage to zero.
        """
        return build_today_snapshot(self._runtime.api_client, self._runtime.cache, day)

    def app_usage_summary(self, day) -> list:
        """
        The ranked application-usage summary for one IST calendar day.

        `day` is required: there is no all-time view of application usage, and
        an unfiltered total was the defect this argument exists to remove.
        The backend's records for that day and the local rows still waiting to
        upload are merged, so an offline day shows its real usage.

        Blocking: call it through `run_in_background`, never on the GUI thread.
        """
        return build_app_usage_summary(
            self._runtime.api_client, self._runtime.cache, day=day
        )

    def url_usage_summary(self, day) -> list:
        """
        The ranked browser URL usage summary for one IST calendar day.

        Same contract as `app_usage_summary`, over the day-scoped aggregate
        endpoint rather than a capped listing, so the total is the day's whole
        usage rather than however much fitted in one page.

        Blocking: call it through `run_in_background`, never on the GUI thread.
        """
        return build_url_usage_summary(
            self._runtime.api_client, self._runtime.cache, day=day
        )

    def screenshot_availability(self, day) -> str:
        """How much of `day`'s screenshot history the desktop will show.

        One of `DateAvailability.FUTURE`, `AVAILABLE` or `ARCHIVED`. The UI
        asks before fetching, so a date outside the desktop's window costs no
        request at all — see `background_services/activity/retention.py`.
        """
        return screenshot_availability(day)

    def activity_availability(self, day) -> str:
        """The same, for the Apps and URLs tabs' longer window."""
        return activity_availability(day)

    @property
    def url_usage(self):
        """The real-time browser URL usage tracking service."""
        return self._runtime.url_usage


    # ── Screenshots ───────────────────────────────────────────────────────────

    @property
    def screenshots(self):
        """The screenshot capture service, for connecting to its signals.

        It is driven by the timer and owns its own schedule; UI code must not
        ask it to capture, and must not start or stop it. There is deliberately
        no "take a screenshot now" call: an on-demand capture outside a window
        would break the per-window count the timeline is grouped by.
        """
        return self._runtime.screenshot

    def screenshot_queue_depth(self) -> Dict[str, int]:
        """Counts of locally queued screenshots by status.

        `{'pending': n, 'uploading': n, 'failed': n}`, omitting empty states.
        Cheap enough to poll for a status surface.
        """
        return self._runtime.cache.count_screenshots_by_status()

    def unattributed_screenshot_count(self) -> int:
        """Captures held back because no time entry id ever reached them.

        These are counted as `pending` by `screenshot_queue_depth`, where they
        read as "about to upload" — but the uploader withholds a row with no
        entry id, so only an adoption can release one. Surfaced separately so
        that a count which does not fall is visible as the stall it is rather
        than hiding inside the pending total.

        Normally zero, and briefly non-zero during an offline start: the
        session's captures are adopted when the queued start lands.
        """
        return self._runtime.cache.count_unattributed_screenshots()

    # ── Idle time ─────────────────────────────────────────────────────────────

    @property
    def idle(self):
        """
        The idle-time service, for connecting to its signals and answering a
        pending idle period.

        It owns detection and every idle-period call to the backend. The popup
        is a view over this service: it renders `pending_period()` and calls
        `resolve()` / `reassign()`. A dialog must not detect inactivity, hold
        a pending period, or talk to the idle endpoints itself — it is
        transient, and the period outlives it.
        """
        return self._runtime.idle

    def idle_config(self) -> Dict[str, Any]:
        """The signed-in user's `{idle_enabled, idle_minutes}`."""
        return self._runtime.idle.idle_config()

    def pending_idle_period(self) -> Optional[Dict[str, Any]]:
        """The unresolved idle period this session is holding, if any."""
        return self._runtime.idle.pending_period()

    def apply_idle_profile(self, user_data: Optional[Dict[str, Any]]) -> None:
        """Seed idle configuration from a `/auth/me` payload.

        Called on login and on session verification, both of which already
        hold the profile — so the user's own idle threshold is in effect
        before tracking starts, without an extra request.
        """
        self._runtime.idle.apply_user_profile(user_data)

    def resolve_idle_period(self, keep_idle_time: bool, action: str) -> None:
        """Answer the pending idle popup. `action` is "stop" or "resume"."""
        self._runtime.idle.resolve(keep_idle_time, action)

    def reassign_idle_period(self, project_id: int, task_id: int) -> None:
        """Move the pending idle period's time to another project/task."""
        self._runtime.idle.reassign(project_id, task_id)

    # ── Updates ───────────────────────────────────────────────────────────────

    @property
    def updates(self):
        """The update service, for connecting to its signals.

        It owns the whole update lifecycle: the schedule, the check, the
        download, the checksum and the installer handoff. UI code must not
        check for updates itself — the announcement is edge-triggered inside
        this service, and a second checker would re-announce the same release
        on every poll — and must not download or verify anything itself, which
        is what keeps "only a verified artifact is ever executed" a property of
        one module rather than a convention.
        """
        return self._runtime.updates

    def latest_release(self) -> Optional[Dict[str, Any]]:
        """The backend's last successful answer about the newest release.

        None means the check has not succeeded yet, which is *not* the same as
        "you are up to date" — display it as unknown rather than as current.
        """
        return self._runtime.updates.latest_release

    def pending_update_count(self) -> int:
        """How many announced releases are newer than the installed build.

        This is what the account menu's "Updates" badge shows. It is derived
        from the durable record every time, so it goes to zero on its own once
        the user has actually installed the update.
        """
        return self._runtime.updates.pending_count

    def update_download_url(self) -> Optional[str]:
        """Where to download the newest announced release, or None."""
        return self._runtime.updates.download_url()

    def pending_update_release(self):
        """The installable release on offer, or None.

        `None` covers two different situations the caller must not conflate:
        there is no newer release, or there is one the client cannot verify
        (an older deployment answering without a checksum). Neither may be
        installed; only the first means "you are up to date".
        """
        return self._runtime.updates.pending_release

    def update_state(self) -> str:
        """Where the updater is, as an `UpdateState` value."""
        return self._runtime.updates.update_state

    def force_update_pending(self) -> bool:
        """True when the user must update before carrying on.

        Read by the main window to decide whether the application may be used.
        It is never persisted, so a backend outage can never leave a client
        locked out with nobody able to say otherwise.
        """
        return self._runtime.updates.force_update_pending

    def start_update(self) -> bool:
        """Download, verify and install the pending release.

        Returns whether the attempt was started. Non-blocking: the download
        runs on the shared task pool and reports through the service's
        `download_progress` signal. Calling it twice is safe — the update state
        machine refuses the second — so a double-click cannot start two
        downloads or two installers.
        """
        return self._runtime.updates.start_update()

    def check_for_updates_now(self) -> None:
        """Ask the backend immediately, without disturbing the schedule.

        Dropped silently if a check or a download is already running, so a
        repeatedly-clicked menu entry produces one request, not one per click.
        """
        self._runtime.updates.check_now()

    # ── Notifications ─────────────────────────────────────────────────────────

    @property
    def notifications(self):
        return self._runtime.notifications

    def notify(self, message: str, level: str = NotificationLevel.INFO,
               key: Optional[str] = None) -> None:
        self._runtime.notifications.notify(message, level, key=key)

    # ── Data access ───────────────────────────────────────────────────────────

    @property
    def cache(self):
        """Repository access to local storage. Read/write via its methods only."""
        return self._runtime.cache

    # ── Diagnostics ───────────────────────────────────────────────────────────

    def health_report(self) -> dict:
        return self._runtime.health_report()

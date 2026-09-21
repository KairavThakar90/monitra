"""
Dashboard window — main application shell.

Assembles sidebar + top bar + task table + activity section.

Ownership: this widget owns no threads and no services. Every background
operation goes through `BackgroundApi`, which runs it on the runtime's bounded
pool and delivers the result back on the GUI thread.

This file is where the worst of the audited failures lived. `_on_queue_empty`
was connected to the sync queue's `queue_empty` signal, which the old consumer
emitted on every 500 ms poll of an empty queue rather than on the transition
into one. The slot then reloaded the project's tasks and today's time entries,
each spawning a fresh QThread. An idle, logged-in application therefore created
two OS threads and issued two HTTP requests every second, forever — measured at
48 threads in 25 seconds. Both halves are fixed: the service now emits an edge,
and this window schedules bounded, de-duplicated work rather than raw threads.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from time import monotonic
from typing import Any, Callable, Dict, List, Optional

from PySide6.QtCore import QTimer, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QFont
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QSplitter, QVBoxLayout, QWidget,
)

import version
from app.api.client import ApiClient
from app.api.exceptions import ApiHttpError
from app.auth.session import SessionManager
from app.portal.service import build_web_url
from app.projects.service import ProjectService
from app.tasks.service import TaskService
from app.time_entries.service import TimeEntryService
from background_services.public_api import (
    BackgroundApi, BreakStatus, NetworkState, NotificationLevel, TodaySnapshot, UpdateState,
)
from core.date_mode import DateMode, as_calendar_day, date_mode, is_live_date
from core.logging_setup import get_logger
from core.time_format import format_hms, ist_clock, ist_day_bounds_utc, ist_today, parse_utc
from ui import icons
from ui.action_banner import ActionBanner
from ui.activity_section import ActivitySection
from ui.feedback_dialog import SUBMIT_KEY, FeedbackDialog
from ui.idle_alert_dialog import IdleAlertDialog
from ui.sidebar import SidebarWidget
from ui.update_dialog import UpdateDialog
from ui.styles import (
    BORDER_LIGHT, CONTENT_BG, ERROR, PROJECT_COLORS, SUCCESS, TEXT_MUTED, WARNING,
)
from ui.stat_cards import StatCardsRow
from ui.task_table import TaskSection
from ui.topbar import TopBar

log = get_logger("dashboard")

#: Which project the user was last in, so reopening the app lands on it with
#: its cached tasks already drawn instead of on an empty task area.
LAST_PROJECT_KEY = "dashboard.last_project_id"

#: Background refresh cadence for project/task data while the window is open,
#: against a backend that has no change fingerprint (`/api/v1/sync/revision`
#: answering 404). Re-downloading every list this often is the only way such
#: a backend can be kept current.
REFRESH_INTERVAL_MS = 120_000

#: The same cadence once the backend's change fingerprint is known to work.
#: Convergence no longer depends on it -- the probe below notices a change
#: within its own interval and triggers a refresh -- so the full round becomes
#: a safety net, run rarely enough that a fleet of idle clients is idle.
REFRESH_INTERVAL_WITH_PROBE_MS = 300_000

#: How often to ask the backend whether anything this user can see has
#: changed. One small request (a fingerprint, no rows); the lists themselves
#: are re-read only when the answer moves. This is what makes a project
#: created on the web, or a membership removed, reach an open desktop within
#: half a minute without anyone pressing Refresh -- and without the lists
#: being polled.
SYNC_PROBE_INTERVAL_MS = 30_000

#: A refresh round that has not reported back after this long is abandoned so
#: the next one can run. Every fetch in a round has a 10s request timeout, so
#: a healthy round is over in seconds; this exists because the round is
#: reference-counted, and a count that never reaches zero -- a callback that
#: raised before it could report, a task dropped with its callbacks -- used to
#: block every later refresh silently for the rest of the session.
REFRESH_STALE_AFTER_S = 90.0


#: What the top-of-content banner says when a break begins and ends. Shown
#: only once the action has actually happened: Break In once the timer
#: service reports the break, Break Out once the held task is running again.
BREAK_STARTED_MESSAGE = "Break started — your current task has been paused."
BREAK_ENDED_MESSAGE = "Break ended — resuming your previous task."


def _is_finished(entry: Dict[str, Any]) -> bool:
    return entry.get("status") in ("stopped", "completed") or bool(entry.get("end_time"))


def banked_seconds(entry: Dict[str, Any]) -> int:
    """The seconds a completed entry contributes to a total.

    `net_seconds` is the backend's netted figure -- `total_seconds` plus the
    entry's signed adjustments (discarded idle time, reassigned idle time,
    unwanted-activity deductions), the same number every report shows.
    Summing the raw `total_seconds` instead put the desktop 21 minutes above
    the web for the same day after one idle period was discarded. An entry
    from an older backend, or one folded locally, carries no `net_seconds`
    and reads as its raw total.
    """
    net = entry.get("net_seconds")
    if net is None:
        return int(entry.get("total_seconds") or 0)
    return max(0, int(net))


def update_menu_action(
    release: Optional[Any], download_url: Optional[str], latest: Optional[Dict[str, Any]]
) -> str:
    """What clicking the account menu's "Updates (N)" entry should do.

    Returns one of `"dialog"`, `"browser"`, `"check"`, `"no-location"`.

    The case that makes this worth its own function is `"check"`. The badge
    count is restored from the **durable** record the moment the window is
    built, so "Updates (1)" is on screen before this session has asked the
    backend anything. The release details behind it are deliberately *not*
    persisted — a withdrawn release must not be installable from a stale local
    copy — so for the first half-minute of a session the badge is real and the
    details are simply not fetched yet.

    Clicking in that window must go and ask, not announce a conclusion. The
    earlier version fell through to "no download location has been published",
    which stated as fact about the deployment something that was only true of
    this client's knowledge.
    """
    if release is not None:
        return "dialog"
    if download_url:
        return "browser"
    if latest is None:
        # No successful check yet this session; the badge came from disk.
        return "check"
    # Checked, and the deployment really did publish no download URL.
    return "no-location"


def manual_check_outcome(
    latest: Optional[Dict[str, Any]], installed: str
) -> tuple[str, str, str]:
    """What to tell a user whose manual update check found no update.

    Returns `(message, level, key)`. Split out as a plain function because the
    distinction it draws is the whole point and is worth testing without
    building a window: there are **three** outcomes here, not two, and
    collapsing them is how a client ends up asserting something it does not
    know.

    * The check failed — the answer is unknown because we never got one.
    * The check succeeded and the deployment has published no release — the
      answer is unknown because *nobody has said*. This is not "you are up to
      date": claiming currency here would invent the one fact the user asked
      for. It is what a deployment with an empty release table answers, which
      is every deployment until the first release is published.
    * The check succeeded and named a version this build is at or above — the
      only case where "you are on the latest version" is a supportable claim.
    """
    if latest is None:
        return (
            "Monitra could not check for updates just now. "
            "It will try again automatically.",
            NotificationLevel.WARNING,
            "update-check-failed",
        )
    if not latest.get("latest_version"):
        return (
            "No release information has been published yet, so Monitra cannot "
            "tell whether a newer version exists.",
            NotificationLevel.INFO,
            "update-unknown",
        )
    return (
        f"Monitra {installed} is the latest version.",
        NotificationLevel.INFO,
        "update-current",
    )


class StatusBar(QFrame):
    """Thin status bar at the bottom of the dashboard."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(26)
        self.setStyleSheet(
            f"QFrame {{ background: #F1F5F9; border-top: 1px solid {BORDER_LIGHT}; }}"
        )
        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 0, 16, 0)
        layout.setSpacing(12)

        self._msg = QLabel("Ready", self)
        self._msg.setFont(QFont("Segoe UI", 10))
        self._msg.setStyleSheet(f"color: {TEXT_MUTED};")
        layout.addWidget(self._msg)
        layout.addStretch()

        self._timer_status = QLabel("", self)
        self._timer_status.setFont(QFont("Segoe UI", 10, QFont.Weight.DemiBold))
        self._timer_status.setStyleSheet(f"color: {TEXT_MUTED};")
        layout.addWidget(self._timer_status)

    def set_message(self, msg: str, color: Optional[str] = None) -> None:
        self._msg.setText(msg)
        self._msg.setStyleSheet(f"color: {color or TEXT_MUTED};")

    def set_timer_info(self, info: str) -> None:
        self._timer_status.setText(info)


class DashboardWindow(QWidget):
    """The signed-in application shell."""

    logout_requested = Signal()
    unauthorized_error = Signal()
    #: The updater has launched an installer that is waiting for this process
    #: to exit. Handled by the main window, which owns quitting — the same
    #: reason logout is a signal rather than a reach up the widget tree.
    quit_requested = Signal()

    def __init__(
        self,
        runtime,
        session_manager: SessionManager,
        project_service: ProjectService,
        task_service: TaskService,
        time_entry_service: TimeEntryService,
        api_client: ApiClient,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.runtime = runtime
        self.api = BackgroundApi(runtime)
        self.session_manager = session_manager
        self.project_service = project_service
        self.task_service = task_service
        self.time_entry_service = time_entry_service
        self.api_client = api_client

        self._projects: List[Dict[str, Any]] = []
        self._current_project: Optional[Dict[str, Any]] = None
        self._current_project_color = PROJECT_COLORS[0]
        self._today_time_entries: List[Dict[str, Any]] = []
        #: The last-fetched persisted activity for today (uploaded + still
        #: queued locally). Refreshed periodically in the background; the
        #: window still being sampled is added on top of this, live, every
        #: tick (see `_update_stat_cards`) via `api.live_activity_totals()`,
        #: which is cheap enough to call every second.
        self._today_activity: TodaySnapshot = TodaySnapshot()
        #: The selected project's tasks, as last rendered. Held only so the
        #: summary cards can count completed vs total without re-fetching.
        self._project_tasks: List[Dict[str, Any]] = []
        self._pending_active_timer: Optional[Dict[str, Any]] = None
        self._had_pending_sync = False
        self._active = False
        #: The date currently selected in the top bar. Never later than today —
        #: the header refuses a future selection and this window refuses to
        #: adopt one, so the two cannot disagree. Drives whether the live timer
        #: is allowed to bleed into the sidebar/task totals: any day but today
        #: must show completed hours only, never a ticking value.
        self._current_date: date = ist_today()
        #: The last `BreakStatus` the timer service reported. Held only to
        #: tell a Break Out that just committed (RESUMING -> NONE with a task
        #: running) from any other way a break ends, because that is the one
        #: transition after which the resumed task's project is brought on
        #: screen for the user.
        self._break_status: str = BreakStatus.NONE
        #: What the sidebar's circular Play starts. Two candidates, in order:
        #: the task selected in the list (clicking a row, or its Start), and
        #: the task tracked last in this session -- so Pause then Play
        #: resumes the same task even after browsing to another project.
        #: Both are `{"project_id", "task_id", "task_name"}`. Neither is ever
        #: guessed: with no candidate Play is disabled and says so.
        self._selected_task: Optional[Dict[str, Any]] = None
        self._last_tracked_task: Optional[Dict[str, Any]] = None
        #: Whether the last committed network state was usable. Starts None so
        #: the first observation is not announced as a recovery — telling the
        #: user they are "back online" before they were ever seen offline was
        #: part of the reported notification noise.
        self._was_online: Optional[bool] = None
        #: How many fetches of the refresh currently in flight are still
        #: outstanding, and whether any of them failed. "Last sync" is only
        #: advanced once a refresh finishes with every fetch successful.
        self._refresh_outstanding = 0
        self._refresh_failed = False
        #: When the refresh round in flight was started (monotonic), for the
        #: REFRESH_STALE_AFTER_S watchdog.
        self._refresh_started_at = 0.0

        #: Consecutive failures of the *first* project load, while nothing is
        #: cached to show instead. A transient hiccup here (a cold backend, a
        #: one-off timeout, a token that needed one refresh) is not a
        #: connectivity change, so it never flips `NetworkState` and the
        #: reconnect-triggered retry in `_on_network_state_changed` never
        #: fires -- the dashboard sat on "Unable to load projects" for the
        #: full two minutes to the next periodic refresh, which read as
        #: "projects don't load; I have to reopen the app" until one of those
        #: repeated launches happened to land after the hiccup passed. This
        #: timer retries on its own bounded backoff instead of waiting on
        #: either of those. Reset to 0 the moment a load succeeds or any
        #: project is already on screen (see `_schedule_empty_project_retry`).
        self._empty_project_load_retries = 0
        self._project_retry_timer = QTimer(self)
        self._project_retry_timer.setSingleShot(True)
        self._project_retry_timer.timeout.connect(self.load_projects)

        #: The backend's change fingerprint as last seen, and whether the
        #: backend offers one at all (None until the first probe answers).
        self._sync_revision: Optional[str] = None
        self._sync_probe_supported: Optional[bool] = None

        #: Bumped on every local task mutation. A task-list fetch records the
        #: value when it is submitted and is discarded on arrival if the
        #: value has moved since: the list it carries predates a change the
        #: user has already seen applied, and painting it would make the new
        #: task vanish until the next refresh.
        self._task_list_version = 0

        #: The mandatory idle popup, while one is on screen. Exactly one may
        #: exist: the idle service holds at most one pending period, and this
        #: reference is what stops a second alert being built for it.
        self._idle_dialog: Optional[IdleAlertDialog] = None

        #: The Feedback & Help dialog, while one is on screen. Held for the
        #: same reason as the idle alert: clicking the sidebar action again
        #: must raise the existing window, not build a second one.
        self._feedback_dialog: Optional[FeedbackDialog] = None

        #: The update prompt, while one is on screen. Exactly one may exist —
        #: the service announces a release on an edge, but a manual check and a
        #: scheduled one can both land, and two update dialogs offering the
        #: same version would be two ways to start the same download.
        self._update_dialog: Optional[UpdateDialog] = None

        #: True between the user asking for a check and its outcome. It is what
        #: makes the "you are up to date" answer reach only the person who
        #: asked — a scheduled check must stay silent, or a toast every ten
        #: hours becomes the notification storm DO_NOT_DO.md records.
        self._awaiting_manual_check = False

        #: The signed-in user's id. Every time-entry query is scoped to it.
        #:
        #: /time-entries applies no user filter for a caller holding
        #: `time_entries:view_all`, which every admin and manager does, so an
        #: unscoped request returns the whole organisation's entries. The
        #: dashboard summed those into TOTAL TIME TODAY: on 2 Sept the admin's
        #: own tracked time was 01:12:55 while the card read 02:08:55, which
        #: is exactly the organisation's running total at that moment. This is
        #: a personal dashboard; it asks for one person's time.
        self._user_id: Optional[int] = None

        self._build_ui()
        self._wire_services()

        # Periodic refresh. A UI-only timer that schedules bounded work; it
        # does not create threads and does not run while signed out.
        self._refresh_timer = QTimer(self)
        self._refresh_timer.timeout.connect(self.refresh_data)

        # The change probe. A UI-only timer scheduling one de-duplicated
        # background request; it refreshes nothing itself and runs only while
        # signed in.
        self._sync_probe_timer = QTimer(self)
        self._sync_probe_timer.timeout.connect(self._probe_sync_revision)

    # ── Construction ──────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        self.setStyleSheet(f"QWidget {{ background: {CONTENT_BG}; }}")

        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        h_split = QWidget(self)
        h_layout = QHBoxLayout(h_split)
        h_layout.setContentsMargins(0, 0, 0, 0)
        h_layout.setSpacing(0)

        self._sidebar = SidebarWidget(self)
        self._sidebar.project_selected.connect(self._on_project_selected)
        self._sidebar.logout_requested.connect(self._handle_logout)
        # The circular Play / Pause. The sidebar reports the click; the
        # request becomes the *task section's* existing Start/Stop -- the
        # same `switch_timer` / `stop_timer` its rows use -- so there is one
        # start path and one stop path whichever control was pressed.
        # Queued, deliberately: the request is emitted from inside the
        # button's own `clicked`, and handling it starts or stops the timer,
        # which re-renders the sidebar -- including that button's icon,
        # enabled state and style -- while the click is still on the stack.
        # One event-loop turn later the button has finished its click.
        self._sidebar.start_requested.connect(
            self._on_play_requested, Qt.ConnectionType.QueuedConnection
        )
        self._sidebar.stop_requested.connect(
            self._on_pause_requested, Qt.ConnectionType.QueuedConnection
        )
        self._sidebar.feedback_requested.connect(self._open_feedback_dialog)
        self._sidebar.profile_requested.connect(self._open_web_profile)
        self._sidebar.updates_requested.connect(self._open_update_download)
        self._sidebar.update_check_requested.connect(self._check_for_updates)
        # The badge is pushed by UpdateService, which owns the check. The
        # connection is cross-thread (the service runs its own loop), so Qt
        # delivers it queued onto this thread -- the sidebar is never touched
        # from the service's thread.
        self.api.updates.pending_count_changed.connect(
            self._sidebar.set_pending_updates
        )
        self._sidebar.set_pending_updates(self.api.pending_update_count())
        # The update prompt. All four connections are cross-thread (the service
        # runs its own loop and its download runs on the task pool), so Qt
        # delivers them queued onto this thread — no widget is ever touched
        # from the service's thread or the pool's.
        self.api.updates.update_offered.connect(self._on_update_offered)
        self.api.updates.state_changed.connect(self._on_update_state_changed)
        self.api.updates.download_progress.connect(self._on_update_progress)
        self.api.updates.update_failed.connect(self._on_update_failed)
        self.api.updates.install_started.connect(self._on_install_started)
        h_layout.addWidget(self._sidebar)

        # Last-sync display: driven entirely by SyncService's own edge signal,
        # never a widget-local timer or guess. Shows the honest "Never" state
        # until the first sync actually completes this session.
        self.api.sync.synced_at_changed.connect(self._sidebar.set_last_synced_at)
        self._sidebar.set_last_synced_at(self.api.last_synced_at())

        right_col = QWidget(h_split)
        right_layout = QVBoxLayout(right_col)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)

        self._topbar = TopBar(right_col)
        self._topbar.date_changed.connect(self._on_date_changed)
        self._topbar.refresh_requested.connect(self.refresh_data)
        right_layout.addWidget(self._topbar)

        # Task and Activity/Screenshot sections share the remaining height
        # via a drag-resizable splitter rather than a fixed 4:6 stretch
        # inside a scroll area -- each section already scrolls its own
        # content internally, so the outer container only needs to divide
        # up the available height, not add a second, redundant scrollbar.
        content_container = QWidget(right_col)
        content_container.setStyleSheet(f"background: {CONTENT_BG};")
        content_outer_layout = QVBoxLayout(content_container)
        content_outer_layout.setContentsMargins(20, 16, 20, 20)
        content_outer_layout.setSpacing(14)

        # ── Summary cards ─────────────────────────────────────────
        # Rendered from data this window already holds: the day's time
        # entries, the selected project's tasks and TimerService's session.
        # The row fetches nothing and counts nothing itself.
        self._stat_cards = StatCardsRow(content_container)
        # Break In / Break Out lives in the ACTIVE TASK card, beside the task
        # it acts on. The card reports the click; the timer service owns the
        # break, exactly as it owns the timer the task rows' Start/Stop
        # drive. Queued for the same reason the sidebar's control is: the
        # handler re-renders the very button whose click is on the stack.
        self._stat_cards.break_in_requested.connect(
            self._on_break_in_requested, Qt.ConnectionType.QueuedConnection
        )
        self._stat_cards.break_out_requested.connect(
            self._on_break_out_requested, Qt.ConnectionType.QueuedConnection
        )
        content_outer_layout.addWidget(self._stat_cards)

        # The transient message at the top of the content area ("Break
        # started…"). An overlay child of the container, not a row in its
        # layout: it takes no space while hidden and moves nothing when it
        # shows. It dismisses itself; see ui/action_banner.py.
        self._action_banner = ActionBanner(content_container)

        self._content_splitter = QSplitter(Qt.Orientation.Vertical, content_container)
        # A section collapsed to 0 height would look like it vanished --
        # each side keeps a usable minimum instead (enforced below).
        self._content_splitter.setChildrenCollapsible(False)
        self._content_splitter.setHandleWidth(10)
        self._content_splitter.setStyleSheet(f"""
            QSplitter::handle {{
                background: {CONTENT_BG};
                border-top: 1px solid {BORDER_LIGHT};
                border-bottom: 1px solid {BORDER_LIGHT};
            }}
            QSplitter::handle:hover {{
                background: {BORDER_LIGHT};
            }}
        """)

        self._task_section = TaskSection(
            api=self.api, task_service=self.task_service,
            time_entry_service=self.time_entry_service, parent=self._content_splitter
        )
        self._task_section.timer_state_changed.connect(self._on_timer_state_changed)
        self._task_section.error_occurred.connect(self._on_error)
        self._task_section.active_timer_conflict.connect(self._reconcile_active_timer)
        self._task_section.task_action_succeeded.connect(self._on_task_action_succeeded)
        self._task_section.task_mutated.connect(self._on_task_mutated)
        # Which task the circular Play starts. The list owns the selection
        # (a row click, or a row's Start); this window only records it.
        self._task_section.task_selected.connect(self._on_task_selected)
        # The Request (manual time entry) button lives in the top bar, but
        # the dialog and its submission stay in TaskSection -- this is the
        # only wire between them.
        self._topbar.request_clicked.connect(self._task_section.open_manual_entry_dialog)
        # Add Task and the search field live in the top bar; the dialog, the
        # create call and the filtering all still belong to TaskSection.
        self._topbar.add_task_clicked.connect(self._task_section.open_add_task_dialog)
        self._topbar.search_changed.connect(self._task_section.apply_search)
        self._task_section.add_task_available.connect(self._topbar.set_add_task_enabled)
        self._task_section.setMinimumHeight(220)
        self._content_splitter.addWidget(self._task_section)

        self._activity_section = ActivitySection(self.api, self.api_client, self._content_splitter)
        self._activity_section.profile_requested.connect(self._open_activity_in_profile)
        self._activity_section.setMinimumHeight(220)
        self._content_splitter.addWidget(self._activity_section)

        # Initial split mirrors the previous 4:6 stretch-factor proportion;
        # the user can drag it anywhere between the two minimums afterward.
        self._content_splitter.setStretchFactor(0, 4)
        self._content_splitter.setStretchFactor(1, 6)
        self._content_splitter.setSizes([400, 600])

        content_outer_layout.addWidget(self._content_splitter)
        right_layout.addWidget(content_container, 1)

        h_layout.addWidget(right_col, 1)
        root_layout.addWidget(h_split, 1)

        self._status_bar = StatusBar(self)
        root_layout.addWidget(self._status_bar)

    def _wire_services(self) -> None:
        """
        Subscribe to the background services.

        Every one of these is an edge-triggered signal. Nothing here is wired
        to a polling signal, which is the invariant that keeps an idle
        application idle.
        """
        sync = self.api.sync
        sync.pending_count_changed.connect(self._on_pending_count_changed)
        sync.queue_drained.connect(self._on_queue_drained)

        network = self.api.network
        network.network_state_changed.connect(self._on_network_state_changed)
        network.latency_measured.connect(self._topbar.set_latency)
        self._topbar.set_network_state(network.network_state)

        timer = self.api.timer
        timer.timer_tick.connect(self._on_timer_tick)
        timer.timer_recovered.connect(self._on_timer_recovered)
        # Both edges of a stop, and the one case a start is refused. See
        # _on_timer_state_changed for why the day is re-read on *finalized*
        # and not on the local stop.
        timer.timer_finalized.connect(self._on_timer_finalized)
        timer.timer_conflict.connect(self._on_timer_conflict)
        # The break, on its transitions only (never a poll of it).
        timer.break_state_changed.connect(self._on_break_state_changed)

        # Unwanted-activity warnings: edge-triggered by the rule engine (one
        # emission per threshold crossing, already cooldown-throttled there);
        # the notification key gives NotificationService a second layer of
        # de-duplication on top.
        activity = self.api.activity
        activity.unwanted_activity_alert.connect(self._on_unwanted_activity_alert)

        # Idle time. `idle_period_opened` is an edge: the service emits it once
        # when the backend accepts a new pending period (or when one is
        # recovered after a restart), never on a poll. The dialog is built here
        # rather than in the service because a service must not own a widget.
        idle = self.api.idle
        idle.idle_period_opened.connect(self._on_idle_period_opened)
        # A crash-recovery gap, shown instantly before the backend has
        # confirmed it -- the popup opens now with the live count, locked,
        # and `_on_idle_period_opened` above unlocks it once confirmed.
        idle.interruption_pending.connect(self._on_interruption_pending)

        # A screenshot was captured. Edge-triggered from the service, which
        # emits once per capture -- at most one per ten-minute window -- so
        # this cannot become a stream of toasts.
        self.api.screenshots.screenshot_captured.connect(self._on_screenshot_captured)

        # The machine came back from sleep. Once per resume, from the
        # recovery service's own heartbeat: every timer above was paused
        # with the machine, so everything on screen is as old as the sleep.
        self.api.lifecycle.system_resumed.connect(self._on_system_resumed)

    def _on_unwanted_activity_alert(self, message: str) -> None:
        self.api.notify(message, NotificationLevel.WARNING, key="unwanted-activity")

    def _on_screenshot_captured(self, record: dict) -> None:
        """Tell the user a screenshot was taken, at the moment it was taken.

        Announced on capture rather than on upload: the capture is the thing
        that concerns the person being recorded, and it is the only part that
        happens at a predictable moment. An upload can be minutes later, or
        after a restart, and a toast then would name a screenshot the user has
        no way to place.

        Keyed on the capture's own id, so the notification service's
        de-duplication cannot swallow a later capture for looking like an
        earlier one.
        """
        when = ist_clock(record.get("captured_at"))
        message = f"Screenshot captured at {when}" if when else "Screenshot captured"
        self.api.notify(
            message,
            NotificationLevel.INFO,
            key=f"screenshot:{record.get('client_screenshot_id', when)}",
        )

    # ── Idle time ─────────────────────────────────────────────────────────────

    def _on_interruption_pending(self, interruption: dict) -> None:
        """A crash-recovery gap was detected -- open the popup instantly.

        This is the provisional view: the live gap is shown at once, but the
        dialog's actions stay locked until `_on_idle_period_opened` confirms
        it with the backend's real idle period (via `bind_confirmed_period`),
        or `IdleService.interruption_withdrawn` closes it because the backend
        never accepted the gap. The backend still decides everything but the
        on-screen count.
        """
        if self._idle_dialog is not None:
            self._idle_dialog.raise_()
            self._idle_dialog.activateWindow()
            return

        dialog = IdleAlertDialog(
            self.api,
            provisional=interruption,
            project_name_resolver=self._project_name_for,
            project_loader=self.project_service.get_projects,
            task_loader=self.task_service.get_tasks_for_project,
            parent=self.window(),
        )
        self._idle_dialog = dialog
        dialog.resolved.connect(self._on_idle_period_resolved)
        dialog.finished.connect(lambda _result: self._forget_idle_dialog())

        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        self.api.notify(
            "You have been idle. Monitra needs to know whether to keep that time.",
            NotificationLevel.WARNING, key="idle-alert",
        )

    def _on_idle_period_opened(self, period: dict) -> None:
        """Raise the mandatory idle popup for a period the backend now holds.

        Exactly one alert: if one is already on screen it is brought forward
        rather than a second being built. The service holds at most one
        pending period, so the two guards agree.
        """
        if self._idle_dialog is not None:
            if self._idle_dialog.is_provisional_for(period):
                # The dialog already on screen is the provisional view of
                # this exact gap -- unlock it rather than build a second one.
                self._idle_dialog.bind_confirmed_period(period)
                return
            self._idle_dialog.raise_()
            self._idle_dialog.activateWindow()
            return

        dialog = IdleAlertDialog(
            self.api,
            period,
            project_name_resolver=self._project_name_for,
            # The same authorised loaders this window uses, so the
            # reassignment dropdowns cannot show a project or task the user is
            # not entitled to — and there is no second way of fetching them.
            project_loader=self.project_service.get_projects,
            task_loader=self.task_service.get_tasks_for_project,
            parent=self.window(),
        )
        self._idle_dialog = dialog
        dialog.resolved.connect(self._on_idle_period_resolved)
        dialog.finished.connect(lambda _result: self._forget_idle_dialog())

        # The user is, by definition, not looking at Monitra: the window may
        # be minimised or hidden in the tray. Show the alert as its own
        # top-level window and put it in front, and notify as well so the
        # taskbar/tray carries the prompt even if focus is stolen back.
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        self.api.notify(
            "You have been idle. Monitra needs to know whether to keep that time.",
            NotificationLevel.WARNING, key="idle-alert",
        )

    def _on_idle_period_resolved(self, result: dict) -> None:
        """The backend accepted an answer (or the period went away).

        Tracked totals may have moved — idle time discarded, or reassigned to
        another project — so today's figures are re-read rather than left
        showing a number the backend no longer agrees with.
        """
        log.info("idle period resolved in UI: %s", result)
        self._load_today_time()
        self._update_stat_cards()

    def _forget_idle_dialog(self) -> None:
        dialog, self._idle_dialog = self._idle_dialog, None
        if dialog is not None:
            dialog.deleteLater()

    def _close_idle_dialog(self) -> None:
        """Tear the popup down on logout or shutdown.

        The pending period is not lost: it lives on the backend, which
        resolves it as discarded when the time entry stops.
        """
        if self._idle_dialog is not None:
            self._idle_dialog.force_close()
            self._forget_idle_dialog()

    # ── Profile (web client handoff) ──────────────────────────────────────────

    #: Where each Activity tab's history lives in the web client. These are the
    #: routes the frontend actually declares (`frontend/src/App.tsx`) — the
    #: member's own screenshots page and the "apps"/"urls" report pages — not
    #: invented paths. Both report pages already read `?start=`/`?end=` to seed
    #: their range, so a day can be handed over in the link.
    _PROFILE_ROUTES = {
        "screenshots": "member/screenshots",
        "apps": "member/reports/apps",
        "urls": "member/reports/urls",
    }

    def _open_activity_in_profile(self, kind: str, day: date) -> None:
        """Open the web page holding the history for one Activity tab and date.

        The desktop deliberately keeps only a recent window of activity (see
        `background_services/activity/retention.py`); everything older is still
        on the server, and this is the way to it. The web client applies its own
        authentication and permission checks on arrival — the handoff signs the
        user in as themselves and grants nothing extra, so this cannot become a
        way to see activity the user could not otherwise see.
        """
        route = self._PROFILE_ROUTES.get(kind)
        if route is None:
            log.warning("no web route for activity tab %r", kind)
            return
        # A single day, expressed as the inclusive from/to span the report and
        # screenshot pages already take.
        iso = day.isoformat()
        self._open_web_profile(route=route, params={"start": iso, "end": iso})

    def _open_web_profile(self, route: Optional[str] = None,
                          params: Optional[Dict[str, Any]] = None) -> None:
        """Open the web client in the browser as the signed-in user.

        The handoff token is fetched on the task pool, never here: minting it
        is a network call, and this runs on the GUI thread. `key` makes a
        second click while one is in flight a no-op rather than a second tab.

        If the token cannot be minted the web client is still opened, without
        one, so the user lands on its login screen. That is the honest
        outcome -- the alternative is a dead menu item that explains nothing.
        """
        base_url = self.runtime.portal_service.web_app_url
        if not base_url:
            self.api.notify(
                "No web dashboard is configured for this installation.",
                NotificationLevel.WARNING, key="web-profile",
            )
            return

        def _open(token: Optional[str]) -> None:
            QDesktopServices.openUrl(
                QUrl(build_web_url(base_url, token, route=route, params=params))
            )

        def _failed(exc: BaseException) -> None:
            log.warning("web profile handoff failed: %s", exc)
            self.api.notify(
                "Opening your profile — please sign in on the website.",
                NotificationLevel.WARNING, key="web-profile",
            )
            _open(None)

        self.api.run_in_background(
            self.runtime.portal_service.create_handoff_token,
            on_success=_open,
            on_error=_failed,
            # Keyed on the destination: a click on the account menu and a click
            # on an Activity tab's "View in Profile" want different pages, and
            # a single shared key would silently drop the second.
            key=f"web-profile-handoff:{route or ''}",
        )

    # ── Updates ───────────────────────────────────────────────────────────────

    def _open_update_download(self) -> None:
        """Open the download page for the pending update.

        The same destination the update notification opens, reached from the
        account menu instead — which is the whole point of the menu entry: a
        toast is transient, and the user who missed it needs a way back.

        No network call and nothing to wait for: the URL came with the update
        check that produced the badge. If the deployment published no download
        URL, say so rather than opening an empty page.
        """
        # An installable release gets the dialog, not the browser: the whole
        # point of the updater is that the user does not have to go and fetch
        # a file themselves. The browser remains the honest fallback for a
        # deployment that publishes a link but no checksum, where installing
        # would mean running something this client cannot verify.
        release = self.api.pending_update_release()
        url = self.api.update_download_url()
        action = update_menu_action(release, url, self.api.latest_release())

        if action == "dialog":
            self._open_update_dialog(release, self.api.force_update_pending())
        elif action == "browser":
            QDesktopServices.openUrl(QUrl(url))
        elif action == "check":
            # The badge is real but this session has not fetched the details
            # yet. Ask now; the answer raises the dialog through the ordinary
            # `update_offered` path.
            self._check_for_updates()
        else:
            self.api.notify(
                "An update is available, but no download location has been "
                "published. Please ask your administrator where to get it.",
                NotificationLevel.WARNING, key="update-no-url",
            )

    # ── Updates ───────────────────────────────────────────────────────────────

    def _check_for_updates(self) -> None:
        """Ask the backend now, from the account menu.

        The answer arrives asynchronously: a newer release raises the dialog
        through `update_offered`, and anything else is reported here. The
        service drops the request if a check or download is already running, so
        a repeatedly-clicked entry produces one request rather than one each.

        Nothing about this disturbs the ten-hour schedule — a successful check
        restamps it, so the next automatic one is ten hours from this, which is
        what someone who just checked would expect.
        """
        if self.api.updates.is_busy:
            self.api.notify(
                "Monitra is already checking for updates.",
                NotificationLevel.INFO, key="update-check-busy",
            )
            return
        self._awaiting_manual_check = True
        self.api.notify(
            "Checking for updates…", NotificationLevel.INFO, key="update-checking",
        )
        self.api.check_for_updates_now()

    def _on_update_state_changed(self, state: str) -> None:
        """Report the outcome of a *manual* check, and only a manual one.

        A scheduled check that finds nothing must stay silent — it runs every
        ten hours and a toast each time would be exactly the level-triggered
        notification storm this project has already paid for. A check the user
        explicitly asked for is different: silence there reads as a broken
        button, so the "you are up to date" answer is given once, to the person
        who asked, and the flag is cleared immediately.
        """
        if not self._awaiting_manual_check:
            return
        if state == UpdateState.UPDATE_AVAILABLE:
            # The dialog is already being raised by `update_offered`; saying
            # "an update is available" beside it would be telling them twice.
            self._awaiting_manual_check = False
            return
        if state == UpdateState.IDLE:
            self._awaiting_manual_check = False
            message, level, key = manual_check_outcome(
                self.api.latest_release(), version.VERSION
            )
            self.api.notify(message, level, key=key)

    def _on_update_offered(self, release, mandatory: bool) -> None:
        """A newer release the client can verify and install."""
        self._open_update_dialog(release, bool(mandatory))

    def _open_update_dialog(self, release, mandatory: bool) -> None:
        """Show the update prompt, or raise the one already up.

        The dialog is owned here rather than by the sidebar for the same reason
        the idle alert is: a window must not be owned by a widget that can be
        rebuilt underneath it.
        """
        if self._update_dialog is not None:
            self._update_dialog.raise_()
            self._update_dialog.activateWindow()
            return

        dialog = UpdateDialog(release, mandatory, parent=self.window())
        self._update_dialog = dialog
        # The dialog asks; the service decides and does. It cannot start two
        # downloads by being clicked twice, because the refusal lives in the
        # service's state machine rather than in this transient window.
        dialog.update_requested.connect(self.api.start_update)
        dialog.finished.connect(lambda _result: self._forget_update_dialog())
        dialog.show()

    def _on_update_progress(self, received: int, total: int) -> None:
        if self._update_dialog is not None:
            self._update_dialog.set_progress(int(received), int(total))

    def _on_update_failed(self, message: str) -> None:
        """The update did not happen. The installation is untouched.

        Reported in the dialog when one is open, because that is where the user
        is looking, and as a notification otherwise — a failure with nowhere to
        appear is a failure the user never learns about.
        """
        if self._update_dialog is not None:
            self._update_dialog.show_error(message)
            return
        self.api.notify(message, NotificationLevel.WARNING, key="update-failed")

    def _on_install_started(self, version_name: str) -> None:
        """The installer is running and is waiting for this process to exit.

        Quitting is an ordinary quit: the normal shutdown path stops services
        in reverse order, flushes the cache and closes the database, so tracked
        time, the sync queue and pending captures are as safe as they are on
        any other exit. Nothing here needs a special case for the timer.
        """
        if self._update_dialog is not None:
            self._update_dialog.close_for_install()
            self._forget_update_dialog()
        self.api.notify(
            f"Installing Monitra {version_name}. The application will restart.",
            NotificationLevel.INFO, key="update-installing",
        )
        self.quit_requested.emit()

    def _forget_update_dialog(self) -> None:
        dialog, self._update_dialog = self._update_dialog, None
        if dialog is not None:
            dialog.deleteLater()

    def _close_update_dialog(self) -> None:
        """Tear the prompt down on logout or shutdown.

        Nothing is lost: the release is not a local decision, and the next
        successful check re-offers it — mandatory or not — so this cannot
        become a way to escape a required update.
        """
        if self._update_dialog is not None:
            self._update_dialog.force_close()
            self._forget_update_dialog()

    # ── Feedback & Help ───────────────────────────────────────────────────────

    def _open_feedback_dialog(self) -> None:
        """Open the Feedback & Help form, or raise the one already open.

        The dialog is owned here rather than by the sidebar for the same
        reason the idle alert is: a window must not be owned by a widget that
        can be rebuilt underneath it.
        """
        if self._feedback_dialog is not None:
            self._feedback_dialog.raise_()
            self._feedback_dialog.activateWindow()
            return

        dialog = FeedbackDialog(
            self.api,
            # The runtime's own client, so the request carries the current
            # session's token and this window invents no second HTTP path.
            submitter=self.runtime.feedback_service.submit_feedback,
            parent=self.window(),
        )
        self._feedback_dialog = dialog
        dialog.finished.connect(lambda _result: self._forget_feedback_dialog())
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _forget_feedback_dialog(self) -> None:
        dialog, self._feedback_dialog = self._feedback_dialog, None
        if dialog is not None:
            dialog.deleteLater()

    def _close_feedback_dialog(self) -> None:
        """Discard an open feedback form on logout or shutdown.

        An unsent draft is not application state and is not preserved; a
        submission already in flight is left to the task runner, which drops
        its callback if the session generation has changed.
        """
        if self._feedback_dialog is not None:
            self._feedback_dialog.force_close()
            self._forget_feedback_dialog()

    # ── Session lifecycle ─────────────────────────────────────────────────────

    def on_login(self, user_data: dict) -> None:
        """
        Initialise the dashboard for a signed-in user.

        Cache first: everything already known locally is rendered immediately,
        then a single round of background refreshes reconciles it. The user is
        never made to wait on the network to reach a usable screen.
        """
        self._active = True
        self._sidebar.set_user(user_data)
        self._task_section.set_user_role(user_data.get("role_name"))
        self._user_id = user_data.get("id")
        self._task_section.set_user_id(user_data.get("id"))
        self._activity_section.set_enabled(True)
        # The profile already carries idle_enabled/idle_minutes, so the user's
        # own threshold is in effect before tracking can start -- without the
        # sign-in path paying for another request.
        self.api.apply_idle_profile(user_data)
        self.api.apply_screenshot_profile(user_data)

        self._render_cached_projects()

        self._refresh_timer.start(REFRESH_INTERVAL_MS)
        # Nothing is known about the backend's fingerprint for this session
        # yet: the first probe records it, later ones compare against it.
        self._sync_revision = None
        self._sync_probe_supported = None
        self._sync_probe_timer.start(SYNC_PROBE_INTERVAL_MS)

        # One bootstrap, not three. refresh_data() already fans out projects,
        # task statuses, the day's time entries and today's activity -- and
        # it does so concurrently on the shared pool. Calling those loaders
        # again here duplicated every one of them: the de-duplication key
        # only suppresses a second submission while the first is still in
        # flight, so a fast reply produced a second identical request.
        # Measured: two GET /time-entries on every single login.
        self.refresh_data()
        # Not part of the refresh round: this asks a different question
        # (does the server think a timer is running) and has its own key.
        self._check_active_timer()

    def on_session_verified(self, user_data: dict) -> None:
        """The restored token was confirmed by the backend."""
        self.api.apply_idle_profile(user_data)
        self.api.apply_screenshot_profile(user_data)
        self._sidebar.set_user(user_data)
        self._task_section.set_user_role(user_data.get("role_name"))
        self._user_id = user_data.get("id")
        self._task_section.set_user_id(user_data.get("id"))

    def reset_state(self) -> None:
        """Clear everything session-scoped on logout."""
        self._active = False
        self._refresh_timer.stop()
        self._sync_probe_timer.stop()
        self._project_retry_timer.stop()
        self._empty_project_load_retries = 0
        self._activity_section.set_enabled(False)
        self.api.cancel_key("load-projects")
        # Parameterised families: `load-tasks:{project_id}`, `load-today:{date}`.
        # Cancelling the bare name matched nothing, so these ran on after
        # logout (their results were still dropped by the session guard).
        self.api.cancel_keys_with_prefix("load-tasks:")
        self.api.cancel_keys_with_prefix("load-today:")
        self.api.cancel_key("load-today-activity")
        self.api.cancel_key("load-statuses")
        self.api.cancel_key("sync-probe")
        self.api.cancel_key("check-active-timer")
        self.api.cancel_key("idle-reassign-projects")
        self.api.cancel_key(SUBMIT_KEY)
        self._close_idle_dialog()
        self._close_feedback_dialog()
        self._close_update_dialog()

        # Those cancellations mean the in-flight refresh's steps will never
        # report back; clear the round so the next session can refresh.
        self._refresh_outstanding = 0
        self._refresh_failed = False

        try:
            self.api.cache.clear_app_state(LAST_PROJECT_KEY)
        except Exception:  # noqa: BLE001
            log.exception("could not clear the last selected project")

        self._projects = []
        self._current_project = None
        self._user_id = None
        self._today_time_entries = []
        self._today_activity = TodaySnapshot()
        self._pending_active_timer = None
        self._sync_revision = None
        self._sync_probe_supported = None
        self._sidebar.set_projects([])
        self._sidebar.set_timer_active(False)
        self._sidebar.set_active_timer_project(None)
        # The runtime forgets the break itself at logout; this is the
        # window's copy of that fact, and of the task Play would start.
        self._break_status = BreakStatus.NONE
        self._selected_task = None
        self._last_tracked_task = None
        self._sidebar.set_break_status(BreakStatus.NONE)
        self._sidebar.set_play_available(False)
        self._sidebar.set_live_date(True)
        self._action_banner.dismiss()
        self._sidebar.set_total_seconds(0)
        self._sidebar.set_project_totals(None)
        self._task_section.set_all_projects([])
        self._task_section.clear()
        self._project_tasks = []
        self._stat_cards.reset()
        self._status_bar.set_message("Ready")
        self._status_bar.set_timer_info("")

    def _handle_logout(self) -> None:
        self.reset_state()
        self.logout_requested.emit()

    # ── Projects ──────────────────────────────────────────────────────────────

    def _render_cached_projects(self) -> None:
        cached = self.api.cache.get_cached_projects()
        if cached:
            self._projects = cached
            self._sidebar.set_projects(cached)
            self._task_section.set_all_projects(cached)
            self._status_bar.set_message("Loaded projects from cache.")
            self._sync_log(
                "cache.loaded", projects=len(cached),
                age_seconds=self._cache_age_for_log(),
            )
            self._apply_active_timer_if_ready()
            self._select_initial_project()
        else:
            # Rendered inside the sidebar's projects area, which holds its
            # geometry whatever the message is -- so the account card and the
            # sync footer do not move between loading, empty and loaded.
            self._sidebar.set_projects_message("Loading projects…")
            self._status_bar.set_message("Loading projects…")

    def load_projects(self, on_done: Optional[Callable[[bool], None]] = None) -> bool:
        """Refresh projects from the backend.

        :param on_done: Called with True/False once the fetch succeeded or
            failed, after the normal handler has run. Used by refresh_data to
            decide whether the refresh as a whole succeeded.
        :return: False if the request was de-duplicated against one already in
            flight, in which case `on_done` is never called.
        """
        return self._run_load(
            self.project_service.get_projects,
            self._on_projects_loaded,
            self._on_projects_error,
            key="load-projects",
            on_done=on_done,
        )

    def _run_load(
        self,
        call: Callable[[], Any],
        on_success: Callable[[Any], None],
        on_error: Callable[[BaseException], None],
        *,
        key: str,
        on_done: Optional[Callable[[bool], None]] = None,
    ) -> bool:
        """Submit one background fetch, reporting its outcome to `on_done`.

        A thin wrapper over api.run_in_background so every loader reports
        success or failure the same way, without any of them growing a second
        code path for the refresh case.

        `on_done` runs whatever the handler does. A handler that raised used
        to skip it, which left the refresh round's outstanding count one too
        high for ever: every later refresh -- periodic, on reconnect, and the
        button -- was then dropped as "already in flight", silently, until the
        user signed out. The exception is still logged by the task runner.
        """
        def succeeded(result: Any) -> None:
            try:
                on_success(result)
            finally:
                if on_done is not None:
                    on_done(True)

        def failed(exc: BaseException) -> None:
            try:
                on_error(exc)
            finally:
                if on_done is not None:
                    on_done(False)

        return self.api.run_in_background(
            call, on_success=succeeded, on_error=failed, key=key
        ) is not None

    def _on_projects_loaded(self, projects: list) -> None:
        """The backend's current project list: reconcile everything derived.

        The server is authoritative. Whatever is selected has to be
        re-checked against the list it just sent, because the list is the
        only thing that knows a project has gone: archived on the web, or
        this user removed from it. Before this the selection was simply left
        alone, so a removed project stayed selected with its cached tasks on
        screen while every refresh asked the backend for them, got 404, and
        reported "showing cached tasks -- retrying" until the user signed out.
        """
        self._sync_log("server.received", resource="projects", count=len(projects))
        # A real answer arrived, empty or not -- the empty-state retry loop
        # was for "couldn't ask", not "asked and there are none".
        self._empty_project_load_retries = 0
        self._project_retry_timer.stop()
        self._projects = projects
        self._sidebar.set_projects(projects)
        self._task_section.set_all_projects(projects)
        self.api.cache.cache_projects(projects)
        if projects:
            # The count now lives in the sidebar's "PROJECTS (N)" header --
            # repeating it here would just be stale, duplicate information.
            self._status_bar.set_message("Ready")
            self._reconcile_selection(projects)
            self._apply_active_timer_if_ready()
            self._select_initial_project()
        else:
            self._status_bar.set_message("No projects found.", TEXT_MUTED)
            if self._current_project:
                self._drop_project_selection(self._current_project.get("id"))
            self._task_section.clear()

    def _reconcile_selection(self, projects: list) -> None:
        """Keep the selection pointing at the server's copy of the project.

        Three cases. The selected project is still listed: adopt the fresh
        record (its name or members may have changed) without disturbing the
        selection. It is not listed: it is no longer this user's, so the
        selection and its cached tasks go, and the usual initial selection
        picks a valid one. Nothing selected: nothing to do.
        """
        current = self._current_project
        if not current:
            return
        current_id = current.get("id")
        fresh = next((p for p in projects if p.get("id") == current_id), None)
        if fresh is None:
            self._sync_log("selection.dropped", project=current_id, reason="not in server list")
            self._drop_project_selection(current_id)
            self._status_bar.set_message(
                "The project you were viewing is no longer available to you.", WARNING
            )
            return
        if fresh is not current:
            self._current_project = fresh

    def _drop_project_selection(self, project_id: Optional[int]) -> None:
        """Clear a selection whose project the server no longer lists.

        The running timer, if any, is deliberately left alone: a project
        sync must never stop, switch or reset tracked time. The backend will
        refuse work against a project the user has lost, and the timer
        service reconciles that on its own path.
        """
        self._current_project = None
        self._project_tasks = []
        self._task_section.clear()
        if project_id is not None:
            self.api.cancel_key(f"load-tasks:{project_id}")
            try:
                self.api.cache.forget_project_tasks(project_id)
            except Exception:  # noqa: BLE001
                log.exception("could not drop cached tasks for project %s", project_id)
        self._update_stat_cards()

    def _on_projects_error(self, exc: BaseException) -> None:
        # Cached data stays on screen. A failed request must not blank a view
        # that is already showing valid local data.
        #
        # It must not touch the connectivity pill either: one failed request is
        # not a connectivity measurement. Ask NetworkService to probe now and
        # let it decide -- it owns that state.
        self._sync_log("refresh.failed", resource="projects", error=str(exc))
        if self._projects:
            self.api.network.check_now()
            age = self._cache_age_for_log()
            self._status_bar.set_message(
                f"Showing projects from {self._describe_age(age)} — retrying.", WARNING
            )
            return
        self._sidebar.set_projects_message("Unable to load projects")
        self._status_bar.set_message(f"Could not load projects: {exc}", ERROR)
        if "session expired" in str(exc).lower():
            self.unauthorized_error.emit()
            return
        self._schedule_empty_project_retry()

    #: Backoff between automatic retries of an empty-state project load,
    #: capped well under REFRESH_INTERVAL_MS so a genuinely transient failure
    #: is not left on screen for the full periodic-refresh interval.
    _EMPTY_PROJECT_RETRY_DELAYS_MS = (3_000, 6_000, 12_000, 24_000)

    def _schedule_empty_project_retry(self) -> None:
        """One more attempt at the first project load, on a short backoff.

        Only for the case `_on_projects_error` already guards on: nothing is
        cached, so there is nothing to show while this waits. A project
        already on screen means a later refresh failed instead, which the
        existing "showing cached data -- retrying" message and the
        network-state/periodic paths already cover.
        """
        if self._projects or not self._active:
            return
        index = min(self._empty_project_load_retries, len(self._EMPTY_PROJECT_RETRY_DELAYS_MS) - 1)
        delay = self._EMPTY_PROJECT_RETRY_DELAYS_MS[index]
        self._empty_project_load_retries += 1
        self._project_retry_timer.start(delay)

    def _cache_age_for_log(self) -> Optional[int]:
        try:
            age = self.api.cache.projects_cache_age_seconds()
        except Exception:  # noqa: BLE001
            log.debug("could not read the projects cache age", exc_info=True)
            return None
        return int(age) if age is not None else None

    @staticmethod
    def _describe_age(age_seconds: Optional[int]) -> str:
        """`age_seconds` in words a status bar can show: "2 minutes ago"."""
        if age_seconds is None:
            return "the local cache"
        if age_seconds < 90:
            return "a moment ago"
        minutes = age_seconds // 60
        if minutes < 90:
            return f"{minutes} minutes ago"
        hours = minutes // 60
        if hours < 36:
            return f"{hours} hours ago"
        return f"{hours // 24} days ago"

    def _remembered_project_id(self) -> Optional[int]:
        """The project this user was last in, if it is still one of theirs."""
        try:
            stored = self.api.cache.load_app_state(LAST_PROJECT_KEY)
        except Exception:  # noqa: BLE001
            log.exception("could not read the last selected project")
            return None
        return stored if isinstance(stored, int) else None

    def _remember_project_id(self, project_id: Optional[int]) -> None:
        if not isinstance(project_id, int):
            return
        try:
            self.api.cache.save_app_state(LAST_PROJECT_KEY, project_id)
        except Exception:  # noqa: BLE001
            # Losing the memory costs one extra click next launch. It must
            # never cost the selection the user just made.
            log.exception("could not record the last selected project")

    def _select_initial_project(self) -> None:
        """Open a project as soon as the list exists, without waiting for a click.

        Rendering the sidebar and leaving the task area empty was the whole of
        the reported problem: the tasks were already in the local cache and
        could have been on screen immediately, but nothing selected a project,
        so the user had to click one and wait out a request to see anything.

        Selection is not forced on top of anything: an explicit choice, and a
        running timer's own project, both win. The remembered project is only
        honoured if it is still in this user's list, so a stale id -- or one
        belonging to whoever signed in last -- falls back to the first project
        rather than selecting nothing.
        """
        if self._current_project or not self._projects:
            return
        if self._pending_active_timer:
            # The running timer decides which project opens, and
            # _apply_active_timer_if_ready is about to select it. Choosing a
            # different one here would draw one project's tasks and replace
            # them a moment later.
            return

        remembered = self._remembered_project_id()
        project = next(
            (p for p in self._projects if p.get("id") == remembered), None
        )
        self._on_project_selected(project or self._projects[0])

    def _on_project_selected(self, project: Dict[str, Any]) -> None:
        self._current_project = project
        project_id = project.get("id")
        self._remember_project_id(project_id)
        project_name = project.get("project_name", "Project")

        index = next(
            (i for i, p in enumerate(self._projects) if p.get("id") == project_id), 0
        )
        self._current_project_color = PROJECT_COLORS[index % len(PROJECT_COLORS)]
        self._sidebar.select_project(project_id)

        # PROJECT STATUS and PROJECT HOURS depend only on the project itself
        # (already loaded) and today's time entries (already loaded) -- not
        # on the task list, so they need not wait for _render_tasks below.
        self._update_stat_cards()

        cached_tasks = self.api.cache.get_cached_tasks(project_id)
        if cached_tasks is not None:
            self._render_tasks(cached_tasks, from_cache=True)
        else:
            self._task_section.set_loading(project_name)

        self._load_tasks(project_id)

    def _load_tasks(
        self, project_id: int, on_done: Optional[Callable[[bool], None]] = None
    ) -> bool:
        version = self._task_list_version
        return self._run_load(
            lambda: self.task_service.get_tasks_for_project(project_id),
            lambda tasks: self._on_tasks_loaded(project_id, tasks, version),
            self._on_tasks_error,
            key=f"load-tasks:{project_id}",
            on_done=on_done,
        )

    def _on_tasks_loaded(
        self, project_id: int, tasks: list, version: Optional[int] = None
    ) -> None:
        # Ignore a response for a project the user has since navigated away
        # from: a slow reply must never overwrite a newer selection.
        if not self._current_project or self._current_project.get("id") != project_id:
            log.debug("discarding tasks for project %s; selection moved on", project_id)
            return
        if version is not None and version != self._task_list_version:
            # The user created, edited or deleted a task while this list was
            # in flight, so it predates a change already on screen. Painting
            # it would undo that change until the next refresh -- the
            # "my new task disappeared" report. Discard it and read again;
            # the reload the mutation asked for was de-duplicated against
            # this very request, so nothing else will.
            self._sync_log(
                "server.discarded", resource="tasks", project=project_id,
                reason="a local change landed while the list was in flight",
            )
            self._load_tasks(project_id)
            return
        self._sync_log("server.received", resource="tasks", project=project_id, count=len(tasks))
        self.api.cache.cache_tasks(project_id, tasks)
        self._render_tasks(tasks, from_cache=False)

    def _on_task_mutated(self, kind: str, project_id: int, payload: object) -> None:
        """Show the result of a task CRUD call on the list already on screen.

        `payload` is the server's own response to the request that made the
        change, so nothing here is guessed: a created task is the row the
        backend just wrote, with the id, status and assignee it chose. It goes
        straight into the cache and the view, and a targeted reload follows to
        reconcile anything derived.

        Before this, a task mutation emitted a blanket refresh -- projects,
        task statuses, the day's time entries and the tasks, four requests --
        and the row appeared only when the last of them came back. That is the
        "after creating a task it takes a few seconds to appear" report.

        Note what is *not* done here: the tracked-time column is not invented.
        `_render_tasks` reads banked seconds from the time entries already
        loaded, so a task created a moment ago shows the zero it has actually
        earned.
        """
        # A slow mutation whose project is no longer selected must not
        # overwrite the newer selection, nor trigger a load for it.
        if not self._current_project or self._current_project.get("id") != project_id:
            log.debug("discarding task mutation for project %s; selection moved on", project_id)
            return

        # Any list fetched before this moment is now stale; see _on_tasks_loaded.
        self._task_list_version += 1
        self._sync_log("mutation.applied", kind=kind, project=project_id,
                       task=payload.get("id") if isinstance(payload, dict) else None)

        tasks = list(self._project_tasks or [])
        task_id = payload.get("id") if isinstance(payload, dict) else None

        if task_id is None:
            # Nothing identifiable came back. Rather than guess, let the
            # reload below be the whole answer.
            log.debug("task mutation %s returned no id; relying on the reload", kind)
            self._load_tasks(project_id)
            return

        if kind == "created":
            if not any(task.get("id") == task_id for task in tasks):
                tasks.append(payload)
        elif kind == "updated":
            index = next(
                (i for i, task in enumerate(tasks) if task.get("id") == task_id), None
            )
            if index is None:
                tasks.append(payload)
            else:
                tasks[index] = payload
        elif kind == "deleted":
            tasks = [task for task in tasks if task.get("id") != task_id]
        else:
            log.warning("unknown task mutation kind %r; reloading instead", kind)
            self._load_tasks(project_id)
            return

        self.api.cache.cache_tasks(project_id, tasks)
        self._render_tasks(tasks, from_cache=False)
        # Reconcile. This is the same targeted load a project selection makes,
        # de-duplicated on its own key, and it no longer stands between the
        # user and their own edit.
        self._load_tasks(project_id)

    def _on_tasks_error(self, exc: BaseException) -> None:
        if getattr(self._task_section, "_has_loaded_tasks", False):
            # See _on_projects_error: the pill belongs to NetworkService.
            self.api.network.check_now()
            self._status_bar.set_message("Showing cached tasks — retrying.", WARNING)
            return
        self._task_section.set_error(str(exc))
        self._status_bar.set_message(f"Could not load tasks: {exc}", ERROR)
        if "session expired" in str(exc).lower():
            self.unauthorized_error.emit()

    def _render_tasks(self, tasks: list, from_cache: bool) -> None:
        for task in tasks:
            task["time_tracked_seconds"] = self._banked_seconds_by_task().get(task.get("id"), 0)
        self._task_section.set_tasks(tasks, self._current_project, self._current_project_color)
        self._project_tasks = tasks or []
        self._update_stat_cards()
        self._status_bar.set_message(
            "Loaded tasks from cache." if from_cache else f"{len(tasks)} tasks loaded."
        )

    # ── Summary cards ─────────────────────────────────────────────────────────

    def _update_stat_cards(self) -> None:
        """Push a fresh snapshot into the three summary cards.

        Everything here is read from state this window already loaded. No
        request is made, no duration is counted: the live session's elapsed
        seconds come from TimerService, exactly as the sidebar's per-project
        total does, and only for today -- a past date shows its completed
        hours alone.
        """
        project = self._current_project
        project_id = project.get("id") if project else None

        # PROJECT STATUS: exactly what an admin set from the web frontend
        # (`projects.status_id` -> `project_statuses.name`/`color`), already
        # inline on every project this window loads. No new request.
        status = (project or {}).get("status") or {}
        self._stat_cards.set_project_status(status.get("name"), status.get("color"))

        # PROJECT HOURS: the selected project's own tracked time for the day,
        # not the total across every project -- the same figure the sidebar
        # already shows next to this project's name.
        viewing_today = is_live_date(self._current_date)
        running = self.api.is_timer_running()
        live = self.api.timer_elapsed_seconds() if (running and viewing_today) else 0
        session = (self.api.active_session() or {}) if running else {}
        tracking_this_project = bool(live) and project_id is not None and session.get("project_id") == project_id
        hours = self._banked_seconds_by_project().get(project_id, 0) if project_id is not None else 0
        if tracking_this_project:
            hours += live
        self._stat_cards.set_total_seconds(hours, tracking_this_project)

        if running:
            self._stat_cards.set_active_task(
                session.get("task_name"),
                session.get("project_name") or self._project_name_for(session.get("project_id")),
            )
        elif self.api.break_status() != BreakStatus.NONE:
            # On break: the held task, shown as paused. The timer is idle and
            # the card must not read as tracking; see set_active_task_on_break.
            held = self.api.pre_break_task() or {}
            self._stat_cards.set_active_task_on_break(
                held.get("task_name"), self._project_name_for(held.get("project_id"))
            )
        else:
            self._stat_cards.set_active_task(None, None)

        # TODAY'S ACTIVITY: the persisted half (uploaded + still queued),
        # last fetched by `_load_today_activity`, plus the window currently
        # being sampled -- cheap and non-blocking enough to add on every
        # tick, which is what keeps the figure moving between the periodic
        # refreshes rather than jumping only once every couple of minutes.
        # Always today, regardless of which date is browsed: this card has
        # never been about a project or a browsed day, only "how active has
        # this signed-in session been so far today".
        totals = self._today_activity.totals + self.api.live_activity_totals()
        self._stat_cards.set_today_activity(
            totals.percent,
            has_measurement=totals.has_measurement,
            is_tracking=running,
        )

    def _project_name_for(self, project_id: Optional[int]) -> Optional[str]:
        if project_id is None:
            return None
        for project in self._projects:
            if project.get("id") == project_id:
                return project.get("project_name")
        return None

    # ── Today's time ──────────────────────────────────────────────────────────

    def _banked_seconds_by_task(self) -> Dict[int, int]:
        """Completed seconds per task for the currently displayed day."""
        totals: Dict[int, int] = {}
        for entry in self._today_time_entries:
            task_id = entry.get("task_id")
            if not task_id:
                continue
            if _is_finished(entry):
                totals[task_id] = totals.get(task_id, 0) + banked_seconds(entry)
        return totals

    def _banked_today(self) -> int:
        """Completed seconds for the displayed day, netted like every report."""
        return sum(banked_seconds(e) for e in self._today_time_entries if _is_finished(e))

    def _banked_seconds_by_project(self) -> Dict[int, int]:
        """Completed seconds per project for the displayed day.

        The same rows, the same netting and the same "finished only" rule as
        `_banked_seconds_by_task`, grouped one level up: a project's figure
        is exactly the sum of its tasks' HOURS column for the day.
        """
        totals: Dict[int, int] = {}
        for entry in self._today_time_entries:
            project_id = entry.get("project_id")
            if not project_id:
                continue
            if _is_finished(entry):
                totals[project_id] = totals.get(project_id, 0) + banked_seconds(entry)
        return totals

    def _project_totals_for_display(self, live: int) -> Dict[int, int]:
        """Per-project figures for the sidebar: banked, plus the live session
        on the project being tracked. `live` is the caller's already-gated
        value -- 0 unless a timer runs *and* today is on screen -- so this
        never adds today's session to another day's totals."""
        totals = self._banked_seconds_by_project()
        if live > 0:
            session = self.api.active_session() or {}
            project_id = session.get("project_id")
            if project_id:
                totals[project_id] = totals.get(project_id, 0) + live
        return totals

    def _load_today_time(
        self,
        target_date: Optional[date] = None,
        on_done: Optional[Callable[[bool], None]] = None,
    ) -> bool:
        target = as_calendar_day(target_date) or ist_today()
        if date_mode(target) == DateMode.FUTURE:
            # A day that has not happened has no entries to return. Asking
            # anyway would be a round trip whose only possible answer is the
            # empty list the caller can have for free.
            log.debug("not requesting time entries for %s: the day is in the future", target)
            return False

        cached = self.api.cache.get_cached_time_entries(target.isoformat())
        if cached:
            self._apply_time_entries(cached, target, update_cache=False)

        api_client = self.api_client
        user_id = self._user_id

        def call():
            # The IST calendar day expressed as its half-open UTC interval,
            # from the one helper that defines it. This used to send naive
            # local-looking strings (00:00:00 .. 23:59:59) which the backend
            # read as UTC, so the desktop was filtering on a UTC day while
            # labelling it the IST day -- everything tracked before 05:30 IST
            # landed on the previous day's screen.
            start, end = ist_day_bounds_utc(target)
            params = {
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
                "limit": 1000,
            }
            # Scoped to the signed-in user. Without it the backend returns
            # every entry in the organisation to anyone holding
            # `time_entries:view_all` -- see self._user_id.
            if user_id is not None:
                params["user_id"] = user_id
            response = api_client.get("/time-entries", params=params)
            data = response.json()
            return data if isinstance(data, list) else []

        return self._run_load(
            call,
            lambda entries: self._apply_time_entries(entries, target, True),
            lambda exc: log.info("could not refresh today's entries: %s", exc),
            key=f"load-today:{target.isoformat()}",
            on_done=on_done,
        )

    def _load_today_activity(self, on_done: Optional[Callable[[bool], None]] = None) -> bool:
        """Refresh TODAY'S ACTIVITY's persisted half (uploaded + still queued)
        in the background. The window still being sampled is layered on top
        of whatever this last fetched, live, every timer tick -- see
        `_update_stat_cards`."""
        today = ist_today()
        return self._run_load(
            lambda: self.api.today_activity_snapshot(today),
            self._on_today_activity_loaded,
            lambda exc: log.info("could not refresh today's activity: %s", exc),
            key="load-today-activity",
            on_done=on_done,
        )

    def _on_today_activity_loaded(self, snapshot: TodaySnapshot) -> None:
        # A failed backend read comes back as `remote_ok=False`, never as an
        # exception (see `today_activity_snapshot`'s own contract) -- keeping
        # the last good snapshot in that case is what stops one transient
        # blip regressing a real percentage to a blank card.
        if snapshot.remote_ok:
            self._today_activity = snapshot
        self._update_stat_cards()

    def _overlay_pending_stops(self, entries: list) -> list:
        """Show the day as the backend *will* record it.

        An entry whose stop is still in the durable queue is `running` on the
        backend and `stopped` here. Rendering the backend's row as-is would
        drop the session's seconds from every total (a running row banks 0)
        until the queue drains; the queued stop carries the instant the user
        pressed Stop, so the finished entry can be shown now with the very
        duration the backend will compute from it. One reconciliation path:
        server rows, plus the local mutations the server has not seen yet.
        """
        cache = getattr(self.api, "cache", None)
        if cache is None:
            return entries
        overlaid = []
        for entry in entries:
            if entry.get("end_time") is None and entry.get("id"):
                try:
                    pending = cache.pending_stop_payload_for_entry(entry.get("id"))
                except Exception:  # noqa: BLE001
                    log.exception("could not check for a queued stop of entry %s", entry.get("id"))
                    pending = None
                stopped_at = parse_utc((pending or {}).get("stopped_at"))
                started = parse_utc(entry.get("start_time"))
                if stopped_at is not None and started is not None:
                    entry = dict(entry)
                    entry["end_time"] = stopped_at.isoformat()
                    entry["status"] = "stopped"
                    entry["total_seconds"] = max(
                        0, round((stopped_at - started).total_seconds())
                    )
                    entry["net_seconds"] = max(
                        0, entry["total_seconds"] + int(entry.get("adjustment_seconds") or 0)
                    )
                    entry["pending_stop"] = True
            overlaid.append(entry)
        return overlaid

    def _apply_time_entries(
        self, entries: list, target: date, update_cache: bool = True
    ) -> None:
        entries = self._overlay_pending_stops(entries)
        if update_cache:
            # Server data, not the cached copy of an older answer.
            self._reconcile_running_entry_adjustment(entries)
        self._today_time_entries = entries
        banked = self._banked_today()

        # Add the live session only for today, and take its value from the
        # timer service rather than from any widget.
        live = 0
        if is_live_date(target) and self.api.is_timer_running():
            live = self.api.timer_elapsed_seconds()
        self._sidebar.set_total_seconds(banked + live)
        self._sidebar.set_project_totals(self._project_totals_for_display(live))

        if update_cache:
            self.api.cache.cache_time_entries(target.isoformat(), entries)

        self._task_section.update_tasks_tracked_times(self._banked_seconds_by_task())
        self._update_stat_cards()

    def _reconcile_running_entry_adjustment(self, entries: list) -> None:
        """Carry the backend's deduction for the running entry into the timer.

        The day's list includes the entry that is running, and its row
        carries `adjustment_seconds` -- the same figure the resolve response
        delivers directly. Reading it here as well covers what that path
        cannot: a restart with the deduction already on the server, an idle
        answer given from another machine, and an unwanted-activity penalty
        applied by the sync queue. `allow_increase=False`, because this list
        is re-read on several triggers and a reply issued before an idle
        answer was committed can land after it; deductions only accumulate
        on a running entry, so a stale list may never undo a fresher one.
        """
        if not self.api.is_timer_running():
            return
        session = self.api.active_session() or {}
        entry_id = session.get("entry_id")
        if not entry_id:
            return
        for entry in entries:
            if entry.get("id") != entry_id or entry.get("end_time") is not None:
                continue
            if "adjustment_seconds" not in entry:
                return
            self.api.timer.apply_entry_adjustment(
                entry_id, entry.get("adjustment_seconds"), allow_increase=False
            )
            return

    def _on_date_changed(self, target_date: date) -> None:
        """Point the whole window at the newly selected day.

        A future date cannot arrive here — the header refuses to select one —
        but it is rejected rather than trusted, because adopting it would put
        the timer totals, the day's entries and all three Activity tabs onto a
        day nothing can be tracked against. The same goes for a value that is
        not a readable calendar day: there is nothing to load for it, and
        defaulting it to today would silently show one day's data under
        another's heading.
        """
        day = as_calendar_day(target_date)
        if day is None or date_mode(day) == DateMode.FUTURE:
            log.warning("ignoring an unselectable date from the header: %r", target_date)
            return
        target_date = day
        self._current_date = target_date
        self._task_section.set_viewing_date(target_date)
        # One selected date drives the whole window: the time entries above and
        # all three Activity tabs below. Before this, the Activity panel ignored
        # the picker entirely and showed an all-time total under whatever date
        # the header happened to say.
        self._activity_section.set_selected_date(target_date)
        self._status_bar.set_message(f"Loading data for {target_date}…")
        self._load_today_time(target_date)
        # The circular control follows the date rule the task rows follow:
        # live only on today.
        self._render_timer_controls()

    # ── Refresh ───────────────────────────────────────────────────────────────

    def refresh_data(self) -> None:
        """
        Re-fetch everything the dashboard displays: projects, task statuses,
        the selected project's tasks, and the viewed day's time entries. Each
        fetch caches its result and re-renders its own widgets, so the screen
        shows the backend's current data rather than the previous snapshot.

        "Last sync" is advanced only once every fetch of the round has come
        back successfully -- a refresh that failed, wholly or partly, leaves
        the previous timestamp alone and reports itself through each loader's
        existing error handling.

        Skipped entirely while the backend is unusable: refreshing into a known
        outage produces nothing but retry noise and reconnect storms.
        """
        if not self._active:
            return
        # Skipped only on *measured* evidence of an outage. UNKNOWN means the
        # first probe has not committed yet, which is not the same thing: a
        # user who has just signed in has proved the backend answers, and
        # refusing to load here left the dashboard empty until a probe caught
        # up -- the reported "data appears after a delay". See
        # NetworkState.WORTH_TRYING.
        if self.api.network_state() not in NetworkState.WORTH_TRYING:
            self._sync_log("refresh.skipped", reason=f"network {self.api.network_state()}")
            self._status_bar.set_message(
                "Offline — showing the last data received.", WARNING
            )
            return
        if self._refresh_outstanding:
            age = monotonic() - self._refresh_started_at
            if age < REFRESH_STALE_AFTER_S:
                log.debug("refresh already in flight; ignoring")
                return
            # The watchdog. See REFRESH_STALE_AFTER_S: a round that never
            # reported back must not block every later one.
            log.error(
                "refresh round abandoned: %d fetch(es) never reported back in %.0fs; "
                "runtime health: %s", self._refresh_outstanding, age, self.api.health_report(),
            )
            self._refresh_outstanding = 0

        self._refresh_failed = False
        self._refresh_started_at = monotonic()
        self._sync_log("refresh.started", project=(self._current_project or {}).get("id"))
        # Callbacks are queued onto this thread, so none of them can run
        # before this method returns -- the count is complete before the
        # first step can decrement it.
        started = sum((
            self.load_projects(on_done=self._on_refresh_step),
            self._load_task_statuses(on_done=self._on_refresh_step),
            self._load_today_time(self._current_date, on_done=self._on_refresh_step),
            self._load_today_activity(on_done=self._on_refresh_step),
            self._load_tasks(self._current_project.get("id"), on_done=self._on_refresh_step)
            if self._current_project else False,
        ))
        self._refresh_outstanding = started
        if started:
            self._status_bar.set_message("Refreshing…")

    def _on_refresh_step(self, ok: bool) -> None:
        """One fetch of the in-flight refresh finished."""
        if self._refresh_outstanding <= 0:
            return
        self._refresh_outstanding -= 1
        self._refresh_failed = self._refresh_failed or not ok
        if self._refresh_outstanding:
            return

        elapsed_ms = int((monotonic() - self._refresh_started_at) * 1000)
        if self._refresh_failed:
            # The failing loader has already reported it and kept the cached
            # view on screen. Claiming a sync that did not happen would be
            # worse than showing the older timestamp.
            self._sync_log("refresh.completed", outcome="partial-failure", elapsed_ms=elapsed_ms)
            return
        self._sync_log("refresh.completed", outcome="ok", elapsed_ms=elapsed_ms)
        self.api.note_pull_succeeded()
        self._status_bar.set_message("Refreshed.")

    def _load_task_statuses(
        self, on_done: Optional[Callable[[bool], None]] = None
    ) -> bool:
        return self._run_load(
            self.task_service.get_task_statuses,
            self.api.cache.cache_task_statuses,
            lambda exc: log.info("could not load task statuses: %s", exc),
            key="load-statuses",
            on_done=on_done,
        )

    # ── Change probe ──────────────────────────────────────────────────────────

    def _probe_sync_revision(self) -> None:
        """Ask the backend whether anything this user can see has changed.

        One small request on the shared pool, de-duplicated by key. The
        lists are re-read only when the fingerprint moves, which is how a
        change made on the web reaches an open desktop within
        SYNC_PROBE_INTERVAL_MS without the lists being polled.

        Skipped while offline (there is nothing to compare against), while a
        refresh round is already in flight (it will fetch the current state
        anyway), and once the backend has answered that it has no such
        endpoint (an older deployment: the periodic full refresh is then the
        only mechanism, and it keeps its shorter cadence).
        """
        if not self._active or self._sync_probe_supported is False:
            return
        if self.api.network_state() not in NetworkState.WORTH_TRYING:
            return
        if self._refresh_outstanding:
            return
        self.api.run_in_background(
            self.project_service.get_sync_revision,
            on_success=self._on_sync_revision,
            on_error=self._on_sync_revision_error,
            key="sync-probe",
        )

    def _on_sync_revision(self, payload: Optional[Dict[str, Any]]) -> None:
        if not self._active:
            return
        if not isinstance(payload, dict) or not payload.get("revision"):
            # No endpoint on this backend. Said once; the full refresh keeps
            # the shorter cadence it started with.
            if self._sync_probe_supported is not False:
                self._sync_probe_supported = False
                self._sync_probe_timer.stop()
                self._sync_log("probe.unsupported", refresh_interval_ms=REFRESH_INTERVAL_MS)
            return
        if self._sync_probe_supported is not True:
            self._sync_probe_supported = True
            self._set_refresh_cadence(REFRESH_INTERVAL_WITH_PROBE_MS)

        revision = str(payload.get("revision"))
        previous = self._sync_revision
        self._sync_revision = revision
        if previous is None:
            components = payload.get("components")
            self._sync_components = dict(components) if isinstance(components, dict) else {}
            self._sync_log("probe.baseline", revision=revision)
            return
        if revision == previous:
            log.debug("sync probe: unchanged (%s)", revision)
            return
        self._sync_log(
            "probe.changed", previous=previous, revision=revision,
            components=self._changed_components(payload),
        )
        self.refresh_data()

    def _changed_components(self, payload: Dict[str, Any]) -> str:
        """Which parts of the fingerprint moved, for the log line."""
        components = payload.get("components")
        if not isinstance(components, dict):
            return "?"
        previous = getattr(self, "_sync_components", None) or {}
        self._sync_components = dict(components)
        moved = [name for name, value in components.items() if previous.get(name) != value]
        return ",".join(moved) if previous else "first-comparison"

    def _on_sync_revision_error(self, exc: BaseException) -> None:
        # Not worth a status-bar message: the probe is a convenience on top
        # of the refresh cadence, and the network service will notice a real
        # outage on its own. A dead session is the exception.
        self._sync_log("probe.failed", error=str(exc))
        if "session expired" in str(exc).lower():
            self.unauthorized_error.emit()

    def _set_refresh_cadence(self, interval_ms: int) -> None:
        if self._refresh_timer.interval() == interval_ms:
            return
        self._sync_log("refresh.cadence", interval_ms=interval_ms)
        self._refresh_timer.setInterval(interval_ms)
        if self._active and not self._refresh_timer.isActive():
            self._refresh_timer.start()

    # ── Sleep / wake ──────────────────────────────────────────────────────────

    def _on_system_resumed(self, gap_seconds: float) -> None:
        """The machine slept for `gap_seconds`; everything on screen is that old.

        The runtime has already asked the network service to probe. If the
        backend is reachable this refresh runs now; if the probe is still
        deciding, the refresh is skipped here and the network service's own
        recovery edge triggers it instead. Either way nothing waits out a
        timer that was paused with the machine.
        """
        if not self._active:
            return
        self._sync_log("resume", gap_seconds=int(gap_seconds))
        self.refresh_data()

    # ── Diagnostics ───────────────────────────────────────────────────────────

    @staticmethod
    def _sync_log(event: str, **fields: Any) -> None:
        """One `sync event=... k=v` line per synchronisation outcome.

        The same shape as the timer's `timing event=` lines, so a support
        engineer can grep one log for the whole story of a session: cache
        painted, refresh started, what the server sent, what was discarded
        and why, what the probe saw. Never a token, never a payload.
        """
        log.info("sync %s", " ".join(
            f"{key}={value}" for key, value in [("event", event), *fields.items()]
            if value is not None
        ))

    # ── Timer ─────────────────────────────────────────────────────────────────

    def _on_timer_state_changed(self, active: bool) -> None:
        self._sidebar.set_timer_active(active)
        self._update_stat_cards()
        if active:
            session = self.api.active_session() or {}
            self._remember_tracked_task(session)
            self._sidebar.set_active_timer_project(session.get("project_id"))
            self._status_bar.set_timer_info(f"{icons.img_tag('circle_filled', SUCCESS, 10)} Timer running")
            self._status_bar.set_message("Tracking time…")
        else:
            self._sidebar.set_active_timer_project(None)
            self._status_bar.set_timer_info("")
            self._status_bar.set_message("Timer stopped.")
            # The local clock has stopped and the session's seconds are
            # already folded into the cached day, so the totals on screen are
            # right now. The day is deliberately *not* re-read from the
            # backend here: the stop request is still in flight at this
            # moment, and a read that overtook it came back with the entry
            # still running and `total_seconds` 0, overwrote the fold, and
            # dropped the day's total by the whole session until the next
            # refresh -- "the time is wrong after Stop until I refresh". The
            # re-read happens on `timer_finalized`, once the backend has the
            # stop.
        self._render_timer_controls()

    # ── The circular Play / Pause ─────────────────────────────────────────────

    def _play_target(self) -> Optional[Dict[str, Any]]:
        """The task Play would start: the selected one, else the last tracked."""
        return self._selected_task or self._last_tracked_task

    def _remember_tracked_task(self, session: Dict[str, Any]) -> None:
        if not session or session.get("task_id") is None:
            return
        self._last_tracked_task = {
            "project_id": session.get("project_id"),
            "task_id": session.get("task_id"),
            "task_name": session.get("task_name"),
        }

    def _on_task_selected(self, task: Optional[Dict[str, Any]]) -> None:
        self._selected_task = dict(task) if task else None
        self._render_timer_controls()

    def _render_timer_controls(self) -> None:
        """Push the timer's state to the two controls that render it.

        The sidebar's circular Play / Pause and the ACTIVE TASK card's Break
        In / Break Out are readouts of TimerService: running or not, on break
        or not, and whether the day on screen is today. Both are re-rendered
        from those facts here, and only here, so they cannot disagree with
        each other or with the task rows.
        """
        running = self.api.is_timer_running()
        status = self.api.break_status()
        live = is_live_date(self._current_date)
        self._sidebar.set_break_status(status)
        self._sidebar.set_live_date(live)
        self._sidebar.set_play_available(self._play_target() is not None)
        self._stat_cards.set_break_control(status, running)

    def _on_play_requested(self) -> None:
        """Play: start the target task through the task section's own Start.

        Every guard the task rows apply is applied here as well, and then
        again in the service: only today, never during a break (Break Out is
        the one control that resumes the held task, and a start here would
        end the break and lose it), and never a second session when one is
        running. With no task to start, nothing is invented -- the control
        is disabled and says so, and a stray click does nothing.
        """
        self._render_timer_controls()
        if not is_live_date(self._current_date):
            self._status_bar.set_message("Go back to today to start the timer.", WARNING)
            return
        if self.api.break_status() != BreakStatus.NONE:
            self._status_bar.set_message("On break. Use Break Out to resume your task.", WARNING)
            return
        if self.api.is_timer_running():
            return
        target = self._play_target()
        if not target:
            self._status_bar.set_message("Select a task to start tracking.", WARNING)
            return
        self._task_section.start_task(
            target["project_id"], target["task_id"], target.get("task_name")
        )
        # The task may be in a project the user browsed away from (Pause,
        # browse, Play). Bring it on screen, the way Break Out and a timer
        # found running at login do; selecting a project is a read of its
        # tasks and touches nothing about the timer.
        if self.api.is_timer_running():
            self._show_project_of(target.get("project_id"))
        self._render_timer_controls()

    def _on_pause_requested(self) -> None:
        """Pause: stop the running task through the task section's own Stop."""
        self._render_timer_controls()
        if not is_live_date(self._current_date):
            self._status_bar.set_message("Go back to today to stop the timer.", WARNING)
            return
        if not self.api.is_timer_running():
            return
        self._task_section.stop_running_task()
        self._render_timer_controls()

    def _show_project_of(self, project_id: Optional[int]) -> None:
        if project_id is None:
            return
        if self._current_project and self._current_project.get("id") == project_id:
            return
        for project in self._projects:
            if project.get("id") == project_id:
                self._on_project_selected(project)
                break

    # ── Break In / Break Out ─────────────────────────────────────────────────

    def _on_break_in_requested(self) -> None:
        """Break In: the ordinary stop, with the running task held for later.

        `for_date` carries the day on screen, as the task rows' Stop does,
        so a break cannot be taken from a browsed date any more than a stop
        can. Whatever the service decided, the controls are re-rendered from
        its state: a refused Break In (nothing running, another day on
        screen) must not leave the button disabled.
        """
        self.api.break_in(for_date=self._current_date)
        self._render_timer_controls()

    def _on_break_out_requested(self) -> None:
        """Break Out: resume the held task -- never the selected one."""
        self.api.break_out(for_date=self._current_date)
        self._render_timer_controls()

    def _on_break_state_changed(self, status: str) -> None:
        previous, self._break_status = self._break_status, status
        self._render_timer_controls()
        self._update_stat_cards()
        if status == BreakStatus.ON_BREAK:
            held = self.api.pre_break_task() or {}
            name = held.get("task_name") or "your task"
            self._status_bar.set_message(f"On break. Break Out resumes '{name}'.")
            # The break has actually begun -- the service reports the state,
            # it does not predict it -- so the message is honest here.
            self._action_banner.show_message(BREAK_STARTED_MESSAGE, "warning")
            return
        if status != BreakStatus.NONE or previous != BreakStatus.RESUMING:
            return
        # Break Out committed. The row now counting may be in a project the
        # user browsed away from during the break; bring that project on
        # screen so the resumed task is visible, the way a timer found
        # running at login is. Selecting a project is a read of its tasks
        # and touches nothing about the timer.
        session = self.api.active_session()
        if not session:
            # RESUMING -> NONE with nothing running: the held task could not
            # be resumed. The service has said why on `timer_error`, and the
            # task section has reported it; there is no "resuming" to announce.
            return
        self._action_banner.show_message(BREAK_ENDED_MESSAGE, "success")
        self._show_project_of(session.get("project_id"))

    def _on_timer_finalized(self, payload: dict) -> None:
        """The backend has committed the stop: re-read the day from it.

        `payload["entry"]` is the finalized record (None when the entry was
        stopped through another path, such as the idle popup). The list the
        backend returns now carries the same `total_seconds` the reports show,
        so the sidebar, the task rows and the summary cards converge on the
        canonical figure without anyone pressing refresh.
        """
        entry = payload.get("entry") or {}
        if entry:
            log.info(
                "stop finalized on backend: entry %s total_seconds=%s",
                entry.get("id"), entry.get("total_seconds"),
            )
        self._load_today_time()
        if self._current_project:
            self._load_tasks(self._current_project.get("id"))

    def _on_timer_conflict(self, active_entry) -> None:
        """The backend refused a start because another entry is running.

        The backend is authoritative: whatever it is tracking is what this
        client shows. When the refusal named the entry it is adopted directly;
        otherwise the backend is asked. Either way the user is told, because
        the row they clicked is not the one now counting.
        """
        self._status_bar.set_message("A timer is already running. Syncing state…", SUCCESS)
        self.api.notify(
            "A timer was already running for your account. Showing that timer.",
            NotificationLevel.WARNING, key="timer-conflict",
        )
        if isinstance(active_entry, dict) and active_entry.get("id"):
            self._on_active_timer_checked(active_entry)
        else:
            self._check_active_timer()

    def _on_timer_tick(self, elapsed: int) -> None:
        """
        Keep the sidebar total live without re-querying anything.

        Only while today is the date on screen. The timer keeps running
        regardless of which date is being viewed, but `_today_time_entries`
        reflects whatever date was last loaded -- if the user has navigated
        to view a past date, folding today's live `elapsed` seconds into that
        day's completed-hours total would silently mix the two. A past date
        must show completed hours only; _apply_time_entries() already set
        that value when the date changed, so this tick is simply skipped.
        """
        if not is_live_date(self._current_date):
            return
        self._sidebar.set_total_seconds(self._banked_today() + elapsed)
        self._sidebar.set_project_totals(self._project_totals_for_display(elapsed))
        self._update_stat_cards()

    def _on_timer_recovered(self, session: dict) -> None:
        elapsed = self.api.timer_elapsed_seconds()
        log.info("timer recovered in UI: task %s, %ds", session.get("task_id"), elapsed)
        self._sidebar.set_timer_active(True)
        self._sidebar.set_active_timer_project(session.get("project_id"))
        self._remember_tracked_task(session)
        self._render_timer_controls()
        self._status_bar.set_message("Recovered a timer that was still running.", SUCCESS)
        # Say how long Monitra was not running, when that is known. Whether
        # that gap counts is not decided here: when it reaches the user's
        # idle threshold the idle service reports it and the ordinary idle
        # popup asks, exactly as for any other stretch of inactivity.
        gap = self._interruption_gap_text(session)
        message = "Recovered a timer that was still running from your last session."
        if gap:
            message = (
                f"Recovered a timer that was still running. Monitra was not "
                f"running for {gap}."
            )
        self.api.notify(message, NotificationLevel.INFO, key="timer-recovered")

    @staticmethod
    def _interruption_gap_text(session: dict) -> Optional[str]:
        interrupted = parse_utc(session.get("interrupted_at_utc"))
        recovered = parse_utc(session.get("recovered_at_utc"))
        if interrupted is None or recovered is None:
            return None
        seconds = int((recovered - interrupted).total_seconds())
        if seconds < 60:
            return None
        return format_hms(seconds)

    def note_exit_in_progress(self, message: str) -> None:
        """The window is closing and the runtime is stopping the timer first.

        Presentation only: the exit itself is owned by the main window and
        the runtime. The status bar is the one place on screen that can say
        why the window is still up after Quit was pressed.
        """
        self._status_bar.set_message(message, SUCCESS)

    def _check_active_timer(self) -> None:
        """Ask the backend whether it believes *this user's* timer is running.

        The user_id filter is not optional. Unscoped, an admin's client asked
        "is any timer running in this organisation" and adopted whatever came
        back -- observed in the log as the admin's desktop trying to stop
        another user's entry and being told "Active timer not found".
        """
        api_client = self.api_client
        user_id = self._user_id
        # When this question was asked. A session bound after this instant
        # is newer than the answer, and an answer of "nothing running" must
        # not end it.
        requested_at = datetime.now(timezone.utc)

        def call():
            # `/time-entries/active` is scoped to the caller by the backend
            # and carries `server_time`, which is what lets the timer count a
            # server-recorded start on this machine's clock. An older
            # deployment without the route answers 404; fall back to the
            # filtered list it does have.
            try:
                response = api_client.get("/time-entries/active")
                data = response.json()
                if isinstance(data, dict) and "entry" in data:
                    entry = data.get("entry")
                    if isinstance(entry, dict) and entry.get("server_time") is None:
                        entry["server_time"] = data.get("server_time")
                    return entry
            except ApiHttpError as exc:
                if exc.status_code != 404:
                    raise
            params = {"status": "running", "limit": 1}
            if user_id is not None:
                params["user_id"] = user_id
            response = api_client.get("/time-entries", params=params)
            entries = response.json()
            if isinstance(entries, list):
                return next(
                    (e for e in entries
                     if e.get("end_time") is None
                     and (user_id is None or e.get("user_id") == user_id)),
                    None,
                )
            return None

        self.api.run_in_background(
            call,
            on_success=lambda entry: self._on_active_timer_checked(entry, requested_at),
            on_error=lambda exc: log.info("could not check for an active timer: %s", exc),
            key="check-active-timer",
        )

    def _on_active_timer_checked(
        self, active_entry: Optional[dict], requested_at: Optional[datetime] = None
    ) -> None:
        if not active_entry or "id" not in active_entry:
            # The backend says nothing is running. For a session this client
            # is counting against an entry the backend has already finalized
            # -- stopped from the web, on another machine, or by an
            # administrator -- that is the end of the session here too; the
            # timer service decides, and keeps any session the backend could
            # not know about yet (an unsent or queued start).
            if requested_at is not None:
                self.api.timer.reconcile_absent_remote(requested_at)
            return
        # The backend calls an entry `running` until our stop reaches it. While
        # that stop is still in the durable queue, adopting the entry would put
        # a timer the user has already stopped back on screen, counting from
        # its original start.
        if self._has_queued_stop(active_entry.get("id"), active_entry.get("client_op")):
            log.info(
                "ignoring backend-running entry %s: its stop is still queued here",
                active_entry.get("id"),
            )
            return
        self._pending_active_timer = active_entry
        self._apply_active_timer_if_ready()

    def _has_queued_stop(self, entry_id, client_op=None) -> bool:
        """Whether the user has already stopped this entry and the stop is
        still on its way. Matched on the entry id, and on the session key the
        entry carries -- a stop queued before the backend issued the id has
        only the key."""
        cache = getattr(self.api, "cache", None)
        if cache is None:
            return False
        try:
            if entry_id is not None and cache.has_pending_stop_for_entry(entry_id):
                return True
            return bool(client_op) and cache.has_pending_stop_for_client_op(client_op)
        except Exception:  # noqa: BLE001
            # A cache that cannot answer must not block the reconciliation it
            # is only advising.
            log.exception("could not check for a queued stop of entry %s", entry_id)
            return False

    def _apply_active_timer_if_ready(self) -> None:
        """
        Adopt a backend-reported running entry once projects are available.

        The timer service re-anchors to the server's `start_time`, so the
        displayed elapsed value matches the record that will be billed.
        """
        entry = self._pending_active_timer
        if not entry or not self._projects:
            return
        self._pending_active_timer = None

        self.api.timer.adopt_remote_session(entry)

        project_id = entry.get("project_id")
        task_id = entry.get("task_id")
        self._sidebar.set_timer_active(True)
        self._sidebar.set_active_timer_project(project_id)
        self._remember_tracked_task(self.api.active_session() or {})
        self._render_timer_controls()

        if self._current_project and self._current_project.get("id") == project_id:
            self._task_section.sync_active_timer(
                task_id, entry.get("id"), self.api.timer_elapsed_seconds()
            )
        else:
            for project in self._projects:
                if project.get("id") == project_id:
                    self._on_project_selected(project)
                    break

        self._load_today_time()

    def _reconcile_active_timer(self) -> None:
        """Handle a 409 conflict by re-reading the server's view of the truth."""
        self._status_bar.set_message("A timer is already running. Syncing state…", SUCCESS)
        self._check_active_timer()

    # ── Sync ──────────────────────────────────────────────────────────────────

    def _on_pending_count_changed(self, pending: int) -> None:
        self._topbar.set_sync_status(pending)
        if pending > 0:
            self._had_pending_sync = True

    def _on_queue_drained(self) -> None:
        """
        Everything queued has now synced.

        This fires once, on the transition into an empty queue — not on every
        poll of one. That distinction is what removed the two-threads-per-second
        storm; see the module docstring.
        """
        if not self._had_pending_sync:
            return
        self._had_pending_sync = False
        self.api.notify(
            "Pending activity synced successfully.",
            NotificationLevel.SUCCESS, key="sync-drained",
        )
        # Re-read what the server now holds, once.
        self._load_today_time()
        if self._current_project:
            self._load_tasks(self._current_project.get("id"))

    # ── Network ───────────────────────────────────────────────────────────────

    def _on_network_state_changed(self, state: str) -> None:
        usable = state in NetworkState.USABLE
        recovered = usable and self._was_online is False
        self._topbar.set_network_state(state)

        if usable:
            self._status_bar.set_message("Online", SUCCESS)
            # Only announce a *recovery*. Announcing the first observation
            # would tell the user they are "back online" before they had ever
            # been seen offline.
            if recovered:
                self.api.notify(
                    "Back online. Syncing pending activity.",
                    NotificationLevel.SUCCESS, key="network-online",
                )
            if not self._projects:
                self.load_projects()
            self.refresh_data()
        elif state == NetworkState.NO_NETWORK:
            self._status_bar.set_message("No network — showing cached data.", WARNING)
            self.api.notify(
                "You are offline. Your activity is saved locally and will sync automatically.",
                NotificationLevel.WARNING, key="network-offline",
            )
        else:
            # The machine has a network; the backend is the problem. Say so,
            # rather than telling the user their internet is down.
            self._status_bar.set_message("Server unreachable — showing cached data.", WARNING)
            self.api.notify(
                "The Monitra server is unreachable. Your activity is saved locally.",
                NotificationLevel.WARNING, key="network-backend-down",
            )

        self._was_online = usable

    # ── Misc ──────────────────────────────────────────────────────────────────

    def _on_task_action_succeeded(self, message: str) -> None:
        self._status_bar.set_message(message, SUCCESS)
        self.api.notify(message, NotificationLevel.SUCCESS, key=f"task-action:{message}")

    def _on_error(self, message: str) -> None:
        self._status_bar.set_message(f"Error: {message}", ERROR)
        if "session expired" in message.lower():
            self.unauthorized_error.emit()

    # ── Compatibility accessors ───────────────────────────────────────────────

    @property
    def is_timer_running(self) -> bool:
        return self.api.is_timer_running()

    def get_running_entry_id(self) -> Optional[int]:
        session = self.api.active_session()
        return session.get("entry_id") if session else None

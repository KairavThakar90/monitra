"""
Monitra Desktop — application entry point.

Startup and shutdown are deliberately ordered here; see ARCHITECTURE.md for
the full contract and core/runtime.py for why each step sits where it does.

Startup:
    QApplication
      -> ApplicationRuntime (storage, domain services, service container)
      -> inspect previous run
      -> restore lightweight session state (local only, no network)
      -> create main window and render the shell
      -> show window                      <- the UI is usable from here
      -> mark UI ready
      -> start background services        <- first thread starts, after the
                                             event loop exists
      -> reconcile with the backend asynchronously

Shutdown:
    quit requested
      -> record clean-shutdown intent
      -> stop accepting new work, cancel in flight
      -> stop services (producers, then consumers, then monitors)
      -> close HTTP client and storage, only once all threads have stopped
      -> exit
"""
from __future__ import annotations

import os
import sys
import time
from typing import Optional

if __name__ == "__main__":
    # From source on Windows, run through a copy of the interpreter stamped with the name
    # notifications should carry (otherwise Windows heads each one "Python"). Before the
    # heavy imports, so nothing is loaded twice; a no-op in an installed build.
    from pathlib import Path

    from core import dev_identity

    _relaunched = dev_identity.relaunch_if_needed(Path(__file__).resolve())
    if _relaunched is not None:
        sys.exit(_relaunched)

from PySide6.QtCore import QByteArray, QRect, QSettings, Qt, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication, QMainWindow, QMessageBox, QStackedWidget

from app.api.exceptions import (
    ApiError,
    ApiHttpError,
    SessionExpiredError,
    SESSION_EXPIRED_MESSAGE,
    LOGIN_DISABLED_CODE, LOGIN_DISABLED_MESSAGE,
)
from app.config import settings
from background_services.public_api import (
    BackgroundApi, NotificationLevel, create_app_icon, set_windows_app_identity,
)
from core.logging_setup import configure_logging, get_logger
from core.paths import data_dir, is_frozen, is_portable, logs_dir
from core.runtime import ApplicationRuntime
from core import single_instance
from ui.dashboard_window import DashboardWindow
from ui.login_window import LoginWindow
from ui.maintenance_toast import MaintenanceToast
from ui.mandatory_update_screen import MandatoryUpdateScreen
from ui.styles import APP_QSS
from version import APP_DISPLAY_NAME, APP_NAME, ORG_NAME, VERSION, display_version

log = get_logger("main")

#: Hard ceiling on how long the shell may wait for startup work before it
#: presents a usable, recoverable state anyway. A loader must never spin
#: forever; this is a backstop, not a substitute for fixing the cause.
STARTUP_BUDGET_MS = 8000

#: Where the window's own preferences live. One spelling, used by both the
#: remembered close choice and the remembered geometry -- two different
#: QSettings scopes would put them in two different places on disk.
SETTINGS_ORGANISATION = "Monitra"
SETTINGS_APPLICATION = "SMSDesktop"
SETTINGS_GEOMETRY_KEY = "window/geometry"

#: The size the window opens at the first time it is ever run, and the size
#: below which its layouts start to be squeezed. Both are *intentions*: they
#: are clamped to whatever the screen can actually show, because a window
#: larger than the desktop cannot be resized back by the user -- its edges are
#: off-screen. `resize(1280, 800)` did exactly that on a 1366x768 laptop, where
#: the work area is about 728px tall, so the status bar was never visible.
DEFAULT_WINDOW_WIDTH = 1280
DEFAULT_WINDOW_HEIGHT = 800
#: How long the OS's "may the session end?" question is remembered. Windows can
#: wait for a person to choose "shut down anyway", so this is generous; it only has
#: to be finite, so a question that was refused is not still true an hour later.
OS_SESSION_QUESTION_MAX_AGE_SECONDS = 15 * 60
#: The window the OS closes for a session ending is closed inside the very message that
#: asked the question (Qt's default commitData closes every visible window), so a close
#: this long after the question is the user's own Quit, not the OS.
OS_SESSION_CLOSE_MAX_AGE_SECONDS = 30

MINIMUM_WINDOW_WIDTH = 1024
MINIMUM_WINDOW_HEIGHT = 680


def _is_macos() -> bool:
    """A function rather than an inline comparison so a test can stand in for it."""
    return sys.platform == "darwin"


def window_settings() -> QSettings:
    """The window's persisted preferences."""
    return QSettings(SETTINGS_ORGANISATION, SETTINGS_APPLICATION)


def available_desktop_rect() -> Optional[QRect]:
    """The work area of the primary screen, excluding the taskbar.

    `None` when Qt reports no screen at all, which happens on a headless host;
    every caller treats that as "do not clamp" rather than as a size of zero.
    """
    screen = QApplication.primaryScreen()
    return screen.availableGeometry() if screen is not None else None


def geometry_is_on_a_screen(rect: QRect) -> bool:
    """Whether a remembered rectangle still lands on a connected display.

    A window restored onto a monitor that has since been unplugged is a window
    the user cannot see and cannot drag back. The test is an intersection
    rather than containment, so a window deliberately left half off the edge
    is still honoured.
    """
    return any(
        screen.availableGeometry().intersects(rect)
        for screen in QApplication.screens()
    )


class MainWindow(QMainWindow):
    """
    Root window. Owns the Login <-> Dashboard swap and the window lifecycle.

    It owns no threads and no services. Everything long-lived belongs to the
    ApplicationRuntime, so a window being closed, hidden or rebuilt cannot
    disturb background processing.
    """

    def __init__(self, runtime: ApplicationRuntime) -> None:
        super().__init__()
        self.runtime = runtime
        self.api = BackgroundApi(runtime)
        self._force_quit = False
        #: Set once an exit is under way, whichever path started it. From
        #: then on a close is neither questioned nor repeated.
        self._exiting = False
        #: Set when the runtime has finished preparing the exit. Qt 6's
        #: `QApplication.quit()` first asks every top-level window to close
        #: and abandons the quit if one ignores the close, so the window
        #: ignores closes while the exit is being prepared and accepts the
        #: one that `quit()` itself delivers.
        self._exit_ready = False
        #: The exit is a restart (an update being installed), not a quit: the
        #: running session is meant to survive it, as after any interruption.
        self._exit_is_restart = False
        #: The OS has *asked* whether the session may end (shutdown, restart,
        #: sign-out; `WM_QUERYENDSESSION`) and when. A question, not the event:
        #: it may be refused. Only an exit that follows it -- `aboutToQuit` from
        #: `WM_ENDSESSION` -- is the shutdown (see `on_about_to_quit`).
        self._os_session_ending = False
        self._os_session_asked_at: Optional[float] = None
        #: The close-intent dialog is on screen. A second close that arrives
        #: while it is -- Cmd+Q or Cmd+W on macOS, where the app menu stays
        #: live under an application-modal dialog -- must not stack a second
        #: dialog on the first.
        self._close_prompt_open = False
        self._startup_guard: Optional[QTimer] = None

        # display_version(), not VERSION: an internal test build says so in
        # its title, so a support report names the build it actually came from.
        self.setWindowTitle(f"{APP_DISPLAY_NAME} {display_version()}")
        self._apply_window_sizing()

        self._build_ui()
        self._wire_runtime()

    # ── Geometry ──────────────────────────────────────────────────────────────

    def _apply_window_sizing(self) -> None:
        """Size the window to fit this machine, and put it back where it was.

        Three things, in order, because each depends on the one before it:

        1. The minimum size is clamped to the work area. A minimum taller than
           the desktop is a window that can never be made to fit -- on a
           1366x768 laptop scaled to 125%, the logical work area is about
           1092x578, which is shorter than the 680px minimum this window
           declares. Qt honours the minimum, so the bottom of the window sat
           under the taskbar with no way to recover it.
        2. A remembered geometry is restored, but only if it still lands on a
           screen that is currently connected.
        3. Otherwise the window opens at its default size, clamped to the work
           area and centred on it.
        """
        available = available_desktop_rect()

        minimum_width, minimum_height = MINIMUM_WINDOW_WIDTH, MINIMUM_WINDOW_HEIGHT
        if available is not None:
            minimum_width = min(minimum_width, available.width())
            minimum_height = min(minimum_height, available.height())
        self.setMinimumSize(minimum_width, minimum_height)

        if self._restore_geometry():
            return

        width, height = DEFAULT_WINDOW_WIDTH, DEFAULT_WINDOW_HEIGHT
        if available is not None:
            width = min(width, available.width())
            height = min(height, available.height())
        self.resize(width, height)
        if available is not None:
            self.move(available.center() - self.rect().center())

    def _restore_geometry(self) -> bool:
        """Reapply the geometry from the last run. False if there is none to
        reapply, or if it would put the window somewhere unreachable."""
        stored = window_settings().value(SETTINGS_GEOMETRY_KEY)
        if not isinstance(stored, QByteArray) or stored.isEmpty():
            return False
        if not self.restoreGeometry(stored):
            log.debug("stored window geometry could not be applied; using the default")
            return False
        if not geometry_is_on_a_screen(self.frameGeometry()):
            log.info("stored window geometry is off every connected screen; recentring")
            return False
        return True

    def _remember_geometry(self) -> None:
        """Persist where and how big the window is.

        Skipped while minimised or hidden to the tray: those states report a
        geometry that is not what the user arranged, and saving it would mean
        the window came back somewhere they never put it. `saveGeometry`
        already records the maximised state and the restored size together, so
        a maximised window reopens maximised and un-maximises to the right
        size.
        """
        if self.isMinimized() or not self.isVisible():
            return
        window_settings().setValue(SETTINGS_GEOMETRY_KEY, self.saveGeometry())

    # ── Construction ──────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        self._stack = QStackedWidget(self)
        self.setCentralWidget(self._stack)

        self._login = LoginWindow(self.runtime.auth_service, self.api, self)
        self._login.login_success.connect(self._on_login_success)

        self._dashboard = DashboardWindow(
            runtime=self.runtime,
            session_manager=self.runtime.session_manager,
            project_service=self.runtime.project_service,
            task_service=self.runtime.task_service,
            time_entry_service=self.runtime.time_entry_service,
            api_client=self.runtime.api_client,
            parent=self,
        )
        self._dashboard.logout_requested.connect(self._on_logout)
        self._dashboard.unauthorized_error.connect(self._on_session_expired)
        # An update installer is running and is waiting for this process to
        # exit. It takes the controlled shutdown path -- services stopped in
        # reverse order, the cache flushed and the database closed -- but as
        # a *restart*, not a quit: the running session is left in its durable
        # record for the relaunched process to recover, exactly as after any
        # other interruption (docs/TIMING_MODEL.md §7).
        self._dashboard.quit_requested.connect(self.exit_for_restart)

        # The page shown instead of the dashboard once a mandatory update's
        # grace period has run out. A page in this stack, like the login
        # page, so that nothing behind it is reachable while it is current:
        # the dashboard, its shortcuts and its menus are simply not on screen.
        # It renders what the update service decided and calls BackgroundApi;
        # the download, verification and install stay the service's.
        self._update_screen = MandatoryUpdateScreen(self)
        self._update_screen.update_requested.connect(self.api.start_update)
        self._update_screen.manual_download_requested.connect(self._open_manual_update_download)

        self._stack.addWidget(self._login)
        self._stack.addWidget(self._dashboard)
        self._stack.addWidget(self._update_screen)
        self._stack.setCurrentWidget(self._login)

        # The maintenance notice. A child of the window rather than of the
        # dashboard, drawn over the stacked pages: it is a statement about
        # Monitra as a whole, it must never block anything underneath it, and
        # it has no state of its own -- shown on the service's "on" edge,
        # hidden on its "off" edge, and repositioned when the window resizes.
        self._maintenance_toast = MaintenanceToast(self)

    def _wire_runtime(self) -> None:
        notifications = self.runtime.notifications
        notifications.restore_requested.connect(self.restore_window)
        notifications.quit_requested.connect(self.quit_application)
        self.runtime.sync.auth_required.connect(self._on_session_expired)
        # Edge-triggered inside the service (on/off only). Hidden on logout
        # through the same edge: `reset_session()` emits False if it was on.
        self.api.maintenance.maintenance_changed.connect(self._on_maintenance_changed)
        # The update-required page. Edge-triggered inside the service: once
        # when the grace period runs out, once when the requirement is
        # satisfied or withdrawn. The progress, failure and install signals
        # feed the page the same way they feed the dialog; all cross-thread,
        # delivered queued onto this thread.
        self.api.updates.blocked_changed.connect(self._on_update_blocked_changed)
        self.api.updates.requirement_changed.connect(self._on_update_requirement_changed)
        self.api.updates.download_progress.connect(self._update_screen.set_progress)
        self.api.updates.update_failed.connect(self._on_update_failed_on_screen)
        self.api.updates.install_started.connect(self._update_screen.set_installing)

    # ── Startup ───────────────────────────────────────────────────────────────

    def begin_startup(self) -> None:
        """
        Resolve the initial screen.

        Runs after the window is already visible, so nothing here can delay the
        shell appearing. If a persisted session exists the dashboard is shown
        immediately from cache and the token is verified in the background —
        the user never waits on a remote call to reach a usable application.
        """
        token = self.runtime.session_manager.access_token
        if not token:
            self._login.reset()
            if self.runtime.session_manager.last_restore_expired:
                # The 90-day window closed while the app was shut. The stored
                # session is already gone; say so in the user's terms rather
                # than letting them wonder why they were signed out.
                log.info("stored session window had expired; requiring re-authentication")
                self._login.error_label.setText(SESSION_EXPIRED_MESSAGE)
            self._stack.setCurrentWidget(self._login)
            return

        # Arm the client with the restored token BEFORE anything can use it.
        # _verify_session() used to be the only place this happened, and it
        # runs after _enter_dashboard() has already scheduled the dashboard's
        # first data load -- a race in which those requests could go out
        # unauthenticated, come back 401, and leave the screen empty until
        # the next refresh. The assignment is a single attribute write; it
        # belongs ahead of the work that depends on it, not beside it.
        self.runtime.api_client.access_token = token

        user_info = self.runtime.session_manager.user_info
        if user_info:
            # Cache-first: render the dashboard now, reconcile afterwards.
            log.info("restoring session from cache; verifying in background")
            self._enter_dashboard(user_info, announce=False)
        else:
            self._login.show_checking_session()

        self._start_startup_guard()
        self._verify_session()

    def _start_startup_guard(self) -> None:
        """Guarantee the login screen reaches a terminal state."""
        self._startup_guard = QTimer(self)
        self._startup_guard.setSingleShot(True)
        self._startup_guard.timeout.connect(self._on_startup_timeout)
        self._startup_guard.start(STARTUP_BUDGET_MS)

    def _cancel_startup_guard(self) -> None:
        if self._startup_guard is not None:
            self._startup_guard.stop()
            self._startup_guard = None

    def _on_startup_timeout(self) -> None:
        """
        Startup verification exceeded its budget.

        The blocking component is named in the log rather than hidden, and the
        user is left with a usable screen instead of a spinner.
        """
        self._startup_guard = None
        if self._stack.currentWidget() is self._dashboard:
            return  # already usable
        log.error(
            "session verification exceeded %dms; runtime health: %s",
            STARTUP_BUDGET_MS, self.runtime.health_report(),
        )
        user_info = self.runtime.session_manager.user_info
        if user_info:
            self._enter_dashboard(user_info, announce=False)
            self.api.notify(
                "Working offline — could not reach the server.",
                NotificationLevel.WARNING, key="startup-offline",
            )
        else:
            self._login.reset()
            self._login.error_label.setText(
                "Could not reach the server. Please check your connection and try again."
            )
            self._stack.setCurrentWidget(self._login)

    def _verify_session(self) -> None:
        """Verify the restored token against the backend, off the GUI thread."""
        token = self.runtime.session_manager.access_token
        api_client = self.runtime.api_client
        api_client.access_token = token

        def call():
            return api_client.get("/auth/me").json()

        self.api.run_in_background(
            call,
            on_success=self._on_verify_success,
            on_error=self._on_verify_error,
            key="verify-session",
        )

    def _on_verify_success(self, user_data: dict) -> None:
        self._cancel_startup_guard()
        # As in _on_login_success: the backend just answered an authenticated
        # request, so the dashboard's first load must not wait on a probe.
        self.runtime.network.note_backend_reachable()
        self.runtime.session_manager.start_session(
            self.runtime.session_manager.access_token, user_data
        )
        if self._stack.currentWidget() is not self._dashboard:
            self._enter_dashboard(user_data, announce=False)
        else:
            self._dashboard.on_session_verified(user_data)

    def _on_verify_error(self, exc: BaseException) -> None:
        self._cancel_startup_guard()

        # A 401 reaching here has already survived a silent refresh attempt
        # inside ApiClient, so it is a real rejection and not merely an expired
        # access token.
        expired = isinstance(exc, SessionExpiredError) or (
            isinstance(exc, ApiHttpError) and exc.status_code in (401, 403)
        ) or (
            isinstance(exc, ApiError) and getattr(exc, "status_code", None) in (401, 403)
        )
        if expired:
            log.info("stored session rejected by the server; requiring re-authentication")
            self._on_session_expired()
            return

        # Anything else is a connectivity problem, not an auth problem. If we
        # have a cached identity, keep working offline rather than logging the
        # user out because the network blipped.
        log.warning("session verification failed (%s); continuing offline if possible", exc)
        user_info = self.runtime.session_manager.user_info
        if user_info:
            if self._stack.currentWidget() is not self._dashboard:
                self._enter_dashboard(user_info, announce=False)
            self.api.notify(
                "Working offline. The authentication server is unreachable.",
                NotificationLevel.WARNING, key="auth-unreachable",
            )
        else:
            self._login.reset()
            self._login.error_label.setText(
                "Network error. Could not connect to the authentication server."
            )
            self._stack.setCurrentWidget(self._login)

    # ── Session transitions ───────────────────────────────────────────────────

    def _enter_dashboard(self, user_data: dict, announce: bool = True) -> None:
        # The dashboard is always signed in -- it owns the timer's rendering,
        # the idle popup and the session's data -- but it is only *shown*
        # while the update policy allows normal use. A restart or a sign-in
        # after the deadline lands on the update-required page instead.
        self._dashboard.on_login(user_data)
        if self.api.update_blocked():
            self._show_update_screen()
        else:
            self._stack.setCurrentWidget(self._dashboard)
        if announce:
            self.api.notify("Logged in successfully", NotificationLevel.SUCCESS, key="login")

    def _on_login_success(self, user_data: dict) -> None:
        self._cancel_startup_guard()
        # The login round trip just succeeded, which is first-hand proof the
        # backend is reachable. Telling NetworkService before the dashboard
        # opens means its first data load is not held back waiting for a
        # health probe to reach the same conclusion.
        self.runtime.network.note_backend_reachable()
        # The signed-in user is named so the runtime can tell whether the local
        # caches belong to them or to whoever used this machine last.
        self.runtime.on_login(user_data.get("id"))
        self._enter_dashboard(user_data)

    def _on_logout(self) -> None:
        """Deliberate logout initiated by the user."""
        log.info("user requested logout")
        self.runtime.on_logout()
        self.runtime.auth_service.logout()
        self._dashboard.reset_state()
        self._login.reset()
        self._stack.setCurrentWidget(self._login)

    def _on_session_expired(self) -> None:
        """The backend rejected our credentials."""
        if self._stack.currentWidget() is self._login:
            return
        excluded = self.runtime.api_client.session_end_reason == LOGIN_DISABLED_CODE
        self.runtime.api_client.session_end_reason = None
        log.info(
            "%s; returning to login",
            "an administrator excluded this account from signing in" if excluded else "session expired",
        )
        # Stops a running timer locally too; the backend already stopped the
        # entry on its own clock when the administrator excluded the account.
        # Involuntary: queued screenshots are kept for the next sign-in.
        self.runtime.on_logout(involuntary=True)
        self.runtime.auth_service.logout()
        self._dashboard.reset_state()
        self._login.reset()
        self._stack.setCurrentWidget(self._login)
        if excluded:
            self._login.show_login_disabled()
            self.api.notify(
                LOGIN_DISABLED_MESSAGE, NotificationLevel.ERROR, key="login-disabled",
            )
            return
        self._login.error_label.setText(SESSION_EXPIRED_MESSAGE)
        self.api.notify(
            SESSION_EXPIRED_MESSAGE,
            NotificationLevel.ERROR, key="session-expired",
        )

    # ── Maintenance notice ────────────────────────────────────────────────────

    def _on_maintenance_changed(self, active: bool) -> None:
        """Show or hide the notice. Nothing else: the timer, the trackers,
        the screenshots and the sync queue are not consulted and not touched."""
        if active:
            self._maintenance_toast.show_notice()
        else:
            self._maintenance_toast.hide_notice()

    # ── Mandatory update: the page instead of the dashboard ───────────────────

    def _signed_in(self) -> bool:
        return self._stack.currentWidget() is not self._login

    def _show_update_screen(self) -> None:
        """Put the update-required page in front of everything.

        The release the page offers is the one the service validated
        (`pending_update_release`); without one -- an answer the updater
        cannot act on -- the page says so and offers the manual link.
        """
        self._update_screen.set_requirement(
            self.api.update_requirement(),
            self.api.pending_update_release(),
            installable=self.api.pending_update_release() is not None,
            manual_url=self.api.update_manual_download_url() is not None,
        )
        self._stack.setCurrentWidget(self._update_screen)
        log.warning("UPDATE_SCREEN shown: the mandatory update's grace period has run out")

    def _on_update_blocked_changed(self, blocked: bool) -> None:
        """The grace period ran out (True) or the requirement is gone (False)."""
        if blocked:
            if self._signed_in():
                self._show_update_screen()
            return
        if self._stack.currentWidget() is self._update_screen:
            log.info("UPDATE_SCREEN released: normal use may resume")
            self._stack.setCurrentWidget(self._dashboard)

    def _on_update_requirement_changed(self, requirement) -> None:
        """Keep the page's words current while it is showing."""
        if self._stack.currentWidget() is self._update_screen and self.api.update_blocked():
            self._show_update_screen()

    def _on_update_failed_on_screen(self, message: str) -> None:
        self._update_screen.show_error(
            message, can_download_manually=self.api.update_manual_download_url() is not None,
        )

    def _open_manual_update_download(self) -> None:
        url = self.api.update_manual_download_url()
        if url:
            from PySide6.QtCore import QUrl
            from PySide6.QtGui import QDesktopServices

            QDesktopServices.openUrl(QUrl(url))

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt override)
        super().resizeEvent(event)
        toast = getattr(self, "_maintenance_toast", None)
        if toast is not None and toast.isVisible():
            toast.reposition()

    # ── Window lifecycle ──────────────────────────────────────────────────────

    def restore_window(self, reason: str = "tray") -> None:
        """Bring the window back from the tray, the taskbar or a second launch.

        `reason` names who asked, for the log: the tray icon, a notification,
        or another launch of Monitra (`on_activation_requested`).
        """
        log.info(
            "WINDOW_RESTORE_REQUESTED reason=%s visible=%s minimised=%s",
            reason, self.isVisible(), self.isMinimized(),
        )
        self.show()
        self.setWindowState(
            (self.windowState() & ~Qt.WindowState.WindowMinimized)
            | Qt.WindowState.WindowActive
        )
        self.raise_()
        self.activateWindow()
        # An idle prompt that is waiting is application-modal: this window
        # would come back unable to take a click, with the one window that can
        # be answered possibly minimised, off-screen or on another virtual
        # desktop. Whoever asked for Monitra is asking for that prompt too.
        self._dashboard.bring_idle_popup_forward(reason)

    def on_activation_requested(self) -> None:
        """Another launch of Monitra asked this instance to show itself.

        The user double-clicked the icon because they could not find the
        window: it is in the tray, minimised, on another virtual desktop, or
        blocked by a popup they cannot see. Every one of those is answered by
        the same restore the tray performs.
        """
        log.info("SINGLE_INSTANCE_ACTIVATION handling: restoring the window")
        self.restore_window("second-launch")

    def quit_application(self) -> None:
        """Explicit quit: stop the timer, then a full controlled shutdown."""
        self._force_quit = True
        self.close()

    def on_about_to_quit(self) -> None:
        """The one shutdown path, whatever triggered the exit.

        On Windows `WM_ENDSESSION` (the shutdown really going ahead) emits
        `aboutToQuit` directly and the process is killed moments later; a
        vetoed shutdown never gets here. So a quit that arrives with the OS
        having asked to end the session *is* the machine shutting down: the
        running timer is stopped -- queued durably, then the record cleared,
        exactly as an explicit quit does it -- before anything is torn down,
        so the backend hears of the stop at the instant of shutdown (now if
        the network is up, from the next launch if it is not) instead of
        carrying an entry for a machine that is switched off.

        An update restart is not this: it relaunches at once, and the session
        survives it (`exit_for_restart`).

        Three things keep a *refused* shutdown from being mistaken for one. The
        question is only remembered for `OS_SESSION_QUESTION_MAX_AGE_SECONDS`
        (Windows can wait on a person to choose "shut down anyway", but not
        for ever); an exit that is a restart is never an OS shutdown, whatever
        was asked earlier; and the memory is consumed here, so one question
        answers for one exit. Measured with the real messages sent to a real
        Qt window (`tests/e2e/real_os_session_end_probe.py`): after
        `WM_QUERYENDSESSION` and a vetoing `WM_ENDSESSION` the flag used to
        stay set, and an update restart that followed stopped the timer.
        """
        asked_at, self._os_session_asked_at = self._os_session_asked_at, None
        asked = self._os_session_ending
        self._os_session_ending = False
        fresh = asked_at is not None and time.monotonic() - asked_at <= OS_SESSION_QUESTION_MAX_AGE_SECONDS
        if asked and fresh and not self._exit_is_restart:
            self.runtime.stop_for_os_shutdown()
        elif asked:
            log.info(
                "the OS had asked to end the session, but this exit is %s; not treating it as a shutdown",
                "a restart" if self._exit_is_restart else "long after the question",
            )
        self.runtime.shutdown()

    def exit_for_restart(self) -> None:
        """Exit so an update can be installed; the session survives it."""
        self._exit_is_restart = True
        self._force_quit = True
        self.close()

    def on_os_session_ending(self, manager) -> None:
        """The OS is asking whether it may shut down, restart or sign out.

        Connected to `QGuiApplication.commitDataRequest`, which Qt emits for
        `WM_QUERYENDSESSION`. That is a *question*, not the event: any
        application may answer no, and then the session carries on. So nothing
        is stopped here -- a timer stopped on a shutdown that did not happen
        would silently end somebody's work. This only records that the
        question was asked, so that if Qt goes on to close the window the
        close is not taken for a Quit, and so that `on_about_to_quit` can tell
        the shutdown that really happened from an ordinary exit.

        No dialog can be shown here. Not verifiable headless: Windows
        delivers the message only to a real session, so the real path is
        covered by the manual matrix.
        """
        log.info("the OS is asking to end the session; waiting to see whether it does")
        self._os_session_ending = True
        self._os_session_asked_at = time.monotonic()
        try:
            manager.release()
        except Exception:  # noqa: BLE001
            pass

    def closeEvent(self, event) -> None:
        """
        Distinguish hide-to-tray from an explicit quit.

        Nothing blocking happens here. The audited implementation performed a
        synchronous 3-second network call and a synchronous batch upload inside
        this handler, which is why quitting could appear to hang. Any work still
        outstanding is durable and is completed by the next run.

        An explicit quit -- the dialog's Quit, a remembered Quit, the tray
        menu -- **always stops a running timer** before the process exits.
        The remembered choice decides only whether the dialog is shown; it
        has no say over the timer. The stop is durable the instant it is
        requested, and the window then waits (without blocking) for it to
        reach the backend, bounded by `EXIT_STOP_FLUSH_BUDGET_MS`, before the
        application quits.
        """
        if self._exiting:
            # The close is already being carried out. Until the runtime has
            # called back, a second X or tray Quit joins the wait and the
            # close is ignored so the window stays up. Once it has, the close
            # is the one `QApplication.quit()` sends to every window before
            # it exits the event loop -- and Qt 6 abandons the quit if that
            # close is ignored -- so it is accepted.
            if self._exit_ready:
                event.accept()
            else:
                event.ignore()
            return

        # Recorded before anything hides or closes, while the window still
        # reports the size and position the user arranged. This is a local
        # settings write, not work: the rule this handler exists to honour is
        # that nothing here waits on the network or on a thread.
        self._remember_geometry()

        # Qt emits nothing when a shutdown is refused, so the flag alone cannot tell the
        # OS closing this window from a user who clicked Quit after the OS was told no:
        # trusting it made that Quit a silent no-op that left the timer running.
        asked_at = self._os_session_asked_at
        if (
            self._os_session_ending
            and asked_at is not None
            and time.monotonic() - asked_at <= OS_SESSION_CLOSE_MAX_AGE_SECONDS
        ):
            log.info("window closed by the OS session ending; not treated as a quit")
            event.accept()
            return

        if not self._force_quit:
            if self._close_prompt_open:
                # The question is already on screen; its answer decides what
                # this close does. Neither a second dialog nor a second answer.
                event.ignore()
                return
            choice = self._ask_close_intent()
            if choice == "cancel":
                event.ignore()
                return
            if choice == "minimize":
                self._minimise_to_background()
                event.ignore()
                return

        stop_timer = not self._exit_is_restart
        log.info("explicit %s requested", "restart" if self._exit_is_restart else "quit")
        self._exiting = True
        # The window stays on screen, with the status bar saying why, until
        # the stop is delivered or cannot be; then the application quits and
        # the runtime is torn down from aboutToQuit -- exactly one shutdown
        # path, whatever triggered the exit.
        event.ignore()
        if stop_timer and self.api.is_timer_running():
            self._dashboard.note_exit_in_progress("Stopping your timer before Monitra exits…")
        self.api.request_exit(self._on_exit_ready, stop_timer=stop_timer)

    def _minimise_to_background(self) -> None:
        """Take the window out of the way and keep everything running.

        On macOS the window is *minimised* to the Dock, not hidden. A hidden
        window has no way back except the menu-bar icon: clicking the Dock
        icon does nothing for it (the system only restores minimised windows),
        and Cmd+Q or the Dock's Quit would skip the close prompt, because Qt
        only offers a close to windows that are visible -- ending the process
        with the timer still running, which is the interruption path, not a
        quit. A minimised window is still a visible window to Qt and comes back
        from the Dock the way every Mac application's does. Elsewhere the
        window hides to the tray as it always has.

        Nothing here touches the timer, the trackers or the sync queue: the
        window is not what any of them run on.
        """
        if _is_macos():
            self.showMinimized()
            where = "in the Dock"
        else:
            self.hide()
            where = "in the system tray"
        self.api.notify(
            f"Monitra is still running {where}. "
            "Time tracking continues in the background.",
            NotificationLevel.INFO, key="minimised-to-tray",
        )

    def _on_exit_ready(self) -> None:
        """The runtime has done what it can for the exit; leave now."""
        log.info("exit preparation complete; quitting the application")
        self._exit_ready = True
        QApplication.instance().quit()

    def _ask_close_intent(self) -> str:
        from PySide6.QtWidgets import QDialog  # noqa: F401 - dialog imports Qt widgets

        from ui.quit_confirm_dialog import QuitConfirmDialog

        remembered = window_settings().value("remember_exit_choice", "")
        if remembered in ("minimize", "quit"):
            return remembered

        dialog = QuitConfirmDialog(self)
        self._close_prompt_open = True
        try:
            dialog.exec()
        finally:
            self._close_prompt_open = False
        return dialog.result_action or "cancel"


def _yield_to_running_instance() -> int:
    """This is the second launch: hand the user over to the first.

    The running instance is asked to show its window (and the idle prompt,
    if one is waiting). Only if it does not take the request -- it is hung,
    or still starting -- is the user told to look in the tray. Nothing here
    touches the other process's lock, and nothing is killed: the running
    instance owns the timer, the queue and the database, and a second launch
    is never a reason to end it.
    """
    log.warning("another Monitra instance is already running; asking it to show its window")
    if single_instance.request_activation():
        log.info("SINGLE_INSTANCE_ACTIVATION sent and taken; exiting")
        return 0
    log.warning("SINGLE_INSTANCE_ACTIVATION unanswered after %.0fs; telling the user",
                single_instance.ACTIVATION_TIMEOUT_SECONDS)
    QMessageBox.information(None, APP_NAME, single_instance.already_running_message(unanswered=True))
    return 0


def main() -> int:
    configure_logging()
    # Naming the real directories here (rather than the historical literal
    # "~/.monitra") is what makes a support report from a packaged install
    # actionable: portable builds and MONITRA_DATA_DIR overrides both move
    # them. See core/paths.py.
    log.info(
        "%s %s starting — data=%s logs=%s frozen=%s portable=%s",
        APP_NAME, display_version(), data_dir(), logs_dir(), is_frozen(), is_portable(),
    )

    # Explicit Windows taskbar Application User Model ID for Monitra identity
    set_windows_app_identity()

    # 1. Qt first. No QThread may be created before this exists.
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(ORG_NAME)
    app.setApplicationDisplayName(APP_DISPLAY_NAME)
    app.setStyleSheet(APP_QSS)
    app.setWindowIcon(create_app_icon())

    app.setApplicationVersion(VERSION)

    font = QFont("Segoe UI")
    font.setPointSize(11)
    app.setFont(font)

    # 1b. Configuration. A packaged build on a staff machine has no console,
    #     so a misconfiguration must be shown, not printed. This is the only
    #     condition that stops startup before the runtime is built: without a
    #     usable backend URL there is nothing the application could do.
    log.info("configuration: %s", settings.describe())
    if settings.error:
        log.critical("configuration error: %s", settings.error)
        QMessageBox.critical(
            None,
            f"{APP_NAME} — configuration problem",
            f"{APP_NAME} cannot start because its configuration is invalid.\n\n"
            f"{settings.error}",
        )
        return 2

    # Closing the last window must not end the process: hide-to-tray keeps the
    # application alive deliberately. Quitting is always explicit.
    app.setQuitOnLastWindowClosed(False)

    # 1c. Single instance. A packaged build hides to the tray on close, so a
    #     user who "closed" it and launched it again would otherwise get a
    #     second full runtime: two timers, two sync services draining one
    #     queue, and two writers on one database. See core/single_instance.py.
    if not single_instance.acquire():
        return _yield_to_running_instance()

    # 2. Runtime: storage, domain services, service container. No threads yet.
    runtime = ApplicationRuntime()
    runtime.inspect_previous_run()
    runtime.restore_session()

    # 3. Shell.
    window = MainWindow(runtime)
    # 3b. The other half of the single-instance guard: a later launch asks
    #     this process to show its window instead of being told to look in
    #     the tray. Owned by the window, so it lives exactly as long as the
    #     thing it restores. See core/single_instance.py.
    activation = single_instance.ActivationListener(parent=window)
    activation.activation_requested.connect(window.on_activation_requested)

    # Exactly one shutdown path, whatever triggers the exit. An exit that comes
    # with the OS having asked to end the session stops the timer first.
    app.aboutToQuit.connect(window.on_about_to_quit)
    # The OS asking to shut down or sign out. Only noted: it may be vetoed, and
    # a timer stopped on a shutdown that did not happen would end real work.
    # Direct, as Qt requires for this signal -- the session manager is waiting
    # on the answer.
    app.commitDataRequest.connect(
        window.on_os_session_ending, Qt.ConnectionType.DirectConnection
    )

    window.show()
    runtime.mark_ui_ready()

    # 4. Background services start only once the event loop is running and the
    #    shell is on screen.
    QTimer.singleShot(0, runtime.start_services)
    QTimer.singleShot(0, window.begin_startup)

    # 5. Packaging self-test hook.
    #
    #    MONITRA_SELFTEST_SECONDS makes the application start normally, run
    #    for that many seconds, and then quit through the ordinary
    #    quit_application() path. It exists so CI can verify the *packaged*
    #    binary — the one users get — actually boots its full runtime and
    #    shuts down cleanly, which is the failure mode packaging introduces
    #    (a missing DLL, a hidden import PyInstaller did not follow) and which
    #    no test run from source can catch. The source-level equivalent,
    #    tests/soak/run_launch_cycles.py, drives main() from injected Python
    #    and so cannot be pointed at an .exe.
    #
    #    It is not a debug mode: it is off unless the variable is set, adds no
    #    UI, changes no behaviour while running, and exposes nothing.
    selftest = os.getenv("MONITRA_SELFTEST_SECONDS")
    if selftest:
        try:
            seconds = max(1.0, float(selftest))
        except ValueError:
            log.error("MONITRA_SELFTEST_SECONDS=%r is not a number; ignoring", selftest)
        else:
            log.info("self-test mode: quitting after %.1fs", seconds)
            QTimer.singleShot(int(seconds * 1000), window.quit_application)

    exit_code = app.exec()

    # Safeguard for exits that bypass aboutToQuit. shutdown() is idempotent.
    runtime.shutdown()
    log.info("Monitra desktop exited with code %d", exit_code)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())

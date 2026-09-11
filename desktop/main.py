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
from typing import Optional

from PySide6.QtCore import QSettings, Qt, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication, QMainWindow, QMessageBox, QStackedWidget

from app.api.exceptions import (
    ApiError,
    ApiHttpError,
    SessionExpiredError,
    SESSION_EXPIRED_MESSAGE,
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
from ui.styles import APP_QSS
from version import APP_DISPLAY_NAME, APP_NAME, ORG_NAME, VERSION

log = get_logger("main")

#: Hard ceiling on how long the shell may wait for startup work before it
#: presents a usable, recoverable state anyway. A loader must never spin
#: forever; this is a backstop, not a substitute for fixing the cause.
STARTUP_BUDGET_MS = 8000


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
        self._startup_guard: Optional[QTimer] = None

        self.setWindowTitle(f"{APP_DISPLAY_NAME} {VERSION}")
        self.setMinimumSize(1024, 680)
        self.resize(1280, 800)

        self._build_ui()
        self._wire_runtime()

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
        # exit. It takes the ordinary explicit-quit path deliberately: the
        # installer needs a *clean* shutdown, which is exactly what that path
        # already guarantees — services stopped in reverse order, the cache
        # flushed and the database closed — so tracked time and the sync queue
        # are as safe as on any other quit.
        self._dashboard.quit_requested.connect(self.quit_application)

        self._stack.addWidget(self._login)
        self._stack.addWidget(self._dashboard)
        self._stack.setCurrentWidget(self._login)

    def _wire_runtime(self) -> None:
        notifications = self.runtime.notifications
        notifications.restore_requested.connect(self.restore_window)
        notifications.quit_requested.connect(self.quit_application)
        self.runtime.sync.auth_required.connect(self._on_session_expired)

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
        self._stack.setCurrentWidget(self._dashboard)
        self._dashboard.on_login(user_data)
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
        log.info("session expired; returning to login")
        self.runtime.on_logout()
        self.runtime.auth_service.logout()
        self._dashboard.reset_state()
        self._login.reset()
        self._login.error_label.setText(SESSION_EXPIRED_MESSAGE)
        self._stack.setCurrentWidget(self._login)
        self.api.notify(
            SESSION_EXPIRED_MESSAGE,
            NotificationLevel.ERROR, key="session-expired",
        )

    # ── Window lifecycle ──────────────────────────────────────────────────────

    def restore_window(self) -> None:
        """Bring the window back from the tray or the taskbar."""
        self.show()
        self.setWindowState(
            (self.windowState() & ~Qt.WindowState.WindowMinimized)
            | Qt.WindowState.WindowActive
        )
        self.raise_()
        self.activateWindow()

    def quit_application(self) -> None:
        """Explicit quit: full controlled shutdown."""
        self._force_quit = True
        self.close()

    def closeEvent(self, event) -> None:
        """
        Distinguish hide-to-tray from an explicit quit.

        Nothing blocking happens here. The audited implementation performed a
        synchronous 3-second network call and a synchronous batch upload inside
        this handler, which is why quitting could appear to hang. Any work still
        outstanding is durable and is completed by the next run.
        """
        if not self._force_quit:
            choice = self._ask_close_intent()
            if choice == "cancel":
                event.ignore()
                return
            if choice == "minimize":
                self.hide()
                self.api.notify(
                    "Monitra is still running in the system tray. "
                    "Time tracking continues in the background.",
                    NotificationLevel.INFO, key="minimised-to-tray",
                )
                event.ignore()
                return

        log.info("explicit quit requested")
        event.accept()
        # Let the close finish, then tear the runtime down from aboutToQuit so
        # there is exactly one shutdown path.
        QApplication.instance().quit()

    def _ask_close_intent(self) -> str:
        from PySide6.QtWidgets import QDialog  # noqa: F401 - dialog imports Qt widgets

        from ui.quit_confirm_dialog import QuitConfirmDialog

        settings = QSettings("Monitra", "SMSDesktop")
        remembered = settings.value("remember_exit_choice", "")
        if remembered in ("minimize", "quit"):
            return remembered

        dialog = QuitConfirmDialog(self)
        dialog.exec()
        return dialog.result_action or "cancel"


def main() -> int:
    configure_logging()
    # Naming the real directories here (rather than the historical literal
    # "~/.monitra") is what makes a support report from a packaged install
    # actionable: portable builds and MONITRA_DATA_DIR overrides both move
    # them. See core/paths.py.
    log.info(
        "%s %s starting — data=%s logs=%s frozen=%s portable=%s",
        APP_NAME, VERSION, data_dir(), logs_dir(), is_frozen(), is_portable(),
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
        log.warning("another Monitra instance is already running; exiting")
        QMessageBox.information(None, APP_NAME, single_instance.already_running_message())
        return 0

    # 2. Runtime: storage, domain services, service container. No threads yet.
    runtime = ApplicationRuntime()
    runtime.inspect_previous_run()
    runtime.restore_session()

    # 3. Shell.
    window = MainWindow(runtime)

    # Exactly one shutdown path, whatever triggers the exit.
    app.aboutToQuit.connect(lambda: runtime.shutdown())

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

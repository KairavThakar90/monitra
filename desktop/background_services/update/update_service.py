"""
update_service — finds a newer Monitra release, and applies it on request.

Scope, and what changed
-----------------------
This began as "Phase 0": it announced a release and stopped there. It now also
downloads, verifies and installs one — but only ever when the user asks, and
only ever an artifact whose SHA-256 matches what the backend said. The
announcement half is unchanged, and the download half is strictly additive: a
deployment that answers without a checksum still produces exactly the old
behaviour, an announcement and a link.

Design constraints this service is built to, all of them from DO_NOT_DO.md:

**No new mechanism.** It is a `LoopService` like every other periodic service,
registered with `ApplicationRuntime`, notifying through the `NotificationService`
that already owns notifications and running its download on the shared
`TaskRunner`. No new thread, no new timer, no second notification path, and no
queue of its own.

**Edge-triggered, never level-triggered.** The backend keeps answering "1.1.0 is
available" every time it is asked. Notifying on each answer would be exactly the
level-triggered signal that once produced a notification storm, so the
announcement fires only when the announced version *changes* — once per release,
per session — and the dialog is offered on the same edge.

**The backend decides.** Whether an update exists, and whether it is mandatory,
are the server's answers, not comparisons made here. One rule, in one place.

**It holds instead of failing.** Unauthenticated, offline, or a backend that
does not implement the endpoint (an older deployment, which is a normal state
during a rollout) are all reasons to wait quietly. A failed update check must
never be able to affect tracking, and must never lock anyone out.

**Nothing is installed that was not verified.** The state machine in `state.py`
is what makes "download while already downloading" impossible rather than
unlikely, and `ReleaseInfo.from_payload` is the trust boundary: a payload that
is missing a digest, or carries a URL that is not absolute HTTPS, yields no
installable release at all.

The ten-hour schedule
---------------------
The check runs about every ten hours, and that has to survive a restart —
someone who closes Monitra at 22:30 and opens it at 08:00 has been away longer
than the interval and should be told about a release immediately, while someone
who restarts twice in a minute should not produce two checks. So the time of the
last *successful* check is persisted, and the delay before the first check of a
session is computed from it. A failed check never moves that timestamp, which is
what stops a backend outage from silently postponing the next attempt by ten
hours.
"""
from __future__ import annotations

import random
import sys
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

from PySide6.QtCore import Signal

import version
from app.api.client import MACHINE_ARCH
from app.api.exceptions import ApiError
from app.updates.service import UpdateApiService
from background_services.network import NetworkState
from background_services.notifications import NotificationLevel
from core.service import LoopService, ServiceState

from .downloader import DownloadError, clear_stale_downloads, download_and_verify
from .installer import InstallError, can_install, launch_installer, read_and_clear_result
from .signature import verify_installer
from .state import ReleaseInfo, UpdateState, can_transition

#: Where the announced versions are persisted. One row in `app_state`, which
#: `LocalCache` already owns -- no new table, no second store.
ANNOUNCED_VERSIONS_KEY = "updates.announced_versions"

#: When the last *successful* check happened, as a Unix timestamp. Persisted so
#: the ten-hour schedule survives a restart; see the module docstring.
LAST_CHECK_KEY = "updates.last_check_at"

#: De-duplication key for the download task. The TaskRunner drops a second
#: submission under the same key while the first is in flight, which is one of
#: the two guards against concurrent downloads (the state machine is the other).
DOWNLOAD_TASK_KEY = "update-download"


def _version_tuple(value: Optional[str]) -> Optional[tuple]:
    """Parse `major.minor.patch` for comparison, or None if it is not that.

    Strict, and deliberately so: `version.py` guarantees this exact shape, so
    anything else came from somewhere that cannot be reasoned about, and
    ordering it numerically would be a guess.
    """
    if not value:
        return None
    parts = str(value).strip().split(".")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        return None
    return tuple(int(part) for part in parts)


def newer_than_installed(versions: List[str], installed: str) -> List[str]:
    """The recorded versions that are strictly newer than the running build.

    This is the whole badge rule. Because the answer is recomputed from the
    installed version every time, upgrading empties it without any explicit
    acknowledgement step -- and a version that is no longer newer can never
    linger as a stale "1".
    """
    current = _version_tuple(installed)
    if current is None:
        return []
    newer = [v for v in versions if (_version_tuple(v) or ()) > current]
    return sorted(set(newer), key=lambda v: _version_tuple(v) or ())


def _fold_platform(value: Optional[str]) -> Optional[str]:
    """`win32`/`darwin`, whatever spelling the backend used, or None."""
    token = (value or "").strip().lower()
    if token in ("win32", "win64", "windows"):
        return "win32"
    if token in ("darwin", "macos", "mac", "osx"):
        return "darwin"
    return token or None


def _fold_arch(value: Optional[str]) -> Optional[str]:
    token = (value or "").strip().lower()
    if token in ("amd64", "x86_64", "x64", "em64t"):
        return "x86_64"
    if token in ("arm64", "aarch64"):
        return "arm64"
    return token or None


def _is_https_url(value: Any) -> bool:
    """A plain https URL with a host -- the only kind opened in a browser."""
    if not isinstance(value, str) or not value or any(c in value for c in " \\\t\r\n\x00"):
        return False
    try:
        parts = urlsplit(value)
    except ValueError:
        return False
    return parts.scheme.lower() == "https" and bool(parts.hostname) and parts.username is None


class UpdateService(LoopService):
    """
    Periodically asks the backend whether a newer release exists, and applies
    one when the user chooses to.

    Signals:
        update_available(str, str) — version, download URL ("" if none was
            given). Emitted only on a change of announced version. Kept for the
            notification path that predates the installer.
        update_offered(object, bool) — a `ReleaseInfo` the user can actually
            install, and whether taking it is mandatory. Emitted on the same
            edge as the announcement, and only when the release is installable.
        pending_count_changed(int) — badge count, on change only.
        state_changed(str) — the update state machine moved.
        download_progress(int, int) — bytes received, total (0 when unknown).
        update_failed(str) — a message fit to show the user. The installed
            application is always still usable when this fires.
        install_started(str) — the installer is running and this process must
            now quit so it can proceed.
    """

    name = "updates"

    update_available = Signal(str, str)
    update_offered = Signal(object, bool)
    #: Number of announced versions newer than the installed one. Emitted only
    #: when the number actually changes, so the menu badge is repainted on a
    #: transition rather than on every poll.
    pending_count_changed = Signal(int)
    state_changed = Signal(str)
    download_progress = Signal(int, int)
    update_failed = Signal(str)
    install_started = Signal(str)

    #: Cadence once a check has succeeded. A release is a human-scale event;
    #: polling faster buys nothing and costs a request per client per interval.
    CHECK_INTERVAL_MS = 10 * 60 * 60 * 1000      # 10 hours
    #: Cadence while holding (offline, signed out, or the endpoint is absent).
    HOLD_INTERVAL_MS = 5 * 60 * 1000             # 5 minutes
    #: Cadence after a check failed for any other reason.
    error_interval_ms = 15 * 60 * 1000           # 15 minutes
    #: The first check runs a short delay after start rather than immediately:
    #: at startup the session is still being restored and the first probe has
    #: not run, so an immediate check would almost always hold anyway. It is
    #: also what keeps the check off the startup path entirely — nothing about
    #: tracking, the timer or sync waits on it.
    FIRST_CHECK_DELAY_MS = 30 * 1000

    interval_ms = CHECK_INTERVAL_MS

    #: One request with TIMEOUT_FAST (5s) is the whole blocking budget of a
    #: tick. The download does not run here — it runs on the TaskRunner — so
    #: this stays small and shutdown stays fast.
    stop_timeout_ms = 8000

    def __init__(self, runtime, update_api: UpdateApiService, cache=None, parent=None) -> None:
        super().__init__(runtime, parent)
        self._update_api = update_api
        self._cache = cache if cache is not None else getattr(runtime, "cache", None)
        self._first_check_done = False
        #: The version this session has already *toasted* about. Holding it is
        #: what makes the notification edge-triggered. Separate from the
        #: persisted record below: the toast is per session, the badge is not.
        self._announced_version: Optional[str] = None
        #: Every version the backend has announced to this installation,
        #: including ones already installed since. Pruned only against the
        #: running version, never trimmed on a guess.
        self._announced_versions: List[str] = []
        #: Published badge count. Read from the GUI thread, written from the
        #: service thread -- a plain int assignment, which is why the list
        #: above is never exposed directly.
        self._pending_count = 0
        #: Last successful answer, for the UI and diagnostics.
        self._latest: Optional[Dict[str, Any]] = None
        #: The installable release, when the last answer described one.
        self._release: Optional[ReleaseInfo] = None
        #: Where the updater is. Written from both the service thread and the
        #: GUI thread, always through `_set_update_state`, which is the only
        #: place a transition is decided.
        self._state = UpdateState.IDLE
        #: True while a mandatory update is outstanding. The window reads it to
        #: decide whether the application may be used at all.
        self._force_pending = False
        #: What the previous process's installer helper reported, read once at
        #: start: ``("success"|"failed"|"interrupted"|"not-applied", target)``.
        #: None when no update was in flight. For diagnostics and tests; the
        #: user is told through a notification.
        self.last_install_outcome: Optional[tuple] = None

    # ── Public state ──────────────────────────────────────────────────────────

    @property
    def pending_count(self) -> int:
        """How many announced versions are newer than the installed build.

        0 means "nothing known to be pending", which includes the case where
        no check has succeeded yet. Safe to read from any thread.
        """
        return self._pending_count

    @property
    def update_state(self) -> str:
        """Where the updater is. Named to avoid shadowing `BaseService.state`,
        which is the *service lifecycle* — the exact overload DO_NOT_DO.md
        records as having crashed startup once already."""
        return self._state

    @property
    def is_busy(self) -> bool:
        """True while a check, download, verification or install is running."""
        return self._state in UpdateState.BUSY

    @property
    def force_update_pending(self) -> bool:
        """True when the user must update before carrying on.

        Deliberately **not persisted**. A mandatory update is a live policy the
        backend states on a successful check; remembering it across restarts
        would mean a client that once saw a force flag could lock its user out
        permanently while the backend was unreachable and unable to say
        otherwise. A lockout must always be something the server is currently
        asserting.
        """
        return self._force_pending

    @property
    def pending_release(self) -> Optional[ReleaseInfo]:
        """The installable release on offer, or None."""
        return self._release

    def download_url(self) -> Optional[str]:
        """Where to get the newest announced release, if the backend said.

        None when the deployment published no download URL — the caller must
        then say so rather than opening an empty page.
        """
        if not self._latest:
            return None
        url = self._latest.get("download_url") or None
        # This is opened in the user's browser, so it is held to plain https: a
        # `file:` or custom-scheme URL from a misbehaving backend must never be
        # handed to the operating system to launch.
        return url if _is_https_url(url) else None

    def manual_download_url(self) -> Optional[str]:
        """Where the user can fetch the new version by hand, when an automatic
        update cannot (or did not) install it. None if there is no safe link.

        The installable release's own artifact URL is preferred; otherwise the
        link the backend announced. Both must be plain https.
        """
        release = self._release
        if release is not None and _is_https_url(release.download_url):
            return release.download_url
        return self.download_url()

    @property
    def latest_release(self) -> Optional[Dict[str, Any]]:
        """The backend's last successful answer, or None if none succeeded.

        None means "not known yet", which is not the same as "up to date";
        callers that display this must say so rather than claiming currency
        the client has not established.
        """
        return self._latest

    def check_now(self) -> None:
        """Request an immediate check (safe from any thread).

        Used by login and by the manual "Check for updates" action. It does not
        disturb the schedule: a successful check restamps `last_check_at`, so
        the next automatic check is ten hours from *this* one, which is what a
        user who just checked would expect.
        """
        if self.is_busy:
            # A second check on top of one already running would be a second
            # request for the same answer. Dropped rather than queued.
            self.log.debug("check already in progress; ignoring the request")
            return
        self.wake()

    def reset_session(self) -> None:
        """Forget what was announced, on logout.

        The next user gets the announcement in their own session rather than
        inheriting an "already told them" flag from the previous one. The
        badge is cleared with it, because the menu it hangs off belongs to the
        session that is ending; the durable record survives, so the next
        successful check restores the count without waiting for the backend to
        announce anything new.

        A download already in flight is *not* cancelled here: the artifact is
        the same file whoever is signed in, it is verified against a digest
        rather than against a session, and abandoning a half-finished download
        because someone signed out would be a worse outcome than finishing it.
        """
        self._announced_version = None
        self._latest = None
        self._first_check_done = False
        self._force_pending = False
        if self._state in (UpdateState.UPDATE_AVAILABLE, UpdateState.FAILED):
            self._set_update_state(UpdateState.IDLE)
        self._publish_count(0)

    # ── The update state machine ──────────────────────────────────────────────

    def _set_update_state(self, target: str) -> bool:
        """Move to `target` if the transition is legal. Returns whether it was.

        Every state change in this service goes through here, which is what
        makes the machine an actual guard rather than a label: an illegal
        transition is refused and logged, so a duplicated signal cannot start a
        second download or a second installer.
        """
        if self._state == target:
            return True
        if not can_transition(self._state, target):
            self.log.warning(
                "refusing illegal update transition %s -> %s", self._state, target
            )
            return False
        self.log.info("update state %s -> %s", self._state, target)
        self._state = target
        self.state_changed.emit(target)
        return True

    def _fail(self, message: str, detail: str = "") -> None:
        """Record a failure, tell the user, and return the updater to rest.

        Always ends at IDLE. A failed update must leave a usable application
        and a machine that can try again — a state it could get stuck outside
        IDLE in would be a way to lose exactly that.
        """
        self.log.error("update failed: %s%s", message, f" ({detail})" if detail else "")
        self.health.last_error = message
        self._set_update_state(UpdateState.FAILED)
        self.update_failed.emit(message)
        self._set_update_state(UpdateState.IDLE)

    # ── Persistence ───────────────────────────────────────────────────────────

    def _load_announced_versions(self) -> List[str]:
        """Read the durable record. Never raises: a missing or corrupt row
        means "nothing recorded", which is a worse badge, not a broken app."""
        if self._cache is None:
            return []
        try:
            stored = self._cache.load_app_state(ANNOUNCED_VERSIONS_KEY)
        except Exception:  # noqa: BLE001
            self.log.exception("could not read the announced-version record")
            return []
        if not isinstance(stored, list):
            return []
        return [str(item) for item in stored if _version_tuple(item) is not None]

    def _save_announced_versions(self) -> None:
        if self._cache is None:
            return
        try:
            self._cache.save_app_state(ANNOUNCED_VERSIONS_KEY, self._announced_versions)
        except Exception:  # noqa: BLE001
            self.log.exception("could not record the announced version")

    def last_check_at(self) -> Optional[float]:
        """When the last successful check happened, as a Unix timestamp.

        None means "never, as far as this installation knows", which is treated
        as due — a fresh install should find out about a release rather than
        wait ten hours to ask.
        """
        if self._cache is None:
            return None
        try:
            stored = self._cache.load_app_state(LAST_CHECK_KEY)
        except Exception:  # noqa: BLE001
            self.log.exception("could not read the last update-check time")
            return None
        if not isinstance(stored, (int, float)):
            return None
        stamp = float(stored)
        if stamp <= 0 or stamp > time.time() + 86400:
            # A timestamp in the future is a clock that moved, not a check that
            # happened. Treated as unknown, so the schedule recovers on its own
            # rather than parking the next check somewhere past the year 2100.
            self.log.warning("discarding an implausible last update-check time")
            return None
        return stamp

    def _record_check_time(self) -> None:
        """Stamp a successful check. Only ever called after a valid response.

        A failed check must not move this: if it did, a backend that was down
        for a minute would postpone the next attempt by ten hours.
        """
        if self._cache is None:
            return
        try:
            self._cache.save_app_state(LAST_CHECK_KEY, time.time())
        except Exception:  # noqa: BLE001
            self.log.exception("could not record the update-check time")

    def _startup_delay_ms(self) -> int:
        """How long to wait before this session's first check.

        The ten-hour interval measured from the last successful check, not from
        launch. Someone away overnight is checked as soon as the application
        settles; someone who restarts twice in a minute is not checked twice.
        """
        last = self.last_check_at()
        if last is None:
            return self.FIRST_CHECK_DELAY_MS
        elapsed_ms = max(0.0, (time.time() - last) * 1000.0)
        remaining = self.CHECK_INTERVAL_MS - elapsed_ms
        if remaining <= 0:
            self.log.info(
                "last update check was %.1f hours ago; checking shortly",
                elapsed_ms / 3_600_000,
            )
            return self.FIRST_CHECK_DELAY_MS
        # Never sooner than the startup delay, so a check still cannot land on
        # the startup path even when the interval has almost elapsed.
        return max(self.FIRST_CHECK_DELAY_MS, int(remaining))

    def _recount(self) -> int:
        """Recompute the badge from the record and the installed version."""
        return len(newer_than_installed(self._announced_versions, version.VERSION))

    def _publish_count(self, count: int) -> None:
        """Emit only on a change. A count re-emitted on every poll would be
        the level-triggered signal this project has already been burned by."""
        if count == self._pending_count:
            return
        self._pending_count = count
        self.log.info("pending update count is now %d", count)
        self.pending_count_changed.emit(count)

    def on_start(self) -> None:
        """Publish the badge before the loop begins.

        Read here, on the GUI thread, rather than in the constructor: the
        runtime's construction does no I/O beyond opening the database, and
        waiting for the first check (30s in) would leave a user who restarted
        specifically to deal with an update looking at an empty menu.
        """
        self._announced_versions = self._load_announced_versions()
        # A record kept only of versions still ahead of us: once the user has
        # updated, the old entries have served their purpose and keeping them
        # would grow one row forever.
        pruned = newer_than_installed(self._announced_versions, version.VERSION)
        if pruned != self._announced_versions:
            self._announced_versions = pruned
            self._save_announced_versions()
        self._publish_count(len(pruned))
        # A partial download from a process that was killed mid-transfer cannot
        # be resumed and has no value; leaving it would spend the user's disk
        # on nothing. Cheap, and it runs before any download of our own.
        # Read *before* anything is cleared or checked: this is what the helper
        # recorded about the install that just relaunched us.
        try:
            self._report_previous_install()
        except Exception:  # noqa: BLE001
            self.log.exception("could not report the previous update's result")
        try:
            clear_stale_downloads()
        except Exception:  # noqa: BLE001
            self.log.exception("could not clear stale update downloads")
        super().on_start()

    def _report_previous_install(self) -> None:
        """Tell the user how the update that relaunched us actually went.

        The installer helper outlives the application and cannot show anything
        itself, so it records what happened (`installer.read_and_clear_result`)
        and this reads it. The comparison that matters is the recorded target
        against the version *running now*: a relaunch of the old version after a
        failed installer must read as a failure, never as success.
        """
        result = read_and_clear_result()
        if not result:
            return
        target = result.get("target", "")
        code = result.get("exit_code")
        running = version.VERSION
        self.log.info(
            "UPDATE_RESULT target=%s running=%s stage=%s exit_code=%s",
            target, running, result.get("stage"), code,
        )

        target_t, running_t = _version_tuple(target), _version_tuple(running)
        if target_t is not None and running_t is not None and running_t > target_t:
            # A stale record from an earlier attempt; the app has since moved on.
            return

        if code == "0" and target_t is not None and target_t == running_t:
            outcome = "success"
            message = f"Monitra was updated to version {running}."
            level, title = NotificationLevel.SUCCESS, "Update installed"
        elif code == "0":
            outcome = "not-applied"
            message = (
                f"The installer for Monitra {target} finished, but you are still "
                f"on version {running}. You can try the update again from the "
                "account menu, or download it manually."
            )
            level, title = NotificationLevel.WARNING, "Update not applied"
        elif code is None:
            outcome = "interrupted"
            message = (
                f"The update to Monitra {target} did not finish installing, so "
                f"you are still on version {running}. Your data is safe. You "
                "can try again from the account menu, or download it manually."
            )
            level, title = NotificationLevel.WARNING, "Update did not finish"
        else:
            outcome = "failed"
            message = (
                f"The update to Monitra {target} could not be installed, so you "
                f"are still on version {running}. Your data is safe. You can try "
                "again from the account menu, or download it manually."
            )
            level, title = NotificationLevel.WARNING, "Update failed"

        self.last_install_outcome = (outcome, target)
        notifications = getattr(self.runtime, "notifications", None)
        if notifications is not None:
            notifications.notify(message, level, title=title, key="update-result")

    def _record_version(self, announced: str) -> None:
        """Add a newly announced version to the durable record."""
        if announced in self._announced_versions:
            return
        self._announced_versions = newer_than_installed(
            self._announced_versions + [announced], version.VERSION
        )
        self._save_announced_versions()

    # ── Loop ──────────────────────────────────────────────────────────────────

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
        if not self._first_check_done:
            # Give startup room to restore the session and run its first probe,
            # and honour the ten-hour interval across the restart.
            self._first_check_done = True
            return self._startup_delay_ms()

        hold_reason = self._should_hold()
        if hold_reason:
            self.log.debug("update check held: %s", hold_reason)
            return self.HOLD_INTERVAL_MS

        # A download or install in progress owns the updater; a scheduled check
        # must not run underneath it and re-answer a question already acted on.
        if self._state in (UpdateState.DOWNLOADING, UpdateState.VERIFYING,
                           UpdateState.READY_TO_INSTALL, UpdateState.INSTALLING):
            self.log.debug("update check skipped: %s in progress", self._state)
            return self.HOLD_INTERVAL_MS

        self._set_update_state(UpdateState.CHECKING)
        self.log.info("update check started")
        try:
            payload = self._update_api.get_latest_version()
        except ApiError as exc:
            # Never surfaced to the user and never allowed to degrade anything:
            # not knowing whether an update exists is not a problem the person
            # tracking time can act on. The timestamp is deliberately not moved,
            # so the next attempt is not postponed by the full interval.
            self.log.info("update check unavailable: %s", exc)
            self.heartbeat(success=False)
            self._set_update_state(UpdateState.IDLE)
            return self._jittered(self.HOLD_INTERVAL_MS)

        # The answer is a claim to be checked, not a value to be trusted: JSON
        # `null`, an array or a bare string is valid JSON and is not a release
        # description. A malformed answer is a failed check -- the schedule is
        # not moved, the state returns to rest, and nothing downstream sees it.
        checked = self._checked_payload(payload)
        if checked is None:
            self.log.warning(
                "update check returned an unusable answer (%s); ignoring it",
                type(payload).__name__,
            )
            self.heartbeat(success=False)
            self._set_update_state(UpdateState.IDLE)
            return self._jittered(self.HOLD_INTERVAL_MS)
        payload = checked

        self._latest = payload
        self.heartbeat()
        self._record_check_time()
        if self.state == ServiceState.DEGRADED:
            self._set_state(ServiceState.RUNNING)
        self.log.info(
            "update check completed: latest=%s available=%s",
            payload.get("latest_version"),
            bool(payload.get("update_available")),
        )

        # Recording and announcing are separate on purpose. The badge is
        # durable and survives a restart; the toast fires once per version per
        # session. A user who dismissed the toast still has the menu entry.
        try:
            self._record(payload)
            self._announce(payload)
        finally:
            # Whatever the handlers above did, the check itself is over. Left in
            # CHECKING it would read as busy -- dropping the next manual check
            # and refusing Update Now -- until something else moved it.
            if self._state == UpdateState.CHECKING:
                self._set_update_state(UpdateState.IDLE)
        return self._jittered(self.CHECK_INTERVAL_MS)

    def _checked_payload(self, payload: Any) -> Optional[Dict[str, Any]]:
        """The backend's answer, validated and normalised, or None if unusable.

        Normalised means: an answer that says "update available" for a version
        that is not strictly newer than the one running, or for another
        platform or architecture, is turned into "no update". The server makes
        the same comparison; making it here as well is what stops a backend bug
        (or a hostile backend) from offering an older or equal build -- or
        another machine's artifact -- as an installable update.
        """
        if not isinstance(payload, dict):
            return None
        available = payload.get("update_available")
        if not isinstance(available, bool):
            return None
        latest = payload.get("latest_version")
        if latest is not None and not isinstance(latest, str):
            return None

        checked = dict(payload)
        if available:
            problem = self._not_an_update(checked)
            if problem:
                self.log.warning(
                    "update check: the backend offered %r but %s; treating it as "
                    "no update", latest, problem,
                )
                checked["update_available"] = False
                checked["force_update"] = False
        return checked

    def _not_an_update(self, payload: Dict[str, Any]) -> Optional[str]:
        """Why an "update available" answer cannot be an update for this
        machine, or None if it can."""
        offered = _version_tuple(payload.get("latest_version"))
        running = _version_tuple(version.VERSION)
        if offered is None:
            return "its version is not major.minor.patch"
        if running is None:
            return "the running version is unreadable"
        if offered <= running:
            return f"it is not newer than the installed {version.VERSION}"

        claimed_platform = _fold_platform(payload.get("platform"))
        if claimed_platform and claimed_platform != _fold_platform(sys.platform):
            return f"it is built for {claimed_platform}, not this machine"
        claimed_arch = _fold_arch(payload.get("architecture"))
        machine_arch = _fold_arch(MACHINE_ARCH)
        if claimed_arch and machine_arch and claimed_arch != machine_arch:
            return f"it is built for {claimed_arch}, not {machine_arch}"
        return None

    def _record(self, payload: Dict[str, Any]) -> None:
        """Persist an announced version and republish the badge count."""
        if not payload.get("update_available"):
            # The backend is offering this client nothing -- either it is
            # current, or the deployment has withdrawn what it was offering by
            # clearing DESKTOP_LATEST_VERSION. Both mean the record is stale,
            # and it is *dropped*, not merely recounted.
            #
            # Recounting is not enough, and getting this wrong would have
            # broken the rollback story outright: a withdrawn release is still
            # numerically newer than the installed build, so the badge would
            # have kept pointing users at a build that had just been pulled --
            # and, since the download URL is withdrawn with it, at nothing at
            # all. The withdrawal lever has to clear the badge as well as the
            # toast, or it only half works.
            self._forget_announced_versions()
            return
        latest = payload.get("latest_version")
        if latest and _version_tuple(latest) is not None:
            self._record_version(latest)
        self._publish_count(self._recount())

    def _forget_announced_versions(self) -> None:
        """Drop the durable record; the backend is offering nothing."""
        if self._announced_versions:
            self._announced_versions = []
            self._save_announced_versions()
        # The toast gate goes with it, so a release that is withdrawn and then
        # re-published announces itself again rather than being silently
        # swallowed as "already told them".
        self._announced_version = None
        # A withdrawn release cannot be a mandatory one. Clearing this is what
        # lets a force flag published in error be taken back.
        self._force_pending = False
        self._release = None
        self._set_update_state(UpdateState.IDLE)
        self._publish_count(0)

    def _announce(self, payload: Dict[str, Any]) -> None:
        """Tell the user about a newer release, at most once per version."""
        if not payload.get("update_available"):
            self._set_update_state(UpdateState.IDLE)
            return
        announced = payload.get("latest_version")
        if not announced:
            # The deployment does not know its latest release. Silence, not a
            # prompt for a version that does not exist.
            self._set_update_state(UpdateState.IDLE)
            return

        # Parsed every time, even when already announced: a release can be
        # re-published with a force flag it did not carry before, and the
        # policy must follow the backend rather than the first answer seen.
        release = ReleaseInfo.from_payload(payload)
        self._release = release
        self._force_pending = bool(release is not None and release.force_update)
        self._set_update_state(UpdateState.UPDATE_AVAILABLE)

        if announced == self._announced_version and not self._force_pending:
            # Already told them this session. A mandatory update is the one
            # exception -- it is re-offered because the user cannot carry on
            # until it is resolved, and a dialog they dismissed by restarting
            # must come back.
            return
        self._announced_version = announced

        download_url = payload.get("download_url") or ""
        self.log.info(
            "update available: %s (installable=%s, mandatory=%s)",
            announced, release is not None, self._force_pending,
        )
        self.update_available.emit(announced, download_url)
        if release is not None:
            # The dialog path. Emitted only for a release that can actually be
            # installed -- one with a verified-shape URL and a digest -- so the
            # UI never offers "Update Now" for something it cannot verify.
            self.update_offered.emit(release, self._force_pending)

        notifications = getattr(self.runtime, "notifications", None)
        if notifications is None:
            return
        if release is not None:
            # There is a dialog for this one; a toast as well would be telling
            # the user the same thing twice in the same moment.
            return
        # A platform toast renders plain text, so the URL in the body is not
        # a link. `link` is what makes the notification actionable: clicking
        # the toast opens the download page. Without it the user is told an
        # update exists, and the address disappears the moment they click.
        message = f"Monitra {announced} is available."
        message += (
            " Click here to download it."
            if download_url
            else " Ask your administrator where to download it."
        )
        # `notify` is safe from any thread: it hops to the notification
        # service's own thread through a queued signal.
        notifications.notify(
            message,
            NotificationLevel.INFO,
            title="Update available",
            key=f"update-available:{announced}",
            link=download_url or None,
        )

    # ── Download, verify, install ─────────────────────────────────────────────

    def start_update(self) -> bool:
        """Download, verify and install the pending release. Returns whether
        the attempt was started.

        Safe to call from the GUI thread and safe to call twice: the state
        machine refuses a second start, and the TaskRunner's de-duplication key
        refuses a second task even if the state check were somehow raced. The
        download itself runs on the shared task pool — never on the GUI thread,
        and never on a thread this service created.
        """
        release = self._release
        if release is None:
            self.log.info("no installable release to start")
            return False
        if self._state in UpdateState.BUSY:
            self.log.info("update already in progress (%s)", self._state)
            return False

        blocked = can_install()
        if blocked:
            # A source checkout or a portable build. Said plainly rather than
            # attempted and failed halfway through.
            self._fail(blocked, detail="install path unavailable")
            return False

        if not self._set_update_state(UpdateState.DOWNLOADING):
            return False

        tasks = getattr(self.runtime, "tasks", None)
        if tasks is None:
            self._fail(
                "The update could not be started.", detail="no task runner"
            )
            return False

        handle = tasks.submit(
            lambda task_handle: self._download(release, task_handle),
            on_success=lambda result: self._on_download_finished(release, result),
            on_error=lambda exc: self._on_download_error(exc),
            key=DOWNLOAD_TASK_KEY,
            pass_handle=True,
        )
        if handle is None:
            # De-duplicated, or the runner is shutting down. Neither is an
            # error the user needs to see; the state simply goes back.
            self.log.info("update download was not started (already running)")
            self._set_update_state(UpdateState.IDLE)
            return False
        return True

    def _download(self, release: ReleaseInfo, task_handle):
        """The blocking half, on the task pool. Never touches a widget."""
        result = download_and_verify(
            url=release.download_url,
            expected_sha256=release.sha256,
            version=release.version,
            expected_size=release.file_size,
            on_progress=self._emit_progress,
            # Cancellation is cooperative and checked between chunks, so a
            # shutdown during a download costs one chunk, not one file.
            should_stop=lambda: task_handle.cancelled or self.stopping,
        )
        # The checksum says this is the file the backend named; the signature
        # says who built it. Checked here, on the pool, because revocation
        # checking can wait on the network and must never run on the GUI thread
        # -- and before the artifact is ever handed to anything that executes it.
        try:
            verify_installer(result.path)
        except BaseException:
            try:
                result.path.unlink(missing_ok=True)
            except OSError:
                self.log.warning("could not remove the unverified update artifact")
            raise
        return result

    def _emit_progress(self, received: int, total: Optional[int]) -> None:
        """Publish download progress. Called from the task pool thread.

        A Qt signal is the only thing crossing the thread boundary; Qt delivers
        it queued onto the GUI thread, so no widget is ever touched from here.
        """
        self.download_progress.emit(int(received), int(total or 0))

    def _on_download_finished(self, release: ReleaseInfo, result) -> None:
        """Verified on disk. Hand off to the installer. On the GUI thread."""
        # VERIFYING is entered and left immediately: the hashing happened
        # during the download, in one pass, so there is no second read to wait
        # on. The state exists so the sequence a reader expects is the sequence
        # the machine actually performs.
        self._set_update_state(UpdateState.VERIFYING)
        if not self._set_update_state(UpdateState.READY_TO_INSTALL):
            return

        try:
            launch_installer(result.path, release.version)
        except InstallError as exc:
            # Nothing has been installed and nothing replaced; the running
            # application is exactly as it was.
            self._fail(exc.message, detail=exc.detail)
            return
        except Exception as exc:  # noqa: BLE001
            # `launch_installer` classifies its own failures, so this is a bug --
            # but a bug here must not strand the state machine at
            # READY_TO_INSTALL with a dialog that can never finish.
            self.log.exception("unexpected failure handing off to the installer")
            self._fail(
                "The update could not be started. Your current version of "
                "Monitra is unaffected.",
                detail=repr(exc),
            )
            return

        self._set_update_state(UpdateState.INSTALLING)
        self.log.info("installer launched for version %s", release.version)
        # The helper is already waiting for this process to exit. Quitting is
        # the caller's job, and it is an ordinary quit: the normal shutdown
        # path flushes the cache, drains what it can and closes the database,
        # so nothing about the update needs special handling for the timer,
        # sync, screenshots or activity capture.
        self.install_started.emit(release.version)

    def _on_download_error(self, exc: BaseException) -> None:
        """A failed download. On the GUI thread."""
        if isinstance(exc, DownloadError):
            self._fail(exc.message, detail=exc.detail)
            return
        self._fail(
            "The update could not be downloaded. Your current version of "
            "Monitra is unaffected.",
            detail=repr(exc),
        )

    @staticmethod
    def _jittered(interval_ms: int) -> int:
        """Spread a fleet's checks out so they do not arrive in lockstep."""
        return int(interval_ms * (0.85 + random.random() * 0.3))

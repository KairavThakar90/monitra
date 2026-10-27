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
import uuid
from datetime import datetime, timezone
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
from .state import ReleaseInfo, UpdateRequirement, UpdateState, can_transition

#: The backend's "you must update by <deadline>", persisted in `app_state` so
#: a restart -- and an offline start -- keeps the deadline. Written only from
#: a successful check (the server's own statement), read at start, dropped
#: when the running version satisfies it or the backend withdraws it.
REQUIREMENT_KEY = "updates.requirement"

#: Install reports not yet delivered to the backend, oldest first. Bounded.
PENDING_REPORTS_KEY = "updates.pending_reports"
MAX_PENDING_REPORTS = 20

#: What the installer was asked to do, written just before the application
#: exits for it, so the next launch can report "from X to Y" rather than only
#: "Y did not happen".
INSTALL_ATTEMPT_KEY = "updates.install_attempt"

#: De-duplication key for the report upload task.
REPORT_TASK_KEY = "update-report"

#: The outcome a failure category maps to, for the backend's report.
_CATEGORY_OUTCOMES = {
    "checksum-failed": "verification-failed",
    "signature-refused": "verification-failed",
    "download-incomplete": "download-failed",
    "http-error": "download-failed",
    "no-connection": "download-failed",
    "timeout": "download-failed",
    "url-refused": "download-failed",
    "disk-space": "download-failed",
    "download-failed": "download-failed",
    "install-unavailable": "install-unavailable",
    "cancelled": "cancelled",
}

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

#: The user's last "Later", persisted in `app_state` so the reminder schedule
#: survives a restart: ``{"version", "postponed_at", "first_offered_at", "count"}``.
POSTPONED_KEY = "updates.postponed"

#: The reminder policy, for an *optional* update the user has postponed.
#: "Later" puts the prompt away for at least this long; it comes back at the
#: first successful check after that (so, with the ten-hour cadence, within a
#: working day and a half). Nothing here applies to a mandatory update, which
#: is re-offered on every check, or to an announce-only release, which has no
#: prompt to postpone.
REMIND_AFTER_SECONDS = 24 * 60 * 60
#: From this many postponements, or this long after the release was first
#: offered, the prompt is shown as a *final reminder*. It still has "Later":
#: only the backend's own policy (`force_update`, `min_supported_version`) can
#: make an update mandatory, and a client must never escalate to that by itself.
FINAL_REMINDER_AFTER_POSTPONEMENTS = 3
FINAL_REMINDER_AFTER_SECONDS = 7 * 24 * 60 * 60

#: Reminder stages, as carried on `update_offered`.
STAGE_INITIAL = "initial"
STAGE_REMINDER = "reminder"
STAGE_FINAL = "final"


def _now() -> float:
    """Wall-clock time. A function so tests can move it."""
    return time.time()


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
        update_offered(object, bool, str) — a `ReleaseInfo` the user can
            actually install, whether taking it is mandatory, and the reminder
            stage (`STAGE_INITIAL`, `STAGE_REMINDER`, `STAGE_FINAL`). Emitted on
            the same edge as the announcement, and only when the release is
            installable; after a "Later", again when the reminder is due.
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
    update_offered = Signal(object, bool, str)
    #: Number of announced versions newer than the installed one. Emitted only
    #: when the number actually changes, so the menu badge is repainted on a
    #: transition rather than on every poll.
    pending_count_changed = Signal(int)
    state_changed = Signal(str)
    download_progress = Signal(int, int)
    update_failed = Signal(str)
    install_started = Signal(str)
    #: The backend's requirement changed: an `UpdateRequirement`, or None
    #: when nothing is required any more. Emitted on a change of the required
    #: version, deadline or blocked flag -- never on every check.
    requirement_changed = Signal(object)
    #: The application must (True) or may again (False) be used normally.
    #: Edge-triggered: once when the grace period runs out (by the backend's
    #: word, or by its clock reaching the deadline while the app is open),
    #: once when the requirement is satisfied or withdrawn.
    blocked_changed = Signal(bool)

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
        #: The persisted "Later" record (see POSTPONED_KEY), or None. Loaded at
        #: start, written by `postpone`, dropped once the version it names is
        #: installed or withdrawn.
        self._postponed: Optional[Dict[str, Any]] = None
        self._postponed_loaded = False
        #: The backend's requirement (see REQUIREMENT_KEY), or None. Written
        #: only through `_set_requirement`, which is what makes the two
        #: signals above edge-triggered.
        self._requirement: Optional[UpdateRequirement] = None
        #: True when the requirement was read back from disk by this process
        #: and no check has replaced it yet: its clock is then measured from
        #: the wall clock at receipt, not from a monotonic clock this process
        #: never had (see `UpdateRequirement.server_now`).
        self._requirement_restarted = False
        #: Whether normal use is blocked. A plain bool, read from the GUI
        #: thread and the timer's thread; written only through `_set_blocked`.
        self._blocked = False
        #: The next check is the user's own "Check for Updates": its answer is
        #: offered even for a postponed release. Consumed by the check.
        self._manual_check = False
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

        Two sources. A backend with release management states a *requirement*
        with a deadline (`is_blocked`, persisted with the deadline it came
        with, so a restart cannot reset it -- the deadline is the server's and
        the server enforces it either way). A backend without one says
        `force_update`, which is deliberately **not persisted**: a bare flag
        remembered across restarts could lock a user out while the backend
        was unreachable and unable to say otherwise.
        """
        return self._blocked or self._force_pending

    @property
    def is_blocked(self) -> bool:
        """True once a mandatory update's grace period has run out.

        Safe to read from any thread (a bool, assigned whole). Written only by
        `_set_blocked`, from the backend's own `blocked` or from its clock
        reaching the deadline it gave -- never from this machine's wall clock.
        """
        return self._blocked

    @property
    def requirement(self) -> Optional[UpdateRequirement]:
        """The backend's statement that this desktop must update, or None."""
        return self._requirement

    def requirement_remaining_seconds(self) -> Optional[int]:
        """Seconds of grace left on the server's clock, or None without a
        deadline. For the countdown; `is_blocked` is what decides."""
        requirement = self._requirement
        if requirement is None:
            return None
        return requirement.remaining_seconds(restarted=self._requirement_restarted)

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

    def check_now(self, manual: bool = False) -> None:
        """Request an immediate check (safe from any thread).

        Used by login, by the manual "Check for updates" action and by the
        API client when the backend answers 426. It does not disturb the
        schedule: a successful check restamps `last_check_at`, so the next
        automatic check is ten hours from *this* one, which is what a user
        who just checked would expect.

        `manual` says the user asked: the answer is then offered even for a
        release they postponed, because a silent "Check for Updates" reads as
        a broken button.
        """
        if manual:
            self._manual_check = True
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
        self._manual_check = False
        # The requirement was the signed-out account's. Logout wipes
        # `app_state` with it; the next account's own requirement comes with
        # that account's first check. The blocked edge is released so the
        # sign-in screen is reachable.
        self._set_requirement(None, persist=False)
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

    def _fail(self, message: str, detail: str = "", category: str = "failed") -> None:
        """Record a failure, tell the user, and return the updater to rest.

        Always ends at IDLE. A failed update must leave a usable application
        and a machine that can try again — a state it could get stuck outside
        IDLE in would be a way to lose exactly that.

        `category` is the short, fixed token support can tell failures apart
        by: `url-refused`, `disk-space`, `no-connection`, `timeout`,
        `http-error`, `download-incomplete`, `checksum-failed`,
        `signature-refused`, `permission`, `install-unavailable`,
        `installer-launch`, `cancelled`.
        """
        self.log.error("update failed: %s%s", message, f" ({detail})" if detail else "")
        target = self._release.version if self._release is not None else None
        self._event("failed", category=category, version=target)
        if target is not None:
            # Told to the backend too, so the release history can show that
            # this machine tried and why it did not get there. The report is
            # durable and retried; it never blocks the user's own retry.
            self._queue_report(
                target_version=target, from_version=version.VERSION,
                outcome=_CATEGORY_OUTCOMES.get(category, "failed"),
                category=category, detail=message,
            )
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

    def _load_postponement(self) -> Optional[Dict[str, Any]]:
        """The persisted "Later" record, or None. Never raises; a corrupt or
        stale record (a version no longer ahead of us) is dropped."""
        if self._cache is None:
            return None
        try:
            stored = self._cache.load_app_state(POSTPONED_KEY)
        except Exception:  # noqa: BLE001
            self.log.exception("could not read the update postponement record")
            return None
        if not isinstance(stored, dict):
            return None
        version_name = stored.get("version")
        try:
            record = {
                "version": str(version_name),
                "postponed_at": float(stored.get("postponed_at")),
                "first_offered_at": float(stored.get("first_offered_at") or stored.get("postponed_at")),
                "count": int(stored.get("count") or 0),
            }
        except (TypeError, ValueError):
            return None
        if _version_tuple(record["version"]) is None:
            return None
        if not newer_than_installed([record["version"]], version.VERSION):
            # Installed since (or never ahead of us): the record has served
            # its purpose and would otherwise outlive the update it was about.
            self._save_postponement(None)
            return None
        return record

    def _current_postponement(self) -> Optional[Dict[str, Any]]:
        """The "Later" record, read from the cache once per process."""
        if not self._postponed_loaded:
            self._postponed = self._load_postponement()
            self._postponed_loaded = True
        return self._postponed

    def _save_postponement(self, record: Optional[Dict[str, Any]]) -> None:
        self._postponed = record
        self._postponed_loaded = True
        if self._cache is None:
            return
        try:
            if record is None:
                self._cache.save_app_state(POSTPONED_KEY, None)
            else:
                self._cache.save_app_state(POSTPONED_KEY, record)
        except Exception:  # noqa: BLE001
            self.log.exception("could not record the update postponement")

    def postpone(self) -> None:
        """The user chose "Later" for the release on offer. Safe from the GUI thread.

        Remembered durably, so the prompt stays away for `REMIND_AFTER_SECONDS`
        across restarts instead of returning at the next login, and so the
        number of postponements can decide when the reminder becomes final.
        The release itself stays on offer: the badge is untouched and the
        account menu's Updates entry opens the dialog at any time.
        """
        release = self._release
        if release is None:
            return
        if self._blocked:
            # The grace period is over. A "Later" that reached here came from
            # a control that had not yet heard the edge; it changes nothing.
            self.log.info("postpone refused: the update's grace period has run out")
            return
        now = _now()
        previous = self._current_postponement()
        if previous is not None and previous.get("version") == release.version:
            count = int(previous.get("count", 0)) + 1
            first = float(previous.get("first_offered_at", now))
        else:
            count, first = 1, now
        self._save_postponement({
            "version": release.version, "postponed_at": now,
            "first_offered_at": first, "count": count,
        })
        self._event("postponed", version=release.version, count=count)

    def reminder_stage(self, release_version: str, now: Optional[float] = None) -> Optional[str]:
        """What kind of prompt `release_version` is due, or None for "not yet".

        * No postponement recorded for this version: `STAGE_INITIAL`.
        * Postponed less than `REMIND_AFTER_SECONDS` ago: None -- the badge
          stays, the dialog does not return.
        * Otherwise `STAGE_REMINDER`, or `STAGE_FINAL` once the user has
          postponed `FINAL_REMINDER_AFTER_POSTPONEMENTS` times or the release
          has been on offer for `FINAL_REMINDER_AFTER_SECONDS`.
        """
        record = self._current_postponement()
        if record is None or record.get("version") != release_version:
            return STAGE_INITIAL
        moment = _now() if now is None else now
        if moment - float(record["postponed_at"]) < REMIND_AFTER_SECONDS:
            return None
        if (
            int(record.get("count", 0)) >= FINAL_REMINDER_AFTER_POSTPONEMENTS
            or moment - float(record["first_offered_at"]) >= FINAL_REMINDER_AFTER_SECONDS
        ):
            return STAGE_FINAL
        return STAGE_REMINDER

    def _event(self, stage: str, **fields: Any) -> None:
        """One structured log line per update lifecycle event.

        `UPDATE_EVENT stage=<stage> version=<v> platform=<p> key=value ...` --
        what support greps for. Values are the version, the platform, a stage
        and a short error category; never a token, a path with a username in
        it, or anything else about the person.
        """
        extra = " ".join(f"{key}={value}" for key, value in fields.items() if value is not None)
        self.log.info(
            "UPDATE_EVENT stage=%s installed=%s platform=%s %s",
            stage, version.VERSION, sys.platform, extra,
        )

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
        self._current_postponement()
        # The backend's requirement from the last run, if the running version
        # does not already satisfy it. Its deadline is the server's; a restart
        # neither resets nor extends it, and the application blocks at the
        # deadline even before the first check of this session can run.
        self._load_requirement()
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
        self._event("install-result", version=target, outcome=outcome,
                    exit_code=code, helper_stage=result.get("stage"))
        attempt = self._load_state(INSTALL_ATTEMPT_KEY)
        from_version = attempt.get("from_version") if isinstance(attempt, dict) else None
        self._save_state(INSTALL_ATTEMPT_KEY, None)
        self._queue_report(
            target_version=target, from_version=from_version if _version_tuple(from_version) else None,
            outcome=outcome, category=None,
            detail=f"exit_code={code} stage={result.get('stage')} running={running}",
        )
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

        # The deadline is kept on the server's clock. Whether it has passed is
        # re-read on every tick -- including the ticks that cannot reach the
        # backend -- so the application blocks at the deadline while offline
        # or signed out just as it does online. `_next_interval` makes the
        # loop wake for the deadline itself.
        self._refresh_blocked_from_clock()

        hold_reason = self._should_hold()
        if hold_reason:
            self.log.debug("update check held: %s", hold_reason)
            return self._next_interval(self.HOLD_INTERVAL_MS)

        # A download or install in progress owns the updater; a scheduled check
        # must not run underneath it and re-answer a question already acted on.
        if self._state in (UpdateState.DOWNLOADING, UpdateState.VERIFYING,
                           UpdateState.READY_TO_INSTALL, UpdateState.INSTALLING):
            self.log.debug("update check skipped: %s in progress", self._state)
            return self._next_interval(self.HOLD_INTERVAL_MS)

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
            self._event("check-unavailable", status=getattr(exc, "status_code", None))
            self.heartbeat(success=False)
            self._set_update_state(UpdateState.IDLE)
            return self._next_interval(self._jittered(self.HOLD_INTERVAL_MS))

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
            return self._next_interval(self._jittered(self.HOLD_INTERVAL_MS))
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
        self._event("checked", latest=payload.get("latest_version"),
                    available=bool(payload.get("update_available")))

        # Recording and announcing are separate on purpose. The badge is
        # durable and survives a restart; the toast fires once per version per
        # session. A user who dismissed the toast still has the menu entry.
        try:
            self._record(payload)
            # The requirement before the announcement: the dialog the
            # announcement raises reads the deadline the requirement carries.
            self._apply_requirement(payload)
            self._announce(payload)
        finally:
            # Whatever the handlers above did, the check itself is over. Left in
            # CHECKING it would read as busy -- dropping the next manual check
            # and refusing Update Now -- until something else moved it.
            self._manual_check = False
            if self._state == UpdateState.CHECKING:
                self._set_update_state(UpdateState.IDLE)
        # A report the previous attempt could not deliver goes now, while the
        # backend is known to answer; never on the GUI thread, never blocking
        # the check.
        self._send_pending_reports()
        return self._next_interval(self._jittered(self.CHECK_INTERVAL_MS))

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
        # And it has nothing left to be reminded about.
        if self._current_postponement() is not None:
            self._save_postponement(None)
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

        if release is not None and not self._force_pending and not release.mandatory:
            # An optional, installable release follows the reminder policy. A
            # "Later" is remembered durably, so neither the next check nor the
            # next login brings the dialog straight back; it returns when due,
            # and as a final reminder once the user has put it off enough.
            stage = self.reminder_stage(announced)
            if stage is None and self._manual_check:
                # The user asked. A postponed release is still the answer to
                # "is there an update?", so it is offered again, as a reminder.
                stage = STAGE_REMINDER
            if stage is None:
                self.log.debug("update %s was postponed recently; not re-offering yet", announced)
                return
            if stage == STAGE_INITIAL and announced == self._announced_version and not self._manual_check:
                # Already told them this session and they have not answered
                # (the dialog is still up, or was closed by a logout).
                return
            self._announced_version = announced
            self.log.info(
                "update available: %s (installable=True, mandatory=False, stage=%s)",
                announced, stage,
            )
            self._event("offered", version=announced, stage_name=stage)
            self.update_available.emit(announced, payload.get("download_url") or "")
            self.update_offered.emit(release, False, stage)
            # There is a dialog for this one; a toast as well would be telling
            # the user the same thing twice in the same moment.
            return

        mandatory = self._force_pending or bool(release is not None and release.mandatory)
        if announced == self._announced_version and not mandatory:
            # Already told them this session. A mandatory update is the one
            # exception -- it is re-offered because the user cannot carry on
            # until it is resolved, and a dialog they dismissed by restarting
            # must come back.
            return
        self._announced_version = announced

        # A mandatory release with a grace period is re-offered on every
        # check; the words say whether this is the first time or a reminder
        # after a "Later", and the dialog itself reads the deadline.
        stage = STAGE_INITIAL
        if mandatory and release is not None and self.reminder_stage(announced) != STAGE_INITIAL:
            stage = STAGE_REMINDER
        download_url = payload.get("download_url") or ""
        self.log.info(
            "update available: %s (installable=%s, mandatory=%s, blocked=%s)",
            announced, release is not None, mandatory, self._blocked,
        )
        self._event("offered", version=announced, installable=release is not None,
                    mandatory=mandatory, stage_name=stage)
        self.update_available.emit(announced, download_url)
        if release is not None:
            # The mandatory dialog path. Emitted only for a release that can
            # actually be installed -- one with a verified-shape URL and a
            # digest -- so the UI never offers "Update Now" for something it
            # cannot verify.
            self.update_offered.emit(release, mandatory, stage)
            return

        notifications = getattr(self.runtime, "notifications", None)
        if notifications is None:
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
            # A source checkout, a portable build, a machine-wide install or a
            # translocated bundle. Said plainly rather than attempted and
            # failed halfway through.
            self._fail(blocked, detail="install path unavailable", category="install-unavailable")
            return False

        if not self._set_update_state(UpdateState.DOWNLOADING):
            return False

        tasks = getattr(self.runtime, "tasks", None)
        if tasks is None:
            self._fail(
                "The update could not be started.", detail="no task runner",
                category="installer-launch",
            )
            return False
        self._event("download-started", version=release.version, size=release.file_size)

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

        self._event("verified", version=release.version, size=result.size_bytes)
        try:
            launch_installer(result.path, release.version)
        except InstallError as exc:
            # Nothing has been installed and nothing replaced; the running
            # application is exactly as it was.
            self._fail(exc.message, detail=exc.detail, category="installer-launch")
            return
        except Exception as exc:  # noqa: BLE001
            # `launch_installer` classifies its own failures, so this is a bug --
            # but a bug here must not strand the state machine at
            # READY_TO_INSTALL with a dialog that can never finish.
            self.log.exception("unexpected failure handing off to the installer")
            self._fail(
                "The update could not be started. Your current version of "
                "Monitra is unaffected.",
                detail=repr(exc), category="installer-launch",
            )
            return

        self._set_update_state(UpdateState.INSTALLING)
        self.log.info("installer launched for version %s", release.version)
        self._event("install-started", version=release.version)
        # What is being attempted, for the next launch's report: the helper
        # records only the target and the exit code, not where it came from.
        self._save_state(INSTALL_ATTEMPT_KEY, {
            "from_version": version.VERSION, "target_version": release.version,
            "started_at": time.time(),
        })
        # The helper is already waiting for this process to exit. Quitting is
        # the caller's job, and it is an ordinary quit: the normal shutdown
        # path flushes the cache, drains what it can and closes the database,
        # so nothing about the update needs special handling for the timer,
        # sync, screenshots or activity capture.
        self.install_started.emit(release.version)

    def _on_download_error(self, exc: BaseException) -> None:
        """A failed download. On the GUI thread."""
        if isinstance(exc, DownloadError):
            self._fail(exc.message, detail=exc.detail, category=exc.category)
            return
        self._fail(
            "The update could not be downloaded. Your current version of "
            "Monitra is unaffected.",
            detail=repr(exc), category="download-failed",
        )

    @staticmethod
    def _jittered(interval_ms: int) -> int:
        """Spread a fleet's checks out so they do not arrive in lockstep."""
        return int(interval_ms * (0.85 + random.random() * 0.3))

    # ── The backend's requirement, and the deadline ──────────────────────────

    def _load_state(self, key: str) -> Any:
        """One `app_state` row, or None. Never raises."""
        if self._cache is None:
            return None
        try:
            return self._cache.load_app_state(key)
        except Exception:  # noqa: BLE001
            self.log.exception("could not read %s", key)
            return None

    def _save_state(self, key: str, value: Any) -> None:
        if self._cache is None:
            return
        try:
            self._cache.save_app_state(key, value)
        except Exception:  # noqa: BLE001
            self.log.exception("could not write %s", key)

    def _load_requirement(self) -> None:
        """Read the persisted requirement at start, if the running version
        does not already satisfy it.

        A requirement satisfied by the version now running has served its
        purpose: it is dropped here, which is the "cleared only after the
        installed version is verified" rule -- nothing clears it earlier, and
        nothing clears it on the installer's word alone.
        """
        requirement = UpdateRequirement.from_record(self._load_state(REQUIREMENT_KEY))
        if requirement is None:
            return
        if not newer_than_installed([requirement.required_version], version.VERSION):
            self.log.info(
                "UPDATE_REQUIREMENT_SATISFIED required=%s running=%s",
                requirement.required_version, version.VERSION,
            )
            self._save_state(REQUIREMENT_KEY, None)
            return
        self._requirement_restarted = True
        self._set_requirement(requirement, persist=False)

    def _apply_requirement(self, payload: Dict[str, Any]) -> None:
        """Take the backend's statement from a successful check.

        The backend's answer replaces whatever was persisted, including one
        that said "blocked": the server is the authority, and a requirement
        it no longer states (satisfied, withdrawn, or never this user's) is
        gone. A requirement for a version this build already satisfies is
        not kept either.
        """
        requirement = UpdateRequirement.from_payload(payload)
        if requirement is not None and not newer_than_installed(
            [requirement.required_version], version.VERSION,
        ):
            requirement = None
        self._requirement_restarted = False
        self._set_requirement(requirement, persist=True)

    def _set_requirement(self, requirement: Optional[UpdateRequirement], *, persist: bool) -> None:
        """The one place the requirement changes. Edge-triggered signals."""
        previous = self._requirement
        self._requirement = requirement
        if persist:
            self._save_state(REQUIREMENT_KEY, requirement.to_record() if requirement else None)
        changed = (
            (previous is None) != (requirement is None)
            or (previous is not None and requirement is not None and (
                previous.required_version != requirement.required_version
                or previous.deadline_at != requirement.deadline_at
                or previous.blocked_by_server != requirement.blocked_by_server
            ))
        )
        if changed:
            if requirement is None:
                self._event("requirement-cleared")
            else:
                self._event(
                    "requirement", required=requirement.required_version,
                    deadline=requirement.deadline_at, blocked=requirement.blocked_by_server,
                )
            self.requirement_changed.emit(requirement)
        self._refresh_blocked_from_clock()

    def _refresh_blocked_from_clock(self) -> None:
        """Re-read whether the deadline has passed, and publish the edge."""
        requirement = self._requirement
        blocked = requirement is not None and requirement.expired(restarted=self._requirement_restarted)
        self._set_blocked(blocked)

    def _set_blocked(self, blocked: bool) -> None:
        if blocked == self._blocked:
            return
        self._blocked = blocked
        requirement = self._requirement
        self._event(
            "blocked" if blocked else "unblocked",
            required=requirement.required_version if requirement else None,
        )
        (self.log.warning if blocked else self.log.info)(
            "UPDATE_%s required=%s", "BLOCKED" if blocked else "UNBLOCKED",
            requirement.required_version if requirement else None,
        )
        self.blocked_changed.emit(blocked)

    def _next_interval(self, interval_ms: int) -> int:
        """`interval_ms`, or sooner if the deadline falls before it.

        The loop then wakes at the deadline and `_refresh_blocked_from_clock`
        publishes the edge within a second of it -- while the application
        stays open, online or not. A deadline already past wakes the loop at
        the hold cadence, so the backend's own verdict is fetched promptly.
        """
        remaining = self.requirement_remaining_seconds()
        if remaining is None or self._blocked:
            return interval_ms
        return max(1000, min(interval_ms, remaining * 1000 + 1000))

    # ── Install reports ──────────────────────────────────────────────────────

    def _queue_report(
        self, *, target_version: str, from_version: Optional[str], outcome: str,
        category: Optional[str], detail: Optional[str],
    ) -> None:
        """Record what happened, durably, and try to deliver it.

        The report carries the instant and this machine's clock so the backend
        places it by age, and a client event id so a retried delivery is one
        row there. Never a path, never a token: the version, the outcome, the
        category and a short message.
        """
        now = datetime.now(timezone.utc).isoformat()
        report = {
            "client_event_id": uuid.uuid4().hex,
            "target_version": target_version,
            "from_version": from_version,
            "outcome": outcome,
            "category": category,
            "detail": (detail or "")[:500] or None,
            "occurred_at": now,
            "client_time": now,
        }
        pending = self._load_pending_reports()
        pending.append(report)
        self._save_state(PENDING_REPORTS_KEY, pending[-MAX_PENDING_REPORTS:])
        self._send_pending_reports()

    def _load_pending_reports(self) -> List[Dict[str, Any]]:
        stored = self._load_state(PENDING_REPORTS_KEY)
        if not isinstance(stored, list):
            return []
        return [item for item in stored if isinstance(item, dict) and item.get("client_event_id")]

    def _send_pending_reports(self) -> None:
        """Deliver queued reports on the task pool. Safe to call from any
        thread; de-duplicated by key so two callers produce one upload."""
        pending = self._load_pending_reports()
        if not pending:
            return
        tasks = getattr(self.runtime, "tasks", None)
        api_client = getattr(self.runtime, "api_client", None)
        if tasks is None or api_client is None or not getattr(api_client, "access_token", None):
            return

        def deliver() -> List[str]:
            delivered: List[str] = []
            for report in pending:
                self._update_api.report_install_outcome(report)
                delivered.append(report["client_event_id"])
            return delivered

        def on_success(delivered: List[str]) -> None:
            remaining = [r for r in self._load_pending_reports() if r["client_event_id"] not in delivered]
            self._save_state(PENDING_REPORTS_KEY, remaining)
            self._event("reported", count=len(delivered))

        def on_error(exc: BaseException) -> None:
            # Kept for the next successful check; a report is never lost to
            # a network blip and never retried in a loop of its own.
            self.log.info("update report not delivered yet: %s", exc)

        tasks.submit(deliver, on_success=on_success, on_error=on_error,
                     key=REPORT_TASK_KEY, guard_generation=False)

"""
The update state machine, and the release descriptor it moves through.

Why a machine and not a few booleans
------------------------------------
Everything that can go wrong with an updater goes wrong through concurrency:
two checks in flight, two dialogs on screen, two downloads writing the same
file, two installers running at once. Booleans do not prevent that — they
record it after the fact. A single ``state``, with the legal transitions
declared in one table, does: a transition that is not in the table is refused
and logged, so "download while already downloading" cannot happen by accident
regardless of which signal arrived twice.

The states mirror the flow exactly once each:

    IDLE ─▶ CHECKING ─▶ UPDATE_AVAILABLE ─▶ DOWNLOADING ─▶ VERIFYING
                                                              │
                    FAILED ◀───────────────────────────────────┤
                                                              ▼
                                        INSTALLING ◀── READY_TO_INSTALL

`FAILED` is reachable from every working state and leads back to `IDLE`,
because a failed update must always leave a usable application behind — that is
the whole safety property of this feature, and a machine that could get stuck
outside `IDLE` would be a way to lose it.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, FrozenSet, Optional

from core.logging_setup import get_logger
from core.validation import validate_sha256, validate_url, validate_version

from . import policy

log = get_logger("updates.state")


def parse_instant(value: Any) -> Optional[float]:
    """An ISO-8601 instant from the backend as Unix seconds, or None.

    The backend writes timezone-aware UTC timestamps; a naive one is read as
    UTC rather than refused, because every instant the server sends is on its
    own clock and it has only one. Anything that is not a string timestamp is
    None: a deadline the client cannot read is no deadline, and the server
    enforces the real one regardless.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


@dataclass(frozen=True)
class UpdateRequirement:
    """The backend's statement that this desktop must update, and by when.

    Every instant here is the **server's**: `deadline_at` and `server_time`
    are Unix seconds on the backend's clock, exactly as it sent them. The
    desktop never compares them with its own wall clock. It measures how long
    ago it received the answer on the monotonic clock (`received_monotonic`)
    and adds that to `server_time`, so a system clock moved forwards or
    backwards by the user changes nothing. After a restart the monotonic clock
    is meaningless, so the wall clock at receipt (`received_wall`) stands in
    -- clamped so that a clock set *backwards* cannot make the deadline look
    further away -- until the next successful check replaces the record with a
    fresh one. The backend enforces the deadline on every request in any case;
    the local copy exists so the screen can count down and so the application
    blocks at the deadline even while offline.
    """

    required_version: str
    deadline_at: Optional[float]
    server_time: float
    received_monotonic: float
    received_wall: float
    #: The backend said the deadline has passed. Authoritative: the local
    #: arithmetic can only ever agree with or anticipate it.
    blocked_by_server: bool = False
    platform: Optional[str] = None
    latest_version: Optional[str] = None
    title: Optional[str] = None
    grace_hours: Optional[int] = None
    first_seen_at: Optional[float] = None

    @staticmethod
    def from_payload(
        payload: Optional[Dict[str, Any]], *,
        received_monotonic: Optional[float] = None, received_wall: Optional[float] = None,
    ) -> Optional["UpdateRequirement"]:
        """The requirement an update-check answer carries, or None.

        Only an answer that says `mandatory: true` with a readable required
        version is a requirement. A deadline that cannot be read leaves the
        requirement without one: the user is told the update is required, is
        never blocked by local arithmetic, and the server's own gate decides.
        """
        if not isinstance(payload, dict) or payload.get("mandatory") is not True:
            return None
        required = validate_version(payload.get("required_version") or payload.get("latest_version"))
        if not required.ok:
            return None
        server_time = parse_instant(payload.get("server_time"))
        now_wall = time.time() if received_wall is None else received_wall
        latest = validate_version(payload.get("latest_version"))
        grace = payload.get("grace_hours")
        return UpdateRequirement(
            required_version=required.value,
            deadline_at=parse_instant(payload.get("deadline_at")),
            server_time=server_time if server_time is not None else now_wall,
            received_monotonic=time.monotonic() if received_monotonic is None else received_monotonic,
            received_wall=now_wall,
            blocked_by_server=payload.get("blocked") is True,
            platform=_as_text(payload.get("platform")),
            latest_version=latest.value if latest.ok else None,
            title=_as_text(payload.get("release_title")),
            grace_hours=grace if isinstance(grace, int) and not isinstance(grace, bool) and grace >= 0 else None,
            first_seen_at=parse_instant(payload.get("first_seen_at")),
        )

    # ── Time ──────────────────────────────────────────────────────────────

    def server_now(self, *, monotonic_now: Optional[float] = None, wall_now: Optional[float] = None,
                   restarted: bool = False) -> float:
        """The server's clock, estimated from the moment this was received.

        In the process that received it: `server_time` plus the monotonic
        time elapsed since. After a restart (`restarted`): the wall clock
        elapsed since, never negative, so a clock set backwards cannot push
        the deadline out -- it can only make the estimate conservative.
        """
        if not restarted:
            elapsed = (time.monotonic() if monotonic_now is None else monotonic_now) - self.received_monotonic
            return self.server_time + max(0.0, elapsed)
        elapsed = (time.time() if wall_now is None else wall_now) - self.received_wall
        return self.server_time + max(0.0, elapsed)

    def remaining_seconds(self, **when: Any) -> Optional[int]:
        """Seconds until the deadline on the server's clock; None without one."""
        if self.deadline_at is None:
            return None
        return max(0, int(self.deadline_at - self.server_now(**when)))

    def expired(self, **when: Any) -> bool:
        """Whether the grace period is over: said by the server, or reached
        by the server's clock as estimated here."""
        if self.blocked_by_server:
            return True
        remaining = self.remaining_seconds(**when)
        return remaining is not None and remaining <= 0

    # ── Persistence ───────────────────────────────────────────────────────

    def to_record(self) -> Dict[str, Any]:
        return {
            "required_version": self.required_version,
            "deadline_at": self.deadline_at,
            "server_time": self.server_time,
            "received_wall": self.received_wall,
            "blocked_by_server": self.blocked_by_server,
            "platform": self.platform,
            "latest_version": self.latest_version,
            "title": self.title,
            "grace_hours": self.grace_hours,
            "first_seen_at": self.first_seen_at,
        }

    @staticmethod
    def from_record(record: Any) -> Optional["UpdateRequirement"]:
        """A persisted requirement, read back after a restart. Never raises."""
        if not isinstance(record, dict):
            return None
        required = validate_version(record.get("required_version"))
        if not required.ok:
            return None
        try:
            server_time = float(record.get("server_time"))
            received_wall = float(record.get("received_wall"))
        except (TypeError, ValueError):
            return None
        deadline = record.get("deadline_at")
        grace = record.get("grace_hours")
        first_seen = record.get("first_seen_at")
        return UpdateRequirement(
            required_version=required.value,
            deadline_at=float(deadline) if isinstance(deadline, (int, float)) else None,
            server_time=server_time,
            # A restarted process has no monotonic history for this record;
            # `server_now(restarted=True)` reads the wall clock instead.
            received_monotonic=time.monotonic(),
            received_wall=received_wall,
            blocked_by_server=record.get("blocked_by_server") is True,
            platform=_as_text(record.get("platform")),
            latest_version=_as_text(record.get("latest_version")),
            title=_as_text(record.get("title")),
            grace_hours=grace if isinstance(grace, int) and not isinstance(grace, bool) else None,
            first_seen_at=float(first_seen) if isinstance(first_seen, (int, float)) else None,
        )


class UpdateState:
    """Where the updater is. Exactly one of these at a time."""

    #: Nothing happening. The only state the application starts and rests in.
    IDLE = "IDLE"
    #: An update check is in flight.
    CHECKING = "CHECKING"
    #: The backend named a newer release; the user has not chosen yet.
    UPDATE_AVAILABLE = "UPDATE_AVAILABLE"
    #: Fetching the artifact to a temporary file.
    DOWNLOADING = "DOWNLOADING"
    #: Hashing what was fetched and comparing it to the backend's digest.
    VERIFYING = "VERIFYING"
    #: Verified on disk, waiting to hand off to the installer.
    READY_TO_INSTALL = "READY_TO_INSTALL"
    #: The installer has been launched; this process is on its way out.
    INSTALLING = "INSTALLING"
    #: Something went wrong. The installed application is untouched.
    FAILED = "FAILED"

    ALL = (
        IDLE, CHECKING, UPDATE_AVAILABLE, DOWNLOADING, VERIFYING,
        READY_TO_INSTALL, INSTALLING, FAILED,
    )

    #: States in which no new work may be started. Used by the manual "Check
    #: for updates" action and by the download trigger, so neither can begin a
    #: second operation on top of one already running.
    BUSY = frozenset({CHECKING, DOWNLOADING, VERIFYING, INSTALLING})


#: The only transitions that may occur. Anything else is a bug, and refusing it
#: here is what makes "two downloads at once" impossible rather than unlikely.
_ALLOWED: Dict[str, FrozenSet[str]] = {
    UpdateState.IDLE: frozenset({UpdateState.CHECKING}),
    UpdateState.CHECKING: frozenset({
        # Nothing newer, or the check failed quietly: back to rest.
        UpdateState.IDLE,
        UpdateState.UPDATE_AVAILABLE,
        UpdateState.FAILED,
    }),
    UpdateState.UPDATE_AVAILABLE: frozenset({
        UpdateState.DOWNLOADING,
        # A later check can find the release withdrawn, which returns the
        # updater to rest rather than leaving a stale offer standing.
        UpdateState.IDLE,
        UpdateState.CHECKING,
        UpdateState.FAILED,
    }),
    UpdateState.DOWNLOADING: frozenset({
        UpdateState.VERIFYING,
        UpdateState.FAILED,
        # Shutdown during a download abandons it; the partial file is discarded
        # and nothing about the installation has changed.
        UpdateState.IDLE,
    }),
    UpdateState.VERIFYING: frozenset({
        UpdateState.READY_TO_INSTALL,
        UpdateState.FAILED,
    }),
    UpdateState.READY_TO_INSTALL: frozenset({
        UpdateState.INSTALLING,
        UpdateState.FAILED,
        UpdateState.IDLE,
    }),
    # Terminal in practice: the process is being replaced. FAILED remains
    # reachable because launching the installer can itself fail, and the user
    # must be left with a working application and an explanation.
    UpdateState.INSTALLING: frozenset({UpdateState.FAILED}),
    UpdateState.FAILED: frozenset({
        UpdateState.IDLE,
        # A retry starts from a fresh check rather than from the old answer,
        # so a failure caused by a withdrawn release does not retry forever
        # against a URL that is gone.
        UpdateState.CHECKING,
    }),
}


def can_transition(current: str, target: str) -> bool:
    """Whether moving from `current` to `target` is legal."""
    if current == target:
        # Re-entering the same state is a no-op, never an error: a service may
        # legitimately report the state it is already in.
        return True
    return target in _ALLOWED.get(current, frozenset())


@dataclass(frozen=True)
class ReleaseInfo:
    """A validated description of one release, safe to act on.

    Constructed only through :meth:`from_payload`, which is the trust boundary:
    everything below has already been checked, so the downloader and the
    installer never have to ask whether a URL is really a URL.

    Frozen because it is read from more than one thread — the service thread
    fills it in, the GUI thread renders it — and an immutable value needs no
    lock to be safe.
    """

    version: str
    download_url: str
    sha256: str
    file_size: Optional[int] = None
    release_notes: Optional[str] = None
    release_notes_url: Optional[str] = None
    force_update: bool = False
    platform: Optional[str] = None
    architecture: Optional[str] = None
    #: The release-management policy, as the backend stated it. `mandatory`
    #: is true from the first encounter (the user may still postpone until
    #: `deadline_at`); `blocked` is true once the backend has stopped
    #: answering this client's protected requests. `force_update` above is
    #: what a backend without grace periods says and means "blocked".
    mandatory: bool = False
    required_version: Optional[str] = None
    title: Optional[str] = None
    deadline_at: Optional[float] = None
    blocked: bool = False

    @property
    def installable(self) -> bool:
        """Whether this release can be downloaded and installed, not merely
        announced. Always True for an instance that exists — the constructor
        refuses anything else — and named so callers read as intent."""
        return bool(self.download_url and self.sha256)

    @staticmethod
    def from_payload(payload: Optional[Dict[str, Any]]) -> Optional["ReleaseInfo"]:
        """Build a release from the backend's answer, or None if it cannot be.

        Returns None rather than raising, and returns None for *any* shortfall:
        a missing checksum, a URL that is not absolute http(s), a version that
        is not `major.minor.patch`. None means "announce this if you like, but
        do not install it", which is exactly the right outcome for a malformed
        response — the alternative is a client that downloads and executes
        whatever an unexpected payload happened to contain.

        This is also what keeps an older deployment working during a rollout:
        it answers without `sha256`, so the client announces the update and
        links to the download page, which is precisely the previous behaviour.
        """
        if not isinstance(payload, dict):
            return None
        if not payload.get("update_available"):
            return None

        version = validate_version(payload.get("latest_version"))
        if not version.ok:
            return None

        # An absolute http(s) URL and nothing else. `validate_url` refuses
        # `javascript:`, `data:` and `file:`, which is the difference between
        # fetching an artifact and being told what to execute.
        url = validate_url(payload.get("download_url"), required=True)
        if not url.ok:
            return None
        # Where this build is willing to fetch an executable from is decided by
        # this build (`policy`), not by the backend: https, an approved host, no
        # credentials. A URL outside that is announce-only -- never installable.
        host_problem = policy.check_url(url.value)
        if host_problem:
            log.warning("update release %s is not installable: %s", version.value, host_problem)
            return None

        digest = validate_sha256(payload.get("sha256"))
        if not digest.ok:
            # No digest means no install. The caller still has the version and
            # may announce it; it simply has nothing to verify against, and
            # running an unverified artifact is the one thing this must not do.
            return None

        size = payload.get("file_size")
        if not isinstance(size, int) or size <= 0:
            size = None

        notes_url = validate_url(payload.get("release_notes_url"))

        return ReleaseInfo(
            version=version.value,
            download_url=url.value,
            sha256=digest.value,
            file_size=size,
            release_notes=_as_text(payload.get("release_notes")),
            release_notes_url=notes_url.value if notes_url.ok else None,
            # Coerced rather than trusted: a non-boolean here decides whether a
            # person can keep using the application, so anything that is not
            # literally True is False.
            force_update=payload.get("force_update") is True,
            platform=_as_text(payload.get("platform")),
            architecture=_as_text(payload.get("architecture")),
            mandatory=payload.get("mandatory") is True or payload.get("force_update") is True,
            required_version=(
                validate_version(payload.get("required_version")).value
                if validate_version(payload.get("required_version")).ok else None
            ),
            title=_as_text(payload.get("release_title")),
            deadline_at=parse_instant(payload.get("deadline_at")),
            blocked=payload.get("blocked") is True or payload.get("force_update") is True,
        )


def _as_text(value: Any) -> Optional[str]:
    """A trimmed string, or None. Never a repr of something unexpected."""
    if not isinstance(value, str):
        return None
    trimmed = value.strip()
    return trimmed or None

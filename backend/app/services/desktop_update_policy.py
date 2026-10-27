"""The mandatory-update policy: who must update, by when, and what is refused.

This is the one place that answers three questions for a desktop client:

1. **Is there an active mandatory policy for the platform it runs on?** --
   :func:`active_policies`. A published, mandatory version record whose
   ``effective_at`` has passed, targeting that platform, *with an installable
   published artifact at or above its floor*. The last condition is what makes
   withdrawal safe: a floor nobody can reach is never enforced, so withdrawing
   a bad release suspends enforcement instead of locking people out with no
   way forward. The grace-period rows stay, and a corrective release with the
   same floor re-arms them with their original deadline.

2. **Where is this user in their grace period?** -- :func:`evaluate`. The
   first time the server sees a user's desktop below an active floor it files
   one ``desktop_update_requirements`` row, stamped with the server's clock and
   the policy's grace. Every later encounter -- a repeated check, another
   device, a restart, a changed system clock -- reads that row. The deadline is
   never moved. "Satisfied" is written by the server when the server itself
   observes the client at or above the floor.

3. **May this request proceed?** -- :func:`enforce`, called from
   ``get_current_user`` for every authenticated desktop request. Past the
   deadline a client below the floor is refused with HTTP 426 and a structured
   body naming the required version, the platform and the deadline. A short
   allow-list of recovery paths stays open (signing in, the update endpoints,
   stopping and heartbeating a timer, resolving idle time, uploading what was
   already captured, the client event trail) so an outdated client can finish
   its work and update, and nothing else.

Everything compares server time with server time. The client's clock plays no
part in any deadline.

Cost. Current clients -- the normal case -- cost nothing: the active policies
are cached for ``POLICY_CACHE_TTL_SECONDS``, and a client at or above every
floor never touches the requirements table from the gate. Only an outdated
client under an active policy reads (and once writes) one row.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.core.request_context import CLIENT_DESKTOP, current_request_context
from app.models.desktop_release_version import (
    REQUIREMENT_PLATFORM_UNKNOWN, TARGETABLE_PLATFORMS, DesktopReleaseVersion,
    DesktopUpdateRequirement,
)
from app.repositories.desktop_release import DesktopReleaseRepository
from app.repositories.desktop_release_version import DesktopReleaseVersionRepository
from app.services import desktop_release_policy as hosting

logger = logging.getLogger(__name__)

#: How long the set of active policies is reused before being re-read. A
#: publish or a withdrawal invalidates it at once in this process; another
#: worker sees the change within this many seconds.
POLICY_CACHE_TTL_SECONDS = 30.0

#: The structured error code a refused request carries. A client that
#: understands it opens its update flow; one that does not shows the message.
UPGRADE_REQUIRED_CODE = "desktop_update_required"

#: Paths (after the optional ``/api/v1`` prefix) an outdated client may still
#: call after its deadline. Each is either the way to recover (sign in, learn
#: the policy, fetch the release, report the install) or the way to finish
#: work already begun without corrupting it (stop, heartbeat, idle time, the
#: uploads of what was captured, the client event trail). Nothing here starts
#: new work or reads the user's data.
EXEMPT_PATH_PATTERNS: Tuple[re.Pattern, ...] = tuple(re.compile(p) for p in (
    r"^/auth(/|$)",
    r"^/health(/|$)",
    r"^/desktop/latest-version$",
    r"^/desktop/update-policy$",
    r"^/desktop/update-reports$",
    r"^/desktop/releases/(latest|downloads|download)$",
    r"^/system/maintenance-status$",
    r"^/time-entries/active$",
    r"^/time-entries/\d+/(stop|heartbeat|screenshots|unwanted-activity|adjustments)$",
    r"^/time-entries/\d+/activity/batch$",
    r"^/time-entries/\d+/app-usage(/batch|/summary)?$",
    r"^/time-entry-activities/batch$",
    r"^/url-usage/batch$",
    r"^/time-entry-screenshots/capture-events$",
    r"^/idle-periods(/|$)",
    r"^/activity-logs/client-events$",
    # The two reads a running session needs to keep capturing correctly: the
    # organisation's screenshot exclusions (the capture gate holds and retries
    # without them -- measured in the release E2E, a refused read here held
    # every capture of a timer the policy had left running) and the capture
    # cadence. Reads of the user's own data they are not.
    r"^/screenshot/privacy-config$",
    r"^/screenshots/config$",
))

_API_PREFIX = "/api/v1"


@dataclass(frozen=True)
class ActivePolicy:
    """One platform's mandatory policy, as it stands right now."""

    platform: str
    #: The version record imposing the floor.
    record_id: int
    version: str
    title: Optional[str]
    #: The oldest version allowed to keep working on this platform.
    floor: str
    floor_tuple: Tuple[int, int, int]
    grace_hours: int
    #: The newest installable published artifact version for the platform.
    installable_version: str


@dataclass
class UpdateVerdict:
    """Where one client stands under the active policy."""

    required: bool = False
    blocked: bool = False
    platform: Optional[str] = None
    required_version: Optional[str] = None
    release_version: Optional[str] = None
    release_title: Optional[str] = None
    grace_hours: Optional[int] = None
    first_seen_at: Optional[datetime] = None
    deadline_at: Optional[datetime] = None
    server_time: Optional[datetime] = None
    #: Whether a grace row was created by this very call.
    created: bool = False

    @property
    def grace_active(self) -> bool:
        return self.required and not self.blocked

    @property
    def remaining_seconds(self) -> Optional[int]:
        if not self.required or self.deadline_at is None or self.server_time is None:
            return None
        return max(0, int((self.deadline_at - self.server_time).total_seconds()))


# ── Time ───────────────────────────────────────────────────────────────────


def now_utc() -> datetime:
    """The server's clock. A function so tests can move it."""
    return datetime.now(timezone.utc)


def aware(value: Optional[datetime]) -> Optional[datetime]:
    """A timezone-aware UTC datetime. SQLite hands timestamps back naive."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


# ── Policy ─────────────────────────────────────────────────────────────────


def floor_for(record: DesktopReleaseVersion) -> Optional[str]:
    """The floor a version record imposes, or None when it imposes none.

    Mandatory: the explicit floor, else the version itself. Not mandatory: only
    an explicit floor counts -- "clients below X must take this" without
    forcing everyone else.
    """
    explicit = (record.required_min_version or "").strip() or None
    if record.mandatory:
        return explicit or record.version
    return explicit


_cache_lock = threading.Lock()
_cache: Dict[str, object] = {"at": 0.0, "value": None}


def invalidate_policy_cache() -> None:
    with _cache_lock:
        _cache["at"] = 0.0
        _cache["value"] = None


@dataclass(frozen=True)
class _Candidate:
    """A published, mandatory-or-floored version record, before the time
    and installability tests that depend on *when* it is asked about."""

    policy: ActivePolicy
    effective_at: Optional[datetime]


@dataclass(frozen=True)
class _PolicyInputs:
    """What the database says, read once and reused for the cache TTL. The
    selection below is pure and runs against the caller's `now` every time,
    so a release that becomes effective in the future is not frozen out (or
    in) by the moment the cache was filled."""

    candidates: Dict[str, Tuple[_Candidate, ...]]
    #: platform -> (version, version tuple) of the newest *installable*
    #: published artifact, or None when there is none.
    installable: Dict[str, Optional[Tuple[str, Tuple[int, int, int]]]]


def active_policies(db: Session, now: Optional[datetime] = None) -> Dict[str, Optional[ActivePolicy]]:
    """platform -> the policy that holds for it at `now`, or None."""
    with _cache_lock:
        cached = _cache["value"]
        fresh = cached is not None and time.monotonic() - float(_cache["at"]) < POLICY_CACHE_TTL_SECONDS
    inputs = cached if fresh else None
    if inputs is None:
        inputs = _load_policy_inputs(db)
        with _cache_lock:
            _cache["value"] = inputs
            _cache["at"] = time.monotonic()
    return _select_policies(inputs, now or now_utc())  # type: ignore[arg-type]


def _load_policy_inputs(db: Session) -> _PolicyInputs:
    from app.services.desktop_release import version_tuple

    records = DesktopReleaseVersionRepository.list_published(db)
    candidates: Dict[str, Tuple[_Candidate, ...]] = {}
    installable: Dict[str, Optional[Tuple[str, Tuple[int, int, int]]]] = {}
    for platform in TARGETABLE_PLATFORMS:
        found: List[_Candidate] = []
        for record in records:
            if platform not in record.targets:
                continue
            floor = floor_for(record)
            floor_tuple = version_tuple(floor)
            if floor is None or floor_tuple is None:
                continue
            found.append(_Candidate(
                policy=ActivePolicy(
                    platform=platform, record_id=record.id, version=record.version,
                    title=record.title, floor=floor, floor_tuple=floor_tuple,
                    grace_hours=int(record.grace_hours), installable_version="",
                ),
                effective_at=aware(record.effective_at),
            ))
        candidates[platform] = tuple(found)
        newest = DesktopReleaseRepository.latest_published(db, platform=platform)
        newest_tuple = version_tuple(newest.version) if newest is not None else None
        if newest is None or newest_tuple is None or hosting.check_download_url(newest.download_url):
            installable[platform] = None
        else:
            installable[platform] = (newest.version, newest_tuple)
    return _PolicyInputs(candidates=candidates, installable=installable)


def _select_policies(inputs: _PolicyInputs, now: datetime) -> Dict[str, Optional[ActivePolicy]]:
    from app.services.desktop_release import version_tuple

    result: Dict[str, Optional[ActivePolicy]] = {}
    for platform in TARGETABLE_PLATFORMS:
        best: Optional[ActivePolicy] = None
        best_key: Optional[Tuple] = None
        for candidate in inputs.candidates.get(platform, ()):
            if candidate.effective_at is not None and candidate.effective_at > now:
                continue
            policy = candidate.policy
            key = (policy.floor_tuple, version_tuple(policy.version) or (0, 0, 0))
            if best_key is not None and key <= best_key:
                continue
            best_key, best = key, policy
        if best is None:
            result[platform] = None
            continue
        # A floor nobody can reach is not enforced. This is what makes a
        # withdrawal safe: with the release gone there is nothing to install,
        # so nothing is refused, and the grace rows wait for a corrective
        # release that carries the same floor.
        newest = inputs.installable.get(platform)
        if newest is None or newest[1] < best.floor_tuple:
            logger.info(
                "DESKTOP_UPDATE_POLICY_SUSPENDED platform=%s floor=%s reason=%s",
                platform, best.floor, "no-installable-release" if newest is None else "below-floor",
            )
            result[platform] = None
            continue
        result[platform] = ActivePolicy(
            platform=best.platform, record_id=best.record_id, version=best.version,
            title=best.title, floor=best.floor, floor_tuple=best.floor_tuple,
            grace_hours=best.grace_hours, installable_version=newest[0],
        )
    return result


def policy_platform(platform_header: Optional[str]) -> Optional[str]:
    """``win32`` / ``darwin`` from what the client sent, or None if neither."""
    from app.services.desktop_release import normalize_platform

    token = normalize_platform(platform_header)
    return token if token in TARGETABLE_PLATFORMS else None


# ── Verdict ────────────────────────────────────────────────────────────────


def evaluate(
    db: Session,
    user,
    client_version: Optional[str],
    platform_header: Optional[str],
    *,
    now: Optional[datetime] = None,
    settle: bool = False,
) -> UpdateVerdict:
    """Where ``user``'s desktop stands under the active policy.

    Creates the grace row on the first encounter (never later). With ``settle``
    (the update-check path) a client observed at or above a floor has its open
    requirement rows marked satisfied -- the server's own observation, not the
    client's claim.

    A client that did not say what it runs on, or named a platform this
    deployment does not ship, is judged against every platform's policy and
    filed under ``unknown``: changing the claim cannot buy a way around a
    policy.
    """
    from app.services.desktop_release import version_tuple

    moment = now or now_utc()
    verdict = UpdateVerdict(server_time=moment)
    client = version_tuple(client_version)
    if client is None:
        # An unreadable version is not evidence of anything; refusing on it
        # would take a fleet offline over a header. Logged, never enforced.
        return verdict

    policies = active_policies(db, moment)
    platform = policy_platform(platform_header)
    if platform is not None:
        candidates = [p for p in (policies.get(platform),) if p is not None]
        key_platform = platform
    else:
        candidates = [p for p in policies.values() if p is not None]
        key_platform = REQUIREMENT_PLATFORM_UNKNOWN
    verdict.platform = key_platform

    owed = [p for p in candidates if client < p.floor_tuple]
    if not owed:
        if settle:
            _settle(db, user.id, key_platform, client, client_version, moment)
        return verdict

    policy = max(owed, key=lambda p: p.floor_tuple)

    # A deadline already running for this floor is the deadline, whichever
    # platform it was filed under. A desktop that stops naming its platform
    # (or names one this deployment does not ship) after being seen on
    # Windows is the same desktop with the same deadline, and a desktop first
    # seen with no platform that later says `win32` is too. Only a genuinely
    # separate platform of the same user -- a Mac beside a Windows machine --
    # gets a grace period of its own.
    related = DesktopReleaseVersionRepository.list_requirements_for_floor(
        db, user_id=user.id, required_version=policy.floor,
    )
    considered = [
        r for r in related
        if r.platform == key_platform
        or r.platform == REQUIREMENT_PLATFORM_UNKNOWN
        or key_platform == REQUIREMENT_PLATFORM_UNKNOWN
    ]
    if considered:
        row = min(considered, key=lambda r: aware(r.deadline_at))
        created = False
    else:
        row, created = DesktopReleaseVersionRepository.create_requirement_if_absent(
            db,
            user_id=user.id,
            platform=key_platform,
            required_version=policy.floor,
            required_tuple=policy.floor_tuple,
            release_version_id=policy.record_id,
            first_seen_at=moment,
            deadline_at=moment + timedelta(hours=policy.grace_hours),
            client_version=client_version,
        )
    if created:
        db.commit()
        logger.info(
            "DESKTOP_UPDATE_GRACE_STARTED user=%s platform=%s floor=%s deadline=%s client=%s",
            user.id, key_platform, policy.floor, row.deadline_at.isoformat(), client_version,
        )
    deadline = aware(row.deadline_at)
    verdict.required = True
    verdict.created = created
    verdict.required_version = policy.floor
    verdict.release_version = policy.installable_version
    verdict.release_title = policy.title
    verdict.grace_hours = policy.grace_hours
    verdict.first_seen_at = aware(row.first_seen_at)
    verdict.deadline_at = deadline
    verdict.blocked = deadline is not None and moment >= deadline
    return verdict


def _settle(
    db: Session, user_id: int, platform: str,
    client: Tuple[int, int, int], client_version: Optional[str], moment: datetime,
) -> None:
    """Mark every open requirement this client now meets as satisfied."""
    rows = DesktopReleaseVersionRepository.list_unsatisfied_for_user(
        db, user_id=user_id, platform=platform,
    )
    changed = False
    for row in rows:
        floor = (row.required_major, row.required_minor, row.required_patch)
        if client >= floor:
            row.satisfied_at = moment
            row.satisfied_version = client_version
            changed = True
            logger.info(
                "DESKTOP_UPDATE_REQUIREMENT_SATISFIED user=%s platform=%s floor=%s client=%s",
                user_id, platform, row.required_version, client_version,
            )
    if changed:
        db.commit()


# ── The gate ───────────────────────────────────────────────────────────────


def strip_api_prefix(path: Optional[str]) -> str:
    value = path or ""
    if value.startswith(_API_PREFIX + "/") or value == _API_PREFIX:
        value = value[len(_API_PREFIX):] or "/"
    return value


def is_exempt_path(path: Optional[str]) -> bool:
    candidate = strip_api_prefix(path)
    return any(pattern.match(candidate) for pattern in EXEMPT_PATH_PATTERNS)


def upgrade_required_detail(verdict: UpdateVerdict, installed_version: Optional[str]) -> dict:
    """The body of a 426, in one documented shape."""
    required = verdict.required_version or "?"
    return {
        "code": UPGRADE_REQUIRED_CODE,
        "message": (
            f"Monitra {required} is required and the update deadline has passed. "
            "Install the update to continue."
        ),
        "update_required": True,
        "required_version": verdict.required_version,
        "latest_version": verdict.release_version,
        "installed_version": installed_version,
        "platform": verdict.platform,
        "grace_active": verdict.grace_active,
        "deadline_at": verdict.deadline_at.isoformat() if verdict.deadline_at else None,
        "first_seen_at": verdict.first_seen_at.isoformat() if verdict.first_seen_at else None,
        "server_time": verdict.server_time.isoformat() if verdict.server_time else None,
        "policy_url": "/desktop/update-policy",
    }


def enforce(db: Session, user) -> None:
    """Refuse a protected request from an outdated desktop past its deadline.

    Called by ``get_current_user`` once the caller is known. Anything that is
    not a desktop client, any exempt path, a service principal, and any
    failure inside the evaluation itself all let the request through: the
    gate may refuse a request only on a policy it could actually read.
    """
    context = current_request_context()
    if context is None or context.client != CLIENT_DESKTOP:
        return
    if getattr(user, "is_service_principal", False):
        return
    if is_exempt_path(context.path):
        return
    try:
        verdict = evaluate(db, user, context.client_version, context.platform)
    except Exception:  # noqa: BLE001 - the gate fails open, never closed
        logger.exception("DESKTOP_UPDATE_GATE_ERROR user=%s path=%s", getattr(user, "id", None), context.path)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return
    if not verdict.blocked:
        return
    logger.info(
        "DESKTOP_UPDATE_REFUSED user=%s path=%s client=%s platform=%s floor=%s deadline=%s",
        user.id, context.path, context.client_version, verdict.platform,
        verdict.required_version, verdict.deadline_at.isoformat() if verdict.deadline_at else None,
    )
    raise HTTPException(
        status_code=status.HTTP_426_UPGRADE_REQUIRED,
        detail=upgrade_required_detail(verdict, context.client_version),
    )


def requirement_summary(
    rows: List[DesktopUpdateRequirement], now: Optional[datetime] = None,
) -> Dict[str, int]:
    """pending / expired / satisfied counts for a set of grace rows."""
    moment = now or now_utc()
    summary = {"pending": 0, "expired": 0, "satisfied": 0}
    for row in rows:
        if row.satisfied_at is not None:
            summary["satisfied"] += 1
        elif aware(row.deadline_at) is not None and moment >= aware(row.deadline_at):
            summary["expired"] += 1
        else:
            summary["pending"] += 1
    return summary

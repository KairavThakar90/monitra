"""Desktop release policy: what a client should install, and who is on what.

Three responsibilities:

1. **Answer the desktop's update check.** Given what the caller is running and
   the machine it is running on, decide whether a newer *compatible* build
   exists and whether taking it is mandatory. Every comparison rule in this
   system lives here, in one place, so a client never has to decide for itself
   whether it is out of date.

2. **Answer the website's download button.** The same lookup, unauthenticated
   and narrowed, so the download link is always the current release rather than
   a versioned URL that ages badly.

3. **Record which version each user is running**, for fleet visibility. This
   happens on the same authenticated request, so it costs nothing extra.

Where releases come from
------------------------
The ``desktop_releases`` table is the source of truth. The three configuration
values that used to be the whole answer (``DESKTOP_LATEST_VERSION`` and
friends) are kept as a **fallback**, and only as one: a deployment that has
registered no releases yet still answers exactly as it did before this table
existed. That is deliberate backward compatibility, not a second mechanism --
as soon as one published row exists for the platform, the table wins and the
configuration is ignored.

Withdrawal still works the way the release runbook says it does. Moving a row
off ``published`` removes it from every lookup here immediately, and clearing
``DESKTOP_LATEST_VERSION`` still withdraws a configuration-only answer.
"""
from __future__ import annotations

import logging
import re
from collections import Counter
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import List, Optional, Tuple

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.activity_log import ActivityLog, ActivityLogAction, ActivityLogModule
from app.models.desktop_release import DesktopRelease, Platform, ReleaseStatus
from app.models.desktop_release_version import (
    TARGETABLE_PLATFORMS, DesktopReleaseVersion, DesktopUpdateReport,
    ReleaseVersionStatus, UpdateReportOutcome,
)
from app.models.user import User
from app.repositories.activity_log import ActivityLogRepository
from app.repositories.desktop_client_version import DesktopClientVersionRepository
from app.repositories.desktop_release import DesktopReleaseRepository
from app.repositories.desktop_release_version import DesktopReleaseVersionRepository
from app.repositories.user import UserRepository
from app.schemas.desktop_release import (
    DesktopClientVersionRead, DesktopReleaseCreate, DesktopReleaseRead,
    DesktopReleaseUpdate, FleetVersionsResponse, GraceStatus, LatestVersionResponse,
    PublicReleaseIndexResponse, PublicReleaseResponse, ReleaseArtifactInput,
    ReleaseAuditEntry, ReleaseProblemRead, ReleaseVersionCreate,
    ReleaseVersionDetail, ReleaseVersionRead, ReleaseVersionStats,
    ReleaseVersionUpdate, UpdatePolicyResponse, UpdateReportCreate,
)
from app.services import desktop_release_policy as policy
from app.services import desktop_update_policy as update_policy

logger = logging.getLogger(__name__)

#: `Monitra/1.0.1` — the identity `desktop/version.py:user_agent()` builds.
#: Anything else (a browser, curl, the React frontend) is not a desktop client
#: and is not recorded.
#:
#: The whole version component must be `major.minor.patch` and nothing else:
#: `desktop/version.py` guarantees that shape, so `Monitra/1.0.0-rc1` is a
#: client this deployment does not recognise, not a 1.0.0 with a suffix to be
#: quietly discarded.
_USER_AGENT_RE = re.compile(r"^Monitra/(\d+\.\d+\.\d+)(?:\s|$)")

_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")


class ReleaseConflict(ValueError):
    """The request conflicts with what is already registered (HTTP 409)."""


class ReleasePolicyViolation(ValueError):
    """The release breaks a rule that must hold before a client may be offered it
    (HTTP 422). Carries every problem found, so one call reports them all.
    """

    def __init__(self, problems: List[policy.Problem], message: Optional[str] = None) -> None:
        self.problems = list(problems)
        super().__init__(message or "; ".join(p.message for p in self.problems))


def parse_client_version(user_agent: Optional[str]) -> Optional[str]:
    """Extract the Monitra version from a User-Agent header, if it is one."""
    if not user_agent:
        return None
    match = _USER_AGENT_RE.match(user_agent.strip())
    return match.group(1) if match else None


def version_tuple(version: Optional[str]) -> Optional[Tuple[int, int, int]]:
    """Parse `major.minor.patch` into a comparable tuple, or None.

    Strict on purpose. The desktop's own `version.py` guarantees this exact
    shape, so anything else is a client we cannot reason about — and comparing
    an unparseable version numerically would be a guess, which is how a fleet
    ends up being told to "update" to something older.
    """
    if not version or not _VERSION_RE.match(version.strip()):
        return None
    return tuple(int(part) for part in version.strip().split("."))  # type: ignore[return-value]


def normalize_platform(value: Optional[str]) -> Optional[str]:
    """Map what a client reported onto a platform token we store.

    `sys.platform` on Windows is `win32` on both 32- and 64-bit Python, and on
    macOS it is `darwin`; Linux reports `linux` (or historically `linux2`).
    Anything else is left alone and simply will not match a release, which is
    the right outcome: an unknown platform must find nothing rather than fall
    through to somebody else's artifact.
    """
    if not value:
        return None
    token = value.strip().lower()
    if not token:
        return None
    if token.startswith("linux"):
        return Platform.LINUX
    if token in ("win32", "win64", "windows", "cygwin", "msys"):
        return Platform.WINDOWS
    if token in ("darwin", "macos", "mac", "osx"):
        return Platform.MACOS
    return token


def normalize_architecture(value: Optional[str]) -> Optional[str]:
    """Fold the several spellings each CPU family answers to.

    `platform.machine()` says `AMD64` on Windows and `x86_64` on macOS for the
    same processor, and `arm64` on Apple Silicon where Linux says `aarch64`.
    Left unfolded, an arm64 Mac asking for `arm64` would miss a build
    registered as `aarch64` and silently fall back to a build for a different
    chip.
    """
    if not value:
        return None
    token = value.strip().lower()
    if token in ("amd64", "x86_64", "x64", "em64t"):
        return "x86_64"
    if token in ("arm64", "aarch64"):
        return "arm64"
    return token or None


class DesktopReleaseService:

    # ── Configuration fallback ────────────────────────────────────────────

    @staticmethod
    def configured_latest_version() -> Optional[str]:
        """The configured latest version, or None if this deployment has none.

        Only consulted when the release table has nothing published; see the
        module docstring.
        """
        configured = (settings.DESKTOP_LATEST_VERSION or "").strip()
        if not configured:
            return None
        if version_tuple(configured) is None:
            # Misconfiguration must not be served to clients as if it were a
            # release. Log it and answer "unknown" instead.
            logger.warning(
                "DESKTOP_LATEST_VERSION=%r is not a major.minor.patch version; "
                "reporting no known release", configured,
            )
            return None
        return configured

    @staticmethod
    def is_update_available(client_version: Optional[str], latest: Optional[str]) -> bool:
        """True only when `latest` is strictly newer than `client_version`.

        An unknown or unparseable client version yields False: a client that
        did not identify itself is not evidence that it is out of date, and
        prompting it would be prompting on no information.
        """
        client = version_tuple(client_version)
        newest = version_tuple(latest)
        if client is None or newest is None:
            return False
        return newest > client

    @staticmethod
    def is_below_minimum(
        client_version: Optional[str], minimum: Optional[str]
    ) -> bool:
        """True when the caller is older than the deployment's floor.

        Same conservatism as above, and for a sharper reason: this is what
        locks a user out of their own application. A version that cannot be
        parsed, or an unset floor, is never grounds for that — an unreadable
        answer must fail *open*, or a single malformed field takes a fleet
        offline.
        """
        client = version_tuple(client_version)
        floor = version_tuple(minimum)
        if client is None or floor is None:
            return False
        return client < floor

    # ── Fleet visibility ──────────────────────────────────────────────────

    @staticmethod
    def record_client_version(
        db: Session,
        current_user: User,
        client_version: Optional[str],
        platform: Optional[str],
    ) -> None:
        """Store the caller's desktop version for fleet visibility.

        Never raises into the request: version visibility is diagnostics, and
        failing an update check because a diagnostics write failed would be a
        worse outcome than not knowing which build someone is on.
        """
        if not client_version or version_tuple(client_version) is None:
            return
        try:
            DesktopClientVersionRepository.upsert(
                db,
                organization_id=current_user.organization_id,
                user_id=current_user.id,
                app_version=client_version,
                platform=(platform or None),
            )
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
            logger.exception("could not record desktop client version for user %s",
                             current_user.id)

    # ── The update check ──────────────────────────────────────────────────

    @staticmethod
    def latest_version(
        db: Session,
        current_user: User,
        client_version: Optional[str],
        platform: Optional[str] = None,
        architecture: Optional[str] = None,
    ) -> LatestVersionResponse:
        """Answer one desktop client's update check.

        The shape returned is the one production clients already parse. Older
        builds read `latest_version`, `download_url` and `update_available` and
        ignore everything else, so they keep announcing updates exactly as they
        did; newer builds additionally get the checksum and policy fields they
        need to install one.
        """
        platform_token = normalize_platform(platform)
        arch_token = normalize_architecture(architecture)

        DesktopReleaseService.record_client_version(
            db, current_user, client_version, platform_token
        )

        release = (
            DesktopReleaseRepository.latest_published(
                db, platform=platform_token, architecture=arch_token
            )
            if platform_token
            else None
        )

        if release is None:
            # No artifact this client can install. Fall back to the
            # configuration answer, which is announcement-only: it has no
            # checksum, so a client that receives it will tell the user an
            # update exists and stop there rather than downloading anything.
            return DesktopReleaseService._configured_answer(client_version)

        url_problem = policy.check_download_url(release.download_url)
        if url_problem:
            # A published row this deployment would no longer accept -- written
            # before the hosting policy existed, or by hand. It is not served:
            # the client would fetch and run whatever is at that address, so the
            # honest answer is "nothing to offer", loudly in the log.
            logger.error(
                "DESKTOP_RELEASE_URL_REFUSED: published %s %s/%s is not served: %s",
                release.version, release.platform, release.architecture or "any",
                url_problem,
            )
            return LatestVersionResponse(client_version=client_version)

        latest = release.version
        available = DesktopReleaseService.is_update_available(client_version, latest)
        record = DesktopReleaseService._version_record(db, release.version)

        # Mandatory only when a *newer* build exists to take. A release marked
        # mandatory does not make a client already running it — or running
        # something newer — mandatory to update; there would be nothing to
        # update to, and the user would be locked out of a current install.
        #
        # The version record's policy is the authority (with its grace
        # period); the artifact row's own flags are the fallback for a row
        # registered before version records existed.
        legacy_forced = bool(available) and (
            bool(release.force_update)
            or DesktopReleaseService.is_below_minimum(
                client_version, release.min_supported_version
            )
        )
        try:
            verdict = update_policy.evaluate(
                db, current_user, client_version, platform_token, settle=True,
            )
        except Exception:  # noqa: BLE001
            # The update check must never fail -- and never lock anyone out --
            # because the policy could not be read. Answered as "not required",
            # loudly in the log; the gate makes the same choice.
            logger.exception("DESKTOP_UPDATE_POLICY_ERROR user=%s", current_user.id)
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
            verdict = update_policy.UpdateVerdict(server_time=update_policy.now_utc())
        mandatory = bool(available) and (verdict.required or (record is None and legacy_forced))
        # What a client that predates grace periods reads. It understands
        # `force_update` as "lock out now", so it is told that only once the
        # deadline has passed -- before that it gets an ordinary offer, which
        # is the closest it can render to "you may postpone until then".
        forced = bool(available) and (
            verdict.blocked if verdict.required else (record is None and legacy_forced)
        )

        sha256, file_size = release.sha256, release.file_size
        if available and DesktopReleaseService._cannot_self_update(client_version):
            # This client's own updater is not one to send an install through
            # (see DESKTOP_AUTO_UPDATE_MIN_CLIENT_VERSION). It is still told an
            # update exists and where to get it; without a checksum its updater
            # announces and stops, and the user installs by hand. The policy
            # itself is unchanged: the gate still enforces the deadline, and the
            # screen then says to install by hand from the link.
            sha256, file_size = None, None
            if record is None:
                forced = False

        notes = (record.release_notes if record is not None and record.release_notes else None) \
            or release.release_notes
        return LatestVersionResponse(
            latest_version=latest,
            download_url=release.download_url,
            release_notes_url=release.release_notes_url,
            update_available=available,
            client_version=client_version,
            sha256=sha256,
            file_size=file_size,
            release_notes=notes,
            force_update=forced,
            min_supported_version=(
                verdict.required_version if verdict.required else release.min_supported_version
            ),
            platform=release.platform,
            architecture=release.architecture,
            mandatory=mandatory,
            required_version=verdict.required_version if verdict.required else None,
            release_title=(record.title if record is not None else None),
            grace_hours=verdict.grace_hours if verdict.required else (
                int(record.grace_hours) if record is not None and record.mandatory else None
            ),
            first_seen_at=verdict.first_seen_at if verdict.required else None,
            deadline_at=verdict.deadline_at if verdict.required else None,
            grace_active=verdict.grace_active,
            blocked=verdict.blocked,
            server_time=verdict.server_time,
        )

    @staticmethod
    def _cannot_self_update(client_version: Optional[str]) -> bool:
        """True for a client older than `DESKTOP_AUTO_UPDATE_MIN_CLIENT_VERSION`.

        Fails open, like every comparison here: an unset floor, or a client that
        did not identify itself, is not grounds to withhold anything.
        """
        floor = settings.DESKTOP_AUTO_UPDATE_MIN_CLIENT_VERSION
        floor = floor.strip() if isinstance(floor, str) else ""
        return bool(floor) and DesktopReleaseService.is_below_minimum(client_version, floor)

    @staticmethod
    def _configured_answer(client_version: Optional[str]) -> LatestVersionResponse:
        """The pre-release-table answer, for a deployment with no rows yet."""
        latest = DesktopReleaseService.configured_latest_version()
        configured_url = (settings.DESKTOP_DOWNLOAD_URL or None) if latest else None
        if configured_url and policy.check_download_url(configured_url):
            # Same host policy as a registered row: the client opens this link in
            # a browser, and an unapproved address is not something to hand out.
            logger.warning("DESKTOP_DOWNLOAD_URL is not an approved release location; not served")
            configured_url = None
        return LatestVersionResponse(
            latest_version=latest,
            download_url=configured_url,
            release_notes_url=(settings.DESKTOP_RELEASE_NOTES_URL or None) if latest else None,
            update_available=DesktopReleaseService.is_update_available(
                client_version, latest
            ),
            client_version=client_version,
            # No checksum is knowable from configuration, so no client may
            # install from this answer. That is the intended limit of the
            # fallback, not an omission.
            sha256=None,
            force_update=False,
        )

    @staticmethod
    def fleet_versions(db: Session, current_user: User) -> FleetVersionsResponse:
        rows = DesktopClientVersionRepository.list_for_organization(
            db, current_user.organization_id
        )
        newest = DesktopReleaseRepository.latest_published_any_platform(db)
        return FleetVersionsResponse(
            latest_version=(
                newest.version if newest is not None
                else DesktopReleaseService.configured_latest_version()
            ),
            counts=dict(Counter(row.app_version for row in rows)),
            clients=[DesktopClientVersionRead.model_validate(row) for row in rows],
        )

    # ── The public download ───────────────────────────────────────────────

    @staticmethod
    def public_latest(
        db: Session,
        *,
        platform: Optional[str],
        architecture: Optional[str] = None,
    ) -> PublicReleaseResponse:
        """The newest published artifact for a platform, for the website.

        `available=False` with every field null is the honest answer when this
        deployment has published nothing for that platform. A download page
        must render that as "no download yet", never as a broken link.
        """
        platform_token = normalize_platform(platform)
        if not platform_token:
            return PublicReleaseResponse(available=False)
        release = DesktopReleaseRepository.latest_published(
            db, platform=platform_token, architecture=normalize_architecture(architecture)
        )
        if release is None:
            return PublicReleaseResponse(platform=platform_token, available=False)
        return DesktopReleaseService._as_public(release)

    @staticmethod
    def public_index(db: Session) -> PublicReleaseIndexResponse:
        """Every platform's current download, for a page listing them all.

        macOS appears twice — once per architecture — because this project
        ships arm64 and x86_64 as separate builds and says so. A page that
        offered one "macOS" download would have to pick one, and half the Macs
        would get a build that cannot run.
        """
        downloads = {}
        for key, platform_token, arch in (
            ("windows", Platform.WINDOWS, None),
            ("macos-arm64", Platform.MACOS, "arm64"),
            ("macos-x86_64", Platform.MACOS, "x86_64"),
        ):
            release = DesktopReleaseRepository.latest_published(
                db, platform=platform_token, architecture=arch
            )
            downloads[key] = (
                DesktopReleaseService._as_public(release)
                if release is not None
                else PublicReleaseResponse(
                    platform=platform_token, architecture=arch, available=False
                )
            )
        newest = DesktopReleaseRepository.latest_published_any_platform(db)
        return PublicReleaseIndexResponse(
            downloads=downloads,
            latest_version=newest.version if newest is not None else None,
        )

    @staticmethod
    def _as_public(release: DesktopRelease) -> PublicReleaseResponse:
        return PublicReleaseResponse(
            version=release.version,
            platform=release.platform,
            architecture=release.architecture,
            download_url=release.download_url,
            file_size=release.file_size,
            sha256=release.sha256,
            release_notes=release.release_notes,
            release_notes_url=release.release_notes_url,
            published_at=release.published_at,
            available=True,
        )

    # ── Release management ────────────────────────────────────────────────

    @staticmethod
    def create_release(
        db: Session, payload: DesktopReleaseCreate, actor: Optional[User] = None,
    ) -> DesktopRelease:
        """Register a build. Always as a draft — publishing is a second act.

        Registering the same artifact twice is refused rather than silently
        overwritten: a version identifies exactly one build, and letting a
        second upload replace the first would mean a client that already
        verified the old checksum can no longer explain what it installed.
        """
        platform_token = normalize_platform(payload.platform)
        arch_token = normalize_architecture(payload.architecture)

        existing = DesktopReleaseRepository.get_artifact(
            db, version=payload.version, platform=platform_token,
            architecture=arch_token,
        )
        if existing is not None:
            raise ReleaseConflict(
                f"{payload.version} for {platform_token}"
                f"/{arch_token or 'any'} is already registered."
            )

        # Registration refuses a URL this deployment would never serve, and a
        # file name that is not the artifact this row claims to be. Signing and
        # file size are judged at publish time instead: an unsigned or unsized
        # build may legitimately be registered as a draft for a pilot.
        candidate = SimpleNamespace(
            version=payload.version, platform=platform_token, architecture=arch_token,
            download_url=payload.download_url, sha256=payload.sha256,
            file_size=payload.file_size or 1, signed=True, status="draft",
        )
        problems = [
            p for p in policy.check_row(candidate, require_signed=False)
            if p.code in ("url", "filename", "sha256")
        ]
        if problems:
            raise ReleasePolicyViolation(problems)

        major, minor, patch = version_tuple(payload.version)  # type: ignore[misc]
        release = DesktopRelease(
            version=payload.version,
            version_major=major,
            version_minor=minor,
            version_patch=patch,
            platform=platform_token,
            architecture=arch_token,
            download_url=payload.download_url,
            file_size=payload.file_size,
            sha256=payload.sha256,
            release_notes=payload.release_notes,
            release_notes_url=payload.release_notes_url,
            status=ReleaseStatus.DRAFT,
            force_update=bool(payload.force_update),
            min_supported_version=payload.min_supported_version,
            signed=bool(payload.signed),
            signer=payload.signer,
        )
        DesktopReleaseRepository.add(db, release)
        # Every version has exactly one record of what was decided about it.
        # The pipeline registers artifacts and knows nothing of policy, so a
        # record created here is a plain draft for every configured platform
        # (optional unless the row said otherwise); an administrator edits it
        # in the panel before publishing.
        DesktopReleaseService.ensure_version_record(
            db, release.version, actor=actor,
            mandatory=bool(payload.force_update),
            required_min_version=payload.min_supported_version,
            release_notes=payload.release_notes,
            release_notes_url=payload.release_notes_url,
        )
        db.commit()
        db.refresh(release)
        logger.info(
            "registered desktop release %s %s/%s as draft",
            release.version, release.platform, release.architecture or "any",
        )
        return release

    @staticmethod
    def update_release(
        db: Session, release_id: int, payload: DesktopReleaseUpdate
    ) -> Optional[DesktopRelease]:
        """Amend a release's notes, policy or status."""
        release = DesktopReleaseRepository.get(db, release_id)
        if release is None:
            return None

        was_published = release.status == ReleaseStatus.PUBLISHED

        if payload.status is not None:
            DesktopReleaseService._validate_status(payload.status)
            if payload.status == ReleaseStatus.PUBLISHED and not was_published:
                DesktopReleaseService._gate_publication(db, release)
            DesktopReleaseService._apply_status(release, payload.status)
        if payload.release_notes is not None:
            release.release_notes = payload.release_notes
        if payload.release_notes_url is not None:
            release.release_notes_url = payload.release_notes_url
        if payload.force_update is not None:
            release.force_update = bool(payload.force_update)
        if payload.min_supported_version is not None:
            release.min_supported_version = payload.min_supported_version

        db.commit()
        db.refresh(release)
        DesktopReleaseService._announce_if_newly_published(db, release, was_published)
        return release

    @staticmethod
    def set_status(
        db: Session, release_id: int, status: str
    ) -> Optional[DesktopRelease]:
        """Publish, withdraw or roll back one release."""
        release = DesktopReleaseRepository.get(db, release_id)
        if release is None:
            return None
        was_published = release.status == ReleaseStatus.PUBLISHED
        DesktopReleaseService._validate_status(status)
        if status == ReleaseStatus.PUBLISHED and not was_published:
            DesktopReleaseService._gate_publication(db, release)
        DesktopReleaseService._apply_status(release, status)
        db.commit()
        db.refresh(release)
        update_policy.invalidate_policy_cache()
        DesktopReleaseService._announce_if_newly_published(db, release, was_published)
        return release

    # ── Readiness and publishing a whole version ──────────────────────────

    @staticmethod
    def readiness(db: Session, version: str) -> policy.Readiness:
        """Whether ``version`` has a complete, valid set of artifacts.

        Read-only. The same judgement publishing applies, available beforehand
        so an administrator can see what is missing rather than find out from a
        refusal.
        """
        rows = DesktopReleaseRepository.list_for_version(db, version)
        return policy.readiness(version, rows, targets=DesktopReleaseService._targets_for(db, version))

    @staticmethod
    def _version_record(db: Session, version: str) -> Optional[DesktopReleaseVersion]:
        """The version's record, or None when it has none (a row registered
        before version records existed). Anything that is not a record is
        treated as none: the fallback below is the artifact row's own flags."""
        record = DesktopReleaseVersionRepository.get_by_version(db, version)
        return record if isinstance(record, DesktopReleaseVersion) else None

    @staticmethod
    def _targets_for(db: Session, version: str) -> Optional[List[str]]:
        """The platforms a version is for, or None (every configured one)."""
        record = DesktopReleaseService._version_record(db, version)
        return record.targets if record is not None else None

    @staticmethod
    def _gate_publication(db: Session, release: DesktopRelease) -> None:
        """Refuse to publish a row unless its whole version is publishable.

        Judged as though this row were already live -- publishing is the act
        being decided, so the row's current ``draft`` (or ``rolled_back``) state
        must not count against it -- and including the row itself even when its
        platform is outside the required set.
        """
        rows = DesktopReleaseRepository.list_for_version(db, release.version)
        view = [
            SimpleNamespace(
                version=r.version, platform=r.platform, architecture=r.architecture,
                download_url=r.download_url, sha256=r.sha256, file_size=r.file_size,
                signed=r.signed,
                status="draft" if r.id == release.id else r.status,
            )
            for r in rows
        ]
        report = policy.readiness(
            release.version, view, targets=DesktopReleaseService._targets_for(db, release.version),
        )
        problems = list(report.problems)
        seen = {(p.code, p.artifact) for p in problems}
        own = next(
            v for v in view
            if v.platform == release.platform and v.architecture == release.architecture
        )
        for extra in policy.check_row(own):
            if (extra.code, extra.artifact) not in seen:
                problems.append(extra)
        if problems:
            logger.warning(
                "DESKTOP_RELEASE_PUBLISH_REFUSED: %s %s/%s: %s",
                release.version, release.platform, release.architecture or "any",
                "; ".join(f"{p.code}({p.artifact})" for p in problems),
            )
            raise ReleasePolicyViolation(
                problems,
                f"Version {release.version} cannot be published yet: "
                + "; ".join(p.message for p in problems),
            )

    @staticmethod
    def publish_version(
        db: Session, version: str, actor: Optional[User] = None,
    ) -> Tuple[List[DesktopRelease], bool]:
        """Publish every required artifact of ``version`` in one step.

        All or nothing: the version is judged first, and only if the whole set
        (for the platforms the version targets) is publishable is any row
        moved. One commit -- the rows, the version record and its audit row
        together -- then at most one announcement, so Windows and both Macs go
        live together and the announcement is queued once the set is complete.
        Returns the rows now published and whether an announcement was queued.

        Publishing twice is safe: a version already published has no draft
        rows to move, records nothing and announces nothing new.
        """
        rows = DesktopReleaseRepository.list_for_version(db, version)
        record = DesktopReleaseService._version_record(db, version)
        targets = record.targets if record is not None else None
        report = policy.readiness(version, rows, targets=targets)
        if not report.ready:
            raise ReleasePolicyViolation(
                report.problems,
                f"Version {version} cannot be published yet: "
                + "; ".join(p.message for p in report.problems),
            )
        if record is not None and record.status == ReleaseVersionStatus.WITHDRAWN:
            raise ReleasePolicyViolation(
                [policy.Problem("withdrawn", f"Version {version} was withdrawn; register a new version.")],
                f"Version {version} was withdrawn and cannot be published again.",
            )

        required = set(policy.required_artifacts(targets))
        changed: List[DesktopRelease] = []
        for row in rows:
            if (row.platform, row.architecture) not in required:
                continue
            if row.status == ReleaseStatus.DRAFT:
                DesktopReleaseService._apply_status(row, ReleaseStatus.PUBLISHED)
                changed.append(row)

        newly_published = bool(changed) or (
            record is not None and record.status != ReleaseVersionStatus.PUBLISHED
        )
        if record is not None and record.status != ReleaseVersionStatus.PUBLISHED:
            now = update_policy.now_utc()
            record.status = ReleaseVersionStatus.PUBLISHED
            record.updated_at = now
            if record.published_at is None:
                record.published_at = now
            record.published_by_user_id = actor.id if actor is not None else record.published_by_user_id
            DesktopReleaseService._audit(
                db, actor, ActivityLogAction.RELEASE_PUBLISHED, record,
                f"Published Monitra {version} for {DesktopReleaseService._platform_words(record.targets)}"
                + DesktopReleaseService._policy_words(record),
            )
        db.commit()
        for row in changed:
            db.refresh(row)
        update_policy.invalidate_policy_cache()

        queued = False
        if changed and newly_published:
            queued = DesktopReleaseService._announce_if_newly_published(
                db, changed[-1], was_published=False
            )
        published = [r for r in rows if r.status == ReleaseStatus.PUBLISHED]
        return published, queued

    @staticmethod
    def _announce_if_newly_published(
        db: Session, release: DesktopRelease, was_published: bool
    ) -> None:
        """Email every active user, on the transition *into* published only.

        Three conditions, and each rules out a way of mailing people twice or
        wrongly:

        * the release must now be published — a draft is by definition not
          something users should be told about;
        * it must not already have been — re-publishing after a withdrawal is
          not new news, and the announcement for that version has been sent;
        * and the announcement itself is keyed on the *version*, inside the
          outbox, so publishing the Windows artifact and then the two macOS
          ones sends one announcement, not three.

        Mail is queued after the commit, never before: the release is live
        whether or not anybody can be told about it, and `queue_release_
        announcements` does not raise, so publishing cannot fail over email.
        """
        if was_published or release.status != ReleaseStatus.PUBLISHED:
            return False

        # Only a COMPLETE release is announced. The email says "a new version is
        # available" and links to the download page; sent when the first
        # artifact goes live, it reaches Mac users before there is anything for
        # them to download. So the announcement waits until every required
        # artifact of the version is published and valid -- whichever publish
        # completes the set is the one that sends it, and the per-version
        # dedupe key makes any later publish a no-op.
        rows = DesktopReleaseRepository.list_for_version(db, release.version)
        record = DesktopReleaseService._version_record(db, release.version)
        targets = record.targets if record is not None else None
        if not policy.is_complete_and_published(release.version, rows, targets=targets):
            logger.info(
                "RELEASE_EMAIL_DEFERRED: %s is not fully published yet; the "
                "announcement waits for the remaining artifacts", release.version,
            )
            return False

        # Imported here rather than at module scope: the email package imports
        # the user repository, and a top-level import would tie this module's
        # import order to it for something only this one path needs.
        from app.services.email import queue_release_announcements

        if record is None:
            return bool(queue_release_announcements(db, release))
        announced = SimpleNamespace(
            version=release.version,
            release_notes=record.release_notes or release.release_notes,
            release_notes_url=release.release_notes_url,
        )
        return bool(queue_release_announcements(
            db, announced,
            platforms=list(record.targets),
            mandatory=bool(record.mandatory),
            grace_hours=int(record.grace_hours),
            required_version=update_policy.floor_for(record),
            title=record.title,
        ))

    @staticmethod
    def _validate_status(status: str) -> None:
        if status not in ReleaseStatus.ALL:
            raise ValueError(
                f"Unknown release status {status!r}. "
                f"Expected one of: {', '.join(ReleaseStatus.ALL)}."
            )

    @staticmethod
    def _apply_status(release: DesktopRelease, status: str) -> None:
        DesktopReleaseService._validate_status(status)
        previous = release.status
        release.status = status
        if status == ReleaseStatus.PUBLISHED and release.published_at is None:
            # Stamped once, on the first publication. Re-publishing after a
            # withdrawal keeps the original date, because that is when the
            # build was first offered to users and that is the fact a support
            # report needs.
            release.published_at = datetime.now(timezone.utc)
        logger.info(
            "desktop release %s %s/%s: %s -> %s",
            release.version, release.platform, release.architecture or "any",
            previous, status,
        )

    @staticmethod
    def list_releases(
        db: Session,
        *,
        status: Optional[str] = None,
        platform: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[DesktopRelease]:
        if status is not None and status not in ReleaseStatus.ALL:
            raise ValueError(
                f"Unknown release status {status!r}. "
                f"Expected one of: {', '.join(ReleaseStatus.ALL)}."
            )
        return DesktopReleaseRepository.list_all(
            db,
            statuses=[status] if status else None,
            platform=normalize_platform(platform),
            limit=limit,
            offset=offset,
        )

    # ── Version records: what an administrator manages ────────────────────

    @staticmethod
    def _platform_words(targets: List[str]) -> str:
        names = {"win32": "Windows", "darwin": "macOS"}
        return ", ".join(names.get(t, t) for t in targets) or "no platform"

    @staticmethod
    def _policy_words(record: DesktopReleaseVersion) -> str:
        floor = update_policy.floor_for(record)
        if record.mandatory:
            return f" (mandatory, {int(record.grace_hours)} h grace, floor {floor})"
        if floor:
            return f" (optional; required below {floor})"
        return " (optional)"

    @staticmethod
    def _audit(
        db: Session, actor: Optional[User], action: str,
        record: DesktopReleaseVersion, description: str,
    ) -> None:
        """One audit row, staged in the caller's transaction.

        Nothing to record without an actor (a test calling the service
        directly): the trail names a person or a credential, never nobody.
        """
        if actor is None or getattr(actor, "id", None) is None:
            return
        if record.id is None:
            db.flush()
        ActivityLogRepository.add(db, ActivityLog(
            organization_id=actor.organization_id,
            user_id=actor.id,
            module=ActivityLogModule.SYSTEM,
            action=action,
            entity_id=record.id,
            description=f"{description} [{getattr(actor, 'username', None) or actor.id}]",
            created_at=update_policy.now_utc(),
        ))
        logger.info("DESKTOP_RELEASE_AUDIT action=%s version=%s by=%s", action, record.version, actor.id)

    @staticmethod
    def _validate_targets(targets: Optional[List[str]]) -> List[str]:
        if targets is None:
            return list(TARGETABLE_PLATFORMS)
        chosen: List[str] = []
        for raw in targets:
            token = normalize_platform(raw)
            if token not in TARGETABLE_PLATFORMS:
                raise ReleasePolicyViolation(
                    [policy.Problem("platform", f"'{raw}' is not a platform a release can target.")],
                )
            if token not in chosen:
                chosen.append(token)
        if not chosen:
            raise ReleasePolicyViolation(
                [policy.Problem("platform", "A release must target at least one platform.")],
            )
        return chosen

    @staticmethod
    def _validate_floor(version: str, floor: Optional[str]) -> Optional[str]:
        if floor is None:
            return None
        floor = floor.strip()
        if not floor:
            return None
        if version_tuple(floor) is None:
            raise ReleasePolicyViolation(
                [policy.Problem("required_min_version", "The required minimum version must be major.minor.patch.")],
            )
        if version_tuple(floor) > version_tuple(version):  # type: ignore[operator]
            raise ReleasePolicyViolation(
                [policy.Problem(
                    "required_min_version",
                    f"The required minimum version ({floor}) cannot be newer than the release ({version}).",
                )],
            )
        return floor

    @staticmethod
    def ensure_version_record(
        db: Session, version: str, *, actor: Optional[User] = None,
        mandatory: bool = False, required_min_version: Optional[str] = None,
        release_notes: Optional[str] = None, release_notes_url: Optional[str] = None,
        title: Optional[str] = None, targets: Optional[List[str]] = None,
        grace_hours: int = 24, effective_at: Optional[datetime] = None,
    ) -> DesktopReleaseVersion:
        """The version's record, created as a draft if it has none. Staged;
        the caller commits."""
        record = DesktopReleaseVersionRepository.get_by_version(db, version)
        if record is not None:
            return record
        major, minor, patch = version_tuple(version)  # type: ignore[misc]
        record = DesktopReleaseVersion(
            version=version, version_major=major, version_minor=minor, version_patch=patch,
            title=title, release_notes=release_notes,
            target_platforms=",".join(DesktopReleaseService._validate_targets(targets)),
            mandatory=bool(mandatory), grace_hours=int(grace_hours),
            required_min_version=DesktopReleaseService._validate_floor(version, required_min_version),
            effective_at=effective_at,
            status=ReleaseVersionStatus.DRAFT,
            created_by_user_id=getattr(actor, "id", None),
            created_at=update_policy.now_utc(), updated_at=update_policy.now_utc(),
        )
        DesktopReleaseVersionRepository.add(db, record)
        DesktopReleaseService._audit(
            db, actor, ActivityLogAction.RELEASE_CREATED, record,
            f"Created Monitra {version} for {DesktopReleaseService._platform_words(record.targets)}"
            + DesktopReleaseService._policy_words(record),
        )
        return record

    @staticmethod
    def create_version(
        db: Session, payload: ReleaseVersionCreate, actor: Optional[User] = None,
    ) -> DesktopReleaseVersion:
        """Create a version record, and register any artifacts given with it.

        Refused when the version already has a record. Artifacts go through the
        same registration as the pipeline's, so a hand-entered URL or digest is
        judged by exactly the rules a CI-registered one is.
        """
        if DesktopReleaseVersionRepository.get_by_version(db, payload.version) is not None:
            raise ReleaseConflict(f"Version {payload.version} already has a release record.")
        targets = DesktopReleaseService._validate_targets(payload.target_platforms)
        for artifact in payload.artifacts:
            if normalize_platform(artifact.platform) not in targets:
                raise ReleasePolicyViolation([policy.Problem(
                    "platform",
                    f"An artifact for {artifact.platform} was given, but the release does not target it.",
                    policy.artifact_label(normalize_platform(artifact.platform) or artifact.platform,
                                          normalize_architecture(artifact.architecture)),
                )])
        record = DesktopReleaseService.ensure_version_record(
            db, payload.version, actor=actor,
            mandatory=payload.mandatory, required_min_version=payload.required_min_version,
            release_notes=payload.release_notes, release_notes_url=payload.release_notes_url,
            title=payload.title, targets=targets, grace_hours=payload.grace_hours,
            effective_at=payload.effective_at,
        )
        for artifact in payload.artifacts:
            DesktopReleaseService._register_artifact(db, payload, artifact)
        db.commit()
        db.refresh(record)
        return record

    @staticmethod
    def _register_artifact(db: Session, version_payload: ReleaseVersionCreate, artifact: ReleaseArtifactInput) -> DesktopRelease:
        """One artifact through `create_release`'s validation, without its commit."""
        payload = DesktopReleaseCreate(
            version=version_payload.version, platform=artifact.platform,
            architecture=artifact.architecture, download_url=artifact.download_url,
            sha256=artifact.sha256, file_size=artifact.file_size,
            release_notes=version_payload.release_notes,
            release_notes_url=version_payload.release_notes_url,
            force_update=False, min_supported_version=None,
            signed=artifact.signed, signer=artifact.signer,
        )
        platform_token = normalize_platform(payload.platform)
        arch_token = normalize_architecture(payload.architecture)
        if DesktopReleaseRepository.get_artifact(
            db, version=payload.version, platform=platform_token, architecture=arch_token,
        ) is not None:
            raise ReleaseConflict(
                f"{payload.version} for {platform_token}/{arch_token or 'any'} is already registered."
            )
        candidate = SimpleNamespace(
            version=payload.version, platform=platform_token, architecture=arch_token,
            download_url=payload.download_url, sha256=payload.sha256,
            file_size=payload.file_size or 1, signed=True, status="draft",
        )
        problems = [
            p for p in policy.check_row(candidate, require_signed=False)
            if p.code in ("url", "filename", "sha256")
        ]
        if problems:
            raise ReleasePolicyViolation(problems)
        major, minor, patch = version_tuple(payload.version)  # type: ignore[misc]
        row = DesktopRelease(
            version=payload.version, version_major=major, version_minor=minor, version_patch=patch,
            platform=platform_token, architecture=arch_token,
            download_url=payload.download_url, file_size=payload.file_size, sha256=payload.sha256,
            release_notes=payload.release_notes, release_notes_url=payload.release_notes_url,
            status=ReleaseStatus.DRAFT, force_update=False, min_supported_version=None,
            signed=bool(payload.signed), signer=payload.signer,
        )
        return DesktopReleaseRepository.add(db, row)

    @staticmethod
    def update_version(
        db: Session, version: str, payload: ReleaseVersionUpdate, actor: Optional[User] = None,
    ) -> Optional[DesktopReleaseVersion]:
        """Amend a version record's title, notes, targets or policy.

        The targets of a published version are fixed: widening them would
        offer a platform an artifact set that was never judged, and narrowing
        them would withdraw a platform's release without the withdrawal path's
        bookkeeping. Policy fields may change while published; a change is
        audited and takes effect at once (the policy cache is dropped).
        """
        record = DesktopReleaseVersionRepository.get_by_version(db, version)
        if record is None:
            return None
        changes: List[str] = []
        if payload.title is not None:
            record.title = payload.title.strip() or None
            changes.append("title")
        if payload.release_notes is not None:
            record.release_notes = payload.release_notes
            changes.append("notes")
        if payload.release_notes_url is not None:
            for row in DesktopReleaseRepository.list_for_version(db, version):
                row.release_notes_url = payload.release_notes_url or None
            changes.append("notes link")
        if payload.target_platforms is not None:
            targets = DesktopReleaseService._validate_targets(payload.target_platforms)
            if record.status == ReleaseVersionStatus.PUBLISHED and targets != record.targets:
                raise ReleasePolicyViolation([policy.Problem(
                    "platform", "The platforms of a published release cannot be changed; withdraw it first.",
                )])
            record.target_platforms = ",".join(targets)
            changes.append(f"platforms={DesktopReleaseService._platform_words(targets)}")
        if payload.mandatory is not None:
            record.mandatory = bool(payload.mandatory)
            changes.append(f"mandatory={'yes' if record.mandatory else 'no'}")
        if payload.grace_hours is not None:
            record.grace_hours = int(payload.grace_hours)
            changes.append(f"grace={record.grace_hours}h")
        if payload.clear_required_min_version:
            record.required_min_version = None
            changes.append("floor cleared")
        elif payload.required_min_version is not None:
            record.required_min_version = DesktopReleaseService._validate_floor(
                version, payload.required_min_version,
            )
            changes.append(f"floor={record.required_min_version}")
        if payload.clear_effective_at:
            record.effective_at = None
            changes.append("effective at publish")
        elif payload.effective_at is not None:
            record.effective_at = payload.effective_at
            changes.append(f"effective={payload.effective_at.isoformat()}")
        if not changes:
            return record
        record.updated_at = update_policy.now_utc()
        if record.status == ReleaseVersionStatus.VALIDATED and any(
            c.startswith("platforms=") for c in changes
        ):
            # The readiness that was recorded judged a different artifact set.
            record.status = ReleaseVersionStatus.DRAFT
            record.validated_at = None
        DesktopReleaseService._audit(
            db, actor, ActivityLogAction.RELEASE_UPDATED, record,
            f"Updated Monitra {version}: " + ", ".join(changes),
        )
        db.commit()
        db.refresh(record)
        update_policy.invalidate_policy_cache()
        return record

    @staticmethod
    def validate_version(
        db: Session, version: str, actor: Optional[User] = None,
    ) -> Tuple[Optional[DesktopReleaseVersion], policy.Readiness]:
        """Run the readiness check and record that it passed.

        `validated` affects nobody; it is the administrator's note that the
        artifact set was checked. A version that fails stays (or returns to)
        draft, with every problem listed.
        """
        record = DesktopReleaseVersionRepository.get_by_version(db, version)
        if record is None:
            return None, policy.Readiness(version=version)
        report = DesktopReleaseService.readiness(db, version)
        if record.status in (ReleaseVersionStatus.DRAFT, ReleaseVersionStatus.VALIDATED):
            now = update_policy.now_utc()
            if report.ready and record.status != ReleaseVersionStatus.VALIDATED:
                record.status = ReleaseVersionStatus.VALIDATED
                record.validated_at = now
                record.updated_at = now
                DesktopReleaseService._audit(
                    db, actor, ActivityLogAction.RELEASE_VALIDATED, record,
                    f"Validated Monitra {version}: {', '.join(report.present)}",
                )
                db.commit()
            elif not report.ready and record.status == ReleaseVersionStatus.VALIDATED:
                record.status = ReleaseVersionStatus.DRAFT
                record.validated_at = None
                record.updated_at = now
                db.commit()
            db.refresh(record)
        return record, report

    @staticmethod
    def withdraw_version(
        db: Session, version: str, actor: Optional[User] = None, *, reason: Optional[str] = None,
    ) -> Tuple[Optional[DesktopReleaseVersion], int]:
        """Take a published version out of circulation.

        Every artifact row moves to `rolled_back`, so the update check stops
        offering the version at once and the previous published version is
        newest again. **A grace period already running is kept, not erased.**
        Enforcement of its floor is suspended by construction -- the policy
        engine enforces only a floor that has an installable published release
        -- and a corrective release published later with the same floor picks
        the deadlines up exactly where they were. Returns the record and how
        many open grace rows now wait.
        """
        record = DesktopReleaseVersionRepository.get_by_version(db, version)
        if record is None:
            return None, 0
        rows = DesktopReleaseRepository.list_for_version(db, version)
        for row in rows:
            if row.status in (ReleaseStatus.PUBLISHED, ReleaseStatus.DRAFT):
                DesktopReleaseService._apply_status(row, ReleaseStatus.ROLLED_BACK)
        now = update_policy.now_utc()
        record.status = ReleaseVersionStatus.WITHDRAWN
        record.withdrawn_at = now
        record.updated_at = now
        record.withdrawn_by_user_id = getattr(actor, "id", None)
        waiting = [
            r for r in DesktopReleaseVersionRepository.list_requirements_for_release(
                db, release_version_id=record.id,
            ) if r.satisfied_at is None
        ]
        DesktopReleaseService._audit(
            db, actor, ActivityLogAction.RELEASE_WITHDRAWN, record,
            f"Withdrew Monitra {version}"
            + (f": {reason.strip()}" if reason and reason.strip() else "")
            + (f"; {len(waiting)} grace period(s) kept, waiting for a corrective release"
               if waiting else ""),
        )
        db.commit()
        db.refresh(record)
        update_policy.invalidate_policy_cache()
        logger.warning(
            "DESKTOP_RELEASE_WITHDRAWN version=%s rows=%d open_requirements=%d",
            version, len(rows), len(waiting),
        )
        return record, len(waiting)

    @staticmethod
    def derived_status(record: DesktopReleaseVersion, published: List[DesktopReleaseVersion]) -> str:
        """`superseded` when a newer published version covers every platform
        this one targets; otherwise the stored status."""
        if record.status != ReleaseVersionStatus.PUBLISHED:
            return record.status
        own = version_tuple(record.version) or (0, 0, 0)
        covered = set()
        for other in published:
            if other.id == record.id:
                continue
            if (version_tuple(other.version) or (0, 0, 0)) <= own:
                continue
            covered.update(other.targets)
        if record.targets and all(t in covered for t in record.targets):
            return "superseded"
        return record.status

    @staticmethod
    def _as_version_read(
        db: Session, record: DesktopReleaseVersion, published: List[DesktopReleaseVersion],
        *, model=ReleaseVersionRead,
    ):
        rows = DesktopReleaseRepository.list_for_version(db, record.version)
        report = policy.readiness(record.version, rows, targets=record.targets)
        return model(
            id=record.id, version=record.version, title=record.title,
            release_notes=record.release_notes, target_platforms=list(record.targets),
            mandatory=bool(record.mandatory), grace_hours=int(record.grace_hours),
            required_min_version=record.required_min_version,
            enforced_floor=update_policy.floor_for(record),
            effective_at=record.effective_at,
            status=DesktopReleaseService.derived_status(record, published),
            created_by_user_id=record.created_by_user_id,
            published_by_user_id=record.published_by_user_id,
            withdrawn_by_user_id=record.withdrawn_by_user_id,
            created_at=record.created_at, updated_at=record.updated_at,
            validated_at=record.validated_at, published_at=record.published_at,
            withdrawn_at=record.withdrawn_at,
            artifacts=[DesktopReleaseRead.model_validate(r) for r in rows],
            ready=report.ready,
            problems=[ReleaseProblemRead(**p.as_dict()) for p in report.problems],
        )

    @staticmethod
    def list_versions(db: Session, *, limit: int = 100, offset: int = 0) -> List[ReleaseVersionRead]:
        records = DesktopReleaseVersionRepository.list_all(db, limit=limit, offset=offset)
        published = DesktopReleaseVersionRepository.list_published(db)
        return [DesktopReleaseService._as_version_read(db, r, published) for r in records]

    @staticmethod
    def version_detail(db: Session, version: str) -> Optional[ReleaseVersionDetail]:
        record = DesktopReleaseVersionRepository.get_by_version(db, version)
        if record is None:
            return None
        published = DesktopReleaseVersionRepository.list_published(db)
        detail = DesktopReleaseService._as_version_read(db, record, published, model=ReleaseVersionDetail)
        detail.stats = DesktopReleaseService._version_stats(db, record)
        detail.history = [
            ReleaseAuditEntry.model_validate(row)
            for row in DesktopReleaseVersionRepository.list_history(db, release_version_id=record.id)
        ]
        return detail

    @staticmethod
    def _version_stats(db: Session, record: DesktopReleaseVersion) -> ReleaseVersionStats:
        targets = list(record.targets)
        targeted = UserRepository.list_release_recipients(db, platforms=targets)
        own = version_tuple(record.version) or (0, 0, 0)
        adoption = {"on_or_above": 0, "below": 0}
        for platform, app_version, count in DesktopReleaseVersionRepository.client_versions_by_platform(db):
            if normalize_platform(platform) not in targets:
                continue
            seen = version_tuple(app_version)
            if seen is None:
                continue
            adoption["on_or_above" if seen >= own else "below"] += count
        requirements = update_policy.requirement_summary(
            DesktopReleaseVersionRepository.list_requirements_for_release(db, release_version_id=record.id)
        )
        return ReleaseVersionStats(
            targeted_users=len(targeted),
            requirements=requirements,
            installs=DesktopReleaseVersionRepository.report_counts(db, target_version=record.version),
            adoption=adoption,
            email=DesktopReleaseVersionRepository.email_counts(db, version=record.version),
        )

    # ── The desktop's policy view and install reports ─────────────────────

    @staticmethod
    def update_policy_view(
        db: Session, current_user: User, client_version: Optional[str],
        platform: Optional[str], architecture: Optional[str],
    ) -> UpdatePolicyResponse:
        """`GET /desktop/update-policy`: the update check plus the grace status."""
        release = DesktopReleaseService.latest_version(
            db, current_user, client_version, platform, architecture,
        )
        verdict = update_policy.evaluate(db, current_user, client_version, platform)
        grace = GraceStatus(
            first_seen_at=verdict.first_seen_at, deadline_at=verdict.deadline_at,
            hours=verdict.grace_hours, active=verdict.grace_active, expired=verdict.blocked,
            remaining_seconds=verdict.remaining_seconds,
        )
        return UpdatePolicyResponse(
            server_time=verdict.server_time or update_policy.now_utc(),
            installed_version=client_version,
            platform=verdict.platform,
            update_required=verdict.required,
            mandatory=verdict.required,
            required_version=verdict.required_version,
            latest_version=release.latest_version,
            release_title=release.release_title or verdict.release_title,
            blocked=verdict.blocked,
            grace=grace,
            release=release,
        )

    @staticmethod
    def record_update_report(
        db: Session, current_user: User, payload: UpdateReportCreate,
        platform: Optional[str], architecture: Optional[str],
    ) -> Tuple[DesktopUpdateReport, bool]:
        """Store what the desktop said about an update. Idempotent on the
        client event id. Returns `(row, created)`."""
        if payload.outcome not in UpdateReportOutcome.ALL:
            raise ValueError(
                f"Unknown update outcome {payload.outcome!r}. "
                f"Expected one of: {', '.join(UpdateReportOutcome.ALL)}."
            )
        existing = DesktopReleaseVersionRepository.get_report_by_event(
            db, user_id=current_user.id, client_event_id=payload.client_event_id,
        )
        if existing is not None:
            return existing, False
        now = update_policy.now_utc()
        occurred = update_policy.aware(payload.occurred_at) or now
        client_time = update_policy.aware(payload.client_time)
        if client_time is not None:
            # Place the event by its age on the client's clock (TIMING_MODEL).
            age = client_time - occurred
            occurred = now - age if age.total_seconds() >= 0 else now
        if occurred > now:
            occurred = now
        row = DesktopUpdateReport(
            user_id=current_user.id,
            organization_id=getattr(current_user, "organization_id", None),
            platform=normalize_platform(platform),
            architecture=normalize_architecture(architecture),
            from_version=payload.from_version,
            target_version=payload.target_version,
            outcome=payload.outcome,
            category=payload.category,
            detail=payload.detail,
            occurred_at=occurred,
            reported_at=now,
            client_event_id=payload.client_event_id,
        )
        try:
            DesktopReleaseVersionRepository.add_report(db, row)
            db.commit()
        except Exception:
            db.rollback()
            existing = DesktopReleaseVersionRepository.get_report_by_event(
                db, user_id=current_user.id, client_event_id=payload.client_event_id,
            )
            if existing is not None:
                return existing, False
            raise
        logger.info(
            "DESKTOP_UPDATE_REPORT user=%s from=%s to=%s outcome=%s category=%s",
            current_user.id, payload.from_version, payload.target_version,
            payload.outcome, payload.category,
        )
        return row, True

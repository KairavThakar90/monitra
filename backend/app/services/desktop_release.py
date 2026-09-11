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
from typing import List, Optional, Tuple

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.desktop_release import DesktopRelease, Platform, ReleaseStatus
from app.models.user import User
from app.repositories.desktop_client_version import DesktopClientVersionRepository
from app.repositories.desktop_release import DesktopReleaseRepository
from app.schemas.desktop_release import (
    DesktopClientVersionRead, DesktopReleaseCreate, DesktopReleaseUpdate,
    FleetVersionsResponse, LatestVersionResponse, PublicReleaseIndexResponse,
    PublicReleaseResponse,
)

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

        latest = release.version
        available = DesktopReleaseService.is_update_available(client_version, latest)

        # Mandatory only when a *newer* build exists to take. A release marked
        # mandatory does not make a client already running it — or running
        # something newer — mandatory to update; there would be nothing to
        # update to, and the user would be locked out of a current install.
        forced = bool(available) and (
            bool(release.force_update)
            or DesktopReleaseService.is_below_minimum(
                client_version, release.min_supported_version
            )
        )

        return LatestVersionResponse(
            latest_version=latest,
            download_url=release.download_url,
            release_notes_url=release.release_notes_url,
            update_available=available,
            client_version=client_version,
            sha256=release.sha256,
            file_size=release.file_size,
            release_notes=release.release_notes,
            force_update=forced,
            min_supported_version=release.min_supported_version,
            platform=release.platform,
            architecture=release.architecture,
        )

    @staticmethod
    def _configured_answer(client_version: Optional[str]) -> LatestVersionResponse:
        """The pre-release-table answer, for a deployment with no rows yet."""
        latest = DesktopReleaseService.configured_latest_version()
        return LatestVersionResponse(
            latest_version=latest,
            download_url=(settings.DESKTOP_DOWNLOAD_URL or None) if latest else None,
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
    def create_release(db: Session, payload: DesktopReleaseCreate) -> DesktopRelease:
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
            raise ValueError(
                f"{payload.version} for {platform_token}"
                f"/{arch_token or 'any'} is already registered."
            )

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
        )
        DesktopReleaseRepository.add(db, release)
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
        DesktopReleaseService._apply_status(release, status)
        db.commit()
        db.refresh(release)
        DesktopReleaseService._announce_if_newly_published(db, release, was_published)
        return release

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
            return
        # Imported here rather than at module scope: the email package imports
        # the user repository, and a top-level import would tie this module's
        # import order to it for something only this one path needs.
        from app.services.email import queue_release_announcements

        queue_release_announcements(db, release)

    @staticmethod
    def _apply_status(release: DesktopRelease, status: str) -> None:
        if status not in ReleaseStatus.ALL:
            raise ValueError(
                f"Unknown release status {status!r}. "
                f"Expected one of: {', '.join(ReleaseStatus.ALL)}."
            )
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

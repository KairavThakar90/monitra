"""Data access for ``desktop_releases``.

Repositories own SQL and nothing else. The rules about *which* release a given
client should be offered, and whether that client is behind it, live in
``app.services.desktop_release`` — there is one comparison rule in this system
and this is not where it is written.

The one piece of judgement that does belong here is the ordering, because it is
a property of the query: releases are ordered by the three integer version
columns, never by the version string. A string sort puts ``1.9.0`` after
``1.10.0``, which would offer a fleet a downgrade and call it an update.
"""
from typing import List, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.desktop_release import DesktopRelease, ReleaseStatus


#: Newest first, by the numeric columns. Reused everywhere so no call site can
#: accidentally sort a different way.
_NEWEST_FIRST = (
    DesktopRelease.version_major.desc(),
    DesktopRelease.version_minor.desc(),
    DesktopRelease.version_patch.desc(),
    # Two artifacts of the same version for the same platform differ only by
    # architecture. The tie-break keeps the ordering total, so paging and
    # "first row wins" are deterministic rather than dependent on the plan.
    DesktopRelease.id.desc(),
)


class DesktopReleaseRepository:

    @staticmethod
    def get(db: Session, release_id: int) -> Optional[DesktopRelease]:
        return db.get(DesktopRelease, release_id)

    @staticmethod
    def get_artifact(
        db: Session,
        *,
        version: str,
        platform: str,
        architecture: Optional[str],
    ) -> Optional[DesktopRelease]:
        """The single row identified by version + platform + architecture."""
        stmt = select(DesktopRelease).where(
            DesktopRelease.version == version,
            DesktopRelease.platform == platform,
        )
        stmt = stmt.where(
            DesktopRelease.architecture.is_(None)
            if architecture is None
            else DesktopRelease.architecture == architecture
        )
        return db.execute(stmt).scalar_one_or_none()

    @staticmethod
    def latest_published(
        db: Session,
        *,
        platform: str,
        architecture: Optional[str] = None,
    ) -> Optional[DesktopRelease]:
        """The newest published artifact this client can actually install.

        Architecture matching is exact-then-portable, and never the other way
        round: a caller that named ``arm64`` is offered an ``arm64`` build if
        one exists, and only falls back to a row with no architecture — which
        means "one build serves every machine on this platform", as the Windows
        installer does. A caller that named nothing takes whatever the newest
        published build for the platform is.

        What this never does is hand back a *different* architecture. An x86_64
        ``.dmg`` on an Apple Silicon Mac is a download that cannot run, and
        offering it would look to the user exactly like a broken update.
        """
        if architecture:
            exact = DesktopReleaseRepository._latest_where(
                db, platform=platform, architecture=architecture
            )
            if exact is not None:
                return exact
            return DesktopReleaseRepository._latest_where(
                db, platform=platform, architecture=None
            )
        return DesktopReleaseRepository._latest_where(db, platform=platform)

    @staticmethod
    def _latest_where(
        db: Session,
        *,
        platform: str,
        architecture: Optional[str] = ...,  # type: ignore[assignment]
    ) -> Optional[DesktopRelease]:
        """Newest published row for a platform, optionally pinned to an arch.

        ``architecture`` left at its sentinel means "do not filter on it";
        ``None`` means "the row that has no architecture".
        """
        stmt = select(DesktopRelease).where(
            DesktopRelease.platform == platform,
            DesktopRelease.status == ReleaseStatus.PUBLISHED,
        )
        if architecture is not ...:
            stmt = stmt.where(
                DesktopRelease.architecture.is_(None)
                if architecture is None
                else DesktopRelease.architecture == architecture
            )
        return db.execute(stmt.order_by(*_NEWEST_FIRST).limit(1)).scalars().first()

    @staticmethod
    def latest_published_any_platform(db: Session) -> Optional[DesktopRelease]:
        """The newest published release across every platform.

        Used only to answer "what is the current version of Monitra" for a
        caller that did not say what it is running on. It must never be used
        to choose a *download*, because the newest row could be for a platform
        the caller cannot run.
        """
        stmt = select(DesktopRelease).where(
            DesktopRelease.status == ReleaseStatus.PUBLISHED
        )
        return db.execute(stmt.order_by(*_NEWEST_FIRST).limit(1)).scalars().first()

    @staticmethod
    def list_published_for_platform(db: Session, platform: str) -> List[DesktopRelease]:
        """Every published artifact for one platform, newest first."""
        stmt = select(DesktopRelease).where(
            DesktopRelease.platform == platform,
            DesktopRelease.status == ReleaseStatus.PUBLISHED,
        )
        return list(db.execute(stmt.order_by(*_NEWEST_FIRST)).scalars())

    @staticmethod
    def list_all(
        db: Session,
        *,
        statuses: Optional[Sequence[str]] = None,
        platform: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[DesktopRelease]:
        """The management listing. Includes drafts, so it is permission-gated."""
        stmt = select(DesktopRelease)
        if statuses:
            stmt = stmt.where(DesktopRelease.status.in_(list(statuses)))
        if platform:
            stmt = stmt.where(DesktopRelease.platform == platform)
        stmt = stmt.order_by(*_NEWEST_FIRST).limit(limit).offset(offset)
        return list(db.execute(stmt).scalars())

    @staticmethod
    def list_for_version(db: Session, version: str) -> List[DesktopRelease]:
        """Every row of one version, in any status.

        The unit of "is this release complete" is the version, not the row: a
        version is Windows plus both macOS architectures, and only the set can
        be judged ready.
        """
        stmt = select(DesktopRelease).where(DesktopRelease.version == version)
        return list(db.execute(stmt.order_by(DesktopRelease.id)).scalars())

    @staticmethod
    def add(db: Session, release: DesktopRelease) -> DesktopRelease:
        """Stage a new release. The caller commits."""
        db.add(release)
        db.flush()
        return release

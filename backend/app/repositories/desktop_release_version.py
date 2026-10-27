"""Data access for version records, grace periods and install reports.

SQL only. Which client is required to update, when its grace period ends and
what a withdrawn release means for a running grace period are all decided in
``app.services.desktop_update_policy``; nothing here judges a version.
"""
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.activity_log import ActivityLog, ActivityLogAction, ActivityLogModule
from app.models.desktop_client_version import DesktopClientVersion
from app.models.desktop_release_version import (
    DesktopReleaseVersion, DesktopUpdateReport, DesktopUpdateRequirement,
    ReleaseVersionStatus,
)
from app.models.email_notification import EmailNotification, TYPE_RELEASE

#: Newest first by the numeric columns -- never by the version string, under
#: which '1.9.0' sorts after '1.10.0'.
_NEWEST_FIRST = (
    DesktopReleaseVersion.version_major.desc(),
    DesktopReleaseVersion.version_minor.desc(),
    DesktopReleaseVersion.version_patch.desc(),
    DesktopReleaseVersion.id.desc(),
)


class DesktopReleaseVersionRepository:

    # ── Version records ───────────────────────────────────────────────────

    @staticmethod
    def get(db: Session, record_id: int) -> Optional[DesktopReleaseVersion]:
        return db.get(DesktopReleaseVersion, record_id)

    @staticmethod
    def get_by_version(db: Session, version: str) -> Optional[DesktopReleaseVersion]:
        stmt = select(DesktopReleaseVersion).where(DesktopReleaseVersion.version == version)
        return db.execute(stmt).scalar_one_or_none()

    @staticmethod
    def list_all(db: Session, *, limit: int = 100, offset: int = 0) -> List[DesktopReleaseVersion]:
        stmt = select(DesktopReleaseVersion).order_by(*_NEWEST_FIRST).limit(limit).offset(offset)
        return list(db.execute(stmt).scalars())

    @staticmethod
    def list_published(db: Session) -> List[DesktopReleaseVersion]:
        """Every published version record, newest first."""
        stmt = (
            select(DesktopReleaseVersion)
            .where(DesktopReleaseVersion.status == ReleaseVersionStatus.PUBLISHED)
            .order_by(*_NEWEST_FIRST)
        )
        return list(db.execute(stmt).scalars())

    @staticmethod
    def add(db: Session, record: DesktopReleaseVersion) -> DesktopReleaseVersion:
        """Stage a record. The caller commits."""
        db.add(record)
        db.flush()
        return record

    # ── Grace periods ─────────────────────────────────────────────────────

    @staticmethod
    def get_requirement(
        db: Session, *, user_id: int, platform: str, required_version: str,
    ) -> Optional[DesktopUpdateRequirement]:
        stmt = select(DesktopUpdateRequirement).where(
            DesktopUpdateRequirement.user_id == user_id,
            DesktopUpdateRequirement.platform == platform,
            DesktopUpdateRequirement.required_version == required_version,
        )
        return db.execute(stmt).scalar_one_or_none()

    @staticmethod
    def create_requirement_if_absent(
        db: Session,
        *,
        user_id: int,
        platform: str,
        required_version: str,
        required_tuple: Tuple[int, int, int],
        release_version_id: Optional[int],
        first_seen_at: datetime,
        deadline_at: datetime,
        client_version: Optional[str],
    ) -> Tuple[DesktopUpdateRequirement, bool]:
        """The row for this user, platform and floor -- creating it exactly once.

        Two requests from two devices arriving together both try to insert;
        the unique constraint lets one through and the other reads what it
        wrote. The savepoint keeps the loser's failed insert from poisoning the
        caller's transaction. Returns ``(row, created)``.
        """
        existing = DesktopReleaseVersionRepository.get_requirement(
            db, user_id=user_id, platform=platform, required_version=required_version,
        )
        if existing is not None:
            return existing, False
        row = DesktopUpdateRequirement(
            user_id=user_id,
            platform=platform,
            required_version=required_version,
            required_major=required_tuple[0],
            required_minor=required_tuple[1],
            required_patch=required_tuple[2],
            release_version_id=release_version_id,
            first_seen_at=first_seen_at,
            deadline_at=deadline_at,
            first_seen_client_version=client_version,
        )
        try:
            with db.begin_nested():
                db.add(row)
                db.flush()
        except IntegrityError:
            existing = DesktopReleaseVersionRepository.get_requirement(
                db, user_id=user_id, platform=platform, required_version=required_version,
            )
            if existing is None:
                raise
            return existing, False
        return row, True

    @staticmethod
    def list_requirements_for_floor(
        db: Session, *, user_id: int, required_version: str,
    ) -> List[DesktopUpdateRequirement]:
        """Every grace row of one user for one floor, whatever the platform."""
        stmt = select(DesktopUpdateRequirement).where(
            DesktopUpdateRequirement.user_id == user_id,
            DesktopUpdateRequirement.required_version == required_version,
        )
        return list(db.execute(stmt).scalars())

    @staticmethod
    def list_unsatisfied_for_user(
        db: Session, *, user_id: int, platform: str,
    ) -> List[DesktopUpdateRequirement]:
        stmt = select(DesktopUpdateRequirement).where(
            DesktopUpdateRequirement.user_id == user_id,
            DesktopUpdateRequirement.platform == platform,
            DesktopUpdateRequirement.satisfied_at.is_(None),
        )
        return list(db.execute(stmt).scalars())

    @staticmethod
    def list_requirements_for_release(
        db: Session, *, release_version_id: int,
    ) -> List[DesktopUpdateRequirement]:
        stmt = select(DesktopUpdateRequirement).where(
            DesktopUpdateRequirement.release_version_id == release_version_id,
        )
        return list(db.execute(stmt).scalars())

    # ── Install reports ───────────────────────────────────────────────────

    @staticmethod
    def get_report_by_event(
        db: Session, *, user_id: int, client_event_id: str,
    ) -> Optional[DesktopUpdateReport]:
        stmt = select(DesktopUpdateReport).where(
            DesktopUpdateReport.user_id == user_id,
            DesktopUpdateReport.client_event_id == client_event_id,
        )
        return db.execute(stmt).scalar_one_or_none()

    @staticmethod
    def add_report(db: Session, report: DesktopUpdateReport) -> DesktopUpdateReport:
        db.add(report)
        db.flush()
        return report

    @staticmethod
    def report_counts(db: Session, *, target_version: str) -> Dict[str, int]:
        """outcome -> number of reports for one version, counting each user's
        latest report of an outcome once (a retry is one row already)."""
        stmt = (
            select(DesktopUpdateReport.outcome, func.count(DesktopUpdateReport.id))
            .where(DesktopUpdateReport.target_version == target_version)
            .group_by(DesktopUpdateReport.outcome)
        )
        return {outcome: int(count) for outcome, count in db.execute(stmt).all()}

    # ── Adoption and email, for the history ───────────────────────────────

    @staticmethod
    def client_versions_by_platform(db: Session) -> List[Tuple[Optional[str], str, int]]:
        """(platform, app_version, users) across the fleet."""
        stmt = (
            select(
                DesktopClientVersion.platform,
                DesktopClientVersion.app_version,
                func.count(DesktopClientVersion.id),
            )
            .group_by(DesktopClientVersion.platform, DesktopClientVersion.app_version)
        )
        return [(platform, version, int(count)) for platform, version, count in db.execute(stmt).all()]

    @staticmethod
    def email_counts(db: Session, *, version: str) -> Dict[str, int]:
        """status -> number of release announcements queued for one version.

        Keyed on the announcement's dedupe key prefix (``release:<version>:``),
        which is how the outbox identifies the event; the rehearsal keys
        (``release-test:``) are a different namespace and are not counted.
        """
        prefix = f"release:{version}:user:"
        stmt = (
            select(EmailNotification.status, func.count(EmailNotification.id))
            .where(
                EmailNotification.notification_type == TYPE_RELEASE,
                EmailNotification.dedupe_key.like(prefix.replace("%", r"\%") + "%", escape="\\"),
            )
            .group_by(EmailNotification.status)
        )
        return {status: int(count) for status, count in db.execute(stmt).all()}

    @staticmethod
    def list_history(
        db: Session, *, release_version_id: int, limit: int = 50,
    ) -> List[ActivityLog]:
        """The audit rows written about one version record, newest first."""
        stmt = (
            select(ActivityLog)
            .where(
                ActivityLog.module == ActivityLogModule.SYSTEM,
                ActivityLog.action.in_(list(ActivityLogAction.RELEASE_ACTIONS)),
                ActivityLog.entity_id == release_version_id,
            )
            .order_by(ActivityLog.created_at.desc(), ActivityLog.id.desc())
            .limit(limit)
        )
        return list(db.execute(stmt).scalars())

    @staticmethod
    def list_recent_history(db: Session, *, limit: int = 100) -> List[ActivityLog]:
        stmt = (
            select(ActivityLog)
            .where(
                ActivityLog.module == ActivityLogModule.SYSTEM,
                ActivityLog.action.in_(list(ActivityLogAction.RELEASE_ACTIONS)),
            )
            .order_by(ActivityLog.created_at.desc(), ActivityLog.id.desc())
            .limit(limit)
        )
        return list(db.execute(stmt).scalars())

"""The version-level release record, the grace-period ledger and install reports.

Why three more tables beside ``desktop_releases``
-------------------------------------------------
``desktop_releases`` is one row per *artifact* (Windows, macOS arm64, macOS
x86_64), and that stays the unit a client downloads. What an administrator
decides about a *version* -- its title, which platforms it is for, whether it is
mandatory, how long the grace period is, which older versions must take it, and
whether it is published -- was smeared across those rows as ``force_update`` and
``min_supported_version``, so the three rows of one version could disagree.
``desktop_release_versions`` holds that decision exactly once.

``desktop_update_requirements`` is the grace period. One row per user, per
platform, per required floor: created by the server the first time that user's
desktop meets the policy, stamped with the server's clock, never extended and
never recreated. A restart, a cleared local cache, a changed system clock or a
thousand repeated checks all find the same row. "Satisfied" is written by the
server when the server itself observes the client at or above the floor; a
client cannot mark its own requirement done.

``desktop_update_reports`` is what the desktop says about an update it ran:
installed, failed, not applied, download or verification refused. Keyed on a
client event id so a retried report is one row. It is evidence for the release
history, not a policy input -- nothing here decides whether a client is current.
"""
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, ForeignKeyConstraint, Identity, Index,
    Integer, String, Text, TIMESTAMP, UniqueConstraint, func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class ReleaseVersionStatus:
    """The lifecycle of a version record.

    ``superseded`` is deliberately not stored: a published version is superseded
    exactly when a newer published version covers every platform it targets,
    and that is a fact derived at read time (``DesktopReleaseService``), so it
    cannot drift out of step with the rows that make it true.
    """

    #: Registered; affects nobody.
    DRAFT = "draft"
    #: An administrator ran the readiness check and it passed. Still affects nobody.
    VALIDATED = "validated"
    #: Live: offered to its target platforms and, if mandatory, enforced.
    PUBLISHED = "published"
    #: Taken back. Its artifacts are no longer offered; see the service for
    #: what happens to a grace period that was already running.
    WITHDRAWN = "withdrawn"

    ALL = (DRAFT, VALIDATED, PUBLISHED, WITHDRAWN)


#: The platforms a version may target. ``sys.platform`` tokens, the same
#: vocabulary ``desktop_releases.platform`` uses.
TARGETABLE_PLATFORMS = ("win32", "darwin")


class DesktopReleaseVersion(Base):
    """What an administrator decided about one desktop version."""

    __tablename__ = "desktop_release_versions"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    #: ``major.minor.patch``; one record per version.
    version: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    version_major: Mapped[int] = mapped_column(Integer, nullable=False)
    version_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    version_patch: Mapped[int] = mapped_column(Integer, nullable=False)

    title: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    #: Version-level notes. Shown in the dialog, the email and the admin panel.
    #: An artifact row's own notes are the fallback when these are empty.
    release_notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    #: Comma-separated subset of ``TARGETABLE_PLATFORMS``. A platform not named
    #: here is never offered this version and never held to its policy.
    target_platforms: Mapped[str] = mapped_column(
        String(64), nullable=False, server_default="win32,darwin",
    )

    #: True: every targeted client below this version must take it, after the
    #: grace period. False: an optional update, which blocks nobody.
    mandatory: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    #: How long a user may postpone a mandatory update, measured from the
    #: server's first sight of their desktop under this policy.
    grace_hours: Mapped[int] = mapped_column(Integer, nullable=False, server_default="24")
    #: The oldest version allowed to keep working, or null. With ``mandatory``
    #: the floor is this value or, when null, the version itself. Without
    #: ``mandatory`` a set floor still makes the update required for clients
    #: below it and nobody else.
    required_min_version: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    #: The policy takes effect no earlier than this instant. Null: at publish.
    effective_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True), nullable=True)

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=ReleaseVersionStatus.DRAFT,
    )

    created_by_user_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    published_by_user_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    withdrawn_by_user_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now(),
    )
    validated_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    #: Stamped on the first publication and kept through a withdrawal.
    published_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    withdrawn_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'validated', 'published', 'withdrawn')",
            name="ck_desktop_release_versions_status",
        ),
        CheckConstraint(
            "grace_hours >= 0 AND grace_hours <= 720",
            name="ck_desktop_release_versions_grace_hours",
        ),
        Index(
            "ix_desktop_release_versions_order",
            "status", "version_major", "version_minor", "version_patch",
        ),
    )

    @property
    def targets(self) -> list:
        return [
            token.strip() for token in (self.target_platforms or "").split(",")
            if token.strip()
        ]

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<DesktopReleaseVersion {self.version} {self.status} targets={self.target_platforms}>"


#: The platform key a requirement is filed under when the desktop named none
#: the server supports. Such a client is held to the strictest active policy
#: of every platform, so changing the claim cannot buy a way around one.
REQUIREMENT_PLATFORM_UNKNOWN = "unknown"


class DesktopUpdateRequirement(Base):
    """One user's grace period for one required floor on one platform."""

    __tablename__ = "desktop_update_requirements"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    #: ``win32``, ``darwin`` or ``unknown`` (see REQUIREMENT_PLATFORM_UNKNOWN).
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    #: The floor the client must reach. The key, with user and platform.
    required_version: Mapped[str] = mapped_column(String(32), nullable=False)
    required_major: Mapped[int] = mapped_column(Integer, nullable=False)
    required_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    required_patch: Mapped[int] = mapped_column(Integer, nullable=False)
    #: The version record whose policy first imposed this floor on this user.
    release_version_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)

    #: Server clock, the first time this desktop met the policy.
    first_seen_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now(),
    )
    #: ``first_seen_at`` plus the policy's grace. Written once; never moved.
    deadline_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    #: What the client was running when first seen, for the history.
    first_seen_client_version: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)

    #: When the server observed this user's desktop at or above the floor.
    satisfied_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    satisfied_version: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now(),
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["user_id"], ["users.id"],
            name="fk_desktop_update_requirements_user", ondelete="CASCADE",
        ),
        # The whole idempotency story: a second encounter, from any device, any
        # restart or any repeated check, finds this row and creates nothing.
        UniqueConstraint(
            "user_id", "platform", "required_version",
            name="uq_desktop_update_requirements_user_platform_floor",
        ),
        Index("ix_desktop_update_requirements_user", "user_id", "platform"),
        Index("ix_desktop_update_requirements_release", "release_version_id"),
    )


class UpdateReportOutcome:
    """What a desktop may report about one update attempt."""

    SUCCESS = "success"
    FAILED = "failed"
    NOT_APPLIED = "not-applied"
    INTERRUPTED = "interrupted"
    DOWNLOAD_FAILED = "download-failed"
    VERIFICATION_FAILED = "verification-failed"
    INSTALL_UNAVAILABLE = "install-unavailable"
    CANCELLED = "cancelled"

    ALL = (
        SUCCESS, FAILED, NOT_APPLIED, INTERRUPTED, DOWNLOAD_FAILED,
        VERIFICATION_FAILED, INSTALL_UNAVAILABLE, CANCELLED,
    )


class DesktopUpdateReport(Base):
    """One update attempt as the desktop reported it."""

    __tablename__ = "desktop_update_reports"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    organization_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    platform: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    architecture: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    from_version: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    target_version: Mapped[str] = mapped_column(String(32), nullable=False)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    #: The desktop's fixed failure category (``checksum-failed``, ...), or null.
    category: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    #: A short, bounded description. Never a path with a username in it.
    detail: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    #: When it happened, placed on the server's clock by age.
    occurred_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    reported_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now(),
    )
    #: The desktop's own id for the attempt; a retried report is one row.
    client_event_id: Mapped[str] = mapped_column(String(64), nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(
            ["user_id"], ["users.id"],
            name="fk_desktop_update_reports_user", ondelete="CASCADE",
        ),
        UniqueConstraint(
            "user_id", "client_event_id", name="uq_desktop_update_reports_event",
        ),
        CheckConstraint(
            "outcome IN ('success', 'failed', 'not-applied', 'interrupted', "
            "'download-failed', 'verification-failed', 'install-unavailable', 'cancelled')",
            name="ck_desktop_update_reports_outcome",
        ),
        Index("ix_desktop_update_reports_target", "target_version", "outcome"),
    )

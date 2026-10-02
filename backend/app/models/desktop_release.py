"""One published (or draft) build of the Monitra desktop client.

Why this table exists
---------------------
The update check used to answer from three configuration values --
``DESKTOP_LATEST_VERSION``, ``DESKTOP_DOWNLOAD_URL``,
``DESKTOP_RELEASE_NOTES_URL``. That was honest and sufficient for an
announcement, but it cannot describe a release: it holds one version for the
whole fleet, so a Windows client and a macOS client are told about the same
artifact, and there is nowhere to record the checksum an auto-updater has to
verify before it runs anything. An updater that downloads and executes a file
it cannot verify is strictly worse than the manual download it replaces.

So a release is a row: one row per *artifact*, because that is the unit a
client actually downloads. Version 2.0.0 is four rows — Windows x86_64, macOS
arm64, macOS x86_64, and the Windows portable zip if one is published — and
each carries its own URL, size and digest.

Scope
-----
Releases are product-wide, not per-organisation. There is deliberately no
``organization_id``: the desktop build is the same software for everyone, and
scoping it per tenant would mean a release could be published for one customer
and silently withheld from another with no way to tell which state was
intended.

Rollback
--------
Rows are never deleted. ``status`` is the only lever, and moving a release to
``disabled`` or ``rolled_back`` immediately stops it being offered — the
lookup only ever considers ``published``. Keeping the row is what makes the
prior version still reachable for a rollback, and what keeps a support report
naming a withdrawn build resolvable.
"""
from datetime import datetime

from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, Identity, Index, Integer, String,
    Text, TIMESTAMP, UniqueConstraint, func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class ReleaseStatus:
    """The lifecycle of a release. Only ``PUBLISHED`` is ever served.

    Four states rather than a boolean, because "not published" covers three
    genuinely different situations that support needs to tell apart: a build
    that is not ready yet, one that was deliberately withdrawn, and one that
    was withdrawn *because it was bad*. Collapsing them would lose the reason.
    """

    #: Built and registered, not yet offered to anyone.
    DRAFT = "draft"
    #: Live. The only status the update check and the download link consider.
    PUBLISHED = "published"
    #: Deliberately taken out of circulation, without a fault implied.
    DISABLED = "disabled"
    #: Withdrawn because the build was bad. Kept for diagnostics and history.
    ROLLED_BACK = "rolled_back"

    ALL = (DRAFT, PUBLISHED, DISABLED, ROLLED_BACK)


class Platform:
    """The platform values a client reports and a release is built for.

    These are ``sys.platform`` strings, not friendly names, because that is
    what the desktop already sends in its ``X-Monitra-Platform`` header and in
    ``desktop_client_versions.platform``. Introducing a second vocabulary
    ("windows", "mac") would mean a translation table, and a translation table
    is where a Windows client eventually gets handed a ``.dmg``.
    """

    WINDOWS = "win32"
    MACOS = "darwin"
    LINUX = "linux"

    ALL = (WINDOWS, MACOS, LINUX)


class DesktopRelease(Base):
    """One downloadable artifact of one desktop version, for one platform."""

    __tablename__ = "desktop_releases"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)

    #: ``major.minor.patch``, exactly as ``desktop/version.py`` spells it.
    version: Mapped[str] = mapped_column(String(32), nullable=False)

    # The version split into its three numbers. Denormalised on purpose: this
    # is what makes "the newest release" a correct ORDER BY instead of a string
    # comparison, under which '1.9.0' sorts after '1.10.0' and the fleet is
    # told to downgrade. The service writes these from `version`; nothing else
    # sets them, so they cannot disagree with it.
    version_major: Mapped[int] = mapped_column(Integer, nullable=False)
    version_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    version_patch: Mapped[int] = mapped_column(Integer, nullable=False)

    #: ``sys.platform`` family this artifact runs on.
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    #: CPU architecture: "x86_64", "arm64". Null means "any architecture on
    #: this platform" — which is the honest answer for the Windows build,
    #: where one installer serves every supported machine. It is *not* a
    #: wildcard that may be handed to a client asking for something specific
    #: when a matching build exists; see the repository's ordering.
    architecture: Mapped[str | None] = mapped_column(String(32), nullable=True)

    #: Where the artifact actually lives. Validated as an absolute http(s) URL
    #: on the way in, so nothing can register a `file:` or `javascript:` target
    #: that a client would then be asked to fetch.
    download_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    #: Size in bytes, for the progress bar and the disk-space check.
    file_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    #: Lower-case hex SHA-256 of the artifact. Required: this column is the
    #: entire reason the desktop may run what it downloaded.
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)

    #: Human-written "what changed for you". Plain text, shown in the dialog.
    release_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Optional link to the fuller notes.
    release_notes_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=ReleaseStatus.DRAFT,
    )

    #: True when every client below this release must take it before carrying
    #: on. Distinct from `min_supported_version` on purpose — see the service.
    force_update: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false",
    )
    #: The oldest version still allowed to keep working. A client below it is
    #: required to update; a client at or above it is not, even if this release
    #: is newer. Null means "no floor", which is the normal case.
    min_supported_version: Mapped[str | None] = mapped_column(String(32), nullable=True)

    #: Whether the release pipeline verified this artifact's code signature
    #: before registering it. A process gate (see `desktop_release_policy`), not
    #: the security boundary: the desktop verifies the real signature itself.
    signed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false",
    )
    #: Who signed it, as the pipeline read it from the verified signature
    #: (certificate subject, or the Apple team). For support reports only.
    signer: Mapped[str | None] = mapped_column(String(255), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now(),
    )
    #: When this release was moved to `published`. Null while it never has been.
    published_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True,
    )

    __table_args__ = (
        # One row per artifact. The architecture is part of the identity
        # because macOS ships arm64 and x86_64 as separate builds of the same
        # version. Postgres treats NULLs as distinct in a unique index, so the
        # "any architecture" row is additionally guarded by the partial index
        # created in the migration.
        UniqueConstraint(
            "version", "platform", "architecture",
            name="uq_desktop_releases_version_platform_arch",
        ),
        CheckConstraint(
            "status IN ('draft', 'published', 'disabled', 'rolled_back')",
            name="ck_desktop_releases_status",
        ),
        CheckConstraint("file_size IS NULL OR file_size > 0",
                        name="ck_desktop_releases_file_size"),
        # The lookup is always "newest published build for this platform", so
        # the index carries the sort key as well as the filter.
        Index(
            "ix_desktop_releases_lookup",
            "platform", "status",
            "version_major", "version_minor", "version_patch",
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<DesktopRelease {self.version} {self.platform}"
            f"/{self.architecture or 'any'} {self.status}>"
        )

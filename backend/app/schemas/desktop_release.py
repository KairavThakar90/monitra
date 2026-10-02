"""Schemas for desktop releases, the update check, and fleet visibility.

Backward compatibility note: ``LatestVersionResponse`` and the two fleet
schemas are the shapes production desktop clients already consume. Fields have
been *added* to the update-check response and nothing has been removed or
renamed, so a client built against the old shape keeps working unchanged —
it simply ignores the new keys. The desktop's own parser reads by key and
tolerates absent ones, which is what makes that safe in both directions during
a rollout.
"""
from datetime import datetime
from typing import Annotated, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.core.validation import (
    OptionalDescription, OptionalUrl, OptionalVersion, Sha256, Version,
    plain_text_field, url_field,
)

#: Platform and architecture are short machine tokens, not prose. They reuse
#: the plain-text rule with a tight override rather than inventing a limit --
#: the catalogue's `max_length` override is exactly what this case is for.
PlatformToken = Annotated[str, plain_text_field(label="Platform", max_length=32, required=True)]
OptionalArchToken = Annotated[
    Optional[str], plain_text_field(label="Architecture", max_length=32)
]
OptionalSigner = Annotated[
    Optional[str], plain_text_field(label="Signer", max_length=255)
]


class LatestVersionResponse(BaseModel):
    """What the desktop client asks for on its periodic update check.

    `latest_version` is null when the deployment has not been told what the
    current release is. That is an honest "unknown", and the client treats it
    as "no update to announce" — it must never be filled in with a guess, or
    every user would be prompted to install a version that does not exist.

    The fields below `client_version` were added for the auto-updater. They are
    all optional, and a client that predates them ignores them: an older build
    keeps behaving exactly as it did, announcing the update and linking to the
    download page rather than installing anything.
    """

    #: The newest published release, or null if this deployment does not know.
    latest_version: Optional[str] = None
    #: Where to download it. Null whenever `latest_version` is null.
    download_url: Optional[str] = None
    #: Optional link to the release notes for that version.
    release_notes_url: Optional[str] = None
    #: True only when a known latest version is strictly newer than the
    #: version the caller reported. The server decides this, not the client,
    #: so the comparison rule lives in exactly one place.
    update_available: bool = False
    #: The version the server understood the caller to be running, echoed back
    #: so a mis-parsed User-Agent is visible instead of silent.
    client_version: Optional[str] = None

    # ── Added for the auto-updater ────────────────────────────────────────
    #: SHA-256 of the artifact at `download_url`. The desktop refuses to run
    #: anything it cannot match against this, so its absence means "announce,
    #: do not install" rather than "install without checking".
    sha256: Optional[str] = None
    #: Size in bytes, for the progress bar and the free-space check.
    file_size: Optional[int] = None
    #: The human-written notes shown in the update dialog.
    release_notes: Optional[str] = None
    #: True when this client must update before it may carry on being used.
    #: Distinct from `update_available`: an update can be available without
    #: being mandatory, and that is the ordinary case.
    force_update: bool = False
    #: The oldest version this deployment still supports, or null for no floor.
    min_supported_version: Optional[str] = None
    #: Platform and architecture the artifact above is built for, echoed back
    #: so a client can refuse a payload that does not match the machine it is
    #: running on rather than downloading something it cannot install.
    platform: Optional[str] = None
    architecture: Optional[str] = None


class DesktopClientVersionRead(BaseModel):
    """One user's last-seen desktop version."""

    model_config = ConfigDict(from_attributes=True)

    user_id: int
    app_version: str
    platform: Optional[str] = None
    first_seen_at: datetime
    last_seen_at: datetime


class FleetVersionsResponse(BaseModel):
    """Fleet view: who is on what, and how many clients per version."""

    latest_version: Optional[str] = None
    #: version -> number of users last seen on it.
    counts: dict = Field(default_factory=dict)
    clients: List[DesktopClientVersionRead] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Release management
# ---------------------------------------------------------------------------


class DesktopReleaseCreate(BaseModel):
    """Register one artifact of one version. Written by the release pipeline.

    A release is created as a *draft*: registering a build and deciding it is
    good are two different acts, and CI can only do the first. Publishing is a
    separate, deliberate call.
    """

    version: Version
    platform: PlatformToken
    architecture: OptionalArchToken = None
    #: Must be absolute http(s). The validator rejects anything else, which is
    #: what stops a `file:` or `javascript:` target ever reaching a client.
    download_url: Annotated[str, url_field(label="Download URL", required=True)]
    sha256: Sha256
    file_size: Optional[int] = Field(default=None, gt=0)
    release_notes: OptionalDescription = None
    release_notes_url: OptionalUrl = None
    force_update: bool = False
    min_supported_version: OptionalVersion = None
    #: Whether the pipeline verified this artifact's code signature before
    #: registering it, and who signed it. Gate for publishing, see
    #: `desktop_release_policy`; the desktop verifies the real signature itself.
    signed: bool = False
    signer: OptionalSigner = None


class DesktopReleaseUpdate(BaseModel):
    """Amend a release. Version, platform and architecture are its identity
    and are deliberately not amendable — a build's identity may not be
    rewritten under the clients that already downloaded it. To correct those,
    register a new row.
    """

    release_notes: OptionalDescription = None
    release_notes_url: OptionalUrl = None
    force_update: Optional[bool] = None
    min_supported_version: OptionalVersion = None
    status: Optional[str] = None


class DesktopReleaseRead(BaseModel):
    """A release as the management API returns it."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    version: str
    platform: str
    architecture: Optional[str] = None
    download_url: str
    file_size: Optional[int] = None
    sha256: str
    release_notes: Optional[str] = None
    release_notes_url: Optional[str] = None
    status: str
    force_update: bool
    min_supported_version: Optional[str] = None
    signed: bool = False
    signer: Optional[str] = None
    created_at: datetime
    published_at: Optional[datetime] = None


class DesktopReleaseListResponse(BaseModel):
    releases: List[DesktopReleaseRead] = Field(default_factory=list)


class ReleaseProblemRead(BaseModel):
    """One reason a version is not ready to publish."""

    code: str
    message: str
    artifact: Optional[str] = None


class ReleaseReadinessResponse(BaseModel):
    """Whether a version has a complete, valid set of artifacts to publish."""

    version: str
    ready: bool
    #: The artifacts (platform or platform/arch) that are registered and live.
    present: List[str] = Field(default_factory=list)
    problems: List[ReleaseProblemRead] = Field(default_factory=list)


class PublishVersionResponse(BaseModel):
    """The outcome of publishing a whole version at once."""

    version: str
    published: List[DesktopReleaseRead] = Field(default_factory=list)
    #: Whether the release announcement was queued by this call. Always false
    #: while RELEASE_EMAIL_ENABLED is off.
    announcement_queued: bool = False


class PublicReleaseResponse(BaseModel):
    """What the website's download button needs, and nothing more.

    Unauthenticated, so it is deliberately narrow: the version, where to get
    it, how big it is and its checksum. No draft is ever visible here, and no
    field describes anything about the deployment or its users.
    """

    version: Optional[str] = None
    platform: Optional[str] = None
    architecture: Optional[str] = None
    download_url: Optional[str] = None
    file_size: Optional[int] = None
    sha256: Optional[str] = None
    release_notes: Optional[str] = None
    release_notes_url: Optional[str] = None
    published_at: Optional[datetime] = None
    #: False when this deployment has published nothing for the platform asked
    #: about. An honest "there is no download" beats a link to nowhere.
    available: bool = False


class PublicReleaseIndexResponse(BaseModel):
    """Every platform's current download, for a page that lists them all."""

    #: platform token -> the newest published artifact for it.
    downloads: Dict[str, PublicReleaseResponse] = Field(default_factory=dict)
    #: The newest published version across all platforms, for display.
    latest_version: Optional[str] = None

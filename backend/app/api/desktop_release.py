"""Desktop release endpoints.

Three audiences, three levels of access, kept apart deliberately:

* **The desktop client** (`/desktop/latest-version`) — authenticated. Asks what
  it should be running and gets back everything it needs to install it.
* **The website** (`/desktop/releases/latest`, `/desktop/releases/download`) —
  unauthenticated, because a person downloading Monitra for the first time has
  no account yet. These only ever expose *published* releases.
* **Release management** (`/desktop/releases` and friends) — gated on
  `manage_desktop_releases`, which only administrators hold. Drafts are visible
  here and nowhere else.

The two endpoints that existed before the release table (`/latest-version` and
`/client-versions`) keep their paths, their methods and their response shapes.
Fields were added to the first; nothing was removed or renamed.
"""
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import get_current_user, require_permission
from app.models.user import User
from app.schemas.desktop_release import (
    DesktopReleaseCreate, DesktopReleaseListResponse, DesktopReleaseRead,
    DesktopReleaseUpdate, FleetVersionsResponse, LatestVersionResponse,
    PublicReleaseIndexResponse, PublicReleaseResponse, PublishVersionResponse,
    ReleaseProblemRead, ReleaseReadinessResponse, ReleaseVersionCreate,
    ReleaseVersionDetail, ReleaseVersionListResponse, ReleaseVersionRead,
    ReleaseVersionUpdate, UpdatePolicyResponse, UpdateReportCreate,
    UpdateReportRead, WithdrawVersionResponse,
)
from app.services.desktop_release import (
    DesktopReleaseService, ReleaseConflict, ReleasePolicyViolation,
    parse_client_version,
)

router = APIRouter(prefix="/desktop", tags=["Desktop releases"])

#: Only an administrator may register, amend or publish a release. Gating this
#: is the difference between a release channel and an arbitrary-code-execution
#: channel: whoever can publish a release decides what every installed client
#: downloads and runs.
require_release_manager = require_permission("manage_desktop_releases")


def require_release_publisher(
    current_user: User = Depends(require_release_manager),
) -> User:
    """A person who may decide what clients are offered -- not a pipeline key.

    Registering a build is the pipeline's job; deciding it is good enough to
    reach every staff machine is a human's. The release credential holds
    `manage_desktop_releases` so it can register drafts, and this is what stops
    that same key from also publishing, withdrawing or announcing one. A leaked
    CI secret then costs a stray draft, never a release.
    """
    if getattr(current_user, "is_service_principal", False):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Release credentials may register builds but cannot publish or "
                "withdraw them. A signed-in administrator must do that."
            ),
        )
    return current_user


#: The same shape the version appears in everywhere else (`major.minor.patch`).
_VERSION_PATH = Path(..., pattern=r"^\d+\.\d+\.\d+$", max_length=32)


def _violation(exc: ReleasePolicyViolation) -> HTTPException:
    """422 carrying every problem found, in a shape a person or a script can read."""
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail={
            "message": str(exc),
            "problems": [problem.as_dict() for problem in exc.problems],
        },
    )


# ── The desktop client's update check ──────────────────────────────────────


@router.get("/latest-version", response_model=LatestVersionResponse)
def get_latest_version(
    user_agent: Optional[str] = Header(default=None, alias="User-Agent"),
    platform: Optional[str] = Header(default=None, alias="X-Monitra-Platform"),
    architecture: Optional[str] = Header(default=None, alias="X-Monitra-Arch"),
    current_version: Optional[str] = Query(
        default=None,
        max_length=32,
        description=(
            "The version the client is running. Optional: it is normally read "
            "from the Monitra/<version> User-Agent the client already sends, "
            "and this parameter only overrides that."
        ),
    ),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db, scope="function"),
):
    """The latest published desktop release, and whether the caller is behind it.

    The desktop client polls this on a slow loop. Two things happen here and
    nothing else: the caller is told what the current release is for the
    machine it is running on, and its own version — read from the
    `Monitra/<version>` User-Agent it already sends — is recorded for fleet
    visibility.

    The platform and architecture headers are what keep a Windows client from
    ever being offered a `.dmg`. A caller that sends neither is answered from
    configuration only, which carries no checksum and therefore cannot drive an
    install — an honest degradation rather than a wrong artifact.

    When the deployment has published nothing and has no configured version,
    every field is null and `update_available` is false. That is deliberate: an
    update prompt pointing at a version nobody published would be worse than no
    prompt at all.
    """
    # The header is the normal source; the query parameter exists so a manual
    # "check for updates" can ask about a specific version, and so this is
    # testable without forging a User-Agent.
    client_version = current_version or parse_client_version(user_agent)
    return DesktopReleaseService.latest_version(
        db, current_user, client_version, platform, architecture
    )


@router.get("/update-policy", response_model=UpdatePolicyResponse)
def get_update_policy(
    user_agent: Optional[str] = Header(default=None, alias="User-Agent"),
    platform: Optional[str] = Header(default=None, alias="X-Monitra-Platform"),
    architecture: Optional[str] = Header(default=None, alias="X-Monitra-Arch"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db, scope="function"),
):
    """Where this desktop stands under the active mandatory-update policy.

    The update check's answer plus the authoritative grace status: whether an
    update is required, the required version, when this user's desktop first
    met the policy, the deadline, whether it has passed, and the release to
    install. Every timestamp is the server's. Exempt from the update gate, so
    a blocked client can always read it and recover.
    """
    return DesktopReleaseService.update_policy_view(
        db, current_user, parse_client_version(user_agent), platform, architecture,
    )


@router.post(
    "/update-reports",
    response_model=UpdateReportRead,
    status_code=status.HTTP_201_CREATED,
)
def report_update_outcome(
    payload: UpdateReportCreate,
    platform: Optional[str] = Header(default=None, alias="X-Monitra-Platform"),
    architecture: Optional[str] = Header(default=None, alias="X-Monitra-Arch"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db, scope="function"),
):
    """The desktop reports how an update attempt ended.

    Idempotent on the client's own event id: a retried report answers with
    the row already stored (200 rather than 201 would be more precise, but
    the client treats either as delivered). This is evidence for the release
    history; it never marks a requirement satisfied -- the server does that
    only on observing the client's version itself.
    """
    try:
        row, _created = DesktopReleaseService.record_update_report(
            db, current_user, payload, platform, architecture,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    return UpdateReportRead.model_validate(row)


@router.get(
    "/client-versions",
    response_model=FleetVersionsResponse,
    dependencies=[Depends(require_permission("view_employees"))],
)
def get_fleet_client_versions(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db, scope="function"),
):
    """Which desktop version each user in the organization was last seen on.

    Answers "did everyone move off the bad build?" without asking each person.
    Gated behind the same permission as viewing employees, because it is a
    per-person view of staff machines.
    """
    return DesktopReleaseService.fleet_versions(db, current_user)


# ── The public download ────────────────────────────────────────────────────


@router.get("/releases/latest", response_model=PublicReleaseResponse)
def get_public_latest_release(
    platform: str = Query(
        ...,
        max_length=32,
        description="Platform token: win32, darwin, or a friendly alias.",
    ),
    arch: Optional[str] = Query(
        default=None, max_length=32,
        description="CPU architecture: x86_64 or arm64. Required for macOS.",
    ),
    db: Session = Depends(get_db, scope="function"),
):
    """The newest published artifact for one platform.

    Unauthenticated on purpose — someone installing Monitra for the first time
    has no account. Only published releases are ever visible: a draft is not a
    release, and exposing one here would let anyone download a build nobody has
    approved.
    """
    return DesktopReleaseService.public_latest(db, platform=platform, architecture=arch)


@router.get("/releases/downloads", response_model=PublicReleaseIndexResponse)
def get_public_download_index(db: Session = Depends(get_db, scope="function")):
    """Every platform's current download, for the website's download page.

    This is what makes the download button always current: the page asks for
    "the latest", not for a versioned filename, so a release published six
    months from now is served without the site being redeployed.
    """
    return DesktopReleaseService.public_index(db)


@router.get("/releases/download", response_class=RedirectResponse)
def download_latest_release(
    platform: str = Query(..., max_length=32),
    arch: Optional[str] = Query(default=None, max_length=32),
    db: Session = Depends(get_db, scope="function"),
):
    """Redirect straight to the newest published artifact for a platform.

    A plain link a download button can point at without any JavaScript. It
    redirects rather than proxying the bytes: the artifact lives in release
    storage, and streaming gigabytes through the API would put the whole
    download path behind this application's availability.

    404 when nothing is published for that platform — an error the caller can
    act on, rather than a redirect to nowhere.
    """
    answer = DesktopReleaseService.public_latest(
        db, platform=platform, architecture=arch
    )
    if not answer.available or not answer.download_url:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No published Monitra release is available for that platform yet.",
        )
    # 302, not 301: the target changes with every release, and a permanent
    # redirect would be cached by browsers and pin users to one version.
    return RedirectResponse(
        url=answer.download_url, status_code=status.HTTP_302_FOUND
    )


# ── Release management ─────────────────────────────────────────────────────


@router.get(
    "/releases",
    response_model=DesktopReleaseListResponse,
    dependencies=[Depends(require_release_manager)],
)
def list_releases(
    status_filter: Optional[str] = Query(default=None, alias="status", max_length=16),
    platform: Optional[str] = Query(default=None, max_length=32),
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db, scope="function"),
):
    """Every release, drafts included. Administrators only."""
    try:
        releases = DesktopReleaseService.list_releases(
            db, status=status_filter, platform=platform, limit=limit, offset=offset
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail=str(exc))
    return DesktopReleaseListResponse(
        releases=[DesktopReleaseRead.model_validate(r) for r in releases]
    )


@router.post(
    "/releases",
    response_model=DesktopReleaseRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_release_manager)],
)
def create_release(
    payload: DesktopReleaseCreate,
    current_user: User = Depends(require_release_manager),
    db: Session = Depends(get_db, scope="function"),
):
    """Register a built artifact. Created as a draft, never published here.

    This is the call the release pipeline makes once a build has passed its own
    checks. CI can prove a build compiles and starts; it cannot decide the
    build is good, so it does not get to publish one.
    """
    try:
        release = DesktopReleaseService.create_release(db, payload, actor=current_user)
    except ReleasePolicyViolation as exc:
        raise _violation(exc)
    except ReleaseConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    return DesktopReleaseRead.model_validate(release)


# ── Version records: what an administrator manages ─────────────────────────


@router.get(
    "/releases/versions",
    response_model=ReleaseVersionListResponse,
    dependencies=[Depends(require_release_manager)],
)
def list_release_versions(
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db, scope="function"),
):
    """Every version record, newest first, with its artifacts and readiness.

    The release history the admin panel lists. `status` is `superseded` for a
    published version that a newer published version covers on every platform
    it targets.
    """
    return ReleaseVersionListResponse(
        versions=DesktopReleaseService.list_versions(db, limit=limit, offset=offset)
    )


@router.post(
    "/releases/versions",
    response_model=ReleaseVersionDetail,
    status_code=status.HTTP_201_CREATED,
)
def create_release_version(
    payload: ReleaseVersionCreate,
    current_user: User = Depends(require_release_manager),
    db: Session = Depends(get_db, scope="function"),
):
    """Create a version record (a draft), optionally with its artifacts.

    An administrator's "New release". The artifacts, if given, pass through
    exactly the validation the pipeline's registrations do (hosting policy,
    file name, digest shape); the policy fields (mandatory, grace, floor,
    targets) are the version's and apply to every artifact of it.
    """
    try:
        DesktopReleaseService.create_version(db, payload, actor=current_user)
    except ReleasePolicyViolation as exc:
        raise _violation(exc)
    except ReleaseConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    return DesktopReleaseService.version_detail(db, payload.version)


@router.get(
    "/releases/versions/{version}",
    response_model=ReleaseVersionDetail,
    dependencies=[Depends(require_release_manager)],
)
def get_release_version(version: str = _VERSION_PATH, db: Session = Depends(get_db, scope="function")):
    """One version record with its artifacts, readiness, statistics and audit
    history: targeted users, grace periods (pending / expired / satisfied),
    install reports, adoption, and the announcement's outbox status."""
    detail = DesktopReleaseService.version_detail(db, version)
    if detail is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Release not found.")
    return detail


@router.patch(
    "/releases/versions/{version}",
    response_model=ReleaseVersionDetail,
)
def update_release_version(
    payload: ReleaseVersionUpdate,
    version: str = _VERSION_PATH,
    current_user: User = Depends(require_release_manager),
    db: Session = Depends(get_db, scope="function"),
):
    """Amend a version's title, notes, targets or update policy. Audited."""
    try:
        record = DesktopReleaseService.update_version(db, version, payload, actor=current_user)
    except ReleasePolicyViolation as exc:
        raise _violation(exc)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Release not found.")
    return DesktopReleaseService.version_detail(db, version)


@router.post(
    "/releases/versions/{version}/validate",
    response_model=ReleaseVersionDetail,
)
def validate_release_version(
    version: str = _VERSION_PATH,
    current_user: User = Depends(require_release_manager),
    db: Session = Depends(get_db, scope="function"),
):
    """Run the readiness check and record that it passed (`validated`).

    Affects nobody: it is the administrator's note that the artifact set was
    checked. The answer lists every problem when it did not pass.
    """
    record, _report = DesktopReleaseService.validate_version(db, version, actor=current_user)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Release not found.")
    return DesktopReleaseService.version_detail(db, version)


@router.post(
    "/releases/versions/{version}/withdraw",
    response_model=WithdrawVersionResponse,
)
def withdraw_release_version(
    version: str = _VERSION_PATH,
    reason: Optional[str] = Query(default=None, max_length=200),
    current_user: User = Depends(require_release_publisher),
    db: Session = Depends(get_db, scope="function"),
):
    """Withdraw a version: its artifacts stop being offered at once.

    A grace period already running against this version's floor is kept, not
    erased: its enforcement is suspended while nothing installable exists for
    the floor, and a corrective release published with the same floor resumes
    it with the original deadline. The answer says how many are waiting.
    """
    record, waiting = DesktopReleaseService.withdraw_version(
        db, version, actor=current_user, reason=reason,
    )
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Release not found.")
    detail = DesktopReleaseService.version_detail(db, version)
    return WithdrawVersionResponse(version=detail, suspended_requirements=waiting)


@router.patch(
    "/releases/{release_id}",
    response_model=DesktopReleaseRead,
    dependencies=[Depends(require_release_manager)],
)
def update_release(
    release_id: int,
    payload: DesktopReleaseUpdate,
    db: Session = Depends(get_db, scope="function"),
    current_user: User = Depends(require_release_manager),
):
    """Amend a release's notes, its update policy, or its status.

    Changing the *status* publishes or withdraws a build, which is a person's
    decision: it is refused for a release credential (see
    `require_release_publisher`).
    """
    if payload.status is not None:
        require_release_publisher(current_user)
    try:
        release = DesktopReleaseService.update_release(db, release_id, payload)
    except ReleasePolicyViolation as exc:
        raise _violation(exc)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail=str(exc))
    if release is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Release not found.")
    return DesktopReleaseRead.model_validate(release)


@router.get(
    "/releases/versions/{version}/readiness",
    response_model=ReleaseReadinessResponse,
    dependencies=[Depends(require_release_manager)],
)
def get_version_readiness(version: str = _VERSION_PATH, db: Session = Depends(get_db, scope="function")):
    """Is this version complete and valid enough to publish, and if not, why?

    Read-only. Lists every missing or invalid artifact at once, so a release can
    be fixed in one pass instead of one refusal at a time.
    """
    report = DesktopReleaseService.readiness(db, version)
    return ReleaseReadinessResponse(
        version=report.version,
        ready=report.ready,
        present=report.present,
        problems=[ReleaseProblemRead(**problem.as_dict()) for problem in report.problems],
    )


@router.post(
    "/releases/versions/{version}/publish",
    response_model=PublishVersionResponse,
    dependencies=[Depends(require_release_publisher)],
)
def publish_version(
    version: str = _VERSION_PATH,
    current_user: User = Depends(require_release_publisher),
    db: Session = Depends(get_db, scope="function"),
):
    """Publish every required artifact of a version together, or none of them.

    The release announcement is queued by this call once the set is complete
    (and only if `RELEASE_EMAIL_ENABLED`). Prefer this to publishing rows one at
    a time: Windows and both Macs go live together. The version record moves
    to `published` in the same commit, with an audit row naming the publisher.
    """
    try:
        published, queued = DesktopReleaseService.publish_version(db, version, actor=current_user)
    except ReleasePolicyViolation as exc:
        raise _violation(exc)
    return PublishVersionResponse(
        version=version,
        published=[DesktopReleaseRead.model_validate(r) for r in published],
        announcement_queued=queued,
    )


@router.post(
    "/releases/{release_id}/publish",
    response_model=DesktopReleaseRead,
    dependencies=[Depends(require_release_publisher)],
)
def publish_release(release_id: int, db: Session = Depends(get_db, scope="function")):
    """Make a draft live. From this moment clients are offered it.

    Refused unless the whole version -- Windows and both macOS architectures --
    is registered and valid, so one platform is never live without the others.
    """
    from app.models.desktop_release import ReleaseStatus

    try:
        release = DesktopReleaseService.set_status(db, release_id, ReleaseStatus.PUBLISHED)
    except ReleasePolicyViolation as exc:
        raise _violation(exc)
    if release is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Release not found.")
    return DesktopReleaseRead.model_validate(release)


@router.post(
    "/releases/{release_id}/rollback",
    response_model=DesktopReleaseRead,
    dependencies=[Depends(require_release_publisher)],
)
def rollback_release(release_id: int, db: Session = Depends(get_db, scope="function")):
    """Withdraw a bad release.

    The row is kept — deleting it would destroy the rollback inventory and make
    every support report naming this build unresolvable. It simply stops being
    offered, and the previous published version becomes the newest again, which
    is the whole rollback mechanism.
    """
    from app.models.desktop_release import ReleaseStatus

    release = DesktopReleaseService.set_status(db, release_id, ReleaseStatus.ROLLED_BACK)
    if release is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Release not found.")
    return DesktopReleaseRead.model_validate(release)

"""Independent mandatory desktop version enforcement service.

This module provides the central, independent version-enforcement mechanism
for Monitra desktop clients. It is completely independent of the legacy
desktop_release_versions and desktop_update_requirements tables and settings.

Enforcement Model
-----------------
1. The global minimum required version is read from the dedicated
   ``desktop_version_enforcement`` database table (cached in memory with
   thread-safe invalidation).
2. Desktop requests are identified via `User-Agent: Monitra/<version>` and the
   associated `X-Monitra-Platform` / `X-Monitra-Arch` headers.
3. Versions are compared strictly as semantic version tuples (major, minor, patch),
   never as lexicographical strings, guaranteeing correct comparison of
   multi-digit components (e.g. 1.10.0 > 1.9.0, 1.3.10 > 1.3.4).
4. Outdated clients (< minimum_required_version) or desktop clients sending
   missing/malformed version information are refused with HTTP 426 Upgrade Required.
5. Supported clients (>= minimum_required_version), web dashboard users, API clients,
   service credentials, and essential recovery endpoints (stop timer, heartbeat,
   offline batch uploads) proceed normally.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.core.permissions import resolve_role_alias
from app.core.request_context import (
    CLIENT_DESKTOP,
    CLIENT_WEB,
    current_request_context,
)
from app.models.activity_log import ActivityLog, ActivityLogModule
from app.models.desktop_version_enforcement import (
    DEFAULT_DOWNLOAD_URL,
    DEFAULT_MESSAGE,
    DEFAULT_MINIMUM_VERSION,
    DesktopVersionEnforcement,
)
from app.models.user import User
from app.repositories.activity_log import ActivityLogRepository
from app.repositories.desktop_version_enforcement import (
    DesktopVersionEnforcementRepository,
)

logger = logging.getLogger("uvicorn.error")

#: Structured machine-readable error code for mandatory desktop upgrade
UPGRADE_REQUIRED_CODE = "DESKTOP_VERSION_UPDATE_REQUIRED"

#: In-memory cache TTL for the active policy row
POLICY_CACHE_TTL_SECONDS = 15.0

#: Roles permitted to view and update the version enforcement configuration
ADMIN_MANAGE_ROLES = frozenset({"administrator", "org_admin", "super_admin"})

#: Action recorded in activity_logs when enforcement setting is updated
ACTION_VERSION_ENFORCEMENT_UPDATED = "desktop_version_enforcement_updated"

#: Regex matching standard 3-component semantic version strings (e.g. '1.3.4')
_SEMVER_REGEX = re.compile(r"^[vV]?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?$")

#: Paths exempt from version enforcement so that an outdated client can finish
#: running timers, record offline captures, upload crash reports, and authenticate
#: without corrupting user data.
_API_PREFIX = "/api/v1"
EXEMPT_PATH_PATTERNS: Tuple[re.Pattern, ...] = tuple(
    re.compile(p)
    for p in (
        r"^/auth(/|$)",
        r"^/health(/|$)",
        r"^/desktop/latest-version$",
        r"^/desktop/update-policy$",
        r"^/desktop/update-reports$",
        r"^/desktop/releases(/|$)",
        r"^/system/maintenance-status$",
        r"^/system/desktop-version-enforcement(/|$)",
        r"^/time-entries/active$",
        r"^/time-entries/\d+/(stop|heartbeat|screenshots|unwanted-activity|adjustments)$",
        r"^/time-entries/\d+/activity/batch$",
        r"^/time-entries/\d+/app-usage(/batch|/summary)?$",
        r"^/time-entry-activities/batch$",
        r"^/url-usage/batch$",
        r"^/time-entry-screenshots/capture-events$",
        r"^/idle-periods(/|$)",
        r"^/activity-logs/client-events$",
        r"^/screenshot/privacy-config$",
        r"^/screenshots/config$",
    )
)


# ── SemVer Comparison ──────────────────────────────────────────────────────────


def parse_version_tuple(version_str: Optional[str]) -> Optional[Tuple[int, int, int]]:
    """Parse a semantic version string into a numeric (major, minor, patch) tuple.

    Returns None if the string is empty, non-numeric, or malformed.
    Pre-releases (e.g. '1.3.4-beta.1') are returned as (1, 3, 3) to sort strictly
    below the release version per SemVer 2.0 specification.
    """
    if not version_str or not isinstance(version_str, str):
        return None
    raw = version_str.strip()
    match = _SEMVER_REGEX.match(raw)
    if not match:
        return None
    try:
        major = int(match.group(1))
        minor = int(match.group(2))
        patch = int(match.group(3))
        prerelease = match.group(4)
        if prerelease:
            # Pre-release of 1.3.4 is older than 1.3.4 final
            return (major, minor, patch - 1 if patch > 0 else 0)
        return (major, minor, patch)
    except (ValueError, TypeError):
        return None


def compare_versions(v1: str, v2: str) -> int:
    """Compare two semantic version strings.

    Returns:
        -1 if v1 < v2
         0 if v1 == v2
         1 if v1 > v2
    Raises ValueError if either string cannot be parsed as a semantic version.
    """
    t1 = parse_version_tuple(v1)
    if t1 is None:
        raise ValueError(f"Invalid semantic version: {v1!r}")
    t2 = parse_version_tuple(v2)
    if t2 is None:
        raise ValueError(f"Invalid semantic version: {v2!r}")

    if t1 < t2:
        return -1
    elif t1 > t2:
        return 1
    return 0


def validate_download_url(url: Optional[str]) -> bool:
    """Validate that the download URL is a valid, safe HTTPS URL."""
    if not url or not isinstance(url, str):
        return False
    clean = url.strip()
    try:
        parts = urlparse(clean)
        return (
            parts.scheme == "https"
            and bool(parts.netloc)
            and "@" not in parts.netloc
            and not any(c in clean for c in ("\r", "\n", "\t", " "))
        )
    except Exception:
        return False


# ── Policy Cache ──────────────────────────────────────────────────────────────


_cache_lock = threading.Lock()
_cached_setting: Dict[str, Any] = {"at": 0.0, "value": None}


def invalidate_policy_cache() -> None:
    """Invalidate the cached enforcement setting across worker threads."""
    with _cache_lock:
        _cached_setting["at"] = 0.0
        _cached_setting["value"] = None


# ── Service ───────────────────────────────────────────────────────────────────


class DesktopVersionEnforcementService:

    @staticmethod
    def get_setting(db: Session) -> DesktopVersionEnforcement:
        """Fetch the active policy, using the in-memory cache if fresh."""
        now = time.monotonic()
        with _cache_lock:
            cached = _cached_setting["value"]
            if cached is not None and (now - _cached_setting["at"] < POLICY_CACHE_TTL_SECONDS):
                return cached

        try:
            setting = DesktopVersionEnforcementRepository.get_or_create(db)
        except Exception:
            logger.warning(
                "DESKTOP_VERSION_ENFORCEMENT_DB_ERROR: using fallback defaults",
                exc_info=True,
            )
            setting = DesktopVersionEnforcement(
                id=1,
                minimum_version=DEFAULT_MINIMUM_VERSION,
                download_url=DEFAULT_DOWNLOAD_URL,
                message=DEFAULT_MESSAGE,
                is_active=True,
            )

        with _cache_lock:
            _cached_setting["value"] = setting
            _cached_setting["at"] = now
        return setting

    @staticmethod
    def get_status(db: Session) -> Dict[str, Any]:
        """Public/client status readout of version enforcement."""
        setting = DesktopVersionEnforcementService.get_setting(db)
        return {
            "minimum_version": setting.minimum_version,
            "download_url": setting.download_url,
            "message": setting.message,
            "is_active": setting.is_active,
            "server_time": datetime.now(timezone.utc),
        }

    @staticmethod
    def get_detail(db: Session, current_user: User) -> Dict[str, Any]:
        """Administrative inspection of enforcement configuration."""
        DesktopVersionEnforcementService._require_manage(current_user)
        setting = DesktopVersionEnforcementService.get_setting(db)
        return {
            "minimum_version": setting.minimum_version,
            "download_url": setting.download_url,
            "message": setting.message,
            "is_active": setting.is_active,
            "updated_at": setting.updated_at,
            "updated_by_user_id": setting.updated_by_user_id,
            "updated_by_username": setting.updated_by_username,
            "server_time": datetime.now(timezone.utc),
        }

    @staticmethod
    def update_setting(
        db: Session,
        current_user: User,
        *,
        minimum_version: Optional[str] = None,
        download_url: Optional[str] = None,
        message: Optional[str] = None,
        is_active: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """Update the version enforcement configuration. Administrators only."""
        DesktopVersionEnforcementService._require_manage(current_user)

        # Validate minimum_version
        if minimum_version is not None:
            clean_ver = minimum_version.strip()
            if parse_version_tuple(clean_ver) is None:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"Invalid semantic version: {minimum_version!r}. Format must be 'major.minor.patch'.",
                )
            minimum_version = clean_ver

        # Validate download_url
        if download_url is not None:
            clean_url = download_url.strip()
            if not validate_download_url(clean_url):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="Invalid download URL. URL must start with 'https://' and have a valid host.",
                )
            download_url = clean_url

        # Validate message
        if message is not None:
            clean_msg = message.strip()
            if not clean_msg:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="Message cannot be empty.",
                )
            message = clean_msg

        now = datetime.now(timezone.utc)
        setting = DesktopVersionEnforcementRepository.get_for_update(db)
        if setting is None:
            setting = DesktopVersionEnforcement(
                id=1,
                minimum_version=DEFAULT_MINIMUM_VERSION,
                download_url=DEFAULT_DOWNLOAD_URL,
                message=DEFAULT_MESSAGE,
                is_active=True,
            )
            DesktopVersionEnforcementRepository.add(db, setting)

        changes: Dict[str, Any] = {}
        if minimum_version is not None and setting.minimum_version != minimum_version:
            changes["minimum_version"] = (setting.minimum_version, minimum_version)
            setting.minimum_version = minimum_version

        if download_url is not None and setting.download_url != download_url:
            changes["download_url"] = (setting.download_url, download_url)
            setting.download_url = download_url

        if message is not None and setting.message != message:
            changes["message"] = (setting.message, message)
            setting.message = message

        if is_active is not None and setting.is_active != is_active:
            changes["is_active"] = (setting.is_active, is_active)
            setting.is_active = is_active

        if not changes:
            # Idempotent: no modifications
            return DesktopVersionEnforcementService.get_detail(db, current_user)

        setting.updated_at = now
        setting.updated_by_user_id = current_user.id
        setting.updated_by_username = current_user.username

        # Write audit trail in activity_logs
        summary = ", ".join(f"{k}: {old}->{new}" for k, (old, new) in changes.items())
        ActivityLogRepository.add(
            db,
            ActivityLog(
                organization_id=current_user.organization_id,
                user_id=current_user.id,
                module=ActivityLogModule.SYSTEM,
                action=ACTION_VERSION_ENFORCEMENT_UPDATED,
                description=(
                    f"Desktop version enforcement updated by {current_user.username} "
                    f"({summary}) at {now.isoformat()}"
                ),
                created_at=now,
            ),
        )
        db.commit()
        db.refresh(setting)
        invalidate_policy_cache()

        logger.info(
            "DESKTOP_VERSION_ENFORCEMENT_UPDATED admin=%s username=%s changes=%s at=%s",
            current_user.id,
            current_user.username,
            summary,
            now.isoformat(),
        )
        return DesktopVersionEnforcementService.get_detail(db, current_user)

    @staticmethod
    def _require_manage(current_user: User) -> None:
        role = resolve_role_alias((current_user.role_name or "").strip().lower())
        if getattr(current_user, "is_service_principal", False) or role not in ADMIN_MANAGE_ROLES:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Insufficient permissions for this action",
            )


# ── The Enforcement Gate ───────────────────────────────────────────────────────


def strip_api_prefix(path: Optional[str]) -> str:
    value = path or ""
    if value.startswith(_API_PREFIX + "/") or value == _API_PREFIX:
        value = value[len(_API_PREFIX) :] or "/"
    return value


def is_exempt_path(path: Optional[str]) -> bool:
    candidate = strip_api_prefix(path)
    return any(pattern.match(candidate) for pattern in EXEMPT_PATH_PATTERNS)


def build_upgrade_required_response(
    setting: DesktopVersionEnforcement,
    installed_version: Optional[str],
) -> HTTPException:
    """Build the canonical HTTP 426 Upgrade Required response."""
    return HTTPException(
        status_code=status.HTTP_426_UPGRADE_REQUIRED,
        detail={
            "code": UPGRADE_REQUIRED_CODE,
            "message": setting.message,
            "minimum_required_version": setting.minimum_version,
            "download_url": setting.download_url,
            "installed_version": installed_version,
        },
    )


def enforce(db: Session, user: User) -> None:
    """Refuse a protected request from an outdated desktop client.

    Called by ``get_current_user`` for every authenticated API request.
    Non-desktop clients (web dashboard, service principals, third-party API clients)
    and exempt recovery endpoints are never blocked.
    """
    if getattr(user, "is_service_principal", False):
        return

    context = current_request_context()
    if context is None:
        return

    # Check exemption paths first (stop timer, heartbeat, batch uploads, etc.)
    if is_exempt_path(context.path):
        return

    # Determine whether caller is a desktop client
    # 1. User-Agent matches Monitra/<version>
    # 2. X-Monitra-Platform or X-Monitra-Arch header present
    is_desktop = (
        context.client == CLIENT_DESKTOP
        or bool(context.platform)
        or bool(context.architecture)
    )

    if not is_desktop:
        # Web dashboard or external API client: never blocked by desktop enforcement
        return

    # Read the active enforcement setting
    setting = DesktopVersionEnforcementService.get_setting(db)
    if not setting.is_active:
        return

    min_tuple = parse_version_tuple(setting.minimum_version)
    if min_tuple is None:
        logger.error(
            "DESKTOP_VERSION_ENFORCEMENT_CONFIG_INVALID: minimum_version=%r is invalid semver",
            setting.minimum_version,
        )
        return

    # Client version parsing and safe failure
    raw_client_version = context.client_version
    client_tuple = parse_version_tuple(raw_client_version)

    if client_tuple is None:
        # Desktop client did not provide a valid semantic version header
        # (missing, corrupted, or spoofed non-semver string).
        # Fail safe: refuse access rather than allowing an outdated client to bypass.
        logger.warning(
            "DESKTOP_VERSION_REFUSED_MALFORMED user=%s path=%s client_version=%r platform=%s",
            getattr(user, "id", None),
            context.path,
            raw_client_version,
            context.platform,
        )
        raise build_upgrade_required_response(setting, raw_client_version)

    # Semantic comparison: client_tuple < min_tuple
    if client_tuple < min_tuple:
        logger.info(
            "DESKTOP_VERSION_REFUSED user=%s path=%s client=%s required=%s platform=%s",
            getattr(user, "id", None),
            context.path,
            raw_client_version,
            setting.minimum_version,
            context.platform,
        )
        raise build_upgrade_required_response(setting, raw_client_version)


def enforce_request(db: Session) -> None:
    """Enforce minimum desktop version on unauthenticated/handshake requests (login, refresh).

    Refuses outdated desktop clients or desktop clients sending missing/malformed
    version headers with HTTP 426 Upgrade Required.
    Web users and non-desktop API clients pass through unaffected.
    """
    context = current_request_context()
    if context is None:
        return

    if is_exempt_path(context.path):
        return

    is_desktop = (
        context.client == CLIENT_DESKTOP
        or bool(context.platform)
        or bool(context.architecture)
    )
    if not is_desktop:
        return

    setting = DesktopVersionEnforcementService.get_setting(db)
    if not setting.is_active:
        return

    min_tuple = parse_version_tuple(setting.minimum_version)
    if min_tuple is None:
        return

    raw_client_version = context.client_version
    client_tuple = parse_version_tuple(raw_client_version)
    if client_tuple is None or client_tuple < min_tuple:
        logger.info(
            "DESKTOP_VERSION_AUTH_REFUSED path=%s client=%s required=%s platform=%s",
            context.path,
            raw_client_version,
            setting.minimum_version,
            context.platform,
        )
        raise build_upgrade_required_response(setting, raw_client_version)


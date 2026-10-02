"""
What the updater is willing to download and run: the client's own rules.

The backend decides *whether* an update exists. It does not get to decide
*where this machine fetches an executable from*, or *whether that executable
must be signed* — those are properties of this installed build, written here, so
a compromised or mistaken backend cannot widen them. Everything the backend
sends (URL, checksum, size) is treated as a claim to be checked against this
file, not as an instruction.

Three rules live here, in one place:

**Where artifacts come from.** HTTPS only, no credentials in the URL, the
default port, and a host on `ALLOWED_DOWNLOAD_HOSTS`. A GitHub release download
redirects to GitHub's own CDN, so redirect *hops* may additionally land on
`ALLOWED_REDIRECT_HOST_SUFFIXES` — but a hop is always HTTPS, always on an
approved host, and never downgrades (`check_redirect`). The check is applied to
the URL the backend gave and again to every `Location` the download is sent to.

**Whether the artifact must be signed.** In production, always. An unsigned
installer is refused, not installed with a warning: an updater that runs
whatever was served is a code-execution channel, and the project's own decision
record makes signing the prerequisite for enabling this feature at all. The one
exception is a development or staging build with
`MONITRA_UPDATE_ALLOW_UNSIGNED=1`, which exists so the update path itself can
be exercised before a certificate does. A production build ignores it.

**Who must have signed it.** `WINDOWS_SIGNER_PINS` / `MACOS_TEAM_ID_PINS` name
the publisher. A valid signature from *any* certificate authority's customer is
not enough — an attacker can buy one — so once the real certificate exists its
subject is pinned here. Empty means "any valid signature", which is the honest
default until there is a certificate to pin.

Test hooks (`MONITRA_UPDATE_EXTRA_HOSTS`, `MONITRA_UPDATE_ALLOW_UNSIGNED`) are
honoured only when the build is not a production one — the same switch that
already lets a development build talk to a loopback backend. See
`app/config.py`.
"""
from __future__ import annotations

import os
from typing import List, Optional, Tuple
from urllib.parse import urlsplit

from core.logging_setup import get_logger

log = get_logger("updates.policy")

#: Exact hostnames an artifact URL may name. GitHub serves release downloads
#: from github.com; a different CDN is a decision made here, in a release of the
#: client, never by a value the backend sends.
ALLOWED_DOWNLOAD_HOSTS: Tuple[str, ...] = ("github.com",)

#: Host suffixes a *redirect* may land on. github.com answers a release download
#: with a 302 to objects.githubusercontent.com / release-assets.githubusercontent.com.
#: Leading dot: `.githubusercontent.com` matches `objects.githubusercontent.com`
#: and not `evilgithubusercontent.com`.
ALLOWED_REDIRECT_HOST_SUFFIXES: Tuple[str, ...] = (".githubusercontent.com",)

#: Most redirect hops followed. GitHub uses one; more than a handful is a loop
#: or a hostile server, and either way a download is not worth continuing.
MAX_REDIRECTS = 5

#: Authenticode signer display names (certificate "simple display name": the
#: subject's CN, or its organisation) that may have signed a Windows installer.
#: Empty = any validly signed installer. SET THIS when the signing certificate
#: is purchased; see docs/Desktop_Artifact_Hosting.md.
WINDOWS_SIGNER_PINS: Tuple[str, ...] = ()

#: Apple Developer Team IDs that may have signed a macOS build. Empty = any
#: Developer ID signature that passes Gatekeeper. SET THIS with the real team.
MACOS_TEAM_ID_PINS: Tuple[str, ...] = ()

#: Redirect statuses a download follows.
REDIRECT_STATUSES = (301, 302, 303, 307, 308)


def _is_production() -> bool:
    """Whether this build is a production one. Fails closed: if the answer
    cannot be determined, it is treated as production."""
    try:
        from app.config import settings

        return bool(settings.is_production)
    except Exception:  # noqa: BLE001 - unknown means strict
        log.warning("could not determine the build environment; assuming production")
        return True


def _csv_env(name: str) -> List[str]:
    return [p.strip().lower() for p in os.environ.get(name, "").split(",") if p.strip()]


def allowed_hosts() -> Tuple[str, ...]:
    """The hosts an artifact URL may name for this build.

    The extra-hosts hook adds to the list only outside production, so a packaged
    production client cannot be pointed at a different download host by an
    environment variable.
    """
    if _is_production():
        return ALLOWED_DOWNLOAD_HOSTS
    return ALLOWED_DOWNLOAD_HOSTS + tuple(_csv_env("MONITRA_UPDATE_EXTRA_HOSTS"))


def unsigned_allowed() -> bool:
    """Whether an unsigned installer may be run. False in production, always."""
    if _is_production():
        return False
    return os.environ.get("MONITRA_UPDATE_ALLOW_UNSIGNED", "").strip() in ("1", "true", "yes")


def _parts(url: str):
    try:
        parts = urlsplit(url)
        return parts, parts.port
    except ValueError:
        return None, None


def check_url(url: Optional[str]) -> Optional[str]:
    """Why `url` may not be fetched as an update artifact, or None if it may.

    The returned text is for the log, not the user: it names the offending host.
    """
    if not isinstance(url, str) or not url:
        return "the download URL is missing"
    if any(ch in url for ch in (" ", "\\", "\t", "\r", "\n", "\x00")):
        return "the download URL contains characters that are not allowed"
    parts, port = _parts(url)
    if parts is None:
        return "the download URL is not a valid URL"
    if parts.scheme.lower() != "https":
        return f"the download URL is not https ({parts.scheme or 'no scheme'})"
    host = (parts.hostname or "").lower()
    if not host:
        return "the download URL has no host"
    if parts.username is not None or parts.password is not None:
        return "the download URL carries credentials"
    if port not in (None, 443):
        return f"the download URL uses a non-default port ({port})"
    if host not in allowed_hosts():
        return f"the download host '{host}' is not an approved release host"
    return None


def check_redirect(url: Optional[str]) -> Optional[str]:
    """Why a redirect `Location` may not be followed, or None if it may.

    Stricter than "any https": a redirect is how a download gets sent somewhere
    its URL did not name, so it is held to the approved hosts and CDN suffixes
    and may never leave https.
    """
    if not isinstance(url, str) or not url:
        return "the redirect has no target"
    parts, port = _parts(url)
    if parts is None:
        return "the redirect target is not a valid URL"
    if parts.scheme.lower() != "https":
        return f"the redirect leaves https ({parts.scheme or 'no scheme'})"
    host = (parts.hostname or "").lower()
    if not host:
        return "the redirect target has no host"
    if parts.username is not None or parts.password is not None:
        return "the redirect target carries credentials"
    if port not in (None, 443):
        return f"the redirect target uses a non-default port ({port})"
    if host in allowed_hosts():
        return None
    if any(host.endswith(suffix) for suffix in ALLOWED_REDIRECT_HOST_SUFFIXES):
        return None
    return f"the redirect target '{host}' is not an approved release host"

"""The rules a desktop release must satisfy before a client may be offered it.

Pure functions over a release row and the settings -- no database, no I/O -- so
every rule can be tested exactly and stated once. ``DesktopReleaseService``
applies them at the three moments that matter: when a row is registered, when
it is published, and when the update check is about to hand it to a client.

Why this exists
---------------
A published row's ``download_url`` is fetched by every installed client and the
file at the end of it is then **executed**. Whoever can write that column
decides what runs on every staff machine, so it is guarded like the code
execution channel it is:

* the URL must be https, on an approved host, optionally under an approved
  prefix, carrying no credentials -- see :func:`check_download_url`;
* the file name must be the one the build scripts produce for that version,
  platform and architecture, so a row cannot name a different version's
  installer, the portable zip, or an unrelated file -- see
  :func:`expected_filename`;
* a version is only ever published as a complete set -- Windows and both macOS
  architectures -- so one platform's users are never offered a release another
  platform's users cannot have -- see :func:`readiness`.

None of this replaces the desktop's own checks (SHA-256, size, signature); it
is the server refusing to *serve* what the client would refuse to *run*.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlsplit

from app.core.config import settings

#: ``(platform, architecture)``. Architecture ``None`` means "one build serves
#: every machine on this platform", as the Windows installer does.
ArtifactKey = Tuple[str, Optional[str]]

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

#: A release row in these states is "in the set" for the purposes of readiness.
#: ``disabled`` and ``rolled_back`` were deliberately withdrawn and must be
#: registered again (as a new row for a new version), never quietly revived.
_LIVE_STATES = ("draft", "published")


def _csv(value: Optional[str]) -> List[str]:
    return [part.strip() for part in (value or "").split(",") if part.strip()]


def allowed_hosts() -> List[str]:
    return [host.lower() for host in _csv(settings.DESKTOP_DOWNLOAD_ALLOWED_HOSTS)]


def allowed_prefixes() -> List[str]:
    return _csv(settings.DESKTOP_DOWNLOAD_URL_PREFIXES)


def required_artifacts() -> List[ArtifactKey]:
    """The artifacts a version must have, from ``DESKTOP_REQUIRED_ARTIFACTS``."""
    required: List[ArtifactKey] = []
    for token in _csv(settings.DESKTOP_REQUIRED_ARTIFACTS):
        platform, _, arch = token.partition(":")
        key = (platform.strip().lower(), (arch.strip().lower() or None))
        if key not in required:
            required.append(key)
    return required


def artifact_label(platform: str, architecture: Optional[str]) -> str:
    return f"{platform}/{architecture}" if architecture else platform


def expected_filename(platform: str, architecture: Optional[str], version: str) -> Optional[str]:
    """The file name the build scripts give this artifact, or None if unknown.

    The names are a contract inside this repository (``packaging/``,
    ``scripts/``, ``desktop/tools/register_release.py``), and the update path
    only ever installs these three kinds of file. The portable zip is
    deliberately not one of them: it has no installer to hand an update to.
    """
    if platform == "win32" and architecture is None:
        return f"Monitra-Setup-{version}.exe"
    if platform == "darwin" and architecture in ("arm64", "x86_64"):
        return f"Monitra-macOS-{architecture}-{version}.dmg"
    return None


def check_download_url(url: Optional[str]) -> Optional[str]:
    """Why ``url`` may not be served as a release artifact, or None if it may.

    Returned as a sentence rather than raised, so a caller can collect every
    problem with a version into one report instead of stopping at the first.
    """
    if not url or not isinstance(url, str):
        return "The download URL is missing."
    if any(ch in url for ch in ("\\", " ", "\t", "\r", "\n", "\x00")):
        return "The download URL contains characters that are not allowed."
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return "The download URL is not a valid URL."

    if parts.scheme.lower() != "https":
        return "The download URL must use https."
    host = (parts.hostname or "").lower()
    if not host:
        return "The download URL has no host."
    if parts.username is not None or parts.password is not None:
        return "The download URL must not carry credentials."
    if port not in (None, 443):
        return "The download URL must use the default https port."
    hosts = allowed_hosts()
    if host not in hosts:
        return (
            f"The download host '{host}' is not an approved release host "
            f"(approved: {', '.join(hosts) or 'none configured'})."
        )
    if ".." in parts.path.split("/"):
        return "The download URL must not contain '..' path segments."
    if parts.fragment:
        return "The download URL must not carry a fragment."

    prefixes = allowed_prefixes()
    if prefixes:
        normalised = f"https://{host}{parts.path}"
        if not any(normalised.startswith(_normalise_prefix(p)) for p in prefixes):
            return "The download URL is not under an approved release location."
    return None


def _normalise_prefix(prefix: str) -> str:
    parts = urlsplit(prefix)
    return f"https://{(parts.hostname or '').lower()}{parts.path}"


@dataclass(frozen=True)
class Problem:
    """One reason a version is not ready to publish."""

    code: str
    message: str
    artifact: Optional[str] = None

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "artifact": self.artifact}


@dataclass
class Readiness:
    """Whether a version may be published, and if not, exactly why."""

    version: str
    problems: List[Problem] = field(default_factory=list)
    present: List[str] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return not self.problems


def check_row(row, *, require_signed: Optional[bool] = None) -> List[Problem]:
    """Everything wrong with one registered artifact, as problems."""
    label = artifact_label(row.platform, row.architecture)
    problems: List[Problem] = []

    url_problem = check_download_url(row.download_url)
    if url_problem:
        problems.append(Problem("url", url_problem, label))
    else:
        expected = expected_filename(row.platform, row.architecture, row.version)
        basename = urlsplit(row.download_url).path.rsplit("/", 1)[-1]
        if expected is not None and basename != expected:
            problems.append(Problem(
                "filename",
                f"The download URL names '{basename}' but this artifact must be "
                f"'{expected}'.",
                label,
            ))

    if not _SHA256_RE.match((row.sha256 or "")):
        problems.append(Problem("sha256", "The SHA-256 is missing or malformed.", label))
    if not row.file_size or row.file_size <= 0:
        problems.append(Problem(
            "file_size",
            "The file size is missing. The desktop verifies it, so a release "
            "cannot be published without one.",
            label,
        ))

    needs_signature = (
        settings.DESKTOP_REQUIRE_SIGNED_RELEASES if require_signed is None else require_signed
    )
    if needs_signature and not bool(getattr(row, "signed", False)):
        problems.append(Problem(
            "unsigned",
            "The artifact was registered as unsigned. Releases must be signed "
            "(DESKTOP_REQUIRE_SIGNED_RELEASES).",
            label,
        ))
    return problems


def readiness(version: str, rows: Iterable, *, require_signed: Optional[bool] = None) -> Readiness:
    """Whether ``version`` has a complete, valid set of artifacts.

    ``rows`` are the registered rows for that one version, in any status. A
    required artifact with no live row is a problem; so is any live row that
    fails :func:`check_row`. Rows outside the required set (a Linux build, say)
    are ignored rather than failed -- they are not what this gate guards.
    """
    report = Readiness(version=version)
    by_key = {}
    for row in rows:
        if row.version != version:
            continue
        by_key.setdefault((row.platform, row.architecture), []).append(row)

    for key in required_artifacts():
        label = artifact_label(*key)
        candidates = by_key.get(key, [])
        live = [r for r in candidates if r.status in _LIVE_STATES]
        if not live:
            if candidates:
                report.problems.append(Problem(
                    "withdrawn",
                    f"The {label} artifact was withdrawn ({candidates[0].status}); "
                    "register it again for a new version.",
                    label,
                ))
            else:
                report.problems.append(Problem(
                    "missing", f"The {label} artifact has not been registered.", label,
                ))
            continue
        report.present.append(label)
        report.problems.extend(check_row(live[0], require_signed=require_signed))
    return report


def is_complete_and_published(version: str, rows: Sequence) -> bool:
    """True when every required artifact of ``version`` is published and valid."""
    report = readiness(version, rows)
    if not report.ready:
        return False
    published = {
        (r.platform, r.architecture)
        for r in rows
        if r.version == version and r.status == "published"
    }
    return all(key in published for key in required_artifacts())

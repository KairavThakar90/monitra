"""
Fetching an update artifact, and proving it is the one the backend named.

The whole safety property of this module is one sentence: **the installed
application is never touched.** A download writes to a scratch directory under
the user's data directory, and only a file whose SHA-256 matches the digest the
backend gave is ever handed to the installer. Every failure path — no network,
a truncated body, a wrong digest, a full disk — ends with the artifact deleted
and the application exactly as it was.

Design notes worth keeping:

**Hashing happens during the download, not after it.** One pass over the bytes
as they arrive, rather than writing the file and reading it back. A 100 MB
installer is not large, but reading it twice doubles the I/O for no benefit,
and a second pass introduces a window in which the file could change between
the write and the check.

**Cancellation is checked between chunks.** A download can take minutes, and
shutdown must not wait it out. `should_stop` is consulted on every chunk, so
the worst case is one chunk's transfer — not one file's.

**Nothing here touches Qt.** It runs on the update service's own thread and
reports progress through a plain callable, so it is directly testable without a
QApplication and cannot accidentally touch a widget.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urljoin, urlparse

import httpx

from core.logging_setup import get_logger
from core.paths import data_dir

from . import policy

log = get_logger("updates.download")

#: Read size. Large enough that the per-chunk overhead is irrelevant, small
#: enough that a stop request is honoured promptly and progress moves visibly.
CHUNK_BYTES = 256 * 1024

#: How long to wait for the server to start responding, and for each subsequent
#: chunk. Deliberately not a total-duration cap: a large artifact on a slow
#: connection is normal, and killing a download that is making steady progress
#: would make the updater unusable on exactly the connections that need it most.
CONNECT_TIMEOUT_SECONDS = 15.0
READ_TIMEOUT_SECONDS = 60.0

#: Refuse to start unless this much more than the artifact will fit. Installers
#: need room to unpack, and filling a user's disk to install an update is a
#: worse outcome than not updating.
FREE_SPACE_HEADROOM_BYTES = 300 * 1024 * 1024

#: Cap on what will be written when the backend gave no size. A response that
#: never ends must not fill the disk; this is a guard against a malformed or
#: hostile server, not a normal-path limit.
MAX_ARTIFACT_BYTES = 2 * 1024 * 1024 * 1024


class DownloadError(Exception):
    """A download or verification failed. Carries a message fit to show a user.

    `detail` is the diagnostic for the log — a URL, an exception string, a byte
    count. It is deliberately kept off the message shown on screen, which says
    what happened and what the user can do about it.
    """

    def __init__(self, message: str, detail: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail


@dataclass(frozen=True)
class DownloadResult:
    path: Path
    sha256: str
    size_bytes: int


def updates_dir() -> Path:
    """The scratch directory downloads land in.

    Under the user's data directory, which `core/paths.py` guarantees is
    outside the installation directory — so an installer replacing the
    application wholesale cannot delete a download in progress, and a download
    can never write into the running application's own files.
    """
    path = data_dir() / "updates"
    path.mkdir(parents=True, exist_ok=True)
    return path


def clear_stale_downloads() -> int:
    """Delete anything left in the scratch directory. Returns the count.

    Called at startup. A partial download from a process that was killed
    mid-transfer has no value — it cannot be resumed, because the artifact it
    was fetching may itself have been withdrawn — and leaving it costs the user
    disk space they never agreed to spend.
    """
    removed = 0
    try:
        directory = updates_dir()
    except OSError:
        log.exception("could not open the update scratch directory")
        return 0
    for entry in directory.iterdir():
        try:
            if entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink()
            removed += 1
        except OSError:
            # A file the OS still holds open is not worth failing startup over.
            log.warning("could not remove stale update file %s", entry.name)
    if removed:
        log.info("cleared %d stale update file(s)", removed)
    return removed


def has_free_space(needed_bytes: Optional[int]) -> bool:
    """Whether there is room for the artifact plus installation headroom.

    True when the answer is unknown: refusing an update because the free-space
    query failed would be a self-inflicted outage. The download itself still
    fails safely if the disk really does fill.
    """
    if not needed_bytes:
        return True
    try:
        usage = shutil.disk_usage(updates_dir())
    except OSError:
        log.warning("could not determine free disk space; continuing")
        return True
    return usage.free >= needed_bytes + FREE_SPACE_HEADROOM_BYTES


def _artifact_name(url: str, version: str) -> str:
    """A safe local filename for the artifact.

    The remote path is used only for its *extension*, and only when that
    extension is one we expect. Nothing from the URL becomes part of the
    filename: a server-controlled name is a path-traversal and a
    wrong-file-type problem at once, and the name has no purpose here beyond
    being recognisable in a log.
    """
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix not in (".exe", ".dmg", ".zip", ".pkg", ".msi"):
        suffix = ".bin"
    return f"Monitra-{version}{suffix}"


def download_and_verify(
    *,
    url: str,
    expected_sha256: str,
    version: str,
    expected_size: Optional[int] = None,
    on_progress: Optional[Callable[[int, Optional[int]], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> DownloadResult:
    """Fetch `url`, verify it against `expected_sha256`, return where it landed.

    :raises DownloadError: on any failure. The installed application is
        untouched in every case, and no partial file survives.
    """
    problem = policy.check_url(url)
    if problem:
        # The digest already protects the *contents*, but plain HTTP also
        # exposes which build each machine is fetching and lets an active
        # attacker waste a user's bandwidth on a body that will be rejected --
        # and a host outside the approved list is somewhere this build was never
        # told to fetch an executable from, whatever the backend says. An
        # update channel is the one thing that must not be downgradeable.
        raise DownloadError(
            "The update could not be downloaded securely.",
            detail=f"refusing the update URL: {problem}",
        )

    if not has_free_space(expected_size):
        raise DownloadError(
            "There is not enough free disk space to download the update.",
            detail=f"needed {expected_size} bytes plus headroom",
        )

    target = updates_dir() / _artifact_name(url, version)
    digest = hashlib.sha256()
    written = 0

    # Written under a temporary name in the same directory and renamed only
    # once verified, so a file at `target` is always a complete, checked
    # artifact. A crash mid-download leaves a `.part` file, which startup
    # clears -- it never leaves something that looks installable.
    handle = None
    partial: Optional[Path] = None
    try:
        fd, partial_name = tempfile.mkstemp(
            dir=str(updates_dir()), prefix=f"{target.stem}.", suffix=".part"
        )
        partial = Path(partial_name)
        handle = os.fdopen(fd, "wb")

        log.info("update download started: version %s", version)
        timeout = httpx.Timeout(
            connect=CONNECT_TIMEOUT_SECONDS, read=READ_TIMEOUT_SECONDS,
            write=READ_TIMEOUT_SECONDS, pool=CONNECT_TIMEOUT_SECONDS,
        )
        # A client of its own rather than the shared API client: this is a
        # request to release storage, not to the backend, and it must carry
        # no Authorization header. A session token has no business being sent
        # to a CDN.
        #
        # Redirects are followed by hand, one hop at a time, so every hop can be
        # judged. GitHub answers a release download with a 302 to its own CDN,
        # so redirects are expected -- but a redirect is also how a download is
        # sent somewhere its URL never named, and httpx's own following would
        # accept an https->http hop or an unrelated host without asking.
        with httpx.Client(timeout=timeout, follow_redirects=False) as client:
            current = url
            for hop in range(policy.MAX_REDIRECTS + 1):
                with client.stream("GET", current) as response:
                    if response.status_code in policy.REDIRECT_STATUSES:
                        next_url = _redirect_target(current, response)
                        problem = policy.check_redirect(next_url)
                        if problem:
                            raise DownloadError(
                                "The update could not be downloaded securely.",
                                detail=f"refusing a redirect: {problem}",
                            )
                        log.info("update download redirected (hop %d)", hop + 1)
                        current = next_url
                        continue

                    response.raise_for_status()
                    total = _declared_size(response, expected_size)
                    for chunk in response.iter_bytes(CHUNK_BYTES):
                        if should_stop is not None and should_stop():
                            raise DownloadError(
                                "The update download was cancelled.",
                                detail="stop requested during download",
                            )
                        handle.write(chunk)
                        digest.update(chunk)
                        written += len(chunk)
                        if written > MAX_ARTIFACT_BYTES:
                            raise DownloadError(
                                "The update download was unexpectedly large and "
                                "has been stopped.",
                                detail=f"exceeded {MAX_ARTIFACT_BYTES} bytes",
                            )
                        if on_progress is not None:
                            on_progress(written, total)
                    break
            else:
                raise DownloadError(
                    "The update could not be downloaded securely.",
                    detail=f"more than {policy.MAX_REDIRECTS} redirects",
                )
        handle.close()
        handle = None

        # A body that ended early hashes differently and would fail below
        # anyway, but saying so plainly makes the log readable: "truncated" and
        # "tampered with" are different incidents.
        if expected_size and written != expected_size:
            raise DownloadError(
                "The update download was incomplete. Please try again.",
                detail=f"expected {expected_size} bytes, received {written}",
            )

        actual = digest.hexdigest()
        if actual != expected_sha256:
            # Never installed, and not kept: a file that failed its checksum is
            # either corrupt or not what the backend described, and there is no
            # version of "keep it around" that ends well.
            log.error(
                "checksum verification FAILED for version %s "
                "(expected %s, computed %s); the download has been discarded",
                version, expected_sha256, actual,
            )
            raise DownloadError(
                "The update could not be verified and has been discarded. "
                "Your current version of Monitra is unaffected.",
                detail=f"sha256 mismatch: expected {expected_sha256}, got {actual}",
            )

        log.info(
            "checksum verification passed for version %s (%d bytes)", version, written
        )
        partial.replace(target)
        partial = None
        return DownloadResult(path=target, sha256=actual, size_bytes=written)

    except DownloadError:
        raise
    except httpx.TimeoutException as exc:
        raise DownloadError(
            "The update download timed out. Please try again.", detail=str(exc)
        )
    except httpx.HTTPStatusError as exc:
        raise DownloadError(
            "The update could not be downloaded.",
            detail=f"HTTP {exc.response.status_code}",
        )
    except httpx.HTTPError as exc:
        raise DownloadError(
            "The update could not be downloaded: no connection.", detail=str(exc)
        )
    except PermissionError as exc:
        raise DownloadError(
            "Monitra does not have permission to save the update.", detail=str(exc)
        )
    except OSError as exc:
        # ENOSPC lands here when the disk fills mid-transfer despite the check.
        raise DownloadError(
            "The update could not be saved to disk.", detail=str(exc)
        )
    finally:
        if handle is not None:
            try:
                handle.close()
            except OSError:
                log.warning("could not close the partial update file")
        if partial is not None:
            # Reached on every failure path, so a partial or rejected artifact
            # never survives to look installable.
            try:
                partial.unlink(missing_ok=True)
            except OSError:
                log.warning("could not remove the partial update file")


def _redirect_target(current: str, response) -> Optional[str]:
    """Where a redirect response says to go, resolved against the URL it came
    from, or None if it did not say. Header lookup is case-insensitive for the
    real httpx headers and tolerant of a plain dict, which tests use."""
    headers = response.headers
    location = headers.get("location") or headers.get("Location")
    if not location:
        return None
    return urljoin(current, location)


def _declared_size(response: httpx.Response, expected: Optional[int]) -> Optional[int]:
    """The size to show progress against.

    The backend's figure is preferred over `Content-Length`: it is the number
    that was hashed, and it arrived over the authenticated API rather than from
    whatever served the artifact.
    """
    if expected:
        return expected
    raw = response.headers.get("Content-Length")
    try:
        value = int(raw) if raw is not None else 0
    except (TypeError, ValueError):
        return None
    return value or None

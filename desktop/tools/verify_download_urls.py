#!/usr/bin/env python
"""
verify_download_urls — are the installers for a version really downloadable?

    python tools/verify_download_urls.py --base-url https://staff.peakworkos.com/download_app_files/ \
        [--version 1.3.3] [--artifacts dist-release-1.3.3]

For each of the three installers a version needs (`Monitra-Setup-<v>.exe`,
`Monitra-macOS-arm64-<v>.dmg`, `Monitra-macOS-x86_64-<v>.dmg`) it fetches
`<base>/<filename>` the way an installed client would -- https, redirects
judged hop by hop, no credential -- and checks that the answer is the installer
and not a page about it:

* HTTP 200 after at most a few redirects, every hop https;
* a body that is not HTML (an IIS "404" page, a Google Drive confirmation page
  and a sign-in page all arrive as `text/html` with a 200 or a 404);
* a size and SHA-256 that match the built artifact (or its `.sha256` sidecar)
  when `--artifacts` names where the CI outputs are.

Exit status is 0 only when every installer passes. Run it after copying the
artifacts to the website and before publishing the backend rows: a row that
points at a file the server will not serve is an update every client fails.

Found this way, 2026-10-27: the website served `Monitra-Setup-1.3.2.exe` and
answered 404 for both `.dmg` files -- IIS has no MIME type for `.dmg`, so the
Mac download buttons had been broken since the files were put there.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

DESKTOP_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(DESKTOP_ROOT))

import version  # noqa: E402

MAX_REDIRECTS = 5
CHUNK = 1024 * 1024


def artifact_names(ver: str) -> Dict[str, str]:
    """platform label -> installer filename, for one version."""
    return {
        "win32": f"Monitra-Setup-{ver}.exe",
        "darwin/arm64": f"Monitra-macOS-arm64-{ver}.dmg",
        "darwin/x86_64": f"Monitra-macOS-x86_64-{ver}.dmg",
    }


@dataclass
class Fetched:
    """What one download attempt saw."""

    status: Optional[int]
    content_type: str
    size: int
    sha256: Optional[str]
    final_url: str
    error: Optional[str] = None


Fetcher = Callable[[str], Fetched]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


def fetch(url: str) -> Fetched:
    """GET `url` as an installed client would, hashing the body as it streams."""
    opener = urllib.request.build_opener(_NoRedirect)
    current = url
    for _hop in range(MAX_REDIRECTS + 1):
        parts = urllib.parse.urlsplit(current)
        if parts.scheme.lower() != "https":
            return Fetched(None, "", 0, None, current, error=f"not https: {current}")
        request = urllib.request.Request(current, headers={"User-Agent": f"Monitra/{version.VERSION}"})
        try:
            response = opener.open(request, timeout=60)
        except urllib.error.HTTPError as exc:
            if exc.code in (301, 302, 303, 307, 308) and exc.headers.get("Location"):
                current = urllib.parse.urljoin(current, exc.headers["Location"])
                continue
            body = exc.read(4096) if exc.fp else b""
            return Fetched(exc.code, exc.headers.get("Content-Type", ""), len(body), None, current,
                           error=f"HTTP {exc.code}")
        except (urllib.error.URLError, OSError) as exc:
            return Fetched(None, "", 0, None, current, error=str(exc.reason if hasattr(exc, "reason") else exc))
        with response:
            digest = hashlib.sha256()
            size = 0
            while True:
                block = response.read(CHUNK)
                if not block:
                    break
                digest.update(block)
                size += len(block)
            return Fetched(response.status, response.headers.get("Content-Type", ""), size,
                           digest.hexdigest(), current)
    return Fetched(None, "", 0, None, current, error="too many redirects")


def expected_digest(artifacts_dir: Optional[Path], filename: str) -> Tuple[Optional[str], Optional[int]]:
    """(sha256, size) of the built artifact, from the file or its sidecar."""
    if artifacts_dir is None:
        return None, None
    matches = list(artifacts_dir.rglob(filename))
    if matches:
        path = matches[0]
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(CHUNK), b""):
                digest.update(block)
        return digest.hexdigest(), path.stat().st_size
    sidecars = list(artifacts_dir.rglob(filename + ".sha256"))
    if sidecars:
        first = sidecars[0].read_text(encoding="utf-8", errors="replace").split()
        return (first[0].lower() if first else None), None
    return None, None


def judge(label: str, filename: str, seen: Fetched, want_sha: Optional[str], want_size: Optional[int]) -> List[str]:
    """Every reason this installer is not downloadable as published."""
    problems: List[str] = []
    if seen.error:
        problems.append(f"{label}: {filename}: {seen.error}")
        if seen.status == 404 and filename.endswith(".dmg") and "text/html" in seen.content_type:
            problems.append(
                f"{label}: a 404 HTML page for a .dmg usually means the web server has no MIME "
                "type for .dmg (IIS: add <mimeMap fileExtension=\".dmg\" "
                "mimeType=\"application/x-apple-diskimage\" /> under <staticContent>)."
            )
        return problems
    if seen.status != 200:
        problems.append(f"{label}: {filename}: HTTP {seen.status}")
    if "text/html" in seen.content_type.lower():
        problems.append(f"{label}: {filename}: the server answered an HTML page, not the installer")
    if seen.size <= 0:
        problems.append(f"{label}: {filename}: empty body")
    if want_size is not None and seen.size != want_size:
        problems.append(f"{label}: {filename}: {seen.size} bytes served, the built artifact is {want_size}")
    if want_sha is not None and seen.sha256 != want_sha:
        problems.append(f"{label}: {filename}: SHA-256 {seen.sha256[:12] if seen.sha256 else '?'}… does not match "
                        f"the built artifact {want_sha[:12]}…")
    return problems


def frontend_download_version(repo_root: Path) -> Optional[str]:
    """The version the website's download buttons serve (`DOWNLOAD_VERSION`)."""
    source = repo_root / "frontend" / "src" / "api" / "desktopRelease.ts"
    try:
        text = source.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        if line.strip().startswith("export const DOWNLOAD_VERSION"):
            return line.split("'")[1] if "'" in line else None
    return None


def verify(base_url: str, ver: str, artifacts_dir: Optional[Path], fetcher: Fetcher = fetch) -> List[str]:
    problems: List[str] = []
    base = base_url.rstrip("/")
    for label, filename in artifact_names(ver).items():
        seen = fetcher(f"{base}/{filename}")
        want_sha, want_size = expected_digest(artifacts_dir, filename)
        problems.extend(judge(label, filename, seen, want_sha, want_size))
        if not problems or not any(p.startswith(label) for p in problems):
            print(f"OK   {filename}: {seen.size} bytes" + (", digest matches the build" if want_sha else ""))
    return problems


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--base-url", required=True,
                        help="https folder the installers are served from, e.g. the website's /download_app_files/")
    parser.add_argument("--version", default=version.VERSION, help="defaults to version.py")
    parser.add_argument("--artifacts", default=None, help="directory holding the CI outputs (or their .sha256 sidecars)")
    args = parser.parse_args(argv)

    if not args.base_url.lower().startswith("https://"):
        print("the base URL must be https", file=sys.stderr)
        return 2
    artifacts_dir = Path(args.artifacts) if args.artifacts else None
    problems = verify(args.base_url, args.version, artifacts_dir)

    linked = frontend_download_version(DESKTOP_ROOT.parent)
    if linked and linked != args.version:
        print(f"note: the website's download buttons still serve {linked} "
              f"(frontend/src/api/desktopRelease.ts DOWNLOAD_VERSION); bump it to {args.version} "
              "in the same deploy that ships these files.")

    if not problems:
        print(f"Monitra {args.version}: all three installers are downloadable from {args.base_url}")
        return 0
    print(f"Monitra {args.version} is NOT downloadable as published:", file=sys.stderr)
    for problem in problems:
        print(f"  - {problem}", file=sys.stderr)
        if os.environ.get("GITHUB_ACTIONS") == "true":
            print(f"::error::{problem}")
    return 1


if __name__ == "__main__":
    sys.exit(main())

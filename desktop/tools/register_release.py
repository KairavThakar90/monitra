#!/usr/bin/env python
"""
register_release — tell the backend what CI just built.

    python tools/register_release.py --tag v1.2.0 --repo owner/name \\
        --artifacts dist/installer/Monitra-Setup-1.2.0.exe ...

For each artifact it computes the SHA-256 and size, works out which platform
and architecture the file is for, and POSTs it to
``/desktop/releases`` — which always creates a **draft**.

Why a draft, always
-------------------
CI can prove a build compiles, starts and passes its tests. It cannot decide
the build is good. Publishing is therefore a separate, deliberate act by a
person, exactly as the GitHub release itself stays a draft until someone
publishes it. This script has no way to publish and is not given a code path
that could.

Why the URL is derived rather than read back
--------------------------------------------
A draft GitHub release's asset URLs are not publicly reachable, so there is
nothing to read at the moment this runs. The public download URL for a release
asset is entirely predictable from the tag and the filename, and it starts
working the instant the release is published — which is the same moment the
backend release is meant to be published. Deriving it keeps both halves in
step without CI having to publish anything early.

How it authenticates
--------------------
With a **service credential** — a long-lived API key held in the repository
secret ``MONITRA_RELEASE_CREDENTIAL`` and sent as an ordinary bearer token. It
belongs to an account whose role is `release_bot`, whose entire authority is
`manage_desktop_releases`.

This used to sign in at ``/auth/dev-login`` with an email and a password, which
had two costs: the deployment had to keep ``ENV=development`` for that route to
exist at all, and a password for a real account sat in CI. The key replaces
both. It does not expire, so nothing is minted here and there is no sign-in
step that can fail; the account behind it has no password at all, so the key is
the only way in and revoking it closes the door completely.

Failure here does not fail the build. The artifacts exist and the GitHub
release exists either way; a backend that was unreachable for a minute should
not turn a good build into a failed one, and the registration can be re-run.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional, Tuple

DESKTOP_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(DESKTOP_ROOT))

import version  # noqa: E402  - after the path is set up

#: How a built filename maps onto a platform and architecture. Ordered, and
#: matched against the *filename* the build scripts produce -- the names are
#: fixed in `packaging/` and `scripts/`, so this is a contract inside this
#: repository rather than a guess about arbitrary input.
ARTIFACT_PATTERNS = (
    (re.compile(r"^Monitra-Setup-.*\.exe$", re.I), "win32", None),
    (re.compile(r"^Monitra-macOS-arm64-.*\.dmg$", re.I), "darwin", "arm64"),
    (re.compile(r"^Monitra-macOS-x86_64-.*\.dmg$", re.I), "darwin", "x86_64"),
)

#: The portable zip is deliberately *not* registered. It is a folder the user
#: unzips wherever they choose, so there is no installer to hand an update to
#: and `installer.can_install()` refuses to auto-update one. Registering it
#: would advertise an update path that does not exist.
IGNORED_PATTERNS = (re.compile(r"^Monitra-Portable-.*\.zip$", re.I),)

CHUNK = 1024 * 1024


def classify(name: str) -> Optional[Tuple[str, Optional[str]]]:
    """(platform, architecture) for an artifact filename, or None to skip."""
    for pattern in IGNORED_PATTERNS:
        if pattern.match(name):
            return None
    for pattern, platform, arch in ARTIFACT_PATTERNS:
        if pattern.match(name):
            return platform, arch
    return None


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def asset_url(repo: str, tag: str, filename: str) -> str:
    """The public download URL a release asset will have once published."""
    return f"https://github.com/{repo}/releases/download/{tag}/{filename}"


def release_notes(version_string: str) -> Optional[str]:
    """This version's section of CHANGELOG.md, as plain text.

    The hand-written note, not the auto-generated commit list: this is what a
    person reads in the update dialog, and `tools/check_changelog.py` has
    already refused the build if it is missing.
    """
    changelog = DESKTOP_ROOT / "CHANGELOG.md"
    if not changelog.is_file():
        return None
    text = changelog.read_text(encoding="utf-8")
    pattern = re.compile(
        rf"^##+\s*\[?{re.escape(version_string)}\]?.*?$(.*?)(?=^##+\s|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(text)
    if not match:
        return None
    body = match.group(1).strip()
    return body or None


def post_release(base_url: str, token: str, payload: dict) -> bool:
    """Register one artifact. Returns whether it is now registered."""
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/desktop/releases",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": f"MonitraReleasePipeline/{version.VERSION}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            print(f"  registered ({response.status})")
            return True
    except urllib.error.HTTPError as exc:
        if exc.code == 409:
            # Already registered. Re-running the pipeline for the same tag is a
            # normal thing to do, and it must not look like a failure.
            print("  already registered; leaving it alone")
            return True
        if exc.code in (401, 403):
            # Named rather than dumped: the body of an auth failure is not
            # something to widen into a public build log, and these two have a
            # specific remedy.
            print(
                f"  FAILED: HTTP {exc.code} — the release credential was "
                "refused. Check that MONITRA_RELEASE_CREDENTIAL holds a current, "
                "unrevoked key (backend/scripts/provision_release_credential.py "
                "show).",
                file=sys.stderr,
            )
            return False
        body = exc.read().decode("utf-8", "replace")[:400]
        print(f"  FAILED: HTTP {exc.code} {body}", file=sys.stderr)
        return False
    except urllib.error.URLError as exc:
        print(f"  FAILED: {exc.reason}", file=sys.stderr)
        return False


def annotate(level: str, message: str) -> None:
    """Surface an outcome on the GitHub Actions run page.

    Every exit path in this script returns 0 on purpose, and the workflow step
    is `continue-on-error` on top of that: a backend that was briefly
    unreachable must not fail a good build. The cost was that "registered four
    artifacts" and "did nothing at all" produced the same green tick, so a
    release could be tagged, built and published on GitHub while the download
    page stayed empty and nothing anywhere said why. This restores the
    difference without making the step fail.

    Outside Actions it is a no-op, so a manual run is unaffected.
    """
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return
    # A workflow command is one line: a newline would end the command and print
    # the remainder as ordinary log output.
    single_line = " ".join(message.split())
    print(f"::{level}::{single_line}")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary:
        return
    try:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(f"- **{level}**: {single_line}\n")
    except OSError:
        # The summary is a convenience. Losing it must not fail registration.
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True, help="Git tag, e.g. v1.2.0")
    parser.add_argument("--repo", required=True, help="owner/name")
    parser.add_argument("--artifacts", nargs="+", required=True)
    parser.add_argument(
        "--force-update", action="store_true",
        help="Mark these artifacts as a mandatory update.",
    )
    parser.add_argument(
        "--min-supported-version", default=None,
        help="Oldest version still allowed to keep working.",
    )
    args = parser.parse_args()

    base_url = os.environ.get("MONITRA_API_BASE_URL", "").strip()
    # The normal path: the service credential, presented as-is. It does not
    # expire, so there is nothing to mint and no sign-in step to fail.
    credential = os.environ.get("MONITRA_RELEASE_CREDENTIAL", "").strip()
    # A person registering a build by hand from a session they already have.
    # Unset in CI -- an access token is valid for thirty minutes, so one stored
    # in a repository secret would be dead long before the next release.
    token = credential or os.environ.get("MONITRA_RELEASE_TOKEN", "").strip()

    if not base_url or not token:
        # Not configured is not a failure: a fork, or a repository that has not
        # been given the credential, still produces perfectly good artifacts.
        print(
            "MONITRA_API_BASE_URL and a release credential "
            "(MONITRA_RELEASE_CREDENTIAL, or MONITRA_RELEASE_TOKEN for a manual "
            "run) are not set; skipping backend registration.\n"
            "The artifacts and the GitHub release are unaffected."
        )
        annotate(
            "warning",
            "Backend registration was SKIPPED: MONITRA_API_BASE_URL and/or a "
            "release credential are not set on this repository. No release row "
            "exists, so the download page will not offer this build.",
        )
        return 0

    expected = version.VERSION
    if args.tag.lstrip("v") != expected:
        # A tag that disagrees with version.py means the artifacts are not the
        # version the tag claims. Registering either number would make a
        # support report untrustworthy.
        print(
            f"tag {args.tag} does not match version.py ({expected}); refusing "
            "to register.",
            file=sys.stderr,
        )
        annotate(
            "error",
            f"Backend registration REFUSED: tag {args.tag} does not match "
            f"version.py ({expected}). No release row was created.",
        )
        return 1

    notes = release_notes(expected)
    failures = 0
    registered = 0

    for raw in args.artifacts:
        path = Path(raw)
        if not path.is_file():
            continue
        classification = classify(path.name)
        if classification is None:
            print(f"{path.name}: not a registrable artifact, skipping")
            continue
        platform, architecture = classification

        print(f"{path.name} -> {platform}/{architecture or 'any'}")
        payload = {
            "version": expected,
            "platform": platform,
            "architecture": architecture,
            "download_url": asset_url(args.repo, args.tag, path.name),
            "sha256": sha256_of(path),
            "file_size": path.stat().st_size,
            "release_notes": notes,
            "force_update": bool(args.force_update),
            "min_supported_version": args.min_supported_version,
        }
        if post_release(base_url, token, payload):
            registered += 1
        else:
            failures += 1

    print(f"\n{registered} artifact(s) registered as drafts, {failures} failed.")
    if not registered:
        annotate(
            "warning",
            "Backend registration created NO release rows: none of the given "
            "artifacts were registrable. The download page will not offer this "
            "build.",
        )
    if failures:
        # Reported, but not fatal: the build and the GitHub release are good,
        # and registration can be re-run without rebuilding anything.
        print(
            "Registration is incomplete. Re-run this step, or register the "
            "remaining artifacts by hand, before publishing the release.",
            file=sys.stderr,
        )
        annotate(
            "warning",
            f"Backend registration is INCOMPLETE: {failures} artifact(s) failed, "
            f"{registered} succeeded. Re-run this step before publishing.",
        )
    print(
        "\nNothing is live yet. Publish each release through "
        "POST /desktop/releases/{id}/publish once the build has been piloted."
    )
    if registered:
        # Said out loud because it is the step that gets forgotten: a registered
        # release is a draft, and the download page serves published rows only.
        annotate(
            "notice",
            f"{registered} artifact(s) registered as DRAFTS -- not downloadable "
            "yet. Publish each through POST /desktop/releases/" "{id}/publish.",
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())

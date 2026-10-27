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

A refused registration FAILS the step (exit 1). It used to be reported and
swallowed, on the theory that a backend unreachable for a minute should not
turn a good build into a failed one -- and that is how release 1.3.2 was
tagged, built and filed on GitHub with a green tick while every registration
had answered 401 and the production backend went on offering 1.1.1. A failed
registration is a release that does not exist for anyone; the run must say so.
Re-running the release job re-registers idempotently (409 = already there).
Only "not configured" (no base URL or credential: a fork) stays non-fatal.
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
sys.path.insert(0, str(Path(__file__).resolve().parent))

import version  # noqa: E402  - after the path is set up
from _attestation import load_attestation  # noqa: E402

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

#: The artifacts a version needs before the backend will publish any of it. The
#: same set as the backend's DESKTOP_REQUIRED_ARTIFACTS default; this script
#: only *warns* about a gap (the backend is what refuses to publish across it,
#: and `verify_release_artifacts.py` is what stops the release job earlier).
REQUIRED_ARTIFACTS = (("win32", None), ("darwin", "arm64"), ("darwin", "x86_64"))

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


def signing_of(path: Path):
    """(signed, signer) as attested for this exact file, else (False, None).

    Read from the `.signature.json` sidecar `attest_signature.py` wrote after
    verifying the finished artifact. No sidecar, an unreadable one, or one made
    for a different file all mean "not known to be signed" -- never "signed".
    """
    record = load_attestation(path)
    if record and record.get("signed") is True:
        signer = record.get("signer")
        return True, signer if isinstance(signer, str) and signer else None
    return False, None


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def asset_url(repo: str, tag: str, filename: str) -> str:
    """The public download URL a GitHub release asset will have once published.

    Only reachable by an installed client while the repository is PUBLIC. The
    source repository is private, so the pipeline normally registers the
    website's copy instead (`download_url_for` with a base URL).
    """
    return f"https://github.com/{repo}/releases/download/{tag}/{filename}"


def download_url_for(base_url: Optional[str], repo: str, tag: str, filename: str) -> str:
    """Where an installed client will fetch this artifact from.

    With a base URL -- the website folder the download page's buttons already
    point at, `https://staff.peakworkos.com/download_app_files/` -- the artifact is
    `<base>/<filename>`: the operator copies the CI artifact there under its
    built name, which is the name the backend's filename rule requires. Without
    one, the GitHub release asset URL, for a public releases repository.

    The base must be https and must not carry a query or a fragment; a
    filename is appended to a *directory*, never spliced into a query string.
    """
    base = (base_url or "").strip()
    if not base:
        return asset_url(repo, tag, filename)
    if not base.lower().startswith("https://") or "?" in base or "#" in base:
        raise ValueError(
            f"the download base URL must be a plain https directory URL, got {base!r}"
        )
    return f"{base.rstrip('/')}/{filename}"


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


#: A service credential is `msk_<key id>_<secret>`. The key id is public: the
#: backend logs it on every refusal and `provision_release_credential.py show`
#: lists it, so printing it is what lets an operator tell "the secret in GitHub
#: is not the key the database holds" from "the key was revoked" without ever
#: seeing the secret half.
SERVICE_KEY_PREFIX = "msk_"


def key_id_of(token: str) -> str | None:
    """The public id of a service credential, or None for any other token."""
    if not isinstance(token, str) or not token.startswith(SERVICE_KEY_PREFIX):
        return None
    body = token[len(SERVICE_KEY_PREFIX):]
    key_id, sep, _secret = body.partition("_")
    return key_id if sep and key_id else None


def preflight(base_url: str, token: str) -> str:
    """Prove the credential before any row is registered.

    One `GET /auth/me` with the credential, which is how every later request
    will be judged. Returns ``"accepted"`` (and prints who the backend took
    the credential for), ``"refused"`` on a 401/403 (nothing further should
    be attempted -- every registration would be refused the same way), or
    ``"unverified"`` when the backend could not be asked at all, in which
    case the registrations are still attempted and report their own failure.
    The secret is never printed; its public key id is.
    """
    key_id = key_id_of(token)
    print(f"credential: service key id {key_id}" if key_id else "credential: a manual token")
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/auth/me",
        headers={
            "Authorization": f"Bearer {token}",
            "User-Agent": f"MonitraReleasePipeline/{version.VERSION}",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = json.loads(response.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            print(
                f"  REFUSED: HTTP {exc.code} -- the backend at this MONITRA_API_BASE_URL does "
                f"not accept this credential{f' (key id {key_id})' if key_id else ''}. "
                "On the server, `python scripts/provision_release_credential.py show` "
                "lists the key ids that database holds; the backend log names the "
                "reason (SERVICE_CREDENTIAL_REFUSED: unknown key_id / bad secret / "
                "revoked / expired). A key issued against another database (the "
                "development one, or the previous host) is 'unknown key_id' here: "
                "issue the key on the database this backend uses and paste it into "
                "the repository secret MONITRA_RELEASE_CREDENTIAL.",
                file=sys.stderr,
            )
            return "refused"
        print(f"  could not verify the credential: HTTP {exc.code}; registering anyway",
              file=sys.stderr)
        return "unverified"
    except (urllib.error.URLError, ValueError, OSError) as exc:
        print(f"  could not verify the credential: {exc}; registering anyway", file=sys.stderr)
        return "unverified"
    who = body.get("username") or body.get("email") or "?"
    print(f"  accepted: the backend takes it for {who} (role {body.get('role_name', '?')})")
    return "accepted"


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
    parser.add_argument(
        "--download-base-url",
        default=os.environ.get("MONITRA_RELEASE_DOWNLOAD_BASE_URL", ""),
        help=(
            "https directory the installers are served from (the website's "
            "/download_app_files/). The artifact's built filename is appended. "
            "Defaults to MONITRA_RELEASE_DOWNLOAD_BASE_URL; empty means the "
            "GitHub release asset URL, which only works for a public repository."
        ),
    )
    args = parser.parse_args()

    try:
        download_url_for(args.download_base_url, args.repo, args.tag, "probe")
    except ValueError as exc:
        print(f"invalid --download-base-url: {exc}", file=sys.stderr)
        annotate("error", f"Backend registration REFUSED: {exc}. No release row was created.")
        return 1

    if version.is_prerelease():
        # An internal test build is handed to testers by hand, and it must
        # never become a row the update check or the public download page can
        # offer -- not even as a draft, because a draft is one click from
        # published. Checked before the credentials, so the answer is the same
        # in CI and by hand.
        print(
            f"version.py marks this build as a pre-release "
            f"({version.display_version()}); refusing to register it. Internal "
            "test builds are distributed manually and are never a release.",
            file=sys.stderr,
        )
        annotate(
            "error",
            f"Backend registration REFUSED: {version.display_version()} is an "
            "internal test build (version.PRERELEASE is set). No release row "
            "was created; production users are unaffected.",
        )
        return 1

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

    # A service key is proven once, before any row is created, so a refused
    # credential is one clear line naming the public key id rather than three
    # refusals -- and a run that registers nothing cannot be mistaken for one
    # that registered half a set. A manual token is a person's session; it is
    # presented as it is.
    if key_id_of(token) is not None and preflight(base_url, token) == "refused":
        annotate(
            "error",
            "Backend registration REFUSED: the release credential was not accepted "
            f"(service key id {key_id_of(token)}). No release row was created. Issue "
            "the key on the database this backend uses "
            "(backend/scripts/provision_release_credential.py) and update the "
            "repository secret MONITRA_RELEASE_CREDENTIAL.",
        )
        return 1

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
    seen = set()

    for raw in args.artifacts:
        path = Path(raw)
        if not path.is_file():
            continue
        classification = classify(path.name)
        if classification is None:
            print(f"{path.name}: not a registrable artifact, skipping")
            continue
        platform, architecture = classification
        seen.add((platform, architecture))
        signed, signer = signing_of(path)

        print(f"{path.name} -> {platform}/{architecture or 'any'}"
              f" ({'signed' if signed else 'NOT signed'})")
        payload = {
            "version": expected,
            "platform": platform,
            "architecture": architecture,
            "download_url": download_url_for(
                args.download_base_url, args.repo, args.tag, path.name
            ),
            "sha256": sha256_of(path),
            "file_size": path.stat().st_size,
            "release_notes": notes,
            "force_update": bool(args.force_update),
            "min_supported_version": args.min_supported_version,
            # What the pipeline verified about the finished file. A process gate
            # for the backend; the desktop verifies the real signature itself.
            "signed": signed,
            "signer": signer,
        }
        if post_release(base_url, token, payload):
            registered += 1
        else:
            failures += 1

    missing = [key for key in REQUIRED_ARTIFACTS if key not in seen]
    if missing:
        names = ", ".join(f"{p}/{a}" if a else p for p, a in missing)
        print(f"\nMISSING required artifact(s): {names}", file=sys.stderr)
        annotate(
            "error",
            f"This release is INCOMPLETE: no artifact was given for {names}. The "
            "backend will refuse to publish this version until every platform is "
            "registered, and nobody should be told it is available.",
        )

    print(f"\n{registered} artifact(s) registered as drafts, {failures} failed.")
    if not registered:
        annotate(
            "warning",
            "Backend registration created NO release rows: none of the given "
            "artifacts were registrable. The download page will not offer this "
            "build.",
        )
    if failures:
        # Fatal. The build and the GitHub release are good, but a release the
        # backend does not know about is not a release: nobody is offered it and
        # the download page stays on the previous version. The job can be re-run
        # without rebuilding anything; rows that did land answer 409 and count
        # as registered.
        print(
            "Registration FAILED for at least one artifact. Fix the cause (see "
            "above) and re-run this job; already-registered rows are kept.",
            file=sys.stderr,
        )
        annotate(
            "error",
            f"Backend registration FAILED: {failures} artifact(s) were refused, "
            f"{registered} succeeded. The release is not registered; re-run this "
            "job once the cause is fixed.",
        )
        return 1
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

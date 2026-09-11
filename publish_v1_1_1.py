#!/usr/bin/env python
"""Put the v1.1.1 desktop release on the LIVE download page.

Why this script exists
----------------------
staff.peakworkos.com/download is empty while localhost:5173/download works,
because the two read different databases. The local backend runs with
ENV=development and therefore uses DATABASE_URL_DEV, which happens to hold
three *published* v1.1.0 rows left over from the old
`manthanstoretransform-creator/staff-management-system` repository. Production
reads Vercel's own DATABASE_URL, which has no desktop_releases rows at all.
The download page renders published rows and nothing else, so production shows
its honest empty state.

The fix is data, not code: register the three v1.1.1 artifacts against the
production API and publish each row.

What it does, in order
----------------------
1. Downloads the three v1.1.1 artifacts and their .sha256 sidecars from the
   GitHub release.
2. Verifies every artifact against its published checksum, and stops if any
   disagree. A row whose sha256 does not match the bytes people download is
   worse than no row: the desktop updater refuses to install anything it
   cannot verify, so a wrong checksum produces an update that can never apply.
3. Registers each artifact by handing them to desktop/tools/register_release.py
   -- the existing, tested registration path -- which creates DRAFT rows.
4. Publishes the three v1.1.1 rows.

Publishing all three matters: a version with only the Windows row published
leaves every Mac user's download button saying "Not available yet".

Usage, from the repository root::

    MONITRA_RELEASE_CREDENTIAL=<the production release key> \
        python publish_v1_1_1.py

    # See what it would do, touching nothing:
    MONITRA_RELEASE_CREDENTIAL=<key> python publish_v1_1_1.py --dry-run

    # Stop after registering drafts, leaving the live page unchanged:
    MONITRA_RELEASE_CREDENTIAL=<key> python publish_v1_1_1.py --no-publish

Getting the credential
----------------------
The key is stored only as a SHA-256 and cannot be read back, so if the GitHub
repository secret MONITRA_RELEASE_CREDENTIAL is the only copy, mint a new one
against production from backend/::

    DATABASE_URL="<production URL>" python scripts/provision_release_credential.py show
    DATABASE_URL="<production URL>" python scripts/provision_release_credential.py \
        rotate --name github-actions-release --i-am-sure

`show` is read-only and prints which database it resolved before doing
anything. Paste the new key into the GitHub secret as well, or the next
tagged release will register nothing.

NOTE ON SIGNING: the v1.1.1 artifacts are UNSIGNED -- the workflow run for the
tag shows "Sign the application and installer", "Import signing certificate"
and "Create the notarization profile" all skipped. CLAUDE.md section 5.6 and
docs/Desktop_Update_Distribution_Decisions.md section 2 say not to publish to
real users until Windows code signing and macOS notarization are in place.
Windows users will see a SmartScreen warning. Publishing anyway is a decision
this script will carry out, not one it makes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

REPO = "KairavThakar90/monitra"
TAG = "v1.1.1"
VERSION = "1.1.1"
DEFAULT_API = "https://monitra-lvzq.vercel.app/api/v1"

ARTIFACTS = (
    "Monitra-Setup-1.1.1.exe",
    "Monitra-macOS-arm64-1.1.1.dmg",
    "Monitra-macOS-x86_64-1.1.1.dmg",
)

REPO_ROOT = Path(__file__).resolve().parent
DESKTOP = REPO_ROOT / "desktop"


def asset_url(name: str) -> str:
    return f"https://github.com/{REPO}/releases/download/{TAG}/{name}"


def download(name: str, into: Path) -> Path:
    target = into / name
    if target.exists():
        print(f"  {name}: already downloaded")
        return target
    print(f"  {name}: downloading ...", flush=True)
    request = urllib.request.Request(
        asset_url(name), headers={"User-Agent": "monitra-release-publisher"}
    )
    with urllib.request.urlopen(request, timeout=300) as response:
        target.write_bytes(response.read())
    return target


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def published_checksum(name: str, into: Path) -> str:
    """The checksum GitHub carries beside the artifact."""
    sidecar = download(name + ".sha256", into)
    # The sidecar is "<hex>  <filename>", as sha256sum writes it.
    return sidecar.read_text(encoding="utf-8").split()[0].strip().lower()


def api(method: str, url: str, token: str, payload=None):
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=body, method=method)
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(request, timeout=60) as response:
        text = response.read().decode("utf-8")
    return json.loads(text) if text else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-base", default=os.environ.get("MONITRA_API_BASE_URL", DEFAULT_API))
    parser.add_argument(
        "--work-dir",
        default=str(Path(tempfile.gettempdir()) / "monitra_release_v1_1_1"),
        help="Where artifacts are downloaded. Outside the repository on "
             "purpose: 141 MB of installers in the tree is one `git add -A` "
             "away from being committed, and they are build output.",
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Download and verify only. Registers and publishes nothing.")
    parser.add_argument("--no-publish", action="store_true",
                        help="Register drafts but leave them unpublished.")
    args = parser.parse_args()

    token = os.environ.get("MONITRA_RELEASE_CREDENTIAL", "").strip()
    if not token and not args.dry_run:
        print("MONITRA_RELEASE_CREDENTIAL is not set. See the docstring for how "
              "to mint one.", file=sys.stderr)
        return 2

    work = Path(args.work_dir)
    work.mkdir(parents=True, exist_ok=True)

    print(f"Target API : {args.api_base}")
    print(f"Release    : {REPO} {TAG}")
    print(f"Work dir   : {work}\n")

    print("1. Downloading and verifying artifacts")
    paths = []
    for name in ARTIFACTS:
        path = download(name, work)
        expected = published_checksum(name, work)
        actual = sha256_of(path)
        if actual != expected:
            print(f"  {name}: CHECKSUM MISMATCH\n"
                  f"      published {expected}\n"
                  f"      on disk   {actual}\n"
                  "  Refusing to register a row whose checksum does not match "
                  "the bytes people will download.", file=sys.stderr)
            return 1
        print(f"  {name}: sha256 verified ({path.stat().st_size} bytes)")
        paths.append(path)

    if args.dry_run:
        print("\nDry run: all three artifacts verified. Nothing was registered.")
        return 0

    print("\n2. Registering draft rows")
    env = dict(os.environ)
    env["MONITRA_API_BASE_URL"] = args.api_base
    env["MONITRA_RELEASE_CREDENTIAL"] = token
    result = subprocess.run(
        [sys.executable, "tools/register_release.py",
         "--tag", TAG, "--repo", REPO,
         "--artifacts", *[str(p) for p in paths]],
        cwd=str(DESKTOP), env=env,
    )
    if result.returncode != 0:
        print("Registration failed. Nothing has been published.", file=sys.stderr)
        return result.returncode

    if args.no_publish:
        print("\n--no-publish: the rows are drafts. The live page is unchanged.")
        return 0

    print("\n3. Publishing the v1.1.1 rows")
    listing = api("GET", f"{args.api_base}/desktop/releases?status=draft&limit=200", token)
    rows = [r for r in (listing or {}).get("releases", []) if r.get("version") == VERSION]
    if not rows:
        print("No draft rows for 1.1.1 came back. Nothing published.", file=sys.stderr)
        return 1

    published = 0
    for row in rows:
        label = f"{row.get('platform')}/{row.get('architecture') or 'any'}"
        try:
            api("POST", f"{args.api_base}/desktop/releases/{row['id']}/publish", token)
        except urllib.error.HTTPError as exc:
            print(f"  {label}: FAILED ({exc.code})", file=sys.stderr)
            continue
        print(f"  {label}: published")
        published += 1

    print(f"\n{published}/{len(rows)} row(s) published.")
    if published != 3:
        print("Fewer than three platforms are live. A version with only some "
              "rows published leaves the other platforms' download buttons "
              "saying \"Not available yet\".", file=sys.stderr)
        return 1

    print("\nVerify:")
    print(f"  curl -s {args.api_base}/desktop/releases/downloads")
    print("  then reload https://staff.peakworkos.com/download")
    return 0


if __name__ == "__main__":
    sys.exit(main())

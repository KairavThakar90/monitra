#!/usr/bin/env python
"""
verify_release_artifacts — is this set of build outputs a releasable version?

    python tools/verify_release_artifacts.py --artifacts artifacts --tag v1.3.2 [--require-signed]

Run by the release job *before* anything is published or registered. It fails
(exit 1) unless the directory holds, for the version in ``version.py``:

* all three installers — Windows, macOS Apple Silicon, macOS Intel — each
  exactly once, each named exactly as the build scripts name it for this
  version, none empty;
* a ``.sha256`` sidecar for each, whose digest is the file's actual digest;
* with ``--require-signed``, a signature attestation (`attest_signature.py`) for
  each, made against this exact file, saying it is validly signed.

and unless the tag agrees with ``version.py`` and the build is not a pre-release.

Why it exists
-------------
Three things used to be checked late or not at all. The tag was compared with
``version.py`` only after the GitHub draft release had already been created, so
a wrong tag left a draft release with no backend rows. A missing platform — one
macOS runner failing — still produced a release, and Mac users were told about
an update with nothing to download. And "signed" was whatever the signing step
claimed. Judging the whole set in one place, before publishing, makes all three
a failed run instead of a stranded release.

The backend repeats the platform-completeness and signing checks at publish time
(`DESKTOP_REQUIRED_ARTIFACTS`, `DESKTOP_REQUIRE_SIGNED_RELEASES`), so this is the
early, loud half of a gate that does not depend on CI having run it.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

DESKTOP_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(DESKTOP_ROOT))
sys.path.insert(0, str(DESKTOP_ROOT / "tools"))

import version  # noqa: E402
import register_release as reg  # noqa: E402
from _attestation import load_attestation  # noqa: E402

#: What a releasable version must contain: (platform, architecture) -> file name.
#: Mirrors the backend's DESKTOP_REQUIRED_ARTIFACTS default.
def required_artifacts(ver: str) -> Dict[Tuple[str, Optional[str]], str]:
    return {
        ("win32", None): f"Monitra-Setup-{ver}.exe",
        ("darwin", "arm64"): f"Monitra-macOS-arm64-{ver}.dmg",
        ("darwin", "x86_64"): f"Monitra-macOS-x86_64-{ver}.dmg",
    }


def label(key: Tuple[str, Optional[str]]) -> str:
    return f"{key[0]}/{key[1]}" if key[1] else key[0]


def read_checksum_sidecar(path: Path) -> Optional[str]:
    """The digest in `<artifact>.sha256` (``<hex>  <name>``), or None."""
    sidecar = path.with_name(path.name + ".sha256")
    try:
        first = sidecar.read_text(encoding="utf-8", errors="replace").split()
    except OSError:
        return None
    return first[0].lower() if first else None


def verify(artifacts_dir: Path, tag: str, *, require_signed: bool) -> List[str]:
    """Every reason this set is not releasable. Empty means it is."""
    problems: List[str] = []
    ver = version.VERSION

    if version.is_prerelease():
        problems.append(
            f"version.py marks this build as a pre-release ({version.display_version()}); "
            "an internal test build is never released."
        )
    if tag.lstrip("v") != ver:
        problems.append(f"tag {tag} does not match version.py ({ver}).")

    found: Dict[Tuple[str, Optional[str]], List[Path]] = {}
    for path in sorted(artifacts_dir.rglob("*")):
        if not path.is_file():
            continue
        classified = reg.classify(path.name)
        if classified is not None:
            found.setdefault(classified, []).append(path)

    for key, expected_name in required_artifacts(ver).items():
        paths = found.get(key, [])
        if not paths:
            problems.append(f"{label(key)}: no installer was built ({expected_name} is missing).")
            continue
        if len(paths) > 1:
            problems.append(f"{label(key)}: {len(paths)} files claim to be this artifact "
                            f"({', '.join(p.name for p in paths)}).")
            continue
        path = paths[0]
        if path.name != expected_name:
            problems.append(f"{label(key)}: found {path.name}, expected {expected_name} "
                            "(the artifact is not this version).")
            continue
        size = path.stat().st_size
        if size <= 0:
            problems.append(f"{label(key)}: {path.name} is empty.")
            continue

        actual = reg.sha256_of(path)
        recorded = read_checksum_sidecar(path)
        if recorded is None:
            problems.append(f"{label(key)}: {path.name}.sha256 is missing.")
        elif recorded != actual:
            problems.append(f"{label(key)}: {path.name}.sha256 says {recorded[:12]}… but the "
                            f"file is {actual[:12]}….")

        if require_signed:
            record = load_attestation(path)
            if record is None:
                problems.append(f"{label(key)}: no signature attestation for this exact file.")
            elif record.get("signed") is not True:
                problems.append(f"{label(key)}: {path.name} is not validly signed"
                                + (f" ({record.get('reason')})" if record.get("reason") else "") + ".")
    return problems


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--artifacts", required=True, help="Directory holding the downloaded build outputs.")
    parser.add_argument("--tag", required=True, help="The release tag, e.g. v1.3.2.")
    parser.add_argument("--require-signed", action="store_true")
    args = parser.parse_args(argv)

    problems = verify(Path(args.artifacts), args.tag, require_signed=args.require_signed)
    if not problems:
        print(f"Monitra {version.VERSION}: every required artifact is present, intact"
              + (" and signed." if args.require_signed else "."))
        if not args.require_signed:
            print("(Signatures were not required for this run.)")
        return 0

    print(f"Monitra {version.VERSION} is NOT releasable:", file=sys.stderr)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    for problem in problems:
        print(f"  - {problem}", file=sys.stderr)
        if os.environ.get("GITHUB_ACTIONS") == "true":
            print(f"::error::{problem}")
    if summary:
        try:
            with open(summary, "a", encoding="utf-8") as handle:
                handle.write("### Release artifacts are not releasable\n"
                             + "".join(f"- {p}\n" for p in problems))
        except OSError:
            pass
    return 1


if __name__ == "__main__":
    sys.exit(main())

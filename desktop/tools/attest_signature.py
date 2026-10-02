#!/usr/bin/env python
"""
attest_signature — verify a built artifact's code signature, and record it.

    python tools/attest_signature.py --platform win32  --artifact dist/installer/Monitra-Setup-1.3.2.exe
    python tools/attest_signature.py --platform darwin --artifact dist/Monitra-macOS-arm64-1.3.2.dmg --require-signed

Writes ``<artifact>.signature.json`` beside the artifact::

    {"artifact": "Monitra-Setup-1.3.2.exe", "artifact_sha256": "…", "signed": true,
     "signer": "Monitra Ltd", "checked_with": "WinVerifyTrust", "reason": null}

Why this exists
---------------
Signing in CI is gated on secrets, so a build can be signed or not, and until
now nothing *checked* which. A signing step that was silently skipped, or ran
against the wrong file, produced a green build and an unsigned installer. This
tool reads the signature back from the finished file and says what it found.

**Windows is verified with the desktop's own verifier**
(`background_services.update.signature`), not with signtool: the question that
matters is "will the updater on a staff machine accept this file", and the only
way to be sure is to ask the code that will. It applies the same publisher pin
(`policy.WINDOWS_SIGNER_PINS`) too, so a certificate the client would refuse is
refused here, in CI, not on the first user's machine.

**The attestation is not the security boundary.** The release registration
carries it to the backend as a *process gate* (an unsigned build cannot be
published by mistake), and the desktop still verifies the real signature before
it runs anything. A forged sidecar can at worst make the backend allow a
publish that the client then refuses.

With ``--require-signed`` the tool exits non-zero when the artifact is not
validly signed — used when the signing secrets are configured, where "not
signed" can only mean the signing step failed.

macOS
-----
Checks ``codesign --verify --strict``, Gatekeeper (``spctl --assess``) and the
notarization staple on the DMG, and records the Authority / Team ID. **Never run
on a real Mac by this project's tests**; only the parsing of those tools' output
is unit-tested.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Tuple

DESKTOP_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(DESKTOP_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _attestation import load_attestation, sha256_of, sidecar_path  # noqa: E402,F401
from background_services.update import policy  # noqa: E402


def parse_codesign_display(text: str) -> Tuple[Optional[str], Optional[str]]:
    """(first Authority, TeamIdentifier) from ``codesign -dv --verbose=4`` output.

    The first ``Authority=`` line is the leaf — "Developer ID Application: Name
    (TEAMID)" — which is the signer. ``TeamIdentifier=not set`` means ad-hoc.
    """
    authority = team = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("Authority=") and authority is None:
            authority = line.split("=", 1)[1].strip()
        elif line.startswith("TeamIdentifier="):
            value = line.split("=", 1)[1].strip()
            team = None if value.lower() in ("not set", "") else value
    return authority, team


def _run(command: List[str]) -> Tuple[int, str]:
    completed = subprocess.run(command, capture_output=True, text=True)
    return completed.returncode, (completed.stdout or "") + (completed.stderr or "")


def attest_windows(artifact: Path) -> dict:
    from background_services.update import signature

    result = signature.verify_authenticode(artifact)
    problem = signature.evaluate(result)
    return {
        "signed": problem is None,
        "signer": result.signer if problem is None else None,
        "checked_with": "WinVerifyTrust",
        "reason": problem,
    }


def attest_macos(artifact: Path) -> dict:
    steps = (
        ("codesign", ["codesign", "--verify", "--strict", "--verbose=2", str(artifact)]),
        ("gatekeeper", ["spctl", "--assess", "--type", "open",
                        "--context", "context:primary-signature", "-v", str(artifact)]),
        ("notarization", ["xcrun", "stapler", "validate", str(artifact)]),
    )
    for label, command in steps:
        code, output = _run(command)
        if code != 0:
            return {"signed": False, "signer": None, "checked_with": "codesign/spctl/stapler",
                    "reason": f"{label} check failed: {output.strip()[:300]}"}
    _code, display = _run(["codesign", "-dv", "--verbose=4", str(artifact)])
    authority, team = parse_codesign_display(display)
    pins = policy.MACOS_TEAM_ID_PINS
    if pins and team not in pins:
        return {"signed": False, "signer": authority, "checked_with": "codesign/spctl/stapler",
                "reason": f"signed by team {team!r}, which is not an approved publisher"}
    return {"signed": True, "signer": authority or team, "checked_with": "codesign/spctl/stapler",
            "reason": None}


def attest(artifact: Path, platform: str) -> dict:
    """Verify `artifact` and return the attestation record (not yet written)."""
    findings = attest_windows(artifact) if platform == "win32" else attest_macos(artifact)
    return {"artifact": artifact.name, "artifact_sha256": sha256_of(artifact), **findings}


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--platform", choices=("win32", "darwin"), required=True)
    parser.add_argument("--artifact", required=True)
    parser.add_argument("--require-signed", action="store_true",
                        help="Exit non-zero unless the artifact is validly signed.")
    args = parser.parse_args(argv)

    artifact = Path(args.artifact)
    if not artifact.is_file():
        print(f"{artifact}: not found", file=sys.stderr)
        return 2

    record = attest(artifact, args.platform)
    sidecar_path(artifact).write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")

    state = "SIGNED" if record["signed"] else "NOT SIGNED"
    print(f"{artifact.name}: {state}"
          + (f" by {record['signer']}" if record["signed"] and record["signer"] else "")
          + (f" ({record['reason']})" if record["reason"] else ""))
    if args.require_signed and not record["signed"]:
        print(
            f"::error::{artifact.name} is not validly signed, but signing is "
            "configured for this build, so the signing step must have failed or "
            "been skipped.",
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

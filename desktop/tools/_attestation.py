"""
The signature attestation sidecar: written by `attest_signature.py`, read by
`register_release.py` and `verify_release_artifacts.py`.

Standard library only, on purpose: registration runs on a bare CI step and must
not import the desktop runtime just to read a small JSON file.

An attestation is ``<artifact>.signature.json`` holding, among other things, the
SHA-256 of the artifact it was made for. `load_attestation` returns it only when
that digest equals the artifact's own — a sidecar left over from an earlier
build, or copied from another file, describes something else and is ignored, so
the artifact is then treated as unsigned rather than trusted on a stale claim.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Optional

CHUNK = 1024 * 1024


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def sidecar_path(artifact: Path) -> Path:
    return artifact.with_name(artifact.name + ".signature.json")


def load_attestation(artifact: Path) -> Optional[dict]:
    """The attestation beside `artifact`, if it is for this exact file."""
    try:
        record = json.loads(sidecar_path(artifact).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict):
        return None
    if record.get("artifact_sha256") != sha256_of(artifact):
        return None
    return record

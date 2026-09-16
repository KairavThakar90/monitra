#!/usr/bin/env python
"""
check_changelog — a release cannot ship without a written release note.

GitHub's auto-generated notes list commit titles. That is not the same thing
as telling a member of staff what changed for them, and it is the only thing a
release carried before this check existed. So: if `version.py` names a version,
`CHANGELOG.md` must have a heading for it.

    python tools/check_changelog.py

Exit status is non-zero when the current version has no entry. The check is
deliberately shallow — it verifies that a human wrote *something* under a
heading for this version, not what they wrote.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

DESKTOP_ROOT = Path(__file__).resolve().parent.parent
CHANGELOG = DESKTOP_ROOT / "CHANGELOG.md"

sys.path.insert(0, str(DESKTOP_ROOT))

from version import display_version  # noqa: E402

#: The heading a release note must sit under. For a production release this
#: is the plain version; for an internal test build it is the pre-release
#: form (`1.2.0-beta.1`), so the note written for the testers of that build is
#: the one required -- a production note for the same number does not count,
#: because it describes a build that does not exist yet.
VERSION = display_version()


def heading_body(text: str, version: str) -> str | None:
    """Return the text under this version's heading, or None if it has none.

    Both `## [1.0.1]` and `## 1.0.1` are accepted; a trailing date is fine.
    """
    pattern = re.compile(
        r"^##\s+\[?" + re.escape(version) + r"\]?.*$", re.MULTILINE
    )
    match = pattern.search(text)
    if match is None:
        return None
    remainder = text[match.end():]
    next_heading = re.search(r"^##\s", remainder, re.MULTILINE)
    return remainder[: next_heading.start()] if next_heading else remainder


def main() -> int:
    if not CHANGELOG.exists():
        print(f"{CHANGELOG.name} is missing; every release needs a written note.")
        return 1

    text = CHANGELOG.read_text(encoding="utf-8")
    body = heading_body(text, VERSION)

    if body is None:
        print(
            f"CHANGELOG.md has no entry for version {VERSION}.\n\n"
            f"Add a `## [{VERSION}]` section describing what changed for the\n"
            f"people who will install it, then re-run this check. Release notes\n"
            f"generated from commit titles are not a substitute -- they answer\n"
            f"a different question."
        )
        return 1

    if not body.strip():
        print(f"CHANGELOG.md has an empty entry for version {VERSION}.")
        return 1

    print(f"CHANGELOG.md documents version {VERSION}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

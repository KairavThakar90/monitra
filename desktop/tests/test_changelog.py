"""
The changelog gate.

A release used to be able to ship with nothing but auto-generated commit
titles as its notes. `tools/check_changelog.py` closes that: the version in
`version.py` must have a written entry. These tests pin the gate itself, so a
future refactor cannot quietly turn it into a no-op that always passes.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import check_changelog  # noqa: E402
import version as version_module  # noqa: E402
from version import VERSION  # noqa: E402


def test_the_current_version_is_documented():
    assert check_changelog.main() == 0


def test_an_internal_test_build_needs_its_own_entry():
    # The gate asks for the display form: while version.py carries a
    # pre-release label, the note required is the one written for that build's
    # testers, not a production note under the bare number for a build that
    # does not exist yet.
    assert check_changelog.VERSION == version_module.display_version()
    if version_module.is_prerelease():
        assert check_changelog.VERSION != VERSION
        assert check_changelog.heading_body(
            f"## [{VERSION}]\n\nProduction note.\n", check_changelog.VERSION
        ) is None


def test_a_missing_entry_fails_the_check():
    body = check_changelog.heading_body("## [9.9.9]\n\nSomething.\n", VERSION)
    assert body is None


def test_an_entry_is_found_with_or_without_brackets_and_a_date():
    for heading in (f"## [{VERSION}]", f"## {VERSION}", f"## [{VERSION}] - 2026-09-03"):
        body = check_changelog.heading_body(f"{heading}\n\nFixed a thing.\n", VERSION)
        assert body is not None and body.strip() == "Fixed a thing."


def test_an_empty_entry_is_not_a_release_note():
    # A heading with nothing under it is the shape a "just make CI pass" edit
    # takes, so it must not satisfy the gate.
    body = check_changelog.heading_body(f"## [{VERSION}]\n\n## [1.0.0]\n\nFirst.\n", VERSION)
    assert body is not None
    assert not body.strip()


def test_an_entry_stops_at_the_next_version_heading():
    text = f"## [{VERSION}]\n\nThis release.\n\n## [1.0.0]\n\nThe previous one.\n"
    body = check_changelog.heading_body(text, VERSION)
    assert "This release." in body
    assert "The previous one." not in body

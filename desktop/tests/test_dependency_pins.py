"""
The GUI toolkit under test is the GUI toolkit that ships.

`requirements.txt` listed a bare `PySide6`, so the release job installed
whatever was newest on the day of the build. PySide6 6.12.0 was published on
2026-10-08; v1.3.2 was built on 2026-10-09 and v1.3.3 on 2026-10-29, so both
shipped Qt 6.12 while every test, every local run and the local `dist/` build
used 6.11.2 -- and the Request dialog's time fields were drawn one pixel wide
only on the shipped toolkit. A green suite had proved nothing about the
installer.

Two things keep that from happening again. The pin is exact, so a build and
a developer's environment resolve to the same version; and the version this
suite runs on *is* the pinned one, so a developer who has upgraded locally is
told so here, before anything is verified on a toolkit that will not ship.
Moving the pin is deliberate: install the new version, run the suite on it,
then change the line.
"""
import re
from pathlib import Path

import PySide6

REQUIREMENTS = Path(__file__).resolve().parent.parent / "requirements.txt"


def _requirement_lines() -> list:
    lines = []
    for raw in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            lines.append(line)
    return lines


def _pyside6_lines() -> list:
    return [line for line in _requirement_lines() if re.match(r"pyside6\b", line, re.IGNORECASE)]


def pinned_pyside6_version() -> str:
    lines = _pyside6_lines()
    assert len(lines) == 1, f"requirements.txt must list PySide6 exactly once, found {lines!r}"
    match = re.fullmatch(r"PySide6\s*==\s*([0-9]+(?:\.[0-9]+)+)", lines[0], re.IGNORECASE)
    assert match is not None, (
        f"PySide6 is not pinned to one exact version: {lines[0]!r}. "
        "Use `PySide6==<version>`; a bare name or a range lets the build ship a "
        "toolkit nothing was tested on."
    )
    return match.group(1)


def test_pyside6_is_pinned_to_one_exact_version():
    assert re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", pinned_pyside6_version())


def test_the_pyside6_under_test_is_the_pinned_one():
    pinned = pinned_pyside6_version()
    assert PySide6.__version__ == pinned, (
        f"This suite is running on PySide6 {PySide6.__version__}, but the build ships "
        f"{pinned}. Install the pinned version (`pip install -r requirements.txt`) -- or, "
        "to move the pin, run the suite on the new version and then change requirements.txt."
    )

"""The type checker's view of macOS-only imports stays in step with the code.

``pyrefly.toml`` lists the pyobjc frameworks (``Quartz``, ``AppKit``) that
are installed only on Darwin -- see the ``sys_platform == "darwin"`` markers
in ``requirements.txt`` -- and imported lazily inside functions that only run
there. On Windows and Linux those packages are legitimately absent, and
without the config every such import is reported as a hard ``missing-import``
error in the IDE, which buries real type errors under noise nobody can act on.

This test reads the config and scans every module in the tree so that:

* a newly imported macOS-only framework that is not listed fails here rather
  than reappearing as a red squiggle on every developer's Windows machine, and
* a framework that is listed but no longer imported anywhere is removed, so
  the list never silently grows into a blanket "ignore missing imports".

``tomllib`` is used rather than a hand-rolled parser so a config that Pyrefly
itself could not read also fails here.
"""
from __future__ import annotations

import ast
import tomllib
from pathlib import Path

DESKTOP_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = DESKTOP_ROOT / "pyrefly.toml"

#: pyobjc framework packages that only exist on macOS. Anything imported from
#: this set must be declared in ``pyrefly.toml``.
MACOS_ONLY_MODULES = frozenset({
    "AppKit",
    "ApplicationServices",
    "Cocoa",
    "CoreFoundation",
    "Foundation",
    "Quartz",
    "objc",
})

#: Directories that hold no first-party source: virtual environments, build
#: output, and packaged artifacts.
_SKIP_DIR_PREFIXES = (".",)
_SKIP_DIRS = frozenset({"build", "dist", "__pycache__", "node_modules"})


def _source_files() -> list[Path]:
    files: list[Path] = []
    for path in DESKTOP_ROOT.rglob("*.py"):
        parts = path.relative_to(DESKTOP_ROOT).parts[:-1]
        if any(p.startswith(_SKIP_DIR_PREFIXES) or p in _SKIP_DIRS for p in parts):
            continue
        files.append(path)
    return files


def _imported_macos_modules(path: Path) -> set[str]:
    """Top-level module names from every import statement in ``path``.

    ``ast`` sees imports at any nesting depth, which matters here: the macOS
    imports are deliberately inside functions, not at module scope.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names = [node.module]
        else:
            continue
        for name in names:
            top = name.split(".", 1)[0]
            if top in MACOS_ONLY_MODULES:
                found.add(top)
    return found


def _configured_ignores() -> list[str]:
    with CONFIG_PATH.open("rb") as fh:
        config = tomllib.load(fh)
    return list(config.get("ignore-missing-imports", []))


def test_config_exists_and_parses():
    assert CONFIG_PATH.is_file(), "desktop/pyrefly.toml is missing"
    ignores = _configured_ignores()
    assert ignores, "ignore-missing-imports must list the macOS-only frameworks"
    assert ignores == sorted(ignores), "keep ignore-missing-imports sorted"


def test_every_imported_macos_framework_is_declared():
    ignores = set(_configured_ignores())
    undeclared: dict[str, set[str]] = {}
    for path in _source_files():
        missing = _imported_macos_modules(path) - ignores
        if missing:
            undeclared[str(path.relative_to(DESKTOP_ROOT))] = missing
    assert not undeclared, (
        "macOS-only frameworks are imported but not listed in "
        f"pyrefly.toml ignore-missing-imports: {undeclared}"
    )


def test_config_only_lists_macos_frameworks_still_in_use():
    ignores = set(_configured_ignores())

    not_macos = ignores - MACOS_ONLY_MODULES
    assert not not_macos, (
        "ignore-missing-imports is reserved for macOS-only pyobjc frameworks; "
        f"remove {sorted(not_macos)} (fix the import, or install the package)"
    )

    in_use: set[str] = set()
    for path in _source_files():
        in_use |= _imported_macos_modules(path)
    stale = ignores - in_use
    assert not stale, f"no module imports {sorted(stale)} any more; remove it"

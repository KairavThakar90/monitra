"""
The Windows UI Automation reader must be *importable* on every platform.

`tracking/browsers/uia.py` is Windows-only by design (`SUPPORTED`), and its
callers all check that flag. But the module sits on the import path of
`tracking.browsers`, `UrlUsageService` and therefore `core.runtime` -- so a
Windows-only attribute evaluated at *import* time (`ctypes.windll`, which does
not exist off Windows) took the whole application down with it: the packaged
macOS build raised AttributeError before a window existed, and the Linux
correctness gate could not collect five test modules. Neither failure was
visible on a Windows development machine, which is why this test simulates the
other platforms in a subprocess rather than trusting the host.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

DESKTOP_ROOT = Path(__file__).resolve().parent.parent

#: Runs with `ctypes.windll` / `ctypes.oledll` removed -- the situation on a
#: Mac or a Linux runner -- and `sys.platform` reporting another OS, then
#: imports the named modules. Any module-level Windows-only lookup fails this.
_PROBE = """
import ctypes, sys
sys.platform = {platform!r}
for name in ("windll", "oledll"):
    if hasattr(ctypes, name):
        delattr(ctypes, name)
import importlib
for module in {modules!r}:
    importlib.import_module(module)
import tracking.browsers.uia as uia
assert uia.SUPPORTED is False, "SUPPORTED must be False off Windows"
assert uia.read_address_bar(12345) is None, "off Windows the reader must answer None"
print("IMPORT_OK")
"""

#: The full chain the runtime pulls in is exercised as Linux, which is what the
#: correctness gate runs on. Under a simulated `darwin` the standard library
#: itself reaches for macOS-only modules (`urllib.request` imports `_scproxy`),
#: which has nothing to do with this defect, so that case stays on the browser
#: package -- the module that actually failed.
_CASES = [
    ("linux", ["tracking.browsers", "background_services.activity.url_usage_service",
               "core.runtime"]),
    ("darwin", ["tracking.browsers"]),
]


@pytest.mark.parametrize("platform,modules", _CASES)
def test_the_runtime_imports_without_windows_only_ctypes(platform, modules):
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", PYTHONPATH=str(DESKTOP_ROOT))
    completed = subprocess.run(
        [sys.executable, "-c", _PROBE.format(platform=platform, modules=modules)],
        cwd=str(DESKTOP_ROOT), env=env, capture_output=True, text=True, timeout=120,
    )
    assert completed.returncode == 0, (
        f"importing {modules} as {platform} failed:\n{completed.stderr[-2000:]}"
    )
    assert "IMPORT_OK" in completed.stdout


def test_no_module_level_windows_only_ctypes_lookup_in_uia():
    """A cheaper, host-independent guard for the same defect: any
    `ctypes.windll` / `ctypes.oledll` reference at module scope must sit under
    the SUPPORTED guard, never at column zero."""
    source = (DESKTOP_ROOT / "tracking" / "browsers" / "uia.py").read_text(encoding="utf-8")
    offenders = [
        line for line in source.splitlines()
        if (line.startswith("_") or line.startswith("ctypes."))
        and ("ctypes.windll" in line or "ctypes.oledll" in line)
    ]
    assert offenders == [], f"Windows-only ctypes lookups at import time: {offenders}"

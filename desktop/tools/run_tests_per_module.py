#!/usr/bin/env python
"""Run the regression suite one test module per interpreter process.

Why this exists
---------------
On the macOS release runners, `python -m pytest tests/ -q` dies inside Qt --
a bus error on Apple Silicon, a segmentation fault on Intel -- while a test
shows an ordinary top-level window under the offscreen platform, roughly a
third of the way through the suite. It is not one test: across four builds on
2026-09-17 it struck the login window, then the maintenance toast's host
widget, and once did not strike at all, so the same commit passed on one
architecture and crashed on the other. A crash that moves between modules and
comes and goes is state one module leaves behind for the next inside the one
long-lived QApplication, and a single interpreter crash there takes every
remaining test with it and fails the build with no result for them.

This runner gives each test module its own interpreter and therefore its own
QApplication. Nothing a module leaves behind can reach the next one, every
module reports its own result, and a crash -- should one still happen -- is
attributed to the one file it happened in instead of ending the run.

What it is not
--------------
It does not skip, retry or soften anything. Every module runs, every failure
fails the run, and a crashed module fails the run too (its signal is
reported). The Windows and Linux gates keep the single-process invocation;
this is used where the platform needs it.

Usage, from desktop/::

    python tools/run_tests_per_module.py            # tests/test_*.py
    python tools/run_tests_per_module.py -- -q -x   # extra pytest arguments
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path

DESKTOP_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = DESKTOP_ROOT / "tests"


def _count(summary: str, word: str) -> int:
    match = re.search(rf"(\d+) {word}", summary)
    return int(match.group(1)) if match else 0


def _annotate(rel: str, status: str, output: str) -> None:
    """Report a failed module as a GitHub Actions error annotation.

    A job's log can only be read by someone signed in with access to the
    repository, but its annotations are served with the check run. A macOS
    failure reported only in the log could not be diagnosed without that
    access (v1.3.0's first build). This puts the failing test ids and the
    assertion or crash lines beside it. Test output only: no environment
    and no secrets are in it.
    """
    lines = output.splitlines()
    picked = [l for l in lines if l.startswith(("FAILED ", "ERROR "))]
    picked += [l for l in lines if l.lstrip().startswith("E ")][:25]
    picked += [l for l in lines if "Fatal Python error" in l or "Current thread" in l]
    picked += [l for l in lines if re.search(r'File ".*", line \d+ in ', l)][:15]
    message = "\n".join(picked[:60]) or "\n".join(lines[-30:])
    # Workflow-command escaping: %, CR and LF must be encoded in the message.
    message = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    print(f"::error file=desktop/{rel},title={status} {rel}::{message}", flush=True)


def main(argv: list[str]) -> int:
    extra = argv[1:]
    if extra and extra[0] == "--":
        extra = extra[1:]
    # Every module `python -m pytest tests/` would collect, sub-packages
    # included (tests/e2e/ is skipped without a backend, but it is collected).
    modules = sorted(TESTS_DIR.rglob("test_*.py"))
    if not modules:
        print("no test modules found under", TESTS_DIR)
        return 2

    totals = {"passed": 0, "failed": 0, "errors": 0, "skipped": 0}
    problems: list[str] = []
    started = time.monotonic()

    for module in modules:
        rel = module.relative_to(DESKTOP_ROOT).as_posix()
        completed = subprocess.run(
            [sys.executable, "-m", "pytest", rel, "-q", "-p", "no:cacheprovider", *extra],
            cwd=str(DESKTOP_ROOT), capture_output=True, text=True,
        )
        output = completed.stdout + completed.stderr
        summary_lines = [l for l in output.splitlines() if re.search(r"\d+ (passed|failed|error|skipped)", l)]
        summary = summary_lines[-1].strip() if summary_lines else ""
        for key, word in (("passed", "passed"), ("failed", "failed"), ("errors", "errors?"),
                          ("skipped", "skipped")):
            totals[key] += _count(summary, word)

        if completed.returncode == 0:
            status = "ok  "
        elif completed.returncode == 5:
            status = "none"          # pytest: no tests collected
        elif completed.returncode < 0 or completed.returncode > 128 \
                or "Fatal Python error" in output:
            status = "CRASH"
            problems.append(f"{rel}: interpreter crashed (exit {completed.returncode})")
        else:
            status = "FAIL"
            problems.append(f"{rel}: exit {completed.returncode}")

        print(f"[{status}] {rel:60s} {summary}", flush=True)
        if status in ("FAIL", "CRASH"):
            # The module's own report, so the cause is in the log next to
            # its name rather than only in a summary.
            tail = output.strip().splitlines()[-60:]
            print("\n".join("        " + l for l in tail), flush=True)
            if os.environ.get("GITHUB_ACTIONS") == "true":
                _annotate(rel, status, output)

    elapsed = time.monotonic() - started
    print()
    print(f"{len(modules)} modules in {elapsed:.0f}s: "
          f"{totals['passed']} passed, {totals['failed']} failed, "
          f"{totals['errors']} errors, {totals['skipped']} skipped")
    if problems:
        print("FAILED modules:")
        for p in problems:
            print("  -", p)
        return 1
    print("PASS: every module passed in its own process.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

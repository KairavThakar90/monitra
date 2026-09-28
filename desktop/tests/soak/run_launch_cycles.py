"""
Repeated launch/quit cycle test.

The spec requires at least 10 launch/quit cycles, because the audited failures
were timing dependent: the application would launch correctly once and hang the
next time. A single successful run proves nothing.

Each cycle runs the real `main()` in a fresh subprocess, so this exercises the
true process lifecycle including `aboutToQuit`, service shutdown and interpreter
exit — not an in-process approximation.

    python tests/soak/run_launch_cycles.py [--cycles 10] [--uptime 6]

Fails (non-zero exit) if any cycle hangs, crashes, leaves a service running, or
logs a thread that would not stop.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

DESKTOP_ROOT = Path(__file__).resolve().parent.parent.parent

CHILD = r'''
import os, sys, threading, time
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, r"{root}")
os.chdir(r"{root}")

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
import main as app_main

UPTIME = {uptime}

def _quit_after_uptime():
    deadline = time.monotonic() + UPTIME
    while time.monotonic() < deadline:
        time.sleep(0.1)
    app = QApplication.instance()
    if app is None:
        return
    for widget in app.topLevelWidgets():
        if hasattr(widget, "quit_application"):
            QTimer.singleShot(0, widget, widget.quit_application)
            return
    QTimer.singleShot(0, app, app.quit)

threading.Thread(target=_quit_after_uptime, daemon=True).start()
code = app_main.main()
sys.exit(code or 0)
'''


def run_cycle(index: int, uptime: float, timeout: float) -> dict:
    """Run one launch/quit cycle in a subprocess."""
    script = CHILD.format(root=str(DESKTOP_ROOT), uptime=uptime)
    env = dict(os.environ, MONITRA_LOG_LEVEL="INFO", QT_QPA_PLATFORM="offscreen")

    started = time.monotonic()
    try:
        completed = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True, text=True, timeout=timeout, env=env,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "cycle": index,
            "ok": False,
            "reason": f"process did not exit within {timeout}s",
            "duration": time.monotonic() - started,
            "output": (exc.stderr or b"").decode("utf-8", "replace")[-2000:]
            if isinstance(exc.stderr, bytes) else (exc.stderr or "")[-2000:],
        }

    duration = time.monotonic() - started
    output = (completed.stdout or "") + (completed.stderr or "")

    problems = []
    if completed.returncode != 0:
        problems.append(f"exit code {completed.returncode}")
    if "Destroyed while thread" in output:
        problems.append("QThread destroyed while still running")
    if "did not stop within" in output:
        problems.append("a service thread refused to stop")
    if "escalating to terminate()" in output:
        problems.append("shutdown escalated to terminate()")
    if "shutdown complete" not in output:
        problems.append("shutdown did not complete cleanly")

    return {
        "cycle": index,
        "ok": not problems,
        "reason": "; ".join(problems),
        "duration": duration,
        "output": output[-2000:] if problems else "",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cycles", type=int, default=10)
    parser.add_argument("--uptime", type=float, default=6.0,
                        help="seconds the app stays up before quitting")
    parser.add_argument("--timeout", type=float, default=60.0,
                        help="seconds before a cycle is declared hung")
    parser.add_argument("--json", type=str, default="")
    args = parser.parse_args()

    print(f"Running {args.cycles} launch/quit cycles "
          f"({args.uptime}s uptime, {args.timeout}s hang timeout)\n")

    results = []
    for index in range(1, args.cycles + 1):
        result = run_cycle(index, args.uptime, args.timeout)
        results.append(result)
        status = "OK  " if result["ok"] else "FAIL"
        print(f"  cycle {index:3d}  {status}  {result['duration']:5.1f}s"
              f"{'  ' + result['reason'] if result['reason'] else ''}")
        if not result["ok"] and result["output"]:
            for line in result["output"].splitlines()[-15:]:
                print(f"        | {line}")

    failures = [r for r in results if not r["ok"]]
    durations = [r["duration"] for r in results]
    print(f"\n{len(results) - len(failures)}/{len(results)} cycles clean; "
          f"launch+quit {min(durations):.1f}-{max(durations):.1f}s "
          f"(mean {sum(durations)/len(durations):.1f}s)")

    if args.json:
        Path(args.json).write_text(json.dumps(results, indent=2), encoding="utf-8")

    if failures:
        print(f"\nFAILED: {len(failures)} cycle(s) did not complete cleanly.")
        return 1
    print("\nPASS: every cycle launched and exited cleanly.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

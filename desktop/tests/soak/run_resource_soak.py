"""
Resource soak for the real Monitra window, in-process, against the stub backend.

    python tests/soak/run_resource_soak.py --duration 600 --accelerate
    python tests/soak/run_resource_soak.py --duration 600 --gc-off     # expose cycles

What it does that `run_soak.py` does not: it builds the real `MainWindow` on the
real Qt platform (fonts, backing stores, HiDPI -- everything an offscreen run
hides), signs in through a seeded session, and then *uses* the application the
way the audit asks for -- switching projects and days, flipping Activity tabs,
refreshing, starting and stopping the timer, taking a break, opening and closing
dialogs, minimising and restoring, and dropping the network -- while sampling
what the operating system says the process costs (working set, private bytes,
threads, handles, CPU) next to what Qt and Python say they are holding
(widgets, timers, live objects).

`--gc-off` disables the cyclic garbage collector for the whole run. That is not
how the application runs; it is how to find out what the application is
*relying on* the collector for. A widget kept alive by a reference cycle looks
like a leak with the collector off and like a sawtooth with it on, and the
sawtooth is what Task Manager showed: hundreds of MB that vanish only when a
collection happens to run. A run that is flat with the collector off has no
such dependency.

`--accelerate` takes one screenshot every ~20 s instead of one per ten minutes,
and refreshes the Activity panel every few seconds, so an hour of the
application's cadence is compressed into minutes. It also takes REAL screenshots
of this machine's screen; they go to the stub, in memory, and nowhere else.

Everything talks to 127.0.0.1. The stub, the data directory and the sign-in are
created here and removed at the end; the real ~/.monitra is never touched.
"""
from __future__ import annotations

import argparse
import gc
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
DESKTOP_ROOT = HERE.parent.parent
sys.path.insert(0, str(DESKTOP_ROOT))
sys.path.insert(0, str(DESKTOP_ROOT / "tools"))


def _parse() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--duration", type=float, default=300.0, help="seconds of operation")
    ap.add_argument("--sample", type=float, default=10.0, help="seconds between samples")
    ap.add_argument("--op-interval", type=float, default=2.0, help="seconds between UI operations")
    ap.add_argument("--port", type=int, default=8791)
    ap.add_argument("--screenshots", type=int, default=40, help="screenshots already stored today")
    ap.add_argument("--accelerate", action="store_true")
    ap.add_argument("--gc-off", action="store_true", help="disable the cyclic collector")
    ap.add_argument("--no-ui-ops", action="store_true", help="idle: sample only")
    ap.add_argument("--csv", type=Path)
    ap.add_argument("--platform", default=None, help="QT_QPA_PLATFORM (default: the real one)")
    ap.add_argument("--outage-every", type=float, default=600.0,
                    help="seconds between injected network outages (0 = none)")
    ap.add_argument("--outage-seconds", type=float, default=90.0)
    ap.add_argument("--settle", type=float, default=60.0,
                    help="idle seconds after the workload before the final sample")
    return ap.parse_args()


ARGS = _parse()

# Environment first: every module below reads it at import time.
_work = Path(tempfile.mkdtemp(prefix="monitra-resource-soak-"))
_data_dir = _work / "data"
os.environ.update({
    "MONITRA_DATA_DIR": str(_data_dir),
    "SMS_API_BASE_URL": f"http://127.0.0.1:{ARGS.port}",
    "MONITRA_AUTH_PROVIDER_LOGIN_URL": f"http://127.0.0.1:{ARGS.port}/__portal/login",
    "MONITRA_ENV": "development",
    "MONITRA_RESOURCE_LOG": "1",
    "MONITRA_RESOURCE_INTERVAL_S": str(max(5, int(ARGS.sample))),
    "MONITRA_LOG_LEVEL": os.environ.get("MONITRA_LOG_LEVEL", "WARNING"),
})
if ARGS.platform:
    os.environ["QT_QPA_PLATFORM"] = ARGS.platform
if ARGS.accelerate:
    os.environ["MONITRA_SCREENSHOT_WINDOW_MINUTES"] = "1"
    os.environ["MONITRA_SCREENSHOTS_PER_WINDOW"] = "3"


def _start_stub() -> subprocess.Popen:
    stub = subprocess.Popen(
        [sys.executable, str(HERE / "stub_backend_server.py"), "--port", str(ARGS.port),
         "--projects", "40", "--tasks-per-project", "30",
         "--screenshots", str(ARGS.screenshots), "--seed-usage", "--churn-timeline"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for _ in range(90):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{ARGS.port}/__admin/stats", timeout=1)
            break
        except Exception:  # noqa: BLE001
            time.sleep(1)
    else:
        stub.kill()
        raise SystemExit("the stub backend did not start")
    subprocess.check_call(
        [sys.executable, str(HERE / "seed_session.py"), "--data-dir", str(_data_dir),
         "--api-base", f"http://127.0.0.1:{ARGS.port}"],
        stdout=subprocess.DEVNULL,
    )
    return stub


def _admin(path: str) -> None:
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{ARGS.port}/__admin/{path}", method="POST")
        urllib.request.urlopen(req, timeout=3).read()
    except Exception:  # noqa: BLE001
        pass


def _stats() -> dict:
    try:
        return json.load(urllib.request.urlopen(f"http://127.0.0.1:{ARGS.port}/__admin/stats", timeout=3))
    except Exception:  # noqa: BLE001
        return {}


def main() -> int:
    stub = _start_stub()
    try:
        return _run()
    finally:
        stub.kill()
        shutil.rmtree(_work, ignore_errors=True)


def _run() -> int:
    import psutil
    from PySide6.QtCore import QObject, QTimer, Qt
    from PySide6.QtWidgets import QApplication

    import main as app_main                    # the real entry module
    import resource_probe as probe
    from core.runtime import ApplicationRuntime

    app = QApplication(sys.argv)
    app.setStyleSheet(app_main.APP_QSS)
    app.setQuitOnLastWindowClosed(False)
    runtime = ApplicationRuntime()
    runtime.inspect_previous_run()
    runtime.restore_session()
    window = app_main.MainWindow(runtime)
    window.show()
    runtime.mark_ui_ready()
    QTimer.singleShot(0, runtime.start_services)
    QTimer.singleShot(0, window.begin_startup)

    proc = psutil.Process()
    proc.cpu_percent(None)
    started = time.monotonic()
    prev_cpu = {}
    prev_t = [started]
    samples = []
    counters = {"ops": 0, "start_stop": 0, "breaks": 0, "dialogs": 0, "notifications": 0,
                "offline": 0, "refreshes": 0, "project_switches": 0, "date_switches": 0,
                "tab_switches": 0, "activity_refreshes": 0, "cards_created": 0, "cards_retired": 0}
    # Count card births and retirements on the real classes. Harness-only:
    # wrappers around the product's own methods, nothing in the product changes.
    from ui import activity_section as _as
    _orig_card_init = _as.ScreenshotCard.__init__
    _orig_retire = _as.ScreenshotsTabView._retire
    def _counting_init(self, *a, **kw):
        counters["cards_created"] += 1
        _orig_card_init(self, *a, **kw)
    def _counting_retire(self, card):
        counters["cards_retired"] += 1
        _orig_retire(self, card)
    _as.ScreenshotCard.__init__ = _counting_init
    _as.ScreenshotsTabView._retire = _counting_retire
    state = {"step": 0, "online": True, "running": False, "settled": False}

    def dashboard():
        return window._dashboard

    def widgets() -> int:
        return len(app.allWidgets())

    def live(type_name: str) -> int:
        return sum(1 for o in gc.get_objects() if type(o).__name__ == type_name)

    def sample() -> None:
        now = time.monotonic()
        s = probe.sample_tree(proc, prev_cpu, prev_t[0], now)
        prev_t[0] = now
        row = {
            "t": now - started, "rss": s.rss_mb, "private": s.private_mb, "threads": s.threads,
            "handles": s.handles, "cpu": s.cpu_pct, "widgets": widgets(),
            "timers": len(window.findChildren(QTimer)) + len(dashboard().findChildren(QTimer)),
            "thumbs": live("ScreenshotThumbnail"), "gc_objects": len(gc.get_objects()),
            "queue": runtime.cache.get_pending_count(),
            "shots_pending": sum(runtime.cache.count_screenshots_by_status().values()),
            "ops": counters["ops"],
        }
        samples.append(row)
        print(
            f"{row['t']:6.0f}s rss={row['rss']:7.1f} priv={row['private']:7.1f} "
            f"thr={row['threads']:3d} hdl={row['handles']:5d} cpu={row['cpu']:5.1f} "
            f"widgets={row['widgets']:5d} thumbs={row['thumbs']:3d} q={row['queue']} "
            f"shots={row['shots_pending']} ops={row['ops']}", flush=True,
        )

    def operate() -> None:
        """One user-like action; cycles through the repertoire."""
        if ARGS.no_ui_ops:
            return
        d = dashboard()
        if window._stack.currentWidget() is not d:
            return
        step = state["step"] = state["step"] + 1
        counters["ops"] += 1
        sidebar = d._sidebar
        activity = d._activity_section
        kind = step % 14
        try:
            if kind == 0:
                d.refresh_data()
                counters["refreshes"] += 1
            elif kind == 1 and sidebar._projects:
                d._on_project_selected(sidebar._projects[step % len(sidebar._projects)])
                counters["project_switches"] += 1
            elif kind == 2:
                from core.time_format import ist_today
                d._on_date_changed(ist_today() - timedelta(days=1 if (step // 14) % 2 else 0))
                counters["date_switches"] += 1
            elif kind == 3:
                activity.switch_tab(("apps", "urls", "screenshots")[(step // 14) % 3])
                counters["tab_switches"] += 1
            elif kind == 4:
                activity.refresh()
                counters["activity_refreshes"] += 1
            elif kind == 5:
                _toggle_timer(d)
            elif kind == 6:
                window.showMinimized()
            elif kind == 7:
                window.showNormal()
                window.raise_()
            elif kind == 8:
                _open_close_dialog(d)
            elif kind == 9:
                activity.refresh()
                counters["activity_refreshes"] += 1
            elif kind == 10:
                _break(d)
            elif kind == 11:
                runtime.notifications.notify(f"soak notification {step}", key=f"soak:{step % 3}")
                counters["notifications"] += 1
            elif kind == 12:
                activity.switch_tab("screenshots")
                activity.refresh()
                counters["tab_switches"] += 1
                counters["activity_refreshes"] += 1
        except Exception as exc:  # noqa: BLE001 - a failing op is reported, not fatal
            print(f"  op {kind} raised {type(exc).__name__}: {exc}", flush=True)

    def _toggle_timer(d) -> None:
        t = runtime.timer
        if t.is_running():
            t.stop_tracking()
            state["running"] = False
        else:
            project = (d._current_project or {})
            tasks = (runtime.cache.get_cached_tasks(project["id"]) or []) if project.get("id") else []
            if tasks:
                t.start_tracking(project["id"], tasks[0]["id"], tasks[0].get("task_name"))
                state["running"] = True
        counters["start_stop"] += 1

    def _break(d) -> None:
        t = runtime.timer
        if not t.is_running():
            return
        if getattr(t, "break_status", "NONE") != "NONE":
            t.break_out()
        else:
            t.break_in()
        counters["breaks"] += 1

    def _open_close_dialog(d) -> None:
        from ui.task_table import AddTaskDialog
        dlg = AddTaskDialog((d._current_project or {}).get("project_name", "Project"), d)
        dlg.show()
        QTimer.singleShot(150, dlg.close)
        QTimer.singleShot(250, dlg.deleteLater)
        counters["dialogs"] += 1

    def _flap_network() -> None:
        _admin("offline?on=1")
        counters["offline"] += 1
        print(f"  -- network outage injected for {ARGS.outage_seconds:.0f}s", flush=True)
        QTimer.singleShot(int(ARGS.outage_seconds * 1000), lambda: (_admin("offline?on=0"),
                          print("  -- network restored", flush=True)))

    outage_timer = QTimer()
    outage_timer.timeout.connect(_flap_network)

    sample_timer = QTimer()
    sample_timer.timeout.connect(sample)
    sample_timer.start(int(ARGS.sample * 1000))
    op_timer = QTimer()
    op_timer.timeout.connect(operate)

    def begin_ops() -> None:
        # Let the first refresh settle, then take the baseline the trend is read from.
        sample()
        if ARGS.gc_off:
            gc.collect()
            gc.disable()
        op_timer.start(int(ARGS.op_interval * 1000))
        if ARGS.outage_every > 0:
            outage_timer.start(int(ARGS.outage_every * 1000))

    def end_ops() -> None:
        op_timer.stop()
        outage_timer.stop()
        if runtime.timer.is_running():
            runtime.timer.stop_tracking()
        print(f"  -- workload finished; idle settle {ARGS.settle:.0f}s", flush=True)

    QTimer.singleShot(25_000, begin_ops)
    QTimer.singleShot(int((25 + ARGS.duration) * 1000), end_ops)
    # exit(), not quit(): quit() asks the window to close, and its close prompt
    # would wait for a person.
    QTimer.singleShot(int((25 + ARGS.duration + ARGS.settle) * 1000), lambda: app.exit(0))
    app.exec()

    sample()
    settled = samples[-1]
    freed = gc.collect()
    app.processEvents()
    after_gc = probe.sample_tree(proc, {}, time.monotonic() - 1, time.monotonic())
    print()
    print(f"operations: {counters}")
    print(f"after idle settle: private {settled['private']:.1f} MB rss {settled['rss']:.1f} MB "
          f"cards alive {settled['thumbs']}")
    print(f"cyclic garbage the collector freed at the end: {freed} objects "
          f"(private {settled['private']:.1f} MB -> {after_gc.private_mb:.1f} MB)")
    _report(samples)
    stats = _stats()
    if stats:
        print("stub saw:", json.dumps({k: stats[k] for k in
              ("uploaded_screenshot_count", "total_connections", "open_connections", "status_counts")
              if k in stats}))
    if ARGS.csv and samples:
        import csv
        with open(ARGS.csv, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(samples[0]))
            w.writeheader()
            w.writerows(samples)
    threads_before = threading_count()
    runtime.shutdown()
    print(f"shutdown clean; python threads {threads_before} -> {threading_count()}")
    return 0


def threading_count() -> int:
    import threading
    return threading.active_count()


def _report(samples) -> None:
    import resource_probe as probe
    if len(samples) < 4:
        return
    half = samples[len(samples) // 2:]
    first, last = samples[0], samples[-1]
    def slope(key):
        return probe.slope_per_hour([(s["t"], float(s[key])) for s in half])
    print(f"{'':<10}{'start':>10}{'end':>10}{'peak':>10}{'trend/h (2nd half)':>22}")
    for key, label in (("rss", "rss MB"), ("private", "private MB"), ("threads", "threads"),
                       ("handles", "handles"), ("widgets", "widgets"), ("timers", "QTimers"),
                       ("thumbs", "thumbnails"), ("gc_objects", "py objects")):
        print(f"{label:<10}{first[key]:>10.0f}{last[key]:>10.0f}{max(s[key] for s in samples):>10.0f}"
              f"{slope(key):>22.1f}")
    cpu = [s["cpu"] for s in samples[1:]] or [0.0]
    print(f"cpu: mean {sum(cpu) / len(cpu):.2f}%  max {max(cpu):.1f}%")


if __name__ == "__main__":
    raise SystemExit(main())

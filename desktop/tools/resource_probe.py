"""
tools/resource_probe.py -- measure the REAL resource usage of a Monitra process.

Python-level heap checks (``tracemalloc``) see only Python objects. A PySide6
application's memory is mostly native: Qt widgets, QPixmap/QImage buffers, the
image libraries, the SQLite page cache, the C runtime heap. What Task Manager
(and Activity Monitor) shows is the process's working set, so that is what this
measures, from outside the process, the way the OS reports it.

Per sample, summed over the process and its children:

    rss        working set (Windows) / resident set (macOS)
    private    private bytes -- committed memory that belongs to this process
               alone (Windows: pagefile-backed "commit"; macOS: the footprint
               psutil reports as ``uss`` when ``private`` does not exist)
    uss        unique set size -- what would be freed if the process exited
    threads    OS threads
    handles    Windows handles / POSIX file descriptors
    cpu        percent of one core, averaged over the sampling interval
    procs      process count (the group Task Manager shows is a tree)

Two ways to use it::

    # launch something, sample it, stop it
    python tools/resource_probe.py --duration 120 --interval 2 -- dist/Monitra/Monitra.exe

    # attach to a running process (the soak harness does this in-process)
    python tools/resource_probe.py --pid 1234 --duration 600 --interval 30

The CSV it writes is the raw evidence; the summary it prints is a *trend*
(least-squares slope over the second half of the run), because one number says
nothing about whether memory is stable or climbing.

This is a development tool. It is never imported by the application.
"""
from __future__ import annotations

import argparse
import csv
import os
import subprocess
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import List, Optional

try:
    import psutil
except ImportError:  # pragma: no cover - the message is the behaviour
    sys.exit("psutil is required: pip install psutil")

MB = 1024 * 1024


@dataclass
class Sample:
    t: float
    rss_mb: float
    private_mb: float
    uss_mb: float
    threads: int
    handles: int
    cpu_pct: float
    procs: int


def _tree(root: "psutil.Process") -> List["psutil.Process"]:
    try:
        return [root] + root.children(recursive=True)
    except psutil.Error:
        return [root]


def sample_tree(root: "psutil.Process", prev_cpu: dict, prev_t: float, now: float) -> Sample:
    rss = private = uss = 0
    threads = handles = 0
    cpu_delta = 0.0
    procs = 0
    for proc in _tree(root):
        try:
            with proc.oneshot():
                info = proc.memory_info()
                rss += info.rss
                # Windows: `private` is the commit charge. Elsewhere it is
                # absent and USS is the closest honest figure.
                private += getattr(info, "private", None) or 0
                try:
                    full = proc.memory_full_info()
                    uss += getattr(full, "uss", 0)
                    if not getattr(info, "private", None):
                        private += getattr(full, "uss", 0)
                except (psutil.AccessDenied, NotImplementedError):
                    pass
                threads += proc.num_threads()
                try:
                    handles += proc.num_handles() if hasattr(proc, "num_handles") else proc.num_fds()
                except (psutil.AccessDenied, NotImplementedError):
                    pass
                cpu = proc.cpu_times()
                total = cpu.user + cpu.system
                cpu_delta += total - prev_cpu.get(proc.pid, total)
                prev_cpu[proc.pid] = total
                procs += 1
        except psutil.Error:
            continue
    elapsed = max(now - prev_t, 1e-6)
    return Sample(
        t=now,
        rss_mb=rss / MB,
        private_mb=private / MB,
        uss_mb=uss / MB,
        threads=threads,
        handles=handles,
        cpu_pct=100.0 * cpu_delta / elapsed,
        procs=procs,
    )


def slope_per_hour(points: List[tuple]) -> float:
    """Least-squares slope of (seconds, value) points, expressed per hour."""
    n = len(points)
    if n < 3:
        return 0.0
    mean_x = sum(p[0] for p in points) / n
    mean_y = sum(p[1] for p in points) / n
    denom = sum((p[0] - mean_x) ** 2 for p in points)
    if denom == 0:
        return 0.0
    return 3600.0 * sum((p[0] - mean_x) * (p[1] - mean_y) for p in points) / denom


def summarise(samples: List[Sample]) -> str:
    if not samples:
        return "no samples"
    t0 = samples[0].t
    lines = [
        f"{'t(s)':>7} {'rss MB':>9} {'private MB':>11} {'uss MB':>9} "
        f"{'thr':>4} {'hdl':>5} {'cpu%':>6} {'procs':>5}"
    ]
    step = max(1, len(samples) // 12)
    for s in samples[::step] + ([samples[-1]] if (len(samples) - 1) % step else []):
        lines.append(
            f"{s.t - t0:7.0f} {s.rss_mb:9.1f} {s.private_mb:11.1f} {s.uss_mb:9.1f} "
            f"{s.threads:4d} {s.handles:5d} {s.cpu_pct:6.1f} {s.procs:5d}"
        )
    half = samples[len(samples) // 2:]
    cpu = [s.cpu_pct for s in samples[1:]] or [0.0]
    lines.append("")
    lines.append(
        f"peak rss {max(s.rss_mb for s in samples):.1f} MB   "
        f"peak private {max(s.private_mb for s in samples):.1f} MB   "
        f"mean cpu {sum(cpu) / len(cpu):.2f}%   max cpu {max(cpu):.1f}%"
    )
    lines.append(
        "second-half trend: "
        f"rss {slope_per_hour([(s.t, s.rss_mb) for s in half]):+.1f} MB/h   "
        f"private {slope_per_hour([(s.t, s.private_mb) for s in half]):+.1f} MB/h   "
        f"threads {slope_per_hour([(s.t, float(s.threads)) for s in half]):+.2f}/h   "
        f"handles {slope_per_hour([(s.t, float(s.handles)) for s in half]):+.1f}/h"
    )
    return "\n".join(lines)


def run(root: "psutil.Process", duration: float, interval: float,
        out: Optional[Path], quiet: bool = False) -> List[Sample]:
    samples: List[Sample] = []
    prev_cpu: dict = {}
    prev_t = time.monotonic()
    sample_tree(root, prev_cpu, prev_t, prev_t)          # prime the CPU deltas
    deadline = prev_t + duration
    writer = None
    handle = None
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        handle = open(out, "w", newline="", encoding="utf-8")
        writer = csv.DictWriter(handle, fieldnames=list(Sample.__annotations__))
        writer.writeheader()
    try:
        while time.monotonic() < deadline:
            time.sleep(interval)
            if not root.is_running():
                print("process exited", file=sys.stderr)
                break
            now = time.monotonic()
            s = sample_tree(root, prev_cpu, prev_t, now)
            prev_t = now
            samples.append(s)
            if writer:
                writer.writerow(asdict(s))
                handle.flush()
            if not quiet:
                print(
                    f"\r{now - deadline + duration:6.0f}s rss={s.rss_mb:7.1f} "
                    f"priv={s.private_mb:7.1f} thr={s.threads:3d} cpu={s.cpu_pct:5.1f}",
                    end="", file=sys.stderr, flush=True,
                )
    finally:
        if handle:
            handle.close()
        if not quiet:
            print(file=sys.stderr)
    return samples


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--pid", type=int, help="attach to a running process")
    p.add_argument("--duration", type=float, default=60.0)
    p.add_argument("--interval", type=float, default=2.0)
    p.add_argument("--out", type=Path, help="CSV path")
    p.add_argument("--data-dir", type=Path,
                   help="MONITRA_DATA_DIR for a launched process (default: a fresh temp dir)")
    p.add_argument("cmd", nargs=argparse.REMAINDER, help="-- command to launch")
    args = p.parse_args(argv)

    child: Optional[subprocess.Popen] = None
    if args.pid:
        root = psutil.Process(args.pid)
    else:
        cmd = [c for c in args.cmd if c != "--"]
        if not cmd:
            p.error("give --pid or a command after --")
        if Path(cmd[0]).exists():            # CreateProcess misreads "a/b.exe"
            cmd[0] = str(Path(cmd[0]).resolve())
        env = dict(os.environ)
        data = args.data_dir or Path(os.environ.get("TEMP", ".")) / f"monitra-probe-{os.getpid()}"
        env["MONITRA_DATA_DIR"] = str(data)
        child = subprocess.Popen(cmd, env=env)
        root = psutil.Process(child.pid)

    try:
        samples = run(root, args.duration, args.interval, args.out)
    finally:
        if child is not None:
            for proc in reversed(_tree(root)):
                try:
                    proc.terminate()
                except psutil.Error:
                    pass
    print(summarise(samples))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""
screenshot.mac_diagnostics — evidence for macOS capture problems. Logging only.

Why this exists
---------------
Some Macs upload screenshots that show only the wallpaper (or only Monitra)
although the permission check passed. Nothing in the pipeline records enough to
say *why*: the permission gate logs only when it refuses, and the capture logs
only its size. This module adds the missing facts, as one-line ``MACDIAG_*``
entries in ``monitra.log``, so a single reproduction on the affected Mac can
tell the possible causes apart:

* **The permission is not in effect** -- the OS preflight says no, or other
  applications' windows exist but none exposes a title.
* **The capture API returns a restricted image** -- the preflight says yes and
  titles are visible, yet a capture holds no other application's pixels. The
  probes below isolate this: an image of *only* the application windows
  (desktop elements excluded) is transparent where nothing of theirs was
  rendered, and a capture of the frontmost application's own window is blank if
  its content is withheld.
* **Monitra itself fills the screen** -- the share of the display covered by
  Monitra's own windows is logged.
* **The wrong display or scale** -- every display's geometry and the pixels
  actually returned for it.

What it is not
--------------
It changes nothing. It never alters what is captured, queued, uploaded or
shown; every entry point swallows its own errors; it does nothing off macOS and
can be switched off with ``MONITRA_MAC_DIAGNOSTICS=0``. The two extra reads of
the screen it makes (the probes) happen only inside a capture that has already
been authorised by a running timer, are held in memory just long enough to
count pixels, and are discarded -- nothing is stored or sent.

What it will not log: window titles, URLs, file names, or any pixel content.
Only counts, sizes, application *names*, process ids, and aggregate colour
statistics.
"""
from __future__ import annotations

import os
import platform
import sys
from typing import Any, Dict, List, Optional, Sequence

from core.logging_setup import get_logger

log = get_logger("screenshot.mac_diag")

_environment_logged = False

#: Samples read per image for the colour statistics. Bounded so a 6K display
#: costs the same as a laptop panel.
_SAMPLE_COLUMNS = 64
_SAMPLE_ROWS = 48
#: Rows, and pixels per row, read as contiguous runs to measure edge density.
_EDGE_ROWS = 48
_EDGE_RUN = 128
_EDGE_STEP = 24


def enabled() -> bool:
    """macOS only, and switchable off. A function so a test can stand in for it."""
    if sys.platform != "darwin":
        return False
    return os.getenv("MONITRA_MAC_DIAGNOSTICS", "1") != "0"


# ── Pure image statistics ─────────────────────────────────────────────────────

def image_stats(
    pixels: bytes, width: int, height: int, row_bytes: Optional[int] = None
) -> Dict[str, Any]:
    """
    Aggregate colour statistics for a 4-bytes-per-pixel buffer (BGRA or RGBA --
    the channel order does not matter to any figure here, except which byte is
    alpha: the fourth).

    Cheap by construction: a fixed grid of samples plus a fixed number of short
    horizontal runs, whatever the image size. No pixel value is returned, only
    aggregates.

    :return: ``distinct_colors`` (of the sampled pixels, 4 bits per channel),
        ``dominant_fraction`` (share of the sampled pixels in the most common
        colour), ``alpha_min/max``, ``transparent_fraction`` (alpha 0),
        ``luma_mean/std``, ``edge_density`` (share of adjacent pixel pairs that
        differ sharply -- text and window chrome score high, a wallpaper low),
        and ``flat`` (one colour dominates). On a buffer that is the wrong size
        the result says so instead of guessing.
    """
    try:
        width, height = int(width), int(height)
        row = int(row_bytes) if row_bytes else width * 4
        if width <= 0 or height <= 0 or row < width * 4:
            return {"error": "bad_geometry"}
        if len(pixels) < row * (height - 1) + width * 4:
            return {"error": "short_buffer", "have": len(pixels), "need": row * height}

        bins: Dict[int, int] = {}
        total = 0
        alpha_min, alpha_max, transparent = 255, 0, 0
        luma_sum = luma_sq = 0.0

        for r in range(_SAMPLE_ROWS):
            y = (r * (height - 1)) // max(1, _SAMPLE_ROWS - 1)
            base = y * row
            for c in range(_SAMPLE_COLUMNS):
                x = (c * (width - 1)) // max(1, _SAMPLE_COLUMNS - 1)
                i = base + x * 4
                b, g, rr, a = pixels[i], pixels[i + 1], pixels[i + 2], pixels[i + 3]
                key = ((rr >> 4) << 8) | ((g >> 4) << 4) | (b >> 4)
                bins[key] = bins.get(key, 0) + 1
                total += 1
                if a < alpha_min:
                    alpha_min = a
                if a > alpha_max:
                    alpha_max = a
                if a == 0:
                    transparent += 1
                luma = 0.299 * rr + 0.587 * g + 0.114 * b
                luma_sum += luma
                luma_sq += luma * luma

        mean = luma_sum / total
        variance = max(0.0, luma_sq / total - mean * mean)

        edges = pairs = 0
        run = min(_EDGE_RUN, width)
        x0 = max(0, (width - run) // 2)
        for r in range(_EDGE_ROWS):
            y = (r * (height - 1)) // max(1, _EDGE_ROWS - 1)
            i = y * row + x0 * 4
            prev = pixels[i + 2] + pixels[i + 1] + pixels[i]
            for k in range(1, run):
                j = i + k * 4
                cur = pixels[j + 2] + pixels[j + 1] + pixels[j]
                if abs(cur - prev) > _EDGE_STEP:
                    edges += 1
                pairs += 1
                prev = cur

        dominant = max(bins.values()) / total
        return {
            "distinct_colors": len(bins),
            "dominant_fraction": round(dominant, 3),
            "alpha_min": alpha_min,
            "alpha_max": alpha_max,
            "transparent_fraction": round(transparent / total, 3),
            "luma_mean": round(mean, 1),
            "luma_std": round(variance ** 0.5, 1),
            "edge_density": round(edges / pairs, 3) if pairs else 0.0,
            "flat": dominant >= 0.9,
        }
    except Exception as exc:  # noqa: BLE001 - diagnostics must never raise
        return {"error": f"stats_failed:{type(exc).__name__}"}


def _fmt(values: Dict[str, Any]) -> str:
    return " ".join(f"{key}={value}" for key, value in values.items())


# ── Environment ───────────────────────────────────────────────────────────────

def log_environment_once() -> None:
    """The facts that decide which macOS behaviours apply. Logged once per run."""
    global _environment_logged
    if _environment_logged or not enabled():
        return
    _environment_logged = True
    try:
        facts: Dict[str, Any] = {
            "macos": platform.mac_ver()[0] or "?",
            "machine": platform.machine(),
            "python": platform.python_version(),
            "frozen": bool(getattr(sys, "frozen", False)),
            "executable": os.path.basename(sys.executable),
            "pid": os.getpid(),
        }
        executable = sys.executable or ""
        # A path under AppTranslocation means the app is running from a
        # randomised read-only copy, where permission grants do not persist.
        facts["translocated"] = "/AppTranslocation/" in executable
        facts["in_applications"] = executable.startswith("/Applications/")
        try:
            import objc  # type: ignore

            facts["pyobjc"] = getattr(objc, "__version__", "?")
        except Exception as exc:  # noqa: BLE001
            facts["pyobjc"] = f"unavailable:{type(exc).__name__}"
        try:
            import mss  # type: ignore

            facts["mss"] = getattr(mss, "__version__", "?")
        except Exception as exc:  # noqa: BLE001
            facts["mss"] = f"unavailable:{type(exc).__name__}"
        try:
            from PySide6 import __version__ as pyside_version
            from PySide6.QtGui import QGuiApplication

            facts["pyside6"] = pyside_version
            facts["qt_platform"] = QGuiApplication.platformName()
        except Exception as exc:  # noqa: BLE001
            facts["pyside6"] = f"unavailable:{type(exc).__name__}"
        try:
            import Quartz  # type: ignore

            facts["has_CGWindowListCreateImage"] = hasattr(Quartz, "CGWindowListCreateImage")
        except Exception as exc:  # noqa: BLE001
            facts["has_CGWindowListCreateImage"] = f"unavailable:{type(exc).__name__}"
        for framework in ("ScreenCaptureKit", "UserNotifications"):
            try:
                __import__(framework)
                facts[framework] = "importable"
            except Exception:  # noqa: BLE001
                facts[framework] = "not_bundled"
        log.info("MACDIAG_ENV %s", _fmt(facts))
    except Exception:  # noqa: BLE001
        log.exception("MACDIAG_ENV failed")


# ── Permission, applications and windows ──────────────────────────────────────

def _frontmost() -> Dict[str, Any]:
    try:
        from AppKit import NSWorkspace  # type: ignore

        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        if app is None:
            return {"frontmost": "none"}
        pid = int(app.processIdentifier())
        return {
            "frontmost_app": str(app.localizedName() or "?"),
            "frontmost_bundle": str(app.bundleIdentifier() or "?"),
            "frontmost_pid": pid,
            "frontmost_is_monitra": pid == os.getpid(),
        }
    except Exception as exc:  # noqa: BLE001
        return {"frontmost": f"unavailable:{type(exc).__name__}"}


def _window_inventory() -> Dict[str, Any]:
    """Counts of the on-screen windows, by who owns them. Never a title."""
    try:
        from Quartz import (  # type: ignore
            CGWindowListCopyWindowInfo,
            kCGNullWindowID,
            kCGWindowListExcludeDesktopElements,
            kCGWindowListOptionOnScreenOnly,
        )

        info = CGWindowListCopyWindowInfo(
            kCGWindowListOptionOnScreenOnly | kCGWindowListExcludeDesktopElements,
            kCGNullWindowID,
        )
        if info is None:
            return {"windows": "none_returned"}
        own = os.getpid()
        total = other = other_named = own_count = 0
        owners: List[str] = []
        for entry in info:
            if int(entry.get("kCGWindowLayer", 0)) != 0:
                continue
            total += 1
            if int(entry.get("kCGWindowOwnerPID", -1)) == own:
                own_count += 1
                continue
            other += 1
            if entry.get("kCGWindowName"):
                other_named += 1
            name = str(entry.get("kCGWindowOwnerName") or "?")
            if name not in owners and len(owners) < 8:
                owners.append(name)
        return {
            "layer0_windows": total,
            "other_app_windows": other,
            "other_app_windows_with_title": other_named,
            "own_windows": own_count,
            "other_owners": ",".join(owners) or "-",
        }
    except Exception as exc:  # noqa: BLE001
        return {"windows": f"unavailable:{type(exc).__name__}"}


def log_access(status) -> None:
    """What the permission gate saw and decided, every time it is asked."""
    if not enabled():
        return
    try:
        log_environment_once()
        from background_services.screenshot import screen_access

        facts: Dict[str, Any] = {
            "decision": getattr(status.state, "value", status.state),
            "allowed": status.allowed,
            "detail": status.detail,
        }
        try:
            facts["preflight_now"] = screen_access._preflight()
        except Exception as exc:  # noqa: BLE001
            facts["preflight_now"] = f"error:{type(exc).__name__}"
        try:
            gate = screen_access._other_app_windows()
            facts["gate_other_windows"] = "unreadable" if gate is None else len(gate)
            facts["gate_titled"] = (
                "unreadable" if gate is None else sum(1 for w in gate if w["name"])
            )
        except Exception as exc:  # noqa: BLE001
            facts["gate_other_windows"] = f"error:{type(exc).__name__}"
        facts.update(_frontmost())
        facts.update(_window_inventory())
        log.info("MACDIAG_ACCESS %s", _fmt(facts))
    except Exception:  # noqa: BLE001
        log.exception("MACDIAG_ACCESS failed")


# ── The capture itself ────────────────────────────────────────────────────────

def log_capture(merged) -> None:
    """What the capture returned, per display, and what the probes see."""
    if not enabled():
        return
    try:
        for placement in merged.placements:
            display = placement.display
            facts: Dict[str, Any] = {
                "display": display.number,
                "primary": display.is_primary,
                "region": f"{display.width}x{display.height}@{display.left:+d},{display.top:+d}",
                "grabbed": f"{placement.width}x{placement.height}",
                "bytes": len(placement.pixels),
                "expected_bytes": placement.width * placement.height * 4,
            }
            facts.update(image_stats(placement.pixels, placement.width, placement.height))
            log.info("MACDIAG_CAPTURE %s", _fmt(facts))
        log.info(
            "MACDIAG_CAPTURE_SUMMARY displays=%d expected=%d complete=%s",
            merged.display_count, merged.displays_expected, merged.complete,
        )
        _log_own_coverage()
        _run_probes()
    except Exception:  # noqa: BLE001
        log.exception("MACDIAG_CAPTURE failed")


def _log_own_coverage() -> None:
    """How much of the main display Monitra's own windows cover."""
    try:
        from Quartz import (  # type: ignore
            CGDisplayBounds,
            CGMainDisplayID,
            CGWindowListCopyWindowInfo,
            kCGNullWindowID,
            kCGWindowListExcludeDesktopElements,
            kCGWindowListOptionOnScreenOnly,
        )

        bounds = CGDisplayBounds(CGMainDisplayID())
        area = max(1.0, float(bounds.size.width) * float(bounds.size.height))
        own = os.getpid()
        covered = 0.0
        for entry in CGWindowListCopyWindowInfo(
            kCGWindowListOptionOnScreenOnly | kCGWindowListExcludeDesktopElements,
            kCGNullWindowID,
        ) or []:
            if int(entry.get("kCGWindowLayer", 0)) != 0:
                continue
            if int(entry.get("kCGWindowOwnerPID", -1)) != own:
                continue
            b = entry.get("kCGWindowBounds") or {}
            covered += float(b.get("Width", 0)) * float(b.get("Height", 0))
        log.info("MACDIAG_OWN_COVERAGE own_window_area_fraction=%.3f", min(1.0, covered / area))
    except Exception as exc:  # noqa: BLE001
        log.info("MACDIAG_OWN_COVERAGE unavailable:%s", type(exc).__name__)


def _probe_image(Quartz, name: str, rect, option: int, window_id: int, extra: Dict[str, Any]) -> None:
    flags = (
        Quartz.kCGWindowImageBoundsIgnoreFraming | Quartz.kCGWindowImageNominalResolution
    )
    image = Quartz.CGWindowListCreateImage(rect, option, window_id, flags)
    facts: Dict[str, Any] = {"probe": name}
    facts.update(extra)
    if image is None:
        facts["result"] = "NULL"
        log.info("MACDIAG_PROBE %s", _fmt(facts))
        return
    width = int(Quartz.CGImageGetWidth(image))
    height = int(Quartz.CGImageGetHeight(image))
    facts["result"] = f"{width}x{height}"
    facts["alpha_info"] = int(Quartz.CGImageGetAlphaInfo(image))
    if width and height:
        provider = Quartz.CGImageGetDataProvider(image)
        data = bytes(Quartz.CGDataProviderCopyData(provider))
        facts.update(
            image_stats(data, width, height, int(Quartz.CGImageGetBytesPerRow(image)))
        )
    log.info("MACDIAG_PROBE %s", _fmt(facts))


def _run_probes() -> None:
    """
    Three reads of the screen through the same API `mss` uses, each isolating
    one explanation. Held in memory only to count pixels, then discarded.

    ``full_display``
        Exactly what `mss` asks for. Should match the MACDIAG_CAPTURE figures;
        if it does not, `mss` is not what is being measured.
    ``applications_only``
        Desktop elements (the wallpaper) excluded. Where no application window
        was rendered the image is transparent, so ``transparent_fraction`` is
        the share of the screen *no* application contributed to. Near 1.0 with
        a browser visibly open means the API is withholding application pixels.
    ``frontmost_window_only``
        Just the topmost other application's window, by its window id. Blank
        or transparent where the window plainly has content means that
        window's pixels are withheld from this process.
    """
    try:
        import Quartz  # type: ignore
    except Exception as exc:  # noqa: BLE001
        log.info("MACDIAG_PROBE unavailable:%s", type(exc).__name__)
        return

    try:
        display = Quartz.CGDisplayBounds(Quartz.CGMainDisplayID())
        _probe_image(
            Quartz, "full_display", display,
            Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID, {},
        )
        _probe_image(
            Quartz, "applications_only", display,
            Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
            Quartz.kCGNullWindowID, {},
        )
    except Exception as exc:  # noqa: BLE001
        log.info("MACDIAG_PROBE display_probes_failed:%s", type(exc).__name__)

    try:
        own = os.getpid()
        listing = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
            Quartz.kCGNullWindowID,
        ) or []
        target = None
        for entry in listing:  # front to back
            if int(entry.get("kCGWindowLayer", 0)) != 0:
                continue
            if int(entry.get("kCGWindowOwnerPID", -1)) == own:
                continue
            b = entry.get("kCGWindowBounds") or {}
            if float(b.get("Width", 0)) < 200 or float(b.get("Height", 0)) < 200:
                continue
            target = entry
            break
        if target is None:
            log.info("MACDIAG_PROBE probe=frontmost_window_only result=no_other_app_window")
            return
        _probe_image(
            Quartz, "frontmost_window_only", Quartz.CGRectNull,
            Quartz.kCGWindowListOptionIncludingWindow, int(target.get("kCGWindowNumber", 0)),
            {
                "owner": str(target.get("kCGWindowOwnerName") or "?"),
                "has_title": bool(target.get("kCGWindowName")),
                "window_size": "{}x{}".format(
                    int((target.get("kCGWindowBounds") or {}).get("Width", 0)),
                    int((target.get("kCGWindowBounds") or {}).get("Height", 0)),
                ),
            },
        )
    except Exception as exc:  # noqa: BLE001
        log.info("MACDIAG_PROBE window_probe_failed:%s", type(exc).__name__)


def reset_for_tests() -> None:
    global _environment_logged
    _environment_logged = False

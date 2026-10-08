"""
Stub backend for resource-usage rigs (memory / CPU measurement of the desktop).

THIS IS A TEST RIG. It lives under tests/soak/ and must never be imported by
product code. It exists so the real desktop application -- from source or the
packaged Monitra.exe -- can run fully signed in, with a realistic amount of
data, without touching any real backend, database or network host.

Everything is stdlib plus Pillow (already a dependency of the desktop). One
process, one in-memory state guarded by a lock, no files written.

    python stub_backend_server.py [--port 8765] [--projects 40]
                                  [--tasks-per-project 30] [--screenshots 40]
                                  [--user-email you@example.test]

Contracts were read from the desktop callers (app/**, background_services/**,
ui/**) and from backend/app/schemas. Every response is shaped like the
backend's own response model; endpoints the desktop calls but the rig only
acknowledges are listed in README_RIG.md.

Fault injection (all on the same port, never itself subject to faults):

    POST /__admin/offline?on=1|0[&mode=503|drop]   503 (default) or hang up
    POST /__admin/fail_rate?p=0.3                  random 500/502/503
    POST /__admin/latency?ms=500                   delay every other request
    GET  /__admin/stats                            counters, see below
"""
from __future__ import annotations

import argparse
import email.policy
import email.parser
import hashlib
import io
import json
import random
import re
import socket
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlsplit

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc

ORG_ID = 1
USER_ID = 101
SCREENSHOT_WINDOW_MINUTES = 10
SEED_ENTRY_ID_BASE = 5000
UPLOADED_SCREENSHOT_ID_BASE = 100000
MAX_STORED_UPLOAD_BYTES = 256 * 1024 * 1024  # cap on RAM held for uploads


# ─── Time helpers ─────────────────────────────────────────────────────────────

def now_utc() -> datetime:
    return datetime.now(UTC)


def iso(dt: Optional[datetime]) -> Optional[str]:
    """ISO-8601 the way pydantic v2 / FastAPI writes a UTC datetime."""
    if dt is None:
        return None
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def ist_day_bounds_utc(day) -> Tuple[datetime, datetime]:
    start = datetime(day.year, day.month, day.day, tzinfo=IST).astimezone(UTC)
    return start, start + timedelta(days=1)


# ─── Synthetic screenshots ────────────────────────────────────────────────────

def _make_desktop_image(seed: int, noise_sigma: float) -> bytes:
    """A 1000x1000 desktop-like WebP: window chrome, text lines, a taskbar and
    gradient fills, with film grain so the encoded size is realistic."""
    from PIL import Image, ImageChops, ImageDraw

    rng = random.Random(seed)
    size = 1000
    palette = [
        (32, 36, 48), (245, 246, 250), (233, 238, 247), (28, 100, 242),
        (255, 255, 255), (60, 64, 76), (16, 185, 129), (250, 245, 232),
    ]
    img = Image.new("RGB", (size, size), palette[seed % 3 + 1])
    d = ImageDraw.Draw(img)
    # Wallpaper-ish vertical gradient.
    top, bottom = (rng.randrange(20, 120), rng.randrange(40, 140), rng.randrange(100, 220)), (
        rng.randrange(120, 220), rng.randrange(60, 160), rng.randrange(80, 200))
    for y in range(size):
        t = y / size
        d.line([(0, y), (size, y)], fill=tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)))
    # Two or three application windows with a title bar and text lines.
    for w in range(rng.randrange(2, 4)):
        x0, y0 = rng.randrange(10, 260), rng.randrange(10, 220)
        x1, y1 = x0 + rng.randrange(480, 720), y0 + rng.randrange(420, 640)
        x1, y1 = min(x1, size - 12), min(y1, size - 70)
        d.rectangle([x0, y0, x1, y1], fill=palette[(seed + w) % 2 + 4], outline=(70, 70, 80))
        d.rectangle([x0, y0, x1, y0 + 30], fill=palette[(seed + w) % 3])
        for k, c in enumerate(((255, 95, 86), (255, 189, 46), (39, 201, 63))):
            d.ellipse([x0 + 10 + k * 20, y0 + 9, x0 + 22 + k * 20, y0 + 21], fill=c)
        d.rectangle([x0, y0 + 31, x0 + 150, y1], fill=(238, 240, 246))
        for row in range(rng.randrange(14, 30)):
            y = y0 + 48 + row * 14
            if y > y1 - 10:
                break
            length = rng.randrange(60, max(80, x1 - x0 - 190))
            d.rectangle([x0 + 170, y, x0 + 170 + length, y + 6], fill=(60 + rng.randrange(80),) * 3)
        for row in range(rng.randrange(6, 12)):
            d.rectangle([x0 + 12, y0 + 46 + row * 26, x0 + 130, y0 + 58 + row * 26], fill=(205, 210, 222))
    # Taskbar.
    d.rectangle([0, size - 52, size, size], fill=(24, 26, 34))
    for k in range(9):
        d.rounded_rectangle([14 + k * 46, size - 44, 14 + k * 46 + 34, size - 10], 6, fill=palette[(k + seed) % len(palette)])
    # Film grain so WebP cannot collapse it to a few KB.
    noise = Image.effect_noise((size, size), noise_sigma).convert("RGB")
    img = ImageChops.add(img, noise, scale=1.0, offset=-128)  # effect_noise is centred on 128
    out = io.BytesIO()
    img.save(out, format="WEBP", quality=72, method=4)
    return out.getvalue()


def build_image_pool(count: int = 6, low: int = 100_000, high: int = 250_000) -> List[bytes]:
    """Generate `count` distinct images whose encoded size is within [low, high]."""
    pool: List[bytes] = []
    for i in range(count):
        sigma = 18.0
        data = b""
        for _ in range(12):
            data = _make_desktop_image(1000 + i, sigma)
            if len(data) < low:
                sigma *= 1.35
            elif len(data) > high:
                sigma *= 0.8
            else:
                break
        pool.append(data)
    return pool


# ─── Seed data ────────────────────────────────────────────────────────────────

CLIENTS = ["Acme", "Globex", "Initech", "Umbrella", "Hooli", "Stark", "Wayne", "Soylent",
           "Tyrell", "Cyberdyne", "Wonka", "Vandelay", "Pied Piper", "Dunder", "Oscorp"]
DELIVERABLES = ["Website Revamp", "Mobile App", "Data Migration", "Analytics Dashboard",
                "CRM Integration", "Brand Refresh", "API Platform", "Onboarding Flow",
                "Support Portal", "Reporting Suite", "SEO Programme", "Billing Rework"]
TASK_VERBS = ["Design", "Implement", "Review", "Test", "Document", "Refactor", "Estimate",
              "Deploy", "Investigate", "Plan", "Prototype", "Audit"]
TASK_NOUNS = ["login screen", "search results", "invoice export", "email templates",
              "permissions model", "caching layer", "landing page", "webhook handler",
              "settings page", "notification rules", "import wizard", "audit log",
              "payment flow", "user profile", "report filters", "data sync"]

PROJECT_STATUSES = [
    {"id": 1, "name": "Active", "color": "#10b981"},
    {"id": 2, "name": "Paused", "color": "#f59e0b"},
    {"id": 3, "name": "Completed", "color": "#6366f1"},
]
TASK_STATUSES = [
    {"id": 1, "name": "To Do", "color": "#94a3b8"},
    {"id": 2, "name": "In Progress", "color": "#3b82f6"},
    {"id": 3, "name": "Completed", "color": "#10b981"},
]


def build_user(email_addr: str) -> Dict[str, Any]:
    """The stub's `GET /auth/me` body (backend UserRead). `seed_session.py`
    stores exactly this, so the restored profile and the live one agree."""
    return {
        "id": USER_ID,
        "organization_id": ORG_ID,
        "username": email_addr.split("@")[0],
        "email": email_addr,
        "name": "Rig Tester",
        "designation": "Software Engineer",
        "role_name": "employee",
        "permissions": {},
        "wp_capabilities": None,
        "idle_enabled": True,
        "idle_minutes": 5,
        "capture_frequency": CAPTURE_FREQUENCY_MINUTES,
        "can_add_tasks": True,
        "can_add_nonbillable_tasks": True,
        "can_login": True,
        "status": "active",
        "is_active": True,
        "hubstaff_user_id": None,
        "created_at": iso(now_utc() - timedelta(days=200)),
        "updated_at": iso(now_utc() - timedelta(days=3)),
    }


class State:
    """All mutable server state. Every access goes through `lock`."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.lock = threading.RLock()
        self.args = args
        self.rng = random.Random(20261008)
        self.started_at = now_utc()

        # Fault injection.
        self.offline = False
        self.offline_mode = "503"
        self.fail_rate = 0.0
        self.latency_ms = 0

        # Counters.
        self.path_counts: Dict[str, int] = {}
        self.status_counts: Dict[str, int] = {}
        self.error_counts: Dict[str, int] = {}
        self.open_connections = 0
        self.total_connections = 0

        # Identity.
        email_addr = args.user_email
        self.user = build_user(email_addr)
        self.person = {"id": USER_ID, "name": self.user["name"], "email": email_addr, "role": "employee"}
        self.leader = {"id": 7, "name": "Team Leader", "email": "leader@example.test", "role": "leader"}
        self.token_counter = 0
        self.revision = 1

        self._build_catalogue()
        self.images = build_image_pool()
        self.entries: Dict[int, Dict[str, Any]] = {}
        self.next_entry_id = SEED_ENTRY_ID_BASE
        self.screenshots: Dict[int, Dict[str, Any]] = {}
        self.screenshot_bytes: Dict[int, bytes] = {}
        self.client_screenshot_ids: Dict[str, int] = {}
        self.next_screenshot_id = UPLOADED_SCREENSHOT_ID_BASE
        self.uploaded_screenshot_count = 0
        self.uploaded_bytes = 0
        self.duplicate_screenshot_uploads = 0
        self.app_usage: Dict[str, int] = {}
        self.url_usage: Dict[Tuple[str, str, str], int] = {}
        self.timeline_requests = 0
        self.churn_timeline = bool(getattr(args, "churn_timeline", False))
        if getattr(args, "seed_usage", False):
            # A day that looks like a working one, so the Apps and URLs tabs
            # have rows to render (and icons/favicons to resolve).
            for i, name in enumerate(SEED_APPS):
                self.app_usage[name] = 5400 - i * 220
            for i, (domain, title) in enumerate(SEED_SITES):
                self.url_usage[(domain, f"https://{domain}/", title)] = 3000 - i * 60
        self.activity_samples: Dict[str, Dict[str, Any]] = {}
        self.upload_counts = {"app_usage": 0, "url_usage": 0, "activity": 0, "idle": 0,
                              "unwanted": 0, "adjustments": 0, "capture_events": 0}
        self.next_idle_id = 1
        self.idle_periods: Dict[int, Dict[str, Any]] = {}
        self._seed_today()

    # -- catalogue ------------------------------------------------------------

    def _build_catalogue(self) -> None:
        a = self.args
        self.projects: List[Dict[str, Any]] = []
        self.tasks_by_project: Dict[int, List[Dict[str, Any]]] = {}
        created = now_utc() - timedelta(days=120)
        task_id = 1
        for pid in range(1, a.projects + 1):
            client = CLIENTS[(pid - 1) % len(CLIENTS)]
            deliverable = DELIVERABLES[((pid - 1) // len(CLIENTS) + pid) % len(DELIVERABLES)]
            status = PROJECT_STATUSES[0] if pid % 11 else PROJECT_STATUSES[1 + (pid // 11) % 2]
            tasks: List[Dict[str, Any]] = []
            for t in range(a.tasks_per_project):
                tstatus = TASK_STATUSES[(0, 0, 1, 1, 2)[self.rng.randrange(5)]]
                name = f"{TASK_VERBS[self.rng.randrange(len(TASK_VERBS))]} {TASK_NOUNS[self.rng.randrange(len(TASK_NOUNS))]}"
                tasks.append({
                    "id": task_id,
                    "project_id": pid,
                    "name": f"{name} #{t + 1}",
                    "description": None if t % 3 else f"Work item {t + 1} for {client} {deliverable}.",
                    "assignee_id": USER_ID,
                    "assignee": self.person,
                    "assignees": [self.person],
                    "status": tstatus,
                    "estimated_hours": float(self.rng.choice((1, 2, 4, 8, 12))),
                    "created_at": iso(created + timedelta(hours=task_id)),
                    "updated_at": iso(created + timedelta(hours=task_id, minutes=30)),
                })
                task_id += 1
            self.tasks_by_project[pid] = tasks
            self.projects.append({
                "id": pid,
                "project_name": f"{client} {deliverable} {pid:02d}",
                "description": f"{deliverable} engagement for {client}.",
                "status": status,
                "owner": None,
                "leader": self.leader,
                "employees": [self.person],
                "deadline": None,
                "billing_type": "fixed" if pid % 3 else "free",
                "category": None,
                "wfpm_project_id": None,
                "fixed_hours": "120.00" if pid % 3 else None,
                "organization_id": ORG_ID,
                "created_at": iso(created),
                "updated_at": iso(created + timedelta(days=2)),
                "employee_count": 1,
                "task_count": len(tasks),
            })
        self.task_index = {t["id"]: t for ts in self.tasks_by_project.values() for t in ts}
        self.project_index = {p["id"]: p for p in self.projects}

    # -- seeded day -----------------------------------------------------------

    def _seed_today(self) -> None:
        """Stopped time entries and screenshots dated today (IST)."""
        k = self.args.screenshots
        if k <= 0:
            return
        now = now_utc()
        today_start, _ = ist_day_bounds_utc(now.astimezone(IST).date())
        spacing = min(timedelta(minutes=SCREENSHOT_WINDOW_MINUTES),
                      max(timedelta(seconds=20), (now - today_start - timedelta(minutes=10)) / k))
        # Newest seeded capture sits a few minutes in the past.
        newest = now - timedelta(minutes=4)
        times = [newest - spacing * i for i in range(k)]
        times = [t for t in reversed(times) if t > today_start + timedelta(minutes=1)]
        if not times:
            return
        blocks = 4
        per_block = -(-len(times) // blocks)
        projects = [p for p in self.projects if p["status"]["name"] == "Active"] or self.projects
        for b in range(blocks):
            chunk = times[b * per_block:(b + 1) * per_block]
            if not chunk:
                continue
            project = projects[(b * 3) % len(projects)]
            task = self.tasks_by_project[project["id"]][b % len(self.tasks_by_project[project["id"]])]
            start = max(today_start + timedelta(seconds=30), chunk[0] - timedelta(minutes=2))
            end = min(chunk[-1] + timedelta(minutes=4), now - timedelta(seconds=45))
            entry = self._new_entry(project["id"], task["id"], start, f"seed-{b}")
            self._finalize(entry, end)
            for i, ts in enumerate(chunk):
                sid = len(self.screenshots) + 1
                image = self.images[sid % len(self.images)]
                self.screenshots[sid] = self._screenshot_record(
                    sid, entry, ts, image, f"seed-shot-{sid}", 1)
                self.screenshot_bytes[sid] = image

    def _screenshot_record(self, sid: int, entry: Dict[str, Any], captured: datetime,
                           image: bytes, client_id: Optional[str], display_count: int) -> Dict[str, Any]:
        return {
            "id": sid,
            "organization_id": ORG_ID,
            "time_entry_id": entry["id"],
            "captured_at": captured,
            "monitor_number": 1,
            "display_count": display_count,
            "file_path": f"stub-drive/User_{USER_ID}/{captured.astimezone(IST).date()}/{sid}.webp",
            "created_at": captured,
            "google_drive_file_id": f"stub-drive-file-{sid}",
            "file_name": f"{sid}.webp",
            "file_size_bytes": len(image),
            "mime_type": "image/webp",
            "width": 1000,
            "height": 1000,
            "upload_status": "uploaded",
            "uploaded_at": captured,
            "client_screenshot_id": client_id,
        }

    # -- entries --------------------------------------------------------------

    def _new_entry(self, project_id: int, task_id: int, start: datetime, client_op: Optional[str]) -> Dict[str, Any]:
        self.next_entry_id += 1
        project = self.project_index[project_id]
        entry = {
            "id": self.next_entry_id,
            "organization_id": ORG_ID,
            "user_id": USER_ID,
            "project_id": project_id,
            "task_id": task_id,
            "start_time": start,
            "end_time": None,
            "total_seconds": 0,
            "status": "running",
            "is_manual": False,
            "is_billable": project["billing_type"] == "fixed",
            "description": None,
            "client_op": client_op,
            "created_at": now_utc(),
            "updated_at": now_utc(),
            "adjustment_seconds": 0,
        }
        self.entries[entry["id"]] = entry
        self.revision += 1
        return entry

    def _finalize(self, entry: Dict[str, Any], end: datetime) -> None:
        end = max(end, entry["start_time"])
        entry["end_time"] = end
        entry["total_seconds"] = round((end - entry["start_time"]).total_seconds())
        entry["status"] = "stopped"
        entry["updated_at"] = now_utc()
        self.revision += 1

    def active_entry(self) -> Optional[Dict[str, Any]]:
        for e in self.entries.values():
            if e["end_time"] is None:
                return e
        return None

    def entry_read(self, e: Dict[str, Any]) -> Dict[str, Any]:
        now = now_utc()
        running = e["end_time"] is None
        elapsed = max(0, round((now - e["start_time"]).total_seconds())) if running else e["total_seconds"]
        net = max(0, elapsed + int(e["adjustment_seconds"]))
        return {
            **{k: e[k] for k in ("id", "organization_id", "user_id", "project_id", "task_id",
                                 "total_seconds", "status", "is_manual", "is_billable",
                                 "description", "client_op", "adjustment_seconds")},
            "start_time": iso(e["start_time"]),
            "end_time": iso(e["end_time"]),
            "created_at": iso(e["created_at"]),
            "updated_at": iso(e["updated_at"]),
            "net_seconds": net,
            "is_running": running,
            "server_time": iso(now),
            "elapsed_seconds": elapsed,
            "elapsed_time": "%02d:%02d:%02d" % (elapsed // 3600, elapsed % 3600 // 60, elapsed % 60),
        }

    # -- screenshots ----------------------------------------------------------

    def screenshot_view(self, s: Dict[str, Any]) -> Dict[str, Any]:
        entry = self.entries.get(s["time_entry_id"])
        task = self.task_index.get(entry["task_id"]) if entry else None
        project = self.project_index.get(entry["project_id"]) if entry else None
        return {
            "id": s["id"],
            "captured_at": iso(s["captured_at"]),
            "monitor_number": s["monitor_number"],
            "display_count": s["display_count"],
            "width": s["width"],
            "height": s["height"],
            "file_size_bytes": s["file_size_bytes"],
            "view_url": f"/time-entry-screenshots/{s['id']}/view",
            "task_id": task["id"] if task else None,
            "task_name": task["name"] if task else None,
            "project_id": project["id"] if project else None,
            "project_name": project["project_name"] if project else None,
        }

    def screenshot_read(self, s: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "id": s["id"], "organization_id": s["organization_id"], "time_entry_id": s["time_entry_id"],
            "captured_at": iso(s["captured_at"]), "monitor_number": s["monitor_number"],
            "display_count": s["display_count"], "file_path": s["file_path"],
            "created_at": iso(s["created_at"]), "google_drive_file_id": s["google_drive_file_id"],
            "file_name": s["file_name"], "file_size_bytes": s["file_size_bytes"],
            "mime_type": s["mime_type"], "width": s["width"], "height": s["height"],
            "upload_status": s["upload_status"], "uploaded_at": iso(s["uploaded_at"]),
            "client_screenshot_id": s["client_screenshot_id"],
        }

    def timeline(self, day) -> Dict[str, Any]:
        start, end = ist_day_bounds_utc(day)
        windows: Dict[int, List[Dict[str, Any]]] = {}
        width = SCREENSHOT_WINDOW_MINUTES * 60
        for s in sorted(self.screenshots.values(), key=lambda x: x["captured_at"]):
            if start <= s["captured_at"] < end:
                windows.setdefault(int(s["captured_at"].timestamp()) // width, []).append(s)
        out = []
        # `--churn-timeline`: every answer differs from the last, so a client
        # refresh rebuilds every card -- the exact pattern that retained
        # pictures before the fix. A real backend changes a window's activity
        # as more of it is measured; this just does it on every request.
        self.timeline_requests += 1
        churn = self.timeline_requests if self.churn_timeline else 0
        for idx in sorted(windows):
            shots = windows[idx]
            w_start = datetime.fromtimestamp(idx * width, UTC)
            digest = int(hashlib.md5(str(idx).encode()).hexdigest()[:4], 16)
            measured = width - (digest % 90)
            out.append({
                "window_start": iso(w_start),
                "window_end": iso(w_start + timedelta(seconds=width)),
                "activity_percentage": (35 + digest % 60 + churn) % 100,
                "activity_measured_seconds": measured,
                "tracked_seconds": width,
                "screenshots": [self.screenshot_view(s) for s in shots],
                "screenshot_count": len(shots),
                "capture_state": "captured",
                "capture_reason": None,
                "capture_attempts": 0,
            })
        return {"success": True, "window_minutes": SCREENSHOT_WINDOW_MINUTES, "windows": out}


# ─── HTTP layer ───────────────────────────────────────────────────────────────

class HttpError(Exception):
    def __init__(self, status: int, detail: Any) -> None:
        super().__init__(str(detail))
        self.status = status
        self.detail = detail


def normalise_path(path: str) -> str:
    """Collapse ids so per-path counters stay few: /time-entries/9/stop -> /time-entries/{id}/stop."""
    return re.sub(r"/\d+(?=/|$)", "/{id}", path)


def parse_multipart(content_type: str, body: bytes) -> Tuple[Dict[str, str], Dict[str, Tuple[str, bytes]]]:
    raw = b"Content-Type: " + content_type.encode("latin-1") + b"\r\nMIME-Version: 1.0\r\n\r\n" + body
    msg = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(raw)
    fields: Dict[str, str] = {}
    files: Dict[str, Tuple[str, bytes]] = {}
    if not msg.is_multipart():
        return fields, files
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        payload = part.get_payload(decode=True) or b""
        filename = part.get_filename()
        if filename is not None:
            files[name] = (filename, payload)
        else:
            fields[name] = payload.decode("utf-8", "replace")
    return fields, files


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "MonitraStub/1.0"
    state: State  # set on the class by main()

    # -- plumbing -------------------------------------------------------------

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: D401 - silence default stderr noise
        if self.state.args.verbose:
            sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    def setup(self) -> None:
        super().setup()
        with self.state.lock:
            self.state.open_connections += 1
            self.state.total_connections += 1

    def finish(self) -> None:
        try:
            super().finish()
        finally:
            with self.state.lock:
                self.state.open_connections -= 1

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        data = self.rfile.read(length) if length > 0 else b""
        return data

    def _send(self, status: int, payload: Any = None, *, raw: Optional[bytes] = None,
              content_type: str = "application/json") -> None:
        body = raw if raw is not None else json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if raw is not None:
            self.send_header("Cache-Control", "private, max-age=3600")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        self._status = status

    def _hang_up(self) -> None:
        self._status = 0
        self.close_connection = True
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    # -- dispatch -------------------------------------------------------------

    def do_GET(self) -> None: self._dispatch()
    def do_POST(self) -> None: self._dispatch()
    def do_PATCH(self) -> None: self._dispatch()
    def do_PUT(self) -> None: self._dispatch()
    def do_DELETE(self) -> None: self._dispatch()

    def _dispatch(self) -> None:
        st = self.state
        split = urlsplit(self.path)
        path = split.path.rstrip("/") or "/"
        query = {k: v[-1] for k, v in parse_qs(split.query).items()}
        self._status = 0
        body = self._read_body()  # always drain, or keep-alive desynchronises

        if path.startswith("/__admin/"):
            self._admin(path, query)
            return

        key = f"{self.command} {normalise_path(path)}"
        with st.lock:
            st.path_counts[key] = st.path_counts.get(key, 0) + 1
            offline, mode = st.offline, st.offline_mode
            fail_rate, latency = st.fail_rate, st.latency_ms

        try:
            if offline:
                if mode == "drop":
                    self._hang_up()
                    return
                raise HttpError(503, "Stub backend is offline (fault injection).")
            if latency:
                time.sleep(latency / 1000.0)
            if fail_rate and random.random() < fail_rate:
                raise HttpError(random.choice((500, 502, 503)), "Injected failure (fault injection).")
            self._route(path, query, body)
        except HttpError as exc:
            detail = exc.detail if isinstance(exc.detail, (dict, list)) else str(exc.detail)
            self._send(exc.status, {"detail": detail})
        except Exception as exc:  # noqa: BLE001 - a rig bug must be visible, not a hang
            sys.stderr.write("stub error on %s: %r\n" % (key, exc))
            self._send(500, {"detail": f"stub error: {exc!r}"})
        finally:
            with st.lock:
                label = str(self._status)
                st.status_counts[label] = st.status_counts.get(label, 0) + 1
                if self._status >= 400 or self._status == 0:
                    ekey = f"{key} -> {self._status}"
                    st.error_counts[ekey] = st.error_counts.get(ekey, 0) + 1

    # -- admin ----------------------------------------------------------------

    def _admin(self, path: str, query: Dict[str, str]) -> None:
        st = self.state
        with st.lock:
            if path == "/__admin/offline" and self.command == "POST":
                st.offline = query.get("on", "1") not in ("0", "false", "off")
                st.offline_mode = query.get("mode", st.offline_mode)
            elif path == "/__admin/fail_rate" and self.command == "POST":
                st.fail_rate = max(0.0, min(1.0, float(query.get("p", "0"))))
            elif path == "/__admin/latency" and self.command == "POST":
                st.latency_ms = max(0, int(float(query.get("ms", "0"))))
            elif path == "/__admin/stats" and self.command == "GET":
                pass
            else:
                self._send(404, {"detail": "unknown admin endpoint"})
                return
            active = [st.entry_read(e) for e in st.entries.values() if e["end_time"] is None]
            stats = {
                "uptime_seconds": round((now_utc() - st.started_at).total_seconds(), 1),
                "offline": st.offline, "offline_mode": st.offline_mode,
                "fail_rate": st.fail_rate, "latency_ms": st.latency_ms,
                "open_connections": st.open_connections,
                "total_connections": st.total_connections,
                "path_counts": dict(sorted(st.path_counts.items())),
                "status_counts": dict(sorted(st.status_counts.items())),
                "error_counts": dict(sorted(st.error_counts.items())),
                "projects": len(st.projects),
                "tasks": len(st.task_index),
                "screenshots_total": len(st.screenshots),
                "uploaded_screenshot_count": st.uploaded_screenshot_count,
                "timeline_requests": st.timeline_requests,
                "duplicate_screenshot_uploads": st.duplicate_screenshot_uploads,
                "uploaded_screenshot_bytes": st.uploaded_bytes,
                "active_entries": len(active),
                "active_entry_ids": [e["id"] for e in active],
                "entries_total": len(st.entries),
                "uploads": dict(st.upload_counts),
            }
        self._send(200, stats)

    # -- routing --------------------------------------------------------------

    def _auth(self) -> None:
        header = self.headers.get("Authorization") or ""
        if not header.startswith("Bearer ") or len(header) < 12:
            raise HttpError(401, "Not authenticated")

    def _route(self, path: str, query: Dict[str, str], body: bytes) -> None:
        st = self.state
        m = self.command
        # The backend answers every router at the bare path and under /api/v1.
        p = path[len("/api/v1"):] if path.startswith("/api/v1/") else path
        ctype = self.headers.get("Content-Type", "")

        def json_body() -> Dict[str, Any]:
            if not body:
                return {}
            try:
                data = json.loads(body)
            except ValueError:
                raise HttpError(422, "Body is not valid JSON")
            return data if isinstance(data, dict) else {}

        # --- no-auth endpoints (the sign-in hops and token refresh) ---
        if m == "POST" and p == "/__portal/login":
            self._send(200, {"status": "success", "access_token": "stub-portal-jwt-" + uuid.uuid4().hex})
            return
        if m == "POST" and p in ("/auth/sso/token", "/auth/refresh"):
            self._send(200, self._token_pair())
            return
        if m == "POST" and p == "/auth/logout":
            self._send(200, {"detail": "ok"})
            return
        if m == "GET" and p == "/health":
            self._send(200, {"status": "ok", "stub": True})
            return

        self._auth()

        if m == "GET" and p == "/auth/me":
            self._send(200, st.user)
        elif m == "POST" and p == "/auth/sso/handoff":
            self._send(200, {"token": "stub-handoff-" + uuid.uuid4().hex, "expires_at": iso(now_utc() + timedelta(minutes=2))})

        elif m == "GET" and p == "/projects":
            self._projects(query)
        elif m == "GET" and p == "/task-statuses":
            self._send(200, TASK_STATUSES)
        elif m == "GET" and p == "/sync/revision":
            with st.lock:
                rev = hashlib.sha1(f"{len(st.projects)}:{len(st.task_index)}:{st.revision}".encode()).hexdigest()[:16]
            self._send(200, {"revision": rev, "components": {"entries": str(st.revision)}, "server_time": iso(now_utc())})
        elif m == "GET" and (mm := re.fullmatch(r"/projects/(\d+)/tasks", p)):
            tasks = st.tasks_by_project.get(int(mm.group(1)))
            if tasks is None:
                raise HttpError(404, "Project not found")
            self._send(200, tasks)
        elif m in ("POST", "PATCH", "DELETE") and re.fullmatch(r"/projects/\d+/tasks(/\d+)?", p):
            raise HttpError(403, "Task mutation is not emulated by this rig")

        elif m == "GET" and p == "/time-entries":
            self._list_entries(query)
        elif m == "GET" and p == "/time-entries/active":
            with st.lock:
                entry = st.active_entry()
                payload = {"entry": st.entry_read(entry) if entry else None, "server_time": iso(now_utc())}
            self._send(200, payload)
        elif m == "POST" and p == "/time-entries/start":
            self._start(json_body())
        elif m == "POST" and (mm := re.fullmatch(r"/time-entries/(\d+)/stop", p)):
            self._stop(int(mm.group(1)), json_body())
        elif m == "GET" and (mm := re.fullmatch(r"/time-entries/(\d+)", p)):
            with st.lock:
                entry = st.entries.get(int(mm.group(1)))
                if not entry:
                    raise HttpError(404, "Time entry not found")
                payload = st.entry_read(entry)
            self._send(200, payload)

        elif m == "POST" and (mm := re.fullmatch(r"/time-entries/(\d+)/screenshots", p)):
            self._upload_screenshot(int(mm.group(1)), ctype, body)
        elif m == "GET" and p == "/time-entry-screenshots/timeline":
            self._screenshot_timeline(query)
        elif m == "GET" and (mm := re.fullmatch(r"/time-entry-screenshots/(\d+)/view", p)):
            with st.lock:
                image = st.screenshot_bytes.get(int(mm.group(1)))
            if image is None:
                raise HttpError(404, "Screenshot not found")
            self._send(200, raw=image, content_type="image/webp")
        elif m == "POST" and p == "/time-entry-screenshots/capture-events":
            events = json_body().get("events") or []
            with st.lock:
                st.upload_counts["capture_events"] += len(events)
            self._send(200, {"success": True, "accepted": len(events), "duplicates": 0, "rejected": []})
        elif m == "GET" and p == "/screenshots/config":
            self._send(200, {"capture_frequency": CAPTURE_FREQUENCY_MINUTES})
        elif m == "GET" and p == "/screenshot/privacy-config":
            self._send(200, {"applications": [], "urls": [], "excluded_applications": [], "excluded_urls": []})

        elif m == "POST" and (mm := re.fullmatch(r"/time-entries/(\d+)/app-usage/batch", p)):
            self._app_usage_batch(json_body())
        elif m == "POST" and (mm := re.fullmatch(r"/time-entries/(\d+)/app-usage", p)):
            self._app_usage_batch({"records": [json_body()]})
        elif m == "POST" and p == "/url-usage/batch":
            self._url_usage_batch(json_body())
        elif m == "POST" and (mm := re.fullmatch(r"/time-entries/(\d+)/activity/batch", p)):
            self._activity_batch(int(mm.group(1)), json_body())
        elif m == "POST" and p in ("/time-entry-activities/batch",):
            self._activity_batch(None, json_body())
        elif m == "GET" and p == "/app-usage/summary":
            with st.lock:
                apps = [{"application_name": n, "duration_seconds": s} for n, s in st.app_usage.items()]
            self._send(200, {"applications": apps})
        elif m == "GET" and p == "/url-usage/summary":
            with st.lock:
                pages = [{"domain": d, "url": u, "page_title": t, "duration_seconds": s}
                         for (d, u, t), s in st.url_usage.items()]
            self._send(200, {"data": {"pages": pages}})
        elif m == "GET" and p == "/time-entry-activities/today":
            self._activity_today()
        elif m == "POST" and re.fullmatch(r"/time-entries/\d+/(unwanted-activity|adjustments)", p):
            with st.lock:
                st.upload_counts["unwanted" if p.endswith("unwanted-activity") else "adjustments"] += 1
            self._send(201, {"success": True, "id": uuid.uuid4().int % 10_000_000})

        elif m == "GET" and p == "/idle-periods/config":
            self._send(200, {"idle_enabled": True, "idle_minutes": 5})
        elif m == "GET" and p == "/idle-periods/active":
            self._send(200, None)
        elif m == "POST" and p == "/idle-periods":
            self._idle_report(json_body())

        elif m == "GET" and p == "/desktop/latest-version":
            ua = self.headers.get("User-Agent", "")
            ver = re.search(r"Monitra/([0-9][^\s;]*)", ua)
            self._send(200, {"latest_version": None, "download_url": None, "release_notes_url": None,
                             "update_available": False, "client_version": ver.group(1) if ver else None})
        elif m == "GET" and p == "/system/maintenance-status":
            self._send(200, {"maintenance_mode": False, "updated_at": None, "server_time": iso(now_utc())})
        elif m == "GET" and p == "/desktop-notifications/schedule":
            # Version 0, nothing configured: the backend's "never touched" answer.
            self._send(200, {"version": 0, "updated_at": None, "server_time": iso(now_utc()),
                             "builtin": [], "custom": []})
        elif m == "POST" and p == "/activity-logs/client-events":
            self._send(200, {"recorded": True, "id": uuid.uuid4().int % 10_000_000})
        else:
            raise HttpError(404, f"The stub does not emulate {m} {path}")

    # -- handlers -------------------------------------------------------------

    def _token_pair(self) -> Dict[str, Any]:
        st = self.state
        with st.lock:
            st.token_counter += 1
            n = st.token_counter
        created = now_utc()
        return {
            "access_token": f"stub-access-token-{n}-{uuid.uuid4().hex}",
            "refresh_token": f"stub-refresh-token-{n}-{uuid.uuid4().hex}",
            "token_type": "bearer",
            "user": st.user,
            "session_created_at": iso(created),
            "session_expires_at": iso(created + timedelta(days=90)),
        }

    def _projects(self, query: Dict[str, str]) -> None:
        st = self.state
        page = max(1, int(query.get("page", 1)))
        limit = max(1, min(100, int(query.get("limit", 20))))
        include_tasks = query.get("include_tasks", "true").lower() not in ("false", "0")
        items = st.projects
        total = len(items)
        chunk = items[(page - 1) * limit: page * limit]
        out = []
        for proj in chunk:
            row = dict(proj)
            row["tasks"] = st.tasks_by_project[proj["id"]] if include_tasks else None
            out.append(row)
        self._send(200, {"items": out, "pagination": {
            "page": page, "limit": limit, "total": total, "total_pages": max(1, -(-total // limit))}})

    def _list_entries(self, query: Dict[str, str]) -> None:
        st = self.state
        start, end = parse_dt(query.get("start_date")), parse_dt(query.get("end_date"))
        limit = max(1, min(10000, int(query.get("limit", 100))))
        with st.lock:
            rows = [e for e in st.entries.values()
                    if (not query.get("user_id") or e["user_id"] == int(query["user_id"]))
                    and (not query.get("project_id") or e["project_id"] == int(query["project_id"]))
                    and (not query.get("task_id") or e["task_id"] == int(query["task_id"]))
                    and (not query.get("status") or e["status"] == query["status"])
                    and (start is None or e["start_time"] >= start)
                    and (end is None or e["start_time"] < end)]
            rows.sort(key=lambda e: e["start_time"], reverse=True)
            payload = [st.entry_read(e) for e in rows[:limit]]
        self._send(200, payload)

    def _start(self, data: Dict[str, Any]) -> None:
        st = self.state
        try:
            project_id, task_id = int(data["project_id"]), int(data["task_id"])
        except (KeyError, TypeError, ValueError):
            raise HttpError(422, "project_id and task_id are required")
        client_op = data.get("client_op")
        with st.lock:
            if client_op:
                for e in st.entries.values():
                    if e["client_op"] == client_op:
                        self._send(200, st.entry_read(e))
                        return
            task = st.task_index.get(task_id)
            if project_id not in st.project_index or task is None or task["project_id"] != project_id:
                raise HttpError(404, "Project or task not found")
            active = st.active_entry()
            if active is not None:
                raise HttpError(409, {"message": "User already has an active timer.",
                                      "active_entry": st.entry_read(active)})
            now = now_utc()
            start = now
            started, client_time = parse_dt(data.get("started_at")), parse_dt(data.get("client_time"))
            if started and client_time:
                start = now - min(max(client_time - started, timedelta(0)), timedelta(hours=24))
            entry = st._new_entry(project_id, task_id, start, client_op)
            payload = st.entry_read(entry)
        self._send(201, payload)

    def _stop(self, entry_id: int, data: Dict[str, Any]) -> None:
        st = self.state
        with st.lock:
            entry = st.entries.get(entry_id)
            if entry is None:
                raise HttpError(404, "Time entry not found")
            if entry["end_time"] is None:
                now = now_utc()
                end = now
                stopped, client_time = parse_dt(data.get("stopped_at")), parse_dt(data.get("client_time"))
                if stopped and client_time:
                    end = now - min(max(client_time - stopped, timedelta(0)), timedelta(hours=24))
                st._finalize(entry, end)
            payload = st.entry_read(entry)
        self._send(200, payload)

    def _upload_screenshot(self, entry_id: int, ctype: str, body: bytes) -> None:
        st = self.state
        if "multipart/form-data" not in ctype:
            raise HttpError(422, "Expected multipart/form-data")
        fields, files = parse_multipart(ctype, body)
        if "file" not in files or not files["file"][1]:
            raise HttpError(422, "file is required")
        client_id = fields.get("client_screenshot_id")
        if not client_id:
            raise HttpError(422, "client_screenshot_id is required")
        file_name, image = files["file"]
        with st.lock:
            entry = st.entries.get(entry_id)
            if entry is None:
                raise HttpError(404, "Time entry not found")
            existing = st.client_screenshot_ids.get(client_id)
            if existing is not None:
                st.duplicate_screenshot_uploads += 1
                self._send(201, {"success": True, "duplicate": True,
                                 "screenshot": st.screenshot_read(st.screenshots[existing])})
                return
            if st.uploaded_bytes + len(image) > MAX_STORED_UPLOAD_BYTES:
                raise HttpError(413, "Stub upload store is full")
            captured = parse_dt(fields.get("captured_at")) or now_utc()
            st.next_screenshot_id += 1
            sid = st.next_screenshot_id
            width = int(fields.get("width") or 1000)
            height = int(fields.get("height") or 1000)
            record = st._screenshot_record(sid, entry, captured, image, client_id,
                                           int(fields.get("display_count") or 1))
            record.update({"monitor_number": int(fields.get("monitor_number") or 1),
                           "width": width, "height": height, "created_at": now_utc(),
                           "uploaded_at": now_utc(), "file_name": file_name or f"{sid}.webp"})
            st.screenshots[sid] = record
            st.screenshot_bytes[sid] = image
            st.client_screenshot_ids[client_id] = sid
            st.uploaded_screenshot_count += 1
            st.uploaded_bytes += len(image)
            payload = {"success": True, "duplicate": False, "screenshot": st.screenshot_read(record)}
        self._send(201, payload)

    def _screenshot_timeline(self, query: Dict[str, str]) -> None:
        st = self.state
        try:
            day = datetime.fromisoformat(query["date"]).date() if query.get("date") else now_utc().astimezone(IST).date()
        except ValueError:
            raise HttpError(422, "date must be YYYY-MM-DD")
        with st.lock:
            payload = st.timeline(day)
        self._send(200, payload)

    def _app_usage_batch(self, data: Dict[str, Any]) -> None:
        st = self.state
        records = data.get("records") or []
        with st.lock:
            for r in records:
                name = r.get("application_name")
                if name:
                    st.app_usage[name] = st.app_usage.get(name, 0) + int(r.get("duration_seconds") or 0)
            st.upload_counts["app_usage"] += len(records)
        self._send(201, {"success": True, "count": len(records)})

    def _url_usage_batch(self, data: Dict[str, Any]) -> None:
        st = self.state
        records = data.get("records") or []
        with st.lock:
            for r in records:
                domain = r.get("domain")
                if domain:
                    key = (domain, r.get("url") or f"https://{domain}", r.get("page_title") or domain)
                    st.url_usage[key] = st.url_usage.get(key, 0) + int(r.get("duration_seconds") or 0)
            st.upload_counts["url_usage"] += len(records)
        self._send(201, {"success": True, "count": len(records)})

    def _activity_batch(self, entry_id: Optional[int], data: Dict[str, Any]) -> None:
        st = self.state
        samples = data.get("samples") or []
        with st.lock:
            for s in samples:
                cid = str(s.get("client_event_id") or uuid.uuid4())
                st.activity_samples.setdefault(cid, {
                    "recorded_at": parse_dt(s.get("recorded_at")),
                    "percent": float(s.get("activity_percentage") or 0),
                    "seconds": int(s.get("window_seconds") or 60),
                })
            st.upload_counts["activity"] += len(samples)
        self._send(201, {"success": True, "count": len(samples), "inserted": len(samples)})

    def _activity_today(self) -> None:
        st = self.state
        with st.lock:
            measured = sum(s["seconds"] for s in st.activity_samples.values())
            weighted = sum(s["percent"] * s["seconds"] for s in st.activity_samples.values())
            pct = weighted / measured if measured else 0.0
            today_start, today_end = ist_day_bounds_utc(now_utc().astimezone(IST).date())
            tracked = 0
            for e in st.entries.values():
                if today_start <= e["start_time"] < today_end:
                    tracked += e["total_seconds"] if e["end_time"] else round((now_utc() - e["start_time"]).total_seconds())
            running = st.active_entry() is not None
        self._send(200, {"data": {
            "measured_seconds": measured, "activity_percentage": round(pct),
            "activity_percentage_exact": pct, "tracked_seconds": tracked, "is_tracking": running}})

    def _idle_report(self, data: Dict[str, Any]) -> None:
        st = self.state
        with st.lock:
            entry = st.entries.get(int(data.get("time_entry_id") or 0))
            if entry is None:
                raise HttpError(404, "Time entry not found")
            st.upload_counts["idle"] += 1
            pid = st.next_idle_id
            st.next_idle_id += 1
            started = parse_dt(data.get("idle_started_at")) or now_utc()
            period = {
                "id": pid, "organization_id": ORG_ID, "user_id": USER_ID, "time_entry_id": entry["id"],
                "original_project_id": entry["project_id"], "original_task_id": entry["task_id"],
                "idle_started_at": iso(started), "idle_detected_at": iso(parse_dt(data.get("idle_detected_at")) or now_utc()),
                "resolved_at": None, "idle_duration_seconds": None, "status": "pending",
                "keep_idle_time": None, "action": None, "counted": None, "reassigned": False,
                "reassigned_at": None, "reassigned_project_id": None, "reassigned_task_id": None,
                "reassigned_time_entry_id": None, "reassigned_seconds": None,
                "created_at": iso(now_utc()), "updated_at": iso(now_utc()),
            }
            st.idle_periods[pid] = period
        self._send(201, period)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128


SEED_APPS = [
    "Google Chrome", "Visual Studio Code", "Microsoft Teams", "Microsoft Edge", "Firefox",
    "Notepad++", "Snipping Tool", "Windows Explorer", "ChatGPT", "Slack", "Postman",
    "Terminal", "Outlook", "Excel", "Word", "Figma", "Zoom", "Spotify", "Notion", "Monitra",
]
SEED_SITES = [(d, d.split(".")[0].title() + " - Home") for d in (
    "github.com", "google.com", "stackoverflow.com", "mail.google.com", "docs.google.com",
    "chatgpt.com", "youtube.com", "linkedin.com", "atlassian.net", "figma.com", "notion.so",
    "slack.com", "microsoft.com", "office.com", "zoom.us", "aws.amazon.com", "npmjs.com",
    "pypi.org", "medium.com", "reddit.com", "wikipedia.org", "trello.com", "asana.com",
    "dropbox.com", "drive.google.com", "calendar.google.com", "meet.google.com",
    "vercel.com", "cloudflare.com", "docker.com",
)]


#: The capture interval the stub reports for the user; `--capture-frequency`.
CAPTURE_FREQUENCY_MINUTES = SCREENSHOT_WINDOW_MINUTES


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--projects", type=int, default=40)
    ap.add_argument("--tasks-per-project", type=int, default=30)
    ap.add_argument("--screenshots", type=int, default=40)
    ap.add_argument("--user-email", default="rig.user@example.test")
    ap.add_argument("--capture-frequency", type=int, default=SCREENSHOT_WINDOW_MINUTES,
                    help="the user's screenshot interval in minutes (the backend's value wins over env)")
    ap.add_argument("--churn-timeline", action="store_true",
                    help="change every window's activity on every timeline request")
    ap.add_argument("--seed-usage", action="store_true",
                    help="start the day with application and URL usage rows")
    ap.add_argument("--verbose", action="store_true", help="log every request to stderr")
    args = ap.parse_args(argv)

    t0 = time.monotonic()
    global CAPTURE_FREQUENCY_MINUTES
    CAPTURE_FREQUENCY_MINUTES = max(1, args.capture_frequency)
    Handler.state = State(args)
    srv = Server(("127.0.0.1", args.port), Handler)
    print(f"stub backend ready on http://127.0.0.1:{args.port} "
          f"({args.projects} projects, {args.tasks_per_project} tasks each, "
          f"{len(Handler.state.screenshots)} screenshots, "
          f"{sum(len(i) for i in Handler.state.images) // len(Handler.state.images) // 1024} KB avg image; "
          f"built in {time.monotonic() - t0:.1f}s)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

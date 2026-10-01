"""
End-to-end smoke test for the WFPM integration (backend/app/WFPM).

Drives the real app over real HTTP against the development database:

* every `/WFPM/sync` route -- create/update project, add/remove member,
  create/update task, assign/remove assignee -- checking both the response and
  the rows the database actually holds;
* several assignees per task (`/WFPM/sync/tasks/{id}/assignees`): the list,
  its order and primary, all-or-nothing validation, time entries surviving a
  removal, and the same task seen through the desktop's own `/api/v1` routes;
* the Monitra -> WFPM timer calls (start and stop), against a local HTTP server
  that stands in for WFPM's endpoints: what each is sent, that a slow or
  failing WFPM never delays or fails a timer start, that the sweeper retries,
  and that a refusal is parked rather than re-sent.

Usage, from backend/:

    python scripts/smoke_wfpm_integration.py

It needs nothing running. It starts its own copy of the backend on a free
local port (with the timer integration pointed at the stand-in), and stops it
when it is done. Exit code 0 means every check passed.

Safety
------
* **Development database only.** The backend it starts refuses to serve a
  single request unless it resolved the database `DATABASE_URL_DEV` names, and
  this script refuses to run at all if `DATABASE_URL`, `DATABASE_URL_DEV` or
  `ENV` is exported in the shell. That second rule is not paranoia: importing
  the backend loads `.env` into `os.environ`, and a child process that
  inherits *that* sees the production `DATABASE_URL` as explicitly exported --
  which the backend treats as authoritative. The child is therefore given the
  environment as it was before anything was imported.
* **Disposable principals.** It acts only as three `wfpm_e2e_*@e2e.invalid`
  users it creates, never as a real member, and the project it creates is led
  by its own disposable administrator rather than by the real user the product
  pins as the leader of WFPM projects.
* **It cleans up in `finally`** -- users, project, tasks, time entries and
  queued timer events -- and reports what is left, which must be nothing.
* It never calls the real WFPM, and never the live deployment.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Captured BEFORE anything from the backend is imported. Importing
# app.core.database runs load_dotenv(), which copies .env -- including the
# production DATABASE_URL -- into os.environ. Handing *that* to the child
# backend makes DATABASE_URL look explicitly exported, which the backend
# treats as authoritative, and the child connects to production. The child
# must get the environment as it really was.
CLEAN_ENV = dict(os.environ)
for _key in ("DATABASE_URL", "DATABASE_URL_DEV", "ENV"):
    if _key in CLEAN_ENV:
        raise SystemExit(f"Refusing to run: {_key} is exported in this shell.")

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND)
os.chdir(BACKEND)


def serve() -> None:
    """`--serve`: run the backend this script tests. Not for direct use.

    The real app, with one verification-only change: WFPM-created projects are
    led by the disposable administrator instead of the real user the product
    pins, so no real member ever leads a test project.
    """
    # Hard stop unless this process resolved the DEVELOPMENT database. Checked
    # in the process that will actually serve requests, before it serves any.
    from app.core.config import settings
    from app.core.database import describe_url, get_database_url

    resolved = describe_url(get_database_url())
    is_dev = bool(settings.DATABASE_URL_DEV) and resolved == describe_url(settings.DATABASE_URL_DEV)
    if settings.ENV == "production" or not is_dev:
        sys.exit(f"REFUSING TO START: resolved database {resolved!r} is not the development database.")
    if settings.DATABASE_URL and resolved == describe_url(settings.DATABASE_URL):
        sys.exit("REFUSING TO START: resolved database is the production database.")
    print(f"E2E backend database: {resolved} (development)", flush=True)

    import app.WFPM.service as wfpm_service

    wfpm_service.DEFAULT_PROJECT_LEADER_ID = int(os.environ["WFPM_E2E_LEADER_ID"])

    import uvicorn

    from app.main import app

    uvicorn.run(app, host="127.0.0.1", port=int(os.environ["WFPM_E2E_PORT"]), log_level="info")


if "--serve" in sys.argv:
    serve()
    sys.exit(0)

import httpx  # noqa: E402
from sqlalchemy import text  # noqa: E402

from tests.e2e_support import EMAIL_DOMAIN, _guarded_engine  # noqa: E402

STAMP = time.strftime("%Y%m%d%H%M%S")
WFPM_PROJECT = f"E2E-P-{STAMP}"
WFPM_TASK = f"E2E-T-{STAMP}"
PROJECT_NAME = f"[WFPM-E2E] {STAMP}"
WFPM_TOKEN = "e2e-wfpm-token"
DISPATCH_TOKEN = "e2e-dispatch-token"
STOP_KEY = "e2e-stop-key"

checks: list[tuple[bool, str]] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    checks.append((bool(ok), label))
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{('  -- ' + detail) if detail and not ok else ''}", flush=True)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ── the stand-in for WFPM ────────────────────────────────────────────────────

class FakeWfpm:
    def __init__(self):
        self.received: list[dict] = []     # requests to the start endpoint
        self.stops: list[dict] = []        # requests to the stop endpoint
        self.status = 200
        self.delay = 0.0
        self.port = free_port()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                body = self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
                status, delay = outer.status, outer.delay
                if delay:
                    time.sleep(delay)
                is_stop = self.path.startswith("/api/monitra/timer/stop")
                (outer.stops if is_stop else outer.received).append({
                    "path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()},
                    "body": json.loads(body or b"{}"), "answered": status, "at": time.time(),
                })
                payload = json.dumps({"ok": status < 300}).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/api/monitra/timer/start"

    @property
    def stop_url(self) -> str:
        # Carries a key in its query string, as WFPM's real stop URL does.
        return f"http://127.0.0.1:{self.port}/api/monitra/timer/stop?key={STOP_KEY}"

    def wait_for(self, count: int, timeout: float = 15.0, *, stops: bool = False) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if len(self.stops if stops else self.received) >= count:
                return True
            time.sleep(0.1)
        return False

    def stop(self):
        self.server.shutdown()


# ── disposable principals ────────────────────────────────────────────────────

def provision(engine) -> dict:
    from app.core.config import settings
    from app.core.permissions import ROLE_PERMISSIONS
    from app.core.security import create_access_token

    org = settings.DEFAULT_ORGANIZATION_ID
    people = {}
    with engine.begin() as conn:
        for key, role in (("admin", "administrator"), ("emp1", "employee"), ("emp2", "employee")):
            user_id = conn.execute(text(
                """
                INSERT INTO users (organization_id, username, email, name, role_name,
                                   permissions, is_active, capture_frequency, status)
                VALUES (:org, :username, :email, :name, :role,
                        CAST(:permissions AS jsonb), true, 0, 'active')
                RETURNING id
                """
            ), {
                "org": org, "username": f"wfpm_e2e_{key}_{STAMP}",
                "email": f"wfpm_e2e_{key}_{STAMP}@{EMAIL_DOMAIN}", "name": f"WFPM E2E {key} {STAMP}",
                "role": role, "permissions": json.dumps({p: True for p in ROLE_PERMISSIONS[role]}),
            }).scalar_one()
            people[key] = {
                "id": user_id, "email": f"wfpm_e2e_{key}_{STAMP}@{EMAIL_DOMAIN}",
                "headers": {"Authorization": "Bearer " + create_access_token(
                    {"user_id": user_id}, expires_delta=timedelta(hours=1))},
            }
    return people


def cleanup(engine, user_ids: list[int]) -> dict:
    """Delete every row the principals created; report what is left (nothing)."""
    with engine.begin() as conn:
        emails = [r[0] for r in conn.execute(text("SELECT email FROM users WHERE id = ANY(:ids)"), {"ids": user_ids})]
        if any(not e.endswith(f"@{EMAIL_DOMAIN}") for e in emails):
            raise SystemExit("Refusing to clean up: not all principals are E2E principals.")
        params = {"ids": user_ids}
        conn.execute(text("DELETE FROM time_entries WHERE user_id = ANY(:ids)"), params)
        # Projects the principals created: tasks, members, assignees, entries and
        # WFPM timer events all cascade from the project / time entry.
        conn.execute(text("DELETE FROM projects WHERE created_by = ANY(:ids)"), params)
        conn.execute(text("DELETE FROM tasks WHERE created_by = ANY(:ids)"), params)
        conn.execute(text("DELETE FROM task_assignees WHERE user_id = ANY(:ids) OR assigned_by = ANY(:ids)"), params)
        conn.execute(text("DELETE FROM project_members WHERE user_id = ANY(:ids) OR created_by = ANY(:ids)"), params)
        conn.execute(text("DELETE FROM refresh_tokens WHERE user_id = ANY(:ids)"), params)
        conn.execute(text("DELETE FROM activity_logs WHERE user_id = ANY(:ids)"), params)
        conn.execute(text("DELETE FROM email_notifications WHERE user_id = ANY(:ids)"), params)
        conn.execute(text("DELETE FROM users WHERE id = ANY(:ids)"), params)
    with engine.connect() as conn:
        left = {
            "users": conn.execute(text("SELECT count(*) FROM users WHERE id = ANY(:ids)"), {"ids": user_ids}).scalar(),
            "e2e_users_any": conn.execute(text("SELECT count(*) FROM users WHERE email LIKE :p"), {"p": f"wfpm_e2e_%@{EMAIL_DOMAIN}"}).scalar(),
            "projects": conn.execute(text("SELECT count(*) FROM projects WHERE project_name LIKE '[WFPM-E2E]%' OR wfpm_project_id LIKE 'E2E-P-%' OR created_by = ANY(:ids)"), {"ids": user_ids}).scalar(),
            "tasks": conn.execute(text("SELECT count(*) FROM tasks WHERE wfpm_task_id LIKE 'E2E-T-%' OR created_by = ANY(:ids)"), {"ids": user_ids}).scalar(),
            "time_entries": conn.execute(text("SELECT count(*) FROM time_entries WHERE user_id = ANY(:ids)"), {"ids": user_ids}).scalar(),
            "wfpm_timer_events": conn.execute(text("SELECT count(*) FROM wfpm_timer_events WHERE user_id = ANY(:ids)"), {"ids": user_ids}).scalar(),
            "project_members": conn.execute(text("SELECT count(*) FROM project_members WHERE user_id = ANY(:ids)"), {"ids": user_ids}).scalar(),
            "task_assignees": conn.execute(text("SELECT count(*) FROM task_assignees WHERE user_id = ANY(:ids)"), {"ids": user_ids}).scalar(),
        }
    return left


def row(engine, sql: str, **params):
    with engine.connect() as conn:
        result = conn.execute(text(sql), params).mappings().first()
        return dict(result) if result else None


def rows(engine, sql: str, **params):
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(text(sql), params).mappings().all()]


def wait_for_event(engine, entry_id: int, status: str, timeout: float = 15.0, event_type: str = "timer_start"):
    deadline = time.time() + timeout
    event = None
    while time.time() < deadline:
        event = row(engine, "SELECT * FROM wfpm_timer_events WHERE time_entry_id = :e AND event_type = :t",
                    e=entry_id, t=event_type)
        if event and event["status"] == status:
            return event
        time.sleep(0.25)
    return event


# ── the run ──────────────────────────────────────────────────────────────────

def main() -> int:
    engine, _ = _guarded_engine()
    fake = FakeWfpm()
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    log_path = os.path.join(tempfile.gettempdir(), f"monitra_wfpm_smoke_{STAMP}.log")
    people: dict = {}
    server = None
    log_file = None
    try:
        people = provision(engine)
        admin, emp1, emp2 = people["admin"], people["emp1"], people["emp2"]
        print(f"principals: admin={admin['id']} emp1={emp1['id']} emp2={emp2['id']}  backend={base}  wfpm={fake.url}")

        env = dict(CLEAN_ENV)
        env.update({
            "WFPM_E2E_LEADER_ID": str(admin["id"]), "WFPM_E2E_PORT": str(port),
            "WFPM_TIMER_START_URL": fake.url, "WFPM_TIMER_STOP_URL": fake.stop_url, "WFPM_API_TOKEN": WFPM_TOKEN,
            "WFPM_TIMER_RETRY_BASE_DELAY_SECONDS": "1", "WFPM_TIMER_RETRY_MAX_DELAY_SECONDS": "2",
            "EMAIL_DISPATCH_TOKEN": DISPATCH_TOKEN, "PYTHONUNBUFFERED": "1",
        })
        log_file = open(log_path, "w", encoding="utf-8")
        server = subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "--serve"],
            env=env, stdout=log_file, stderr=subprocess.STDOUT,
        )
        client = httpx.Client(base_url=base, timeout=60.0)
        for _ in range(120):
            try:
                if client.get("/health").status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.5)
            if server.poll() is not None:
                raise SystemExit("backend exited at startup; see " + log_path)
        else:
            raise SystemExit("backend did not start; see " + log_path)

        print("\n[0] the backend under test is on the development database")
        log_file.flush()
        with open(log_path, encoding="utf-8") as handle:
            boot_log = handle.read()
        from app.core.config import settings
        from app.core.database import describe_url

        dev = describe_url(settings.DATABASE_URL_DEV)
        prod = describe_url(settings.DATABASE_URL) if settings.DATABASE_URL else None
        check(f"E2E backend database: {dev} (development)" in boot_log
              and "explicit DATABASE_URL" not in boot_log
              and (prod is None or prod == dev or prod not in boot_log),
              "the backend under test resolved the development database, not production")
        if not checks[-1][0]:
            raise SystemExit("aborting: the backend under test is not on the development database")

        print("\n[1] configuration")
        health = client.get("/health").json()
        check(health.get("wfpm_timer_sync") == {"configured": True, "stop_configured": True, "token_present": True}, "/health reports the WFPM start and stop integration as configured", str(health.get("wfpm_timer_sync")))
        check(STOP_KEY not in json.dumps(health), "/health never carries the stop URL's key")

        print("\n[2] WFPM -> Monitra: project")
        body = {"wfpm_project_id": WFPM_PROJECT, "project_name": PROJECT_NAME, "description": "E2E", "billing_type": "free"}
        r = client.post("/WFPM/sync/projects", json=body, headers=admin["headers"])
        check(r.status_code == 201, "create project -> 201", r.text)
        project = r.json()
        project_id = project["id"]
        check(project["wfpm_project_id"] == WFPM_PROJECT, "response carries wfpm_project_id")
        db_project = row(engine, "SELECT id, wfpm_project_id, project_name, leader_id, status_id, status FROM projects WHERE id = :i", i=project_id)
        check(db_project and db_project["wfpm_project_id"] == WFPM_PROJECT, "projects.wfpm_project_id is stored", str(db_project))
        check(len(project["tasks"]) == 4 and all(t["wfpm_task_id"] is None for t in project["tasks"]), "project is seeded with the 4 default tasks, none linked")

        r = client.post("/WFPM/sync/projects", json=body | {"project_name": PROJECT_NAME + " retry"}, headers=admin["headers"])
        check(r.status_code == 200 and r.json()["id"] == project_id, "repeated create -> 200 with the same project", r.text)
        count = row(engine, "SELECT count(*) AS n FROM projects WHERE wfpm_project_id = :w", w=WFPM_PROJECT)["n"]
        check(count == 1, "still exactly one project row for that WFPM id", str(count))

        r = client.patch(f"/WFPM/sync/projects/{WFPM_PROJECT}", json={"project_name": PROJECT_NAME + " v2", "status": "paused"}, headers=admin["headers"])
        check(r.status_code == 200 and r.json()["project_name"].endswith("v2") and r.json()["status"]["name"].lower() == "paused", "update project by WFPM id (name + status)", r.text)
        db_project = row(engine, "SELECT project_name, status FROM projects WHERE id = :i", i=project_id)
        check(db_project["project_name"].endswith("v2") and db_project["status"] == "pending", "update is persisted (legacy status column follows)", str(db_project))
        r = client.patch(f"/WFPM/sync/projects/{WFPM_PROJECT}", json={"status": "active"}, headers=admin["headers"])
        check(r.status_code == 200, "status back to active", r.text)

        r = client.patch(f"/WFPM/sync/projects/{project_id}", json={"project_name": "wrong id space"}, headers=admin["headers"])
        check(r.status_code == 404, "a Monitra id is not accepted where a WFPM id is expected -> 404", r.text)
        r = client.get("/WFPM/sync/projects/has%20spaces", headers=admin["headers"])
        check(r.status_code == 422, "malformed WFPM id -> 422", r.text)

        print("\n[3] WFPM -> Monitra: members")
        r = client.post(f"/WFPM/sync/projects/{WFPM_PROJECT}/members", json={"member_ids": [emp1["id"], emp2["id"]]}, headers=admin["headers"])
        check(r.status_code == 200 and sorted(r.json()["added_member_ids"]) == sorted([emp1["id"], emp2["id"]]), "assign two members", r.text)
        r = client.post(f"/WFPM/sync/projects/{WFPM_PROJECT}/members", json={"member_ids": [emp1["id"]]}, headers=admin["headers"])
        check(r.status_code == 200 and r.json()["already_assigned_member_ids"] == [emp1["id"]] and r.json()["added_member_ids"] == [], "assigning an existing member again is harmless", r.text)
        r = client.get(f"/WFPM/sync/projects/{WFPM_PROJECT}", headers=emp2["headers"])
        check(r.status_code == 200, "a member can read the project by its WFPM id", r.text)
        r = client.delete(f"/WFPM/sync/projects/{WFPM_PROJECT}/members/{emp2['id']}", headers=admin["headers"])
        check(r.status_code == 204, "remove member -> 204", r.text)
        members = {m["user_id"] for m in rows(engine, "SELECT user_id FROM project_members WHERE project_id = :p", p=project_id)}
        check(emp1["id"] in members and emp2["id"] not in members, "project_members reflects the removal", str(members))
        r = client.delete(f"/WFPM/sync/projects/{WFPM_PROJECT}/members/{emp2['id']}", headers=admin["headers"])
        check(r.status_code == 404, "removing a non-member -> 404", r.text)
        r = client.get(f"/WFPM/sync/projects/{WFPM_PROJECT}", headers=emp2["headers"])
        check(r.status_code == 404, "the removed employee can no longer reach the project through its WFPM id", r.text)
        r = client.patch(f"/WFPM/sync/projects/{WFPM_PROJECT}", json={"project_name": "nope"}, headers=emp1["headers"])
        check(r.status_code == 403, "an employee cannot update a project through WFPM (no projects:update) -> 403", r.text)

        print("\n[4] WFPM -> Monitra: task + assignee")
        task_body = {"wfpm_task_id": WFPM_TASK, "name": f"[WFPM-E2E] task {STAMP}", "assignee_id": emp1["id"], "estimated_hours": 3.5}
        r = client.post(f"/WFPM/sync/projects/{WFPM_PROJECT}/tasks", json=task_body, headers=admin["headers"])
        check(r.status_code == 201, "create task -> 201", r.text)
        task = r.json()
        task_id = task["id"]
        check(task["wfpm_task_id"] == WFPM_TASK and task["assignee_id"] == emp1["id"], "response carries wfpm_task_id and the assignee")
        db_task = row(engine, "SELECT wfpm_task_id, assignee_id, project_id, estimated_hours FROM tasks WHERE id = :i", i=task_id)
        check(db_task["wfpm_task_id"] == WFPM_TASK and db_task["project_id"] == project_id and float(db_task["estimated_hours"]) == 3.5, "tasks.wfpm_task_id is stored", str(db_task))
        r = client.post(f"/WFPM/sync/projects/{WFPM_PROJECT}/tasks", json=task_body, headers=admin["headers"])
        check(r.status_code == 200 and r.json()["id"] == task_id, "repeated create -> 200 with the same task", r.text)
        count = row(engine, "SELECT count(*) AS n FROM tasks WHERE wfpm_task_id = :w", w=WFPM_TASK)["n"]
        check(count == 1, "still exactly one task row for that WFPM id", str(count))

        r = client.patch(f"/WFPM/sync/tasks/{WFPM_TASK}", json={"name": f"[WFPM-E2E] task {STAMP} v2", "status": "in_progress"}, headers=admin["headers"])
        check(r.status_code == 200 and r.json()["name"].endswith("v2") and r.json()["status"]["name"].lower().replace(" ", "") == "inprogress", "update task by WFPM id (name + status)", r.text)

        r = client.delete(f"/WFPM/sync/tasks/{WFPM_TASK}/assignee", headers=admin["headers"])
        unassigned = row(engine, "SELECT assignee_id, (SELECT count(*) FROM task_assignees WHERE task_id = :i) AS n FROM tasks WHERE id = :i", i=task_id)
        check(r.status_code == 200 and r.json()["assignee_id"] is None and unassigned == {"assignee_id": None, "n": 0}, "remove assignee clears tasks.assignee_id and task_assignees", f"{r.text} {unassigned}")
        r = client.put(f"/WFPM/sync/tasks/{WFPM_TASK}/assignee", json={"assignee_id": emp2["id"]}, headers=admin["headers"])
        check(r.status_code == 400, "assigning someone who is not a project member -> 400", r.text)
        r = client.put(f"/WFPM/sync/tasks/{WFPM_TASK}/assignee", json={"assignee_id": emp1["id"]}, headers=admin["headers"])
        assigned = row(engine, "SELECT assignee_id, (SELECT count(*) FROM task_assignees WHERE task_id = :i AND user_id = :u) AS n FROM tasks WHERE id = :i", i=task_id, u=emp1["id"])
        check(r.status_code == 200 and assigned == {"assignee_id": emp1["id"], "n": 1}, "assign writes both representations", f"{r.text} {assigned}")

        r = client.get(f"/api/v1/projects/{project_id}/tasks", headers=emp1["headers"])
        names = [t["id"] for t in r.json()] if r.status_code == 200 else []
        check(r.status_code == 200 and task_id in names, "the WFPM task is an ordinary task: the desktop's /api/v1 task list shows it to its assignee", r.text[:200])
        check(r.status_code == 200 and all("wfpm_task_id" not in t for t in r.json()), "and /api/v1's TaskRead shape is unchanged (no wfpm fields)")

        print("\n[5] Monitra -> WFPM: timer start")
        now = datetime.now(timezone.utc)
        start_body = {"project_id": project_id, "task_id": task_id, "client_op": f"e2e:{STAMP}:1",
                      "started_at": now.isoformat(), "client_time": now.isoformat()}
        t0 = time.time()
        r = client.post("/time-entries/start", json=start_body, headers=emp1["headers"])
        baseline = time.time() - t0
        check(r.status_code == 201, "start timer on the linked task -> 201", r.text)
        entry = r.json()
        check(not any("wfpm" in k.lower() for k in entry), "the start response the desktop reads is unchanged")
        check(fake.wait_for(1), "WFPM's endpoint received a request")
        got = fake.received[0] if fake.received else {"body": {}, "headers": {}}
        b = got["body"]
        check(b.get("wfpm_task_id") == WFPM_TASK and b.get("wfpm_project_id") == WFPM_PROJECT, "payload names the WFPM task and project", str(b))
        check(b.get("event") == "timer_start" and b.get("event_id") == f"monitra:timer_start:{entry['id']}", "payload carries event + event_id", str(b))
        check(b.get("monitra_time_entry_id") == entry["id"] and b.get("monitra_task_id") == task_id and b.get("monitra_user_id") == emp1["id"] and b.get("user_email") == emp1["email"], "payload carries the Monitra ids and the user's email", str(b))
        started = datetime.fromisoformat(b["started_at"]) if b.get("started_at") else None
        persisted = datetime.fromisoformat(entry["start_time"].replace("Z", "+00:00"))
        check(started == persisted and b.get("stopped_at") is None, "started_at is the persisted start_time; stopped_at is null", f"{b.get('started_at')} vs {entry['start_time']}")
        check(got["headers"].get("authorization") == f"Bearer {WFPM_TOKEN}" and got["headers"].get("idempotency-key") == b.get("event_id"), "Authorization and Idempotency-Key headers are sent", str(got["headers"]))
        event = wait_for_event(engine, entry["id"], "sent")
        check(event and event["status"] == "sent" and event["attempt_count"] == 1 and event["response_status"] == 200, "wfpm_timer_events row is `sent` after one attempt", str(event))

        r = client.post("/time-entries/start", json=start_body, headers=emp1["headers"])
        time.sleep(2.0)
        check(r.status_code == 200 and r.json()["id"] == entry["id"] and len(fake.received) == 1, "a replayed start (same client_op) -> 200 and WFPM is not told twice", f"{r.status_code} requests={len(fake.received)}")

        print("\n[5b] Monitra -> WFPM: timer stop")
        r = client.post(f"/time-entries/{entry['id']}/stop", json={}, headers=emp1["headers"])
        stopped_entry = r.json()
        check(r.status_code == 200 and not any("wfpm" in k.lower() for k in stopped_entry), "stop -> 200 and the stop response the desktop reads is unchanged", r.text)
        check(fake.wait_for(1, stops=True), "WFPM's stop endpoint received a request")
        check(len(fake.received) == 1, "and the start endpoint was not called again", f"start requests={len(fake.received)}")
        got = fake.stops[0] if fake.stops else {"body": {}, "headers": {}, "path": ""}
        b = got["body"]
        check(got["path"].endswith(f"?key={STOP_KEY}"), "the stop URL's ?key= reaches WFPM unchanged", got["path"])
        check(b.get("event") == "timer_stop" and b.get("event_id") == f"monitra:timer_stop:{entry['id']}" and got["headers"].get("idempotency-key") == b.get("event_id"), "payload carries event + event_id, and the Idempotency-Key matches", str(b))
        check(b.get("wfpm_task_id") == WFPM_TASK and b.get("wfpm_project_id") == WFPM_PROJECT, "stop payload names the WFPM task and project", str(b))
        check(b.get("monitra_user_id") == emp1["id"] and b.get("monitra_time_entry_id") == entry["id"] and b.get("monitra_task_id") == task_id and b.get("monitra_project_id") == project_id, "stop payload carries the Monitra ids", str(b))
        check("user_email" not in b and "user_name" not in b, "stop payload carries no email or name (WFPM finds the user by monitra_user_id)", str(b))
        s_at = datetime.fromisoformat(b["started_at"]) if b.get("started_at") else None
        e_at = datetime.fromisoformat(b["stopped_at"]) if b.get("stopped_at") else None
        persisted_start = datetime.fromisoformat(stopped_entry["start_time"].replace("Z", "+00:00"))
        persisted_end = datetime.fromisoformat(stopped_entry["end_time"].replace("Z", "+00:00"))
        check(s_at == persisted_start and e_at == persisted_end, "started_at / stopped_at are the persisted start_time / end_time", f"{b.get('started_at')} {b.get('stopped_at')} vs {stopped_entry['start_time']} {stopped_entry['end_time']}")
        check(e_at is not None and s_at is not None and round((e_at - s_at).total_seconds()) == stopped_entry["total_seconds"], "WFPM's duration equals Monitra's total_seconds", f"{stopped_entry['total_seconds']}")
        stop_event = wait_for_event(engine, entry["id"], "sent", event_type="timer_stop")
        check(stop_event and stop_event["attempt_count"] == 1 and stop_event["response_status"] == 200, "the timer_stop row is `sent` after one attempt", str(stop_event))
        check(STOP_KEY not in json.dumps(stop_event, default=str), "the stored event never contains the stop URL's key", str(stop_event))
        r = client.post(f"/time-entries/{entry['id']}/stop", json={}, headers=emp1["headers"])
        time.sleep(2.0)
        stop_rows = row(engine, "SELECT count(*) AS n FROM wfpm_timer_events WHERE time_entry_id = :e AND event_type = 'timer_stop'", e=entry["id"])["n"]
        check(r.status_code == 200 and len(fake.stops) == 1 and stop_rows == 1, "a repeated stop -> 200 and WFPM is not told twice", f"{r.status_code} requests={len(fake.stops)} rows={stop_rows}")

        print("\n[6] a task WFPM does not know starts no WFPM timer")
        default_task = row(engine, "SELECT id FROM tasks WHERE project_id = :p AND wfpm_task_id IS NULL ORDER BY id LIMIT 1", p=project_id)["id"]
        r = client.post("/time-entries/start", json={"project_id": project_id, "task_id": default_task, "client_op": f"e2e:{STAMP}:2"}, headers=emp1["headers"])
        unlinked_entry = r.json().get("id")
        time.sleep(2.0)
        none_queued = row(engine, "SELECT count(*) AS n FROM wfpm_timer_events WHERE time_entry_id = :e", e=unlinked_entry)["n"]
        check(r.status_code == 201 and none_queued == 0 and len(fake.received) == 1, "unlinked task: timer starts, nothing queued, nothing sent", f"{r.status_code} queued={none_queued} requests={len(fake.received)}")
        client.post(f"/time-entries/{unlinked_entry}/stop", json={}, headers=emp1["headers"])
        time.sleep(1.5)
        check(len(fake.stops) == 1, "stopping an unlinked task's timer sends no stop either", f"stop requests={len(fake.stops)}")

        print("\n[7] WFPM slow and failing: the start does not wait, and the sweeper retries")
        fake.status, fake.delay = 503, 6.0
        t0 = time.time()
        r = client.post("/time-entries/start", json={"project_id": project_id, "task_id": task_id, "client_op": f"e2e:{STAMP}:3"}, headers=emp1["headers"])
        latency = time.time() - t0
        retry_entry = r.json().get("id")
        check(r.status_code == 201, "start -> 201 while WFPM is down", r.text)
        check(latency < 6.0 and len(fake.received) == 1, f"start answered in {latency:.2f}s (baseline {baseline:.2f}s) while WFPM takes 6s to answer -- delivery is after the response", f"requests={len(fake.received)}")
        check(fake.wait_for(2, timeout=20), "WFPM's endpoint did receive the attempt (and answered 503)")
        event = wait_for_event(engine, retry_entry, "pending")
        deadline = time.time() + 15
        while time.time() < deadline and (not event or event["attempt_count"] < 1 or event["response_status"] != 503):
            time.sleep(0.25)
            event = row(engine, "SELECT * FROM wfpm_timer_events WHERE time_entry_id = :e AND event_type = 'timer_start'", e=retry_entry)
        check(event and event["status"] == "pending" and event["attempt_count"] == 1 and event["response_status"] == 503 and "503" in (event["last_error"] or ""), "event stays `pending` after the 503, attempt 1 recorded with the reason", str(event))

        r = client.post("/internal/wfpm/timer-events/dispatch")
        check(r.status_code == 401, "sweeper without the dispatch token -> 401", r.text)
        fake.status, fake.delay = 200, 0.0
        time.sleep(2.5)   # past next_attempt_at (base 1s, cap 2s in this run)
        r = client.post("/internal/wfpm/timer-events/dispatch", headers={"X-Email-Dispatch-Token": DISPATCH_TOKEN})
        check(r.status_code == 200 and r.json().get("sent") == 1 and r.json().get("attempted") == 1, "sweeper delivers the parked event", r.text)
        event = row(engine, "SELECT * FROM wfpm_timer_events WHERE time_entry_id = :e AND event_type = 'timer_start'", e=retry_entry)
        check(event["status"] == "sent" and event["attempt_count"] == 2 and event["last_error"] is None, "event is `sent` on attempt 2", str(event))
        same_event = [x for x in fake.received if x["body"].get("monitra_time_entry_id") == retry_entry]
        check(len(same_event) == 2 and same_event[0]["body"]["event_id"] == same_event[1]["body"]["event_id"] == same_event[1]["headers"].get("idempotency-key"), "both attempts carried the same event_id / Idempotency-Key", str([x["body"].get("event_id") for x in same_event]))
        r = client.post("/internal/wfpm/timer-events/dispatch", headers={"X-Email-Dispatch-Token": DISPATCH_TOKEN})
        check(r.status_code == 200 and r.json().get("attempted") == 0, "a second sweep has nothing to do", r.text)
        client.post(f"/time-entries/{retry_entry}/stop", json={}, headers=emp1["headers"])

        print("\n[8] WFPM refuses the event")
        fake.status = 404
        before = len(fake.received)
        r = client.post("/time-entries/start", json={"project_id": project_id, "task_id": task_id, "client_op": f"e2e:{STAMP}:4"}, headers=emp1["headers"])
        rejected_entry = r.json().get("id")
        event = wait_for_event(engine, rejected_entry, "rejected")
        check(r.status_code == 201 and event and event["status"] == "rejected" and event["response_status"] == 404, "a 404 from WFPM parks the event as `rejected`; the Monitra timer still started", str(event))
        fake.status = 200
        time.sleep(2.5)
        r2 = client.post("/internal/wfpm/timer-events/dispatch", headers={"X-Email-Dispatch-Token": DISPATCH_TOKEN})
        check(r2.json().get("attempted") == 0 and len(fake.received) == before + 1, "a rejected event is not retried", f"{r2.text} requests={len(fake.received) - before}")
        client.post(f"/time-entries/{rejected_entry}/stop", json={}, headers=emp1["headers"])

        print("\n[9] a stop WFPM cannot take right now is retried, with the same event_id")
        r = client.post("/time-entries/start", json={"project_id": project_id, "task_id": task_id, "client_op": f"e2e:{STAMP}:5"}, headers=emp1["headers"])
        retry_stop_entry = r.json().get("id")
        check(r.status_code == 201, "start another timer on the linked task -> 201", r.text)
        # Let the start's own delivery finish before WFPM starts failing, so the
        # only thing parked is the stop.
        wait_for_event(engine, retry_stop_entry, "sent")
        fake.status = 503
        stops_before = len(fake.stops)
        r = client.post(f"/time-entries/{retry_stop_entry}/stop", json={}, headers=emp1["headers"])
        check(r.status_code == 200, "stop -> 200 while WFPM is answering 503", r.text)
        check(fake.wait_for(stops_before + 1, stops=True), "WFPM's stop endpoint received the attempt (and answered 503)")
        stop_event = wait_for_event(engine, retry_stop_entry, "pending", event_type="timer_stop")
        deadline = time.time() + 15
        while time.time() < deadline and (not stop_event or stop_event["attempt_count"] < 1 or stop_event["response_status"] != 503):
            time.sleep(0.25)
            stop_event = row(engine, "SELECT * FROM wfpm_timer_events WHERE time_entry_id = :e AND event_type = 'timer_stop'", e=retry_stop_entry)
        check(stop_event and stop_event["status"] == "pending" and stop_event["attempt_count"] == 1 and stop_event["response_status"] == 503, "the stop stays `pending` after the 503", str(stop_event))
        fake.status = 200
        time.sleep(2.5)
        r = client.post("/internal/wfpm/timer-events/dispatch", headers={"X-Email-Dispatch-Token": DISPATCH_TOKEN})
        check(r.status_code == 200 and r.json().get("sent") == 1, "the sweeper delivers the parked stop", r.text)
        stop_event = row(engine, "SELECT * FROM wfpm_timer_events WHERE time_entry_id = :e AND event_type = 'timer_stop'", e=retry_stop_entry)
        check(stop_event["status"] == "sent" and stop_event["attempt_count"] == 2, "the stop is `sent` on attempt 2", str(stop_event))
        attempts = [x for x in fake.stops if x["body"].get("monitra_time_entry_id") == retry_stop_entry]
        check(len(attempts) == 2 and attempts[0]["body"]["event_id"] == attempts[1]["body"]["event_id"] == attempts[1]["headers"].get("idempotency-key") == f"monitra:timer_stop:{retry_stop_entry}", "both attempts carried the same event_id / Idempotency-Key", str([x["body"].get("event_id") for x in attempts]))

        print("\n[10] several assignees")
        a_path = f"/WFPM/sync/tasks/{WFPM_TASK}/assignees"
        a_headers = admin["headers"]

        def held():
            """What the database holds: (tasks.assignee_id, task_assignees in insertion order)."""
            primary = row(engine, "SELECT assignee_id FROM tasks WHERE id = :i", i=task_id)["assignee_id"]
            members = [r["user_id"] for r in rows(engine, "SELECT user_id FROM task_assignees WHERE task_id = :i ORDER BY id", i=task_id)]
            return primary, members

        def listed(response):
            return [person["id"] for person in response.json().get("assignees", [])] if response.status_code in (200, 201) else None

        r = client.put(f"/WFPM/sync/tasks/{WFPM_TASK}/assignee", json={"assignee_id": emp1["id"]}, headers=a_headers)
        check(r.status_code == 200 and held() == (emp1["id"], [emp1["id"]]), "start from one assignee (the single route)", f"{r.status_code} {held()}")

        r = client.put(a_path, json={"assignee_ids": [emp1["id"], emp2["id"]]}, headers=a_headers)
        check(r.status_code == 400 and str(emp2["id"]) in r.text and str(emp1["id"]) not in r.json().get("detail", ""), "a non-member in the list -> 400 naming only the offender", r.text)
        check(held() == (emp1["id"], [emp1["id"]]), "and nothing changed -- not even the valid id was added", str(held()))
        r = client.put(a_path, json={"assignee_ids": [emp1["id"], 0]}, headers=a_headers)
        check(r.status_code == 422, "a malformed list -> 422", r.text)
        r = client.put(a_path, json={"assignee_ids": list(range(1, 52))}, headers=a_headers)
        check(r.status_code == 422, "more than 50 ids -> 422", r.text[:120])
        r = client.put(a_path, json={"assignee_ids": "101"}, headers=a_headers)
        check(r.status_code == 422, "a list that is not a list -> 422", r.text[:120])
        r = client.put(a_path, json={"assignee_ids": [emp1["id"], emp2["id"]]}, headers=emp1["headers"])
        check(r.status_code == 400, "an employee may call it (tasks:update) but the same validation applies -> 400", r.text)

        r = client.put(a_path, json={"assignee_ids": [emp1["id"], emp2["id"]], "add_missing_members": True}, headers=a_headers)
        body = r.json() if r.status_code == 200 else {}
        check(r.status_code == 200 and listed(r) == [emp1["id"], emp2["id"]], "add_missing_members: the non-member is added to the project and assigned", r.text)
        check(body.get("assignee_id") == emp1["id"] and (body.get("assignee") or {}).get("id") == emp1["id"], "assignee_id / assignee stay the first (primary) assignee", str(body.get("assignee")))
        check(held() == (emp1["id"], [emp1["id"], emp2["id"]]), "both representations hold the list", str(held()))
        is_member = row(engine, "SELECT count(*) AS n FROM project_members WHERE project_id = :p AND user_id = :u", p=project_id, u=emp2["id"])["n"]
        check(is_member == 1, "the project membership was written", str(is_member))

        ids_before = [r_["id"] for r_ in rows(engine, "SELECT id FROM task_assignees WHERE task_id = :i ORDER BY id", i=task_id)]
        r = client.put(a_path, json={"assignee_ids": [emp1["id"], emp2["id"]]}, headers=a_headers)
        ids_after = [r_["id"] for r_ in rows(engine, "SELECT id FROM task_assignees WHERE task_id = :i ORDER BY id", i=task_id)]
        check(r.status_code == 200 and ids_after == ids_before, "repeating the same call changes nothing (no row rewritten)", f"{ids_before} -> {ids_after}")
        r = client.put(a_path, json={"assignee_ids": [emp1["id"], emp2["id"], emp1["id"]]}, headers=a_headers)
        check(r.status_code == 200 and listed(r) == [emp1["id"], emp2["id"]], "duplicates are ignored", r.text)

        r = client.get(a_path, headers=a_headers)
        check(r.status_code == 200 and [p_["id"] for p_ in r.json()] == [emp1["id"], emp2["id"]] and set(r.json()[0]) == {"id", "name", "email", "role"}, "GET returns the ordered list of {id, name, email, role}", r.text[:200])
        r = client.get(f"/WFPM/sync/tasks/{WFPM_TASK}", headers=a_headers)
        check(r.status_code == 200 and listed(r) == [emp1["id"], emp2["id"]], "the task read carries the whole set", r.text[:200])
        r = client.get(f"/WFPM/sync/projects/{WFPM_PROJECT}", headers=a_headers)
        project_task = next((t for t in r.json().get("tasks", []) if t.get("wfpm_task_id") == WFPM_TASK), {}) if r.status_code == 200 else {}
        check([p_["id"] for p_ in project_task.get("assignees", [])] == [emp1["id"], emp2["id"]], "and so does the task inside the project read", str(project_task.get("assignees")))

        r = client.put(a_path, json={"assignee_ids": [emp2["id"], emp1["id"]]}, headers=a_headers)
        check(r.status_code == 200 and listed(r) == [emp2["id"], emp1["id"]] and r.json()["assignee_id"] == emp2["id"], "reordering changes the primary and the order", r.text[:200])
        check(held() == (emp2["id"], [emp2["id"], emp1["id"]]), "and the database follows", str(held()))

        # Every assignee can track time -- emp1 is no longer the primary.
        now = datetime.now(timezone.utc)
        for person, tag in ((emp1, "a"), (emp2, "b")):
            r = client.post("/time-entries/start", json={"project_id": project_id, "task_id": task_id, "client_op": f"e2e:{STAMP}:m{tag}",
                                                          "started_at": now.isoformat(), "client_time": now.isoformat()}, headers=person["headers"])
            check(r.status_code == 201, f"assignee {tag} ({'primary' if tag == 'b' else 'not primary'}) can start a timer on the task", r.text[:200])
            if r.status_code == 201:
                client.post(f"/time-entries/{r.json()['id']}/stop", json={}, headers=person["headers"])
        entries_before = row(engine, "SELECT count(*) AS n FROM time_entries WHERE task_id = :t AND user_id = :u", t=task_id, u=emp1["id"])["n"]
        check(entries_before >= 2, "emp1 has time entries on the task", str(entries_before))
        r = client.get(f"/api/v1/projects/{project_id}/tasks", headers=emp1["headers"])
        check(r.status_code == 200 and task_id in [t["id"] for t in r.json()], "a non-primary assignee sees the task in the desktop's own task list", r.text[:200])

        r = client.delete(f"{a_path}/{emp1['id']}", headers=a_headers)
        check(r.status_code == 204 and r.content == b"" and held() == (emp2["id"], [emp2["id"]]), "DELETE one assignee -> 204", f"{r.status_code} {held()}")
        entries_after = row(engine, "SELECT count(*) AS n FROM time_entries WHERE task_id = :t AND user_id = :u", t=task_id, u=emp1["id"])["n"]
        check(entries_after == entries_before, "removing an assignee kept their time entries", f"{entries_before} -> {entries_after}")
        r = client.delete(f"{a_path}/{emp1['id']}", headers=a_headers)
        check(r.status_code == 404, "removing someone who is not assigned -> 404", r.text)

        r = client.post(a_path, json={"assignee_ids": [emp1["id"], emp2["id"]]}, headers=a_headers)
        check(r.status_code == 200 and listed(r) == [emp2["id"], emp1["id"]] and r.json()["assignee_id"] == emp2["id"], "POST adds; someone already assigned is not an error; the primary is kept", r.text[:200])

        r = client.put(a_path, json={"assignee_ids": []}, headers=a_headers)
        check(r.status_code == 200 and r.json()["assignee_id"] is None and r.json()["assignee"] is None and r.json()["assignees"] == [] and held() == (None, []), "[] leaves the task unassigned in both representations", f"{r.text[:160]} {held()}")
        entries_cleared = row(engine, "SELECT count(*) AS n FROM time_entries WHERE task_id = :t", t=task_id)["n"]
        check(entries_cleared >= 2, "and every time entry is still there", str(entries_cleared))

        r = client.put(a_path, json={"assignee_ids": [emp1["id"]]}, headers=a_headers)
        r2 = client.get(a_path, headers=emp2["headers"])
        check(r.status_code == 200 and r2.status_code == 404, "a task the caller cannot see (held by someone else) -> 404", f"{r2.status_code}")
        r = client.get("/WFPM/sync/tasks/E2E-NOPE/assignees", headers=a_headers)
        check(r.status_code == 404, "a task that is not linked -> 404", r.text)

        r = client.put(f"/WFPM/sync/tasks/{WFPM_TASK}/assignee", json={"assignee_id": emp2["id"]}, headers=a_headers)
        check(r.status_code == 200 and listed(r) == [emp2["id"]] and held() == (emp2["id"], [emp2["id"]]), "the single route still means: the set becomes just this person", f"{r.text[:160]} {held()}")

        second_task = f"{WFPM_TASK}-b"
        body = {"wfpm_task_id": second_task, "name": f"[WFPM-E2E] two assignees {STAMP}", "assignee_id": emp1["id"], "assignee_ids": [emp2["id"], emp1["id"]]}
        r = client.post(f"/WFPM/sync/projects/{WFPM_PROJECT}/tasks", json=body, headers=a_headers)
        created = r.json() if r.status_code == 201 else {}
        check(r.status_code == 201 and listed(r) == [emp2["id"], emp1["id"]] and created.get("assignee_id") == emp2["id"], "create with assignee_ids (which wins over assignee_id) -> 201", r.text[:200])
        r = client.post(f"/WFPM/sync/projects/{WFPM_PROJECT}/tasks", json={**body, "assignee_ids": [emp1["id"]]}, headers=a_headers)
        check(r.status_code == 200 and r.json().get("id") == created.get("id") and listed(r) == [emp2["id"], emp1["id"]], "a repeated create -> 200, the task as it stands, nothing re-applied", r.text[:200])
        r = client.post(f"/WFPM/sync/projects/{WFPM_PROJECT}/tasks", json={"wfpm_task_id": f"{WFPM_TASK}-c", "name": "bad", "assignee_ids": [emp1["id"], admin["id"]]}, headers=a_headers)
        nothing = row(engine, "SELECT count(*) AS n FROM tasks WHERE wfpm_task_id = :w", w=f"{WFPM_TASK}-c")["n"]
        check(r.status_code == 400 and str(admin["id"]) in r.text and nothing == 0, "create with an invalid assignee -> 400 and no task", f"{r.status_code} {r.text[:160]} rows={nothing}")

        r = client.get(f"/WFPM/sync/tasks/{WFPM_TASK}", headers=a_headers)
        check(r.status_code == 200 and "assignees" in r.json() and r.json()["assignee_id"] == emp2["id"], "assignee_id / assignee / assignees all present for existing callers", r.text[:160])

        running = row(engine, "SELECT count(*) AS n FROM time_entries WHERE user_id = ANY(:u) AND end_time IS NULL", u=[emp1["id"], emp2["id"]])["n"]
        check(running == 0, "no timer left running", str(running))
        client.close()
    finally:
        if server is not None:
            server.terminate()
            try:
                server.wait(timeout=15)
            except subprocess.TimeoutExpired:
                server.kill()
        if log_file is not None:
            log_file.close()
        fake.stop()
        if people:
            print("\n[cleanup]")
            left = cleanup(engine, [p["id"] for p in people.values()])
            check(all(v == 0 for v in left.values()), "every row the run created is gone", str(left))
            print("  remaining:", left)

    failed = [label for ok, label in checks if not ok]
    print(f"\nbackend log: {log_path}")
    print(f"{len(checks) - len(failed)}/{len(checks)} checks passed")
    for label in failed:
        print("  FAILED:", label)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

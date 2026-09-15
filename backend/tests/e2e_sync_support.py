"""
Fixtures for the real-HTTP, real-database synchronisation E2E suite.

Run from the backend directory, in the backend's own environment, by the
desktop E2E test (`desktop/tests/e2e/test_sync_lifecycle_e2e.py`), which
cannot import the backend package alongside the desktop's own `app` package.
Everything it needs is printed as one JSON document on stdout:

    python tests/e2e_sync_support.py provision            -> {"employee": {...}, "admin": {...}, ...}
    python tests/e2e_sync_support.py cleanup '<that json>'

Two principals, because synchronisation is a two-client story: an ordinary
`employee` is "the desktop", and an `administrator` is "the web" -- the
account that creates projects, adds and removes members and edits tasks
through the same API the web client uses. Neither is anybody's real account.

Safety
------
* Runs only against the development database: the resolved URL must be the
  one `DATABASE_URL_DEV` names, and `ENV` must not be production. Anything
  else is a hard stop before a single row is written.
* Both principals are named `e2e_sync_<stamp>_*@e2e.invalid` (a reserved
  TLD) so they are unmistakable, and `cleanup` deletes them together with
  every project, task, membership, assignment and time entry they created.
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import timedelta

BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_ROOT not in sys.path:
    sys.path.insert(0, BACKEND_ROOT)

from sqlalchemy import text  # noqa: E402

EMAIL_DOMAIN = "e2e.invalid"

EMPLOYEE_PERMISSIONS = {
    "tasks:view": True,
    "tasks:create": True,
    "tasks:update": True,
    "projects:view": True,
    "time_entries:manage_own": True,
}

ADMIN_PERMISSIONS = {
    "projects:create": True,
    "projects:update": True,
    "projects:delete": True,
    "projects:view": True,
    "tasks:create": True,
    "tasks:update": True,
    "tasks:delete": True,
    "tasks:view": True,
    "time_entries:manage_own": True,
    "time_entries:view_all": True,
}


def _guarded_engine():
    from app.core.config import settings
    from app.core.database import describe_url, get_database_url, get_engine

    if settings.ENV == "production":
        sys.exit("Refusing to provision E2E fixtures with ENV=production.")
    resolved = get_database_url()
    dev = settings.DATABASE_URL_DEV
    if not dev or describe_url(resolved) != describe_url(dev):
        sys.exit(
            f"Refusing: the resolved database {describe_url(resolved)!r} is not the "
            f"development database DATABASE_URL_DEV names."
        )
    return get_engine(), resolved


def _insert_user(conn, org: int, stamp: str, kind: str, role: str, permissions: dict) -> int:
    return conn.execute(
        text(
            """
            INSERT INTO users (organization_id, username, email, name, role_name,
                               permissions, is_active, capture_frequency, status)
            VALUES (:org, :username, :email, :name, :role,
                    CAST(:permissions AS jsonb), true, 0, 'active')
            RETURNING id
            """
        ),
        {
            "org": org,
            "username": f"e2e_sync_{stamp}_{kind}",
            "email": f"e2e_sync_{stamp}_{kind}@{EMAIL_DOMAIN}",
            "name": f"E2E Sync {kind} {stamp}",
            "role": role,
            "permissions": json.dumps(permissions),
        },
    ).scalar_one()


def _status_id(conn, table: str, wanted: str) -> int:
    rows = conn.execute(text(f"SELECT id, name FROM {table}")).fetchall()
    for row_id, name in rows:
        if "".join(ch for ch in str(name).lower() if ch.isalnum()) == wanted:
            return int(row_id)
    sys.exit(f"No {wanted!r} row in {table}; the status catalogue is not seeded.")


def provision() -> dict:
    from app.core.config import settings
    from app.core.security import create_access_token

    engine, database_url = _guarded_engine()
    stamp = time.strftime("%Y%m%d%H%M%S")
    org = settings.DEFAULT_ORGANIZATION_ID
    with engine.begin() as conn:
        employee_id = _insert_user(conn, org, stamp, "employee", "employee", EMPLOYEE_PERMISSIONS)
        admin_id = _insert_user(conn, org, stamp, "admin", "administrator", ADMIN_PERMISSIONS)
        active_status = _status_id(conn, "project_statuses", "active")
        todo_status = _status_id(conn, "task_statuses", "todo")
        # Project A exists before the desktop opens: the "loads automatically
        # on startup" case. Led by the admin, staffed with the employee.
        project_id = conn.execute(
            text(
                """
                INSERT INTO projects (organization_id, project_name, created_by, status,
                                      status_id, leader_id)
                VALUES (:org, :name, :creator, 'active', :status_id, :leader)
                RETURNING id
                """
            ),
            {"org": org, "name": f"E2E sync A {stamp}", "creator": admin_id,
             "status_id": active_status, "leader": admin_id},
        ).scalar_one()
        task_id = conn.execute(
            text(
                """
                INSERT INTO tasks (organization_id, project_id, task_name, created_by, status, status_id)
                VALUES (:org, :project, :name, :creator, 'todo', :status_id)
                RETURNING id
                """
            ),
            {"org": org, "project": project_id, "name": f"E2E sync task A1 {stamp}",
             "creator": admin_id, "status_id": todo_status},
        ).scalar_one()
        conn.execute(
            text(
                """
                INSERT INTO project_members (organization_id, project_id, user_id, created_by)
                VALUES (:org, :project, :user, :user)
                ON CONFLICT ON CONSTRAINT uq_project_member DO NOTHING
                """
            ),
            {"org": org, "project": project_id, "user": employee_id},
        )

    return {
        "stamp": stamp,
        "organization_id": org,
        "employee": {
            "user_id": employee_id,
            "token": create_access_token({"user_id": employee_id}, expires_delta=timedelta(hours=2)),
            "profile": {"id": employee_id, "role_name": "employee",
                        "name": f"E2E Sync employee {stamp}",
                        "email": f"e2e_sync_{stamp}_employee@{EMAIL_DOMAIN}"},
        },
        "admin": {
            "user_id": admin_id,
            "token": create_access_token({"user_id": admin_id}, expires_delta=timedelta(hours=2)),
        },
        "project_id": project_id,
        "task_id": task_id,
        "active_status_id": active_status,
        "todo_status_id": todo_status,
        "database_url": database_url,
    }


def cleanup(fixture: dict) -> None:
    engine, _ = _guarded_engine()
    employee_id = int(fixture["employee"]["user_id"])
    admin_id = int(fixture["admin"]["user_id"])
    with engine.begin() as conn:
        for user_id in (employee_id, admin_id):
            email = conn.execute(
                text("SELECT email FROM users WHERE id = :id"), {"id": user_id}
            ).scalar()
            if not email or not email.endswith(f"@{EMAIL_DOMAIN}"):
                sys.exit(f"Refusing to clean up user {user_id}: not an E2E principal.")
        # Every row the run created, in foreign-key-safe order. Projects made
        # through the API carry the admin as creator; their default tasks
        # and members hang off them.
        ids = {"e": employee_id, "a": admin_id}
        conn.execute(text("DELETE FROM time_entries WHERE user_id IN (:e, :a)"), ids)
        conn.execute(text(
            "DELETE FROM task_assignees WHERE user_id IN (:e, :a) OR task_id IN "
            "(SELECT id FROM tasks WHERE created_by IN (:e, :a) OR project_id IN "
            "(SELECT id FROM projects WHERE created_by IN (:e, :a)))"
        ), ids)
        conn.execute(text(
            "DELETE FROM tasks WHERE created_by IN (:e, :a) OR project_id IN "
            "(SELECT id FROM projects WHERE created_by IN (:e, :a))"
        ), ids)
        conn.execute(text(
            "DELETE FROM project_members WHERE user_id IN (:e, :a) OR project_id IN "
            "(SELECT id FROM projects WHERE created_by IN (:e, :a))"
        ), ids)
        conn.execute(text("DELETE FROM projects WHERE created_by IN (:e, :a)"), ids)
        conn.execute(text("DELETE FROM refresh_tokens WHERE user_id IN (:e, :a)"), ids)
        conn.execute(text("DELETE FROM users WHERE id IN (:e, :a)"), ids)


def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[1] == "provision":
        print(json.dumps(provision()))
        return 0
    if len(argv) >= 3 and argv[1] == "cleanup":
        cleanup(json.loads(argv[2]))
        print(json.dumps({"cleaned": True}))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))

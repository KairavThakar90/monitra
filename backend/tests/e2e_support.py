"""
Fixtures for the real-HTTP, real-database timing E2E suite.

Run from the backend directory, in the backend's own environment, by the
desktop E2E test (`desktop/tests/e2e/test_timing_lifecycle_e2e.py`), which
cannot import the backend package alongside the desktop's own `app` package.
Everything it needs -- a disposable principal, its project and task, a token
for it, and the database URL to verify rows against -- is printed as one JSON
document on stdout.

    python tests/e2e_support.py provision            -> {"user_id": ..., ...}
    python tests/e2e_support.py provision administrator
    python tests/e2e_support.py cleanup '<that json>'

Safety
------
* Runs only against the development database: the resolved URL must be the
  one `DATABASE_URL_DEV` names, and `ENV` must not be production. Anything
  else is a hard stop before a single row is written.
* The principal is an ordinary `employee` -- the least privilege that can
  track time -- never an administrator, and never anybody's real account.
  It is named `e2e_timing_<stamp>@e2e.invalid` (a reserved TLD) so it is
  unmistakable, and `cleanup` deletes it together with everything it wrote.
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


#: The one other role a suite may ask for. An administrator principal exists
#: so the maintenance-mode E2E can flip the switch through the real endpoint;
#: it is as disposable as the employee and is cleaned up the same way.
ADMINISTRATOR_PERMISSIONS = {
    **EMPLOYEE_PERMISSIONS,
    "projects:create": True,
    "time_entries:view_all": True,
    "view_employees": True,
    "manage_employees": True,
}

PROVISIONABLE_ROLES = {
    "employee": EMPLOYEE_PERMISSIONS,
    "administrator": ADMINISTRATOR_PERMISSIONS,
}


def provision(role: str = "employee") -> dict:
    from app.core.config import settings
    from app.core.security import create_access_token

    if role not in PROVISIONABLE_ROLES:
        sys.exit(f"Refusing to provision role {role!r}; one of {sorted(PROVISIONABLE_ROLES)}.")
    engine, database_url = _guarded_engine()
    stamp = time.strftime("%Y%m%d%H%M%S") + f"{int((time.time() % 1) * 1000):03d}"
    org = settings.DEFAULT_ORGANIZATION_ID
    with engine.begin() as conn:
        user_id = conn.execute(
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
                "username": f"e2e_timing_{stamp}",
                "email": f"e2e_timing_{stamp}@{EMAIL_DOMAIN}",
                "name": f"E2E Timing {stamp}",
                "role": role,
                "permissions": json.dumps(PROVISIONABLE_ROLES[role]),
            },
        ).scalar_one()
        project_id = conn.execute(
            text(
                """
                INSERT INTO projects (organization_id, project_name, created_by, status)
                VALUES (:org, :name, :creator, 'active')
                RETURNING id
                """
            ),
            {"org": org, "name": f"E2E timing {stamp}", "creator": user_id},
        ).scalar_one()
        task_id = conn.execute(
            text(
                """
                INSERT INTO tasks (organization_id, project_id, task_name, created_by, status)
                VALUES (:org, :project, :name, :creator, 'todo')
                RETURNING id
                """
            ),
            {"org": org, "project": project_id, "name": f"E2E timing task {stamp}",
             "creator": user_id},
        ).scalar_one()
        conn.execute(
            text(
                """
                INSERT INTO project_members (organization_id, project_id, user_id, created_by)
                VALUES (:org, :project, :user, :user)
                ON CONFLICT ON CONSTRAINT uq_project_member DO NOTHING
                """
            ),
            {"org": org, "project": project_id, "user": user_id},
        )
        conn.execute(
            text(
                """
                INSERT INTO task_assignees (task_id, user_id, assigned_by)
                VALUES (:task, :user, :user)
                """
            ),
            {"task": task_id, "user": user_id},
        )

    token = create_access_token({"user_id": user_id}, expires_delta=timedelta(hours=2))
    return {
        "user_id": user_id,
        "username": f"e2e_timing_{stamp}",
        "role": role,
        "organization_id": org,
        "project_id": project_id,
        "task_id": task_id,
        "token": token,
        "database_url": database_url,
    }


def cleanup(fixture: dict) -> None:
    engine, _ = _guarded_engine()
    user_id = int(fixture["user_id"])
    with engine.begin() as conn:
        email = conn.execute(
            text("SELECT email FROM users WHERE id = :id"), {"id": user_id}
        ).scalar()
        if not email or not email.endswith(f"@{EMAIL_DOMAIN}"):
            sys.exit(f"Refusing to clean up user {user_id}: not an E2E principal.")
        # Every row the principal wrote, in foreign-key-safe order. Deleting
        # the user cascades to time_entries and their satellites; the project
        # and task are removed explicitly because they were created by SQL,
        # not through the API, and carry no owner cascade.
        conn.execute(text("DELETE FROM time_entries WHERE user_id = :id"), {"id": user_id})
        conn.execute(text("DELETE FROM task_assignees WHERE user_id = :id"), {"id": user_id})
        conn.execute(text("DELETE FROM project_members WHERE user_id = :id"), {"id": user_id})
        conn.execute(text("DELETE FROM tasks WHERE id = :id"), {"id": int(fixture["task_id"])})
        conn.execute(text("DELETE FROM projects WHERE id = :id"), {"id": int(fixture["project_id"])})
        conn.execute(text("DELETE FROM refresh_tokens WHERE user_id = :id"), {"id": user_id})
        # An administrator principal may have flipped the maintenance switch:
        # its audit rows go with it, and the switch is left off.
        conn.execute(text("DELETE FROM activity_logs WHERE user_id = :id"), {"id": user_id})
        conn.execute(
            text(
                "UPDATE system_settings SET value = '{\"enabled\": false}'::jsonb, "
                "updated_by_user_id = NULL, updated_by_username = NULL "
                "WHERE key = 'maintenance_mode' AND updated_by_user_id = :id"
            ),
            {"id": user_id},
        )
        conn.execute(text("DELETE FROM users WHERE id = :id"), {"id": user_id})


def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[1] == "provision":
        print(json.dumps(provision(argv[2] if len(argv) >= 3 else "employee")))
        return 0
    if len(argv) >= 3 and argv[1] == "cleanup":
        cleanup(json.loads(argv[2]))
        print(json.dumps({"cleaned": True}))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))

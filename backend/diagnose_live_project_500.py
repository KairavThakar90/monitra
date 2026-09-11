#!/usr/bin/env python
"""Read-only: why does POST /projects return 500 on the live deployment?

Writes nothing. Every statement is a SELECT. It prints which database it
resolved before touching it, so there is no doubt about what was inspected.

Run it against the SAME database the live deployment uses. On Vercel that is
whatever DATABASE_URL is set to in the project's environment variables, so copy
that value here rather than assuming backend/.env matches::

    cd backend
    DATABASE_URL="<the value from Vercel>" python diagnose_live_project_500.py

For comparison, run it against the database your local app uses, where project
creation works::

    cd backend
    DATABASE_URL="<DATABASE_URL_DEV from backend/.env>" python diagnose_live_project_500.py

The difference between the two outputs is the bug.

What it checks, in the order `ProjectManagementService.create` would hit them:

1. alembic_version -- expected head is d9b3f1a72c40. A database behind
   472c3616ebd5 cannot store status 'pending' or 'todo'.
2. project_statuses / task_statuses contents -- ids AND names. Before the
   name-based fix, an id outside 1-4 (or 1-3) raised a bare KeyError, which is
   an unhandled exception and therefore a 500.
3. The columns `create` writes on projects, project_members and tasks. A column
   the migrations never added is a ProgrammingError at INSERT time.
4. The projects_status_check constraint, which decides whether the legacy
   status string the service derives can actually be stored.
"""
from __future__ import annotations

import os
import sys

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from sqlalchemy import text

from app.core.database import describe_url, get_database_url, get_session_local

EXPECTED_HEAD = "d9b3f1a72c40"

# Every column ProjectManagementService.create writes, per table.
WRITTEN = {
    "projects": [
        "organization_id", "project_name", "description", "status", "status_id",
        "leader_id", "deadline", "billing_type", "fixed_hours", "is_billable",
        "created_by",
    ],
    "project_members": ["project_id", "organization_id", "user_id", "created_by"],
    "tasks": [
        "organization_id", "project_id", "task_name", "status", "status_id",
        "created_by",
    ],
}


def main() -> int:
    url = get_database_url()
    print(f"Database: {describe_url(url)}\n")

    session = get_session_local()()
    try:
        print("1. Migration state")
        rows = session.execute(text("SELECT version_num FROM alembic_version")).scalars().all()
        for value in rows:
            mark = "OK" if value == EXPECTED_HEAD else f"BEHIND/AHEAD (expected {EXPECTED_HEAD})"
            print(f"   {value}  {mark}")
        if not rows:
            print("   NO ROWS -- alembic has never run against this database.")

        print("\n2. Status tables (create() maps these by name)")
        for table in ("project_statuses", "task_statuses"):
            try:
                found = session.execute(
                    text(f"SELECT id, name FROM {table} ORDER BY id")
                ).all()
            except Exception as exc:  # noqa: BLE001 - reporting, not handling
                print(f"   {table}: UNREADABLE -- {type(exc).__name__}: {exc}")
                continue
            if not found:
                print(f"   {table}: EMPTY -- create() cannot resolve a status.")
            for row in found:
                print(f"   {table}: id={row[0]!r} name={row[1]!r}")

        print("\n3. Columns create() writes")
        for table, columns in WRITTEN.items():
            present = {
                r[0] for r in session.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = :t"
                    ),
                    {"t": table},
                ).all()
            }
            if not present:
                print(f"   {table}: TABLE MISSING")
                continue
            missing = [c for c in columns if c not in present]
            print(f"   {table}: {'all present' if not missing else 'MISSING ' + ', '.join(missing)}")

        print("\n4. projects.status CHECK constraint")
        found = session.execute(
            text(
                "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conrelid = 'projects'::regclass AND contype = 'c'"
            )
        ).all()
        if not found:
            print("   none defined")
        for name, definition in found:
            print(f"   {name}: {definition}")
            for value in ("active", "pending", "todo", "completed"):
                if f"'{value}'" not in definition:
                    print(f"      NOTE: '{value}' is NOT accepted by this constraint.")

        print("\n5. Model columns missing from this database")
        # The decisive check. create() does not only INSERT -- it SELECTs User,
        # TaskStatus, ProjectMember and Task through the ORM, and a SELECT names
        # every column the model declares. One column the migrations never added
        # is a ProgrammingError on a plain read: an unhandled exception, so a 500.
        import app.models  # noqa: F401 - registers every mapper
        from app.core.database import Base
        drift = False
        for table_name, table in sorted(Base.metadata.tables.items()):
            present = {
                r[0] for r in session.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = :t"
                    ),
                    {"t": table_name},
                ).all()
            }
            if not present:
                print(f"   {table_name}: TABLE MISSING ENTIRELY")
                drift = True
                continue
            missing = [c.name for c in table.columns if c.name not in present]
            if missing:
                print(f"   {table_name}: MISSING {', '.join(missing)}")
                drift = True
        if not drift:
            print("   none -- every model column exists.")

        print("6. Seeded task status named Todo (create() stamps default tasks with it)")
        found = session.execute(text("SELECT id, name FROM task_statuses")).all()
        todo = [r for r in found if "".join(ch for ch in r[1].lower() if ch.isalnum()) == "todo"]
        print(f"   {'found: ' + repr(todo) if todo else 'NONE -- create() raises 500 by design here.'}")
    finally:
        session.close()

    print("\nRead-only: nothing was written.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

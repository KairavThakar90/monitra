"""
A real PostgreSQL for the tests that cannot be answered by a mock: partial
unique indexes, row locks, `now()` arithmetic, compare-and-set under two
connections.

Opt-in: set ``MONITRA_PG_TEST_URL`` to a *loopback* database you do not mind
losing, e.g. a throwaway cluster::

    initdb -D pgdata -U postgres --auth=trust
    pg_ctl -D pgdata -o "-p 58432" start
    psql -h 127.0.0.1 -p 58432 -U postgres -c "create database monitra_test"
    set MONITRA_PG_TEST_URL=postgresql+psycopg2://postgres@127.0.0.1:58432/monitra_test

The tests TRUNCATE every table they touch. They refuse any host that is not
loopback, so they can never be pointed at the shared development database or
at production (the same guard `tests/load/seed_load_users.py` has).

Alembic cannot build a database from scratch here (the first migration expects
an `organizations` table that predates it), so the schema is built from the
models, the stub `organizations` table gets the two columns the real one has,
and organization 1 is inserted.
"""
from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Optional
from urllib.parse import urlparse

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

LOOPBACK = {"127.0.0.1", "localhost", "::1"}
_TRUNCATE = (
    "time_entry_reconciliations, time_entry_idle_periods, time_entry_adjustments, "
    "time_entry_activity, time_entry_app_usage, time_entry_screenshots, wfpm_timer_events, "
    "time_entries, activity_logs, tasks, projects, users"
)


def pg_url() -> Optional[str]:
    url = os.environ.get("MONITRA_PG_TEST_URL")
    if not url:
        return None
    host = urlparse(url).hostname
    if host not in LOOPBACK:
        raise RuntimeError(
            f"MONITRA_PG_TEST_URL points at {host!r}; these tests truncate tables and only "
            "run against a loopback database."
        )
    return url


def engine_for(url: str) -> Engine:
    engine = create_engine(url, pool_size=8, max_overflow=8)
    with engine.begin() as conn:
        exists = conn.execute(text("select to_regclass('public.time_entries')")).scalar()
        if exists is None:
            from app.core.database import Base
            import app.main  # noqa: F401  (registers every model)

            Base.metadata.create_all(conn)
            conn.execute(text("ALTER TABLE organizations ADD COLUMN IF NOT EXISTS name text DEFAULT 'Test Org'"))
            conn.execute(text("ALTER TABLE organizations ADD COLUMN IF NOT EXISTS slug text DEFAULT 'test-org'"))
        conn.execute(text("INSERT INTO organizations (id) VALUES (1) ON CONFLICT DO NOTHING"))
    return engine


def sessions(engine: Engine) -> sessionmaker:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=True)


def reset_and_seed(engine: Engine) -> SimpleNamespace:
    """Empty the timer tables and create one member, one project, one task."""
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {_TRUNCATE} RESTART IDENTITY CASCADE"))
        conn.execute(text(
            "INSERT INTO users (organization_id, username, email, name, role_name, capture_frequency) "
            "VALUES (1, 'kunal', 'kunal@example.invalid', 'Kunal Vaghela', 'employee', 3)"
        ))
        user_id = conn.execute(text("select id from users where username = 'kunal'")).scalar()
        conn.execute(text(
            "INSERT INTO projects (organization_id, project_name, created_by) VALUES (1, 'Coach Companion', :u)"
        ), {"u": user_id})
        project_id = conn.execute(text("select id from projects")).scalar()
        conn.execute(text(
            "INSERT INTO tasks (organization_id, project_id, task_name, created_by) "
            "VALUES (1, :p, 'Mobile Application Development', :u)"
        ), {"p": project_id, "u": user_id})
        task_id = conn.execute(text("select id from tasks")).scalar()
        conn.execute(text(
            "INSERT INTO tasks (organization_id, project_id, task_name, created_by) "
            "VALUES (1, :p, 'Second task', :u)"
        ), {"p": project_id, "u": user_id})
        task2_id = conn.execute(text("select id from tasks where task_name = 'Second task'")).scalar()
    return SimpleNamespace(org=1, user_id=user_id, project_id=project_id, task_id=task_id, task2_id=task2_id)


def user_object(db: Session, user_id: int):
    from app.models.user import User

    user = db.get(User, user_id)
    user.permissions = {}
    return user

#!/usr/bin/env python
"""Prove POST /projects succeeds against the development database.

Nothing persists. The session is joined to an outer transaction that is rolled
back at the end, so the commit inside ProjectManagementService.create lands on
a SAVEPOINT and disappears with it. Run it twice and the database is unchanged
both times.

    cd backend
    python verify_create_on_dev_db.py                 # dev DB (ENV=development)
    DATABASE_URL="<other URL>" python verify_create_on_dev_db.py

The point: live and local run identical code -- the deployed OpenAPI is
byte-identical to the locally generated one -- so if creation succeeds here and
500s in production, the difference is the database, not the build.
"""
from __future__ import annotations

import os
import sys
from datetime import date, timedelta

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import describe_url, get_database_url, get_engine
from app.models.project_status import ProjectStatus
from app.models.user import User
from app.schemas.project_management import BillingType, ProjectCreate
from app.services.project_management import ProjectManagementService


def main() -> int:
    url = get_database_url()
    print(f"Database: {describe_url(url)}\n")

    exclude_creator = "--exclude-creator" in sys.argv
    leader_other = "--leader-other" in sys.argv
    print(f"Creator in member list: {not exclude_creator}")
    connection = get_engine().connect()
    outer = connection.begin()          # everything below lives inside this
    session = Session(bind=connection, join_transaction_mode="create_savepoint")
    try:
        actor = session.scalar(
            select(User).where(User.is_active.is_(True), User.role_name == "admin")
        )
        if actor is None:
            print("No active admin in this database; cannot exercise create().",
                  file=sys.stderr)
            return 2

        members = list(session.scalars(
            select(User)
            .where(User.organization_id == actor.organization_id,
                   User.is_active.is_(True))
            .limit(60)
        ).all())
        statuses = list(session.scalars(select(ProjectStatus).order_by(ProjectStatus.id)).all())

        from app.core.permissions import LEADER_ROLE_NAMES
        leader_id = next((m.id for m in members if m.id != actor.id and m.role_name in set(LEADER_ROLE_NAMES)), actor.id) if leader_other else actor.id
        print(f"Actor    : {actor.role_name} id={actor.id} in organization {actor.organization_id}")
        print(f"Leader   : id={leader_id} ({'someone else' if leader_id != actor.id else 'the creator'})")
        print(f"Members  : {len(members)} active users offered to the project")
        print(f"Statuses : {[(s.id, s.name) for s in statuses]}\n")

        for status_row in statuses:
            payload = ProjectCreate(
                project_name=f"__verify__ {status_row.name}",
                description="Created and rolled back by verify_create_on_dev_db.py",
                status_id=status_row.id,
                leader_id=leader_id,
                employee_ids=[m.id for m in members if not exclude_creator or m.id != actor.id],
                deadline=date.today() + timedelta(days=20),
                billing_type=BillingType.free,
            )
            try:
                result = ProjectManagementService.create(session, actor, payload)
            except Exception as exc:  # noqa: BLE001 - this is the report
                print(f"  status {status_row.id} ({status_row.name}): FAILED -- "
                      f"{type(exc).__name__}: {exc}")
                continue
            print(f"  status {status_row.id} ({status_row.name}): created id={result['id']} "
                  f"status={result['status'].name!r} members={len(result['employees'])} "
                  f"tasks={len(result['tasks'])}")
    finally:
        session.close()
        outer.rollback()
        connection.close()

    print("\nRolled back: the database is exactly as it was.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

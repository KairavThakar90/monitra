"""The project list's member filter (`GET /projects?employee_ids=...`).

Runs against a real SQLite database, not a mocked session -- the same
reasoning `test_task_isolation.py` documents: the fix is a SQL `WHERE`
clause (`Project.id IN (SELECT project_id FROM project_members WHERE
user_id IN (...))`), and a MagicMock would happily return whatever list the
test handed it regardless of what the clause says. Only a real query can
show that an unstaffed project is actually excluded.
"""
import unittest
from datetime import datetime

from sqlalchemy import BigInteger, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session

from app.core.database import Base
from app.models.project import Project
from app.models.project_member import ProjectMember
from app.models.task import Task
from app.models.user import User
from app.services.project_management import ProjectManagementService
from tests.status_catalog_stub import rows, status_catalog

PROJECT_STATUSES = rows((1, "Active"))


def _sqlite_schema(engine, *models):
    """Create these models' tables on SQLite (see test_task_isolation.py)."""
    tables = [model.__table__ for model in models]
    stripped = []
    for table in tables:
        for column in table.columns:
            default = column.server_default
            if default is not None and "::" in str(getattr(default, "arg", "")):
                stripped.append((column, default))
                column.server_default = None
    try:
        Base.metadata.create_all(engine, tables=tables)
    finally:
        for column, default in stripped:
            column.server_default = default


@compiles(JSONB, "sqlite")
def _jsonb_is_json_on_sqlite(type_, compiler, **kw):
    return "JSON"


@compiles(BigInteger, "sqlite")
def _bigint_is_integer_on_sqlite(type_, compiler, **kw):
    return "INTEGER"


ORG = 1
ALICE = 101
BOB = 102
ADMIN = 1


def _user(user_id: int) -> User:
    return User(
        id=user_id, organization_id=ORG, username=f"u{user_id}", email=f"u{user_id}@example.com",
        name=f"User {user_id}", role_name="employee", permissions={}, status="active", is_active=True,
        idle_enabled=True, idle_minutes=5, capture_frequency=10,
    )


def _project(project_id: int, name: str) -> Project:
    return Project(
        id=project_id, organization_id=ORG, project_name=name, description="",
        status="active", status_id=1, leader_id=None, deadline=None,
        billing_type="free", fixed_hours=None, created_by=ADMIN,
        created_at=datetime(2026, 1, 1), updated_at=datetime(2026, 1, 1),
    )


def _admin() -> User:
    return User(
        id=ADMIN, organization_id=ORG, username="admin", email="admin@example.com",
        name="Admin", role_name="administrator", permissions={}, status="active", is_active=True,
        idle_enabled=True, idle_minutes=5, capture_frequency=10,
    )


class ProjectMemberFilterTests(unittest.TestCase):
    """Two projects, staffed with different people."""

    def setUp(self):
        self.engine = create_engine("sqlite://")
        _sqlite_schema(self.engine, Project, ProjectMember, User, Task)
        self.db = Session(self.engine)

        self.db.add_all([_user(ALICE), _user(BOB), _admin()])
        self.db.add_all([
            _project(1, "Alice's project"),
            _project(2, "Bob's project"),
            _project(3, "Both"),
        ])
        self.db.add_all([
            ProjectMember(organization_id=ORG, project_id=1, user_id=ALICE, created_by=ADMIN),
            ProjectMember(organization_id=ORG, project_id=2, user_id=BOB, created_by=ADMIN),
            ProjectMember(organization_id=ORG, project_id=3, user_id=ALICE, created_by=ADMIN),
            ProjectMember(organization_id=ORG, project_id=3, user_id=BOB, created_by=ADMIN),
        ])
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def _names(self, **kwargs) -> list[str]:
        with status_catalog(project_statuses=PROJECT_STATUSES):
            result = ProjectManagementService.list(
                self.db, _admin(), page=1, limit=20, search=None, status_id=None,
                leader_id=None, billing_type=None, include_tasks=False, **kwargs,
            )
        return sorted(p["project_name"] for p in result["items"])

    def test_no_filter_returns_every_project(self):
        self.assertEqual(self._names(), ["Alice's project", "Bob's project", "Both"])

    def test_one_member_returns_only_their_projects(self):
        self.assertEqual(self._names(employee_ids=[ALICE]), ["Alice's project", "Both"])

    def test_several_members_is_a_union_not_an_intersection(self):
        # Selecting both Alice and Bob must not require a project to have
        # *both* of them -- it is "staffed with any of these people".
        self.assertEqual(
            self._names(employee_ids=[ALICE, BOB]),
            ["Alice's project", "Bob's project", "Both"],
        )

    def test_a_member_staffed_on_nothing_returns_no_projects(self):
        self.assertEqual(self._names(employee_ids=[999]), [])

    def test_an_empty_list_is_the_same_as_no_filter(self):
        self.assertEqual(self._names(employee_ids=[]), ["Alice's project", "Bob's project", "Both"])


if __name__ == "__main__":
    unittest.main()

"""`ProjectManagementService.hours_summary` -- all-time tracked hours per
project, behind the Project Management page's Used/Remaining Hours columns.

Runs against a real SQLite database, not a mocked session -- the figure
being tested is a SQL SUM across `time_entries`, and a MagicMock would
happily return whatever total the test handed it regardless of the rows
actually in scope. See test_project_member_filter.py for the same reasoning.
"""
import unittest
from datetime import datetime, timezone

from sqlalchemy import BigInteger, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session

from app.core.database import Base
from app.models.manual_time_entry import ManualTimeEntry
from app.models.project import Project
from app.models.project_member import ProjectMember
from app.models.task import Task
from app.models.time_entry import TimeEntry
from app.models.time_entry_adjustment import TimeEntryAdjustment
from app.models.user import User
from app.services.project_management import ProjectManagementService


def _sqlite_schema(engine, *models):
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
OTHER_ORG = 2
ADMIN = 1
ALICE = 101


def _admin(organization_id=ORG) -> User:
    return User(
        id=ADMIN, organization_id=organization_id, username="admin", email="admin@example.com",
        name="Admin", role_name="administrator", permissions={}, status="active", is_active=True,
        idle_enabled=True, idle_minutes=5, capture_frequency=10,
    )


def _project(project_id: int, organization_id=ORG, billing_type="fixed", fixed_hours=None, status="active") -> Project:
    return Project(
        id=project_id, organization_id=organization_id, project_name=f"Project {project_id}", description="",
        status=status, status_id=1, leader_id=None, deadline=None,
        billing_type=billing_type, fixed_hours=fixed_hours, created_by=ADMIN,
        created_at=datetime(2026, 1, 1), updated_at=datetime(2026, 1, 1),
    )


def _entry(entry_id: int, project_id: int, seconds: int, organization_id=ORG, user_id=None, start=None, task_id=1) -> TimeEntry:
    """`time_entries` carries a unique index on `user_id` scoped to `end_time
    IS NULL` in Postgres; SQLAlchemy's `postgresql_where` is dropped on
    SQLite, so the index becomes unconditionally unique there. Each entry
    therefore gets its own user unless the test asks otherwise."""
    start = start or datetime(2026, 1, 1, tzinfo=timezone.utc)
    return TimeEntry(
        id=entry_id, organization_id=organization_id, user_id=user_id or (ALICE + entry_id), project_id=project_id, task_id=task_id,
        start_time=start, end_time=start, total_seconds=seconds, status="completed", is_manual=False,
        is_billable=True,
    )


class ProjectHoursSummaryTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        _sqlite_schema(
            self.engine, Project, ProjectMember, User, Task, TimeEntry,
            ManualTimeEntry, TimeEntryAdjustment,
        )
        self.db = Session(self.engine)
        self.db.add(_admin())
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def test_sums_tracked_seconds_for_a_project(self):
        self.db.add(_project(1, fixed_hours=10))
        self.db.add_all([_entry(1, 1, 3600), _entry(2, 1, 1800)])
        self.db.commit()

        [summary] = ProjectManagementService.hours_summary(self.db, _admin())
        self.assertEqual(summary["project_id"], 1)
        self.assertEqual(summary["total_used_seconds"], 5400)
        self.assertEqual(summary["total_used_hours"], 1.5)

    def test_project_with_no_time_entries_is_zero_not_missing(self):
        self.db.add(_project(1, billing_type="free"))
        self.db.commit()

        [summary] = ProjectManagementService.hours_summary(self.db, _admin())
        self.assertEqual(summary["total_used_seconds"], 0)
        self.assertEqual(summary["total_used_hours"], 0.0)

    def test_project_id_filter_narrows_the_result(self):
        self.db.add_all([_project(1), _project(2)])
        self.db.add_all([_entry(1, 1, 3600), _entry(2, 2, 7200)])
        self.db.commit()

        result = ProjectManagementService.hours_summary(self.db, _admin(), project_ids=[1])
        self.assertEqual([r["project_id"] for r in result], [1])

    def test_archived_projects_are_excluded(self):
        self.db.add(_project(1, status="archived"))
        self.db.add(_entry(1, 1, 3600))
        self.db.commit()

        self.assertEqual(ProjectManagementService.hours_summary(self.db, _admin()), [])

    def test_scoped_to_the_callers_organization(self):
        self.db.add(_project(1, organization_id=OTHER_ORG))
        self.db.add(_entry(1, 1, 3600, organization_id=OTHER_ORG))
        self.db.commit()

        self.assertEqual(ProjectManagementService.hours_summary(self.db, _admin()), [])

    def test_no_projects_in_scope_returns_an_empty_list(self):
        self.assertEqual(ProjectManagementService.hours_summary(self.db, _admin()), [])

    def test_time_on_default_tasks_counts_as_internal_not_used(self):
        """Time against the four seeded default tasks is Internal Hours;
        everything else is Used Hours; the grand total carries both."""
        self.db.add(_project(1, fixed_hours=10))
        self.db.add_all([
            Task(id=1, organization_id=ORG, project_id=1, task_name="Internal Discussion", created_by=ADMIN),
            Task(id=2, organization_id=ORG, project_id=1, task_name="Send Client Update", created_by=ADMIN),
            Task(id=3, organization_id=ORG, project_id=1, task_name="Build the feature", created_by=ADMIN),
        ])
        self.db.add_all([
            _entry(1, 1, 3600, task_id=1),   # internal
            _entry(2, 1, 1800, task_id=2),   # internal
            _entry(3, 1, 7200, task_id=3),   # real work
        ])
        self.db.commit()

        [summary] = ProjectManagementService.hours_summary(self.db, _admin())
        self.assertEqual(summary["internal_seconds"], 5400)
        self.assertEqual(summary["internal_hours"], 1.5)
        self.assertEqual(summary["total_used_seconds"], 7200)
        self.assertEqual(summary["total_used_hours"], 2.0)
        self.assertEqual(summary["total_tracked_seconds"], 12600)

    def test_the_split_survives_the_route_response_schema(self):
        """The route serialises through `ProjectHoursSummaryResponse`, which
        silently *drops* any field it does not declare -- exactly how the
        internal/used split reached the service but never the browser. This
        pushes the service payload through the real schema so a field added
        to one but not the other fails here instead of in production."""
        from app.schemas.project_management import ProjectHoursSummaryResponse

        self.db.add(_project(1, fixed_hours=10))
        self.db.add(Task(id=1, organization_id=ORG, project_id=1, task_name="Internal Discussion", created_by=ADMIN))
        self.db.add(_entry(1, 1, 3600, task_id=1))
        self.db.commit()

        payload = ProjectHoursSummaryResponse(
            items=ProjectManagementService.hours_summary(self.db, _admin())
        )
        item = payload.items[0]
        self.assertEqual(item.internal_seconds, 3600)
        self.assertEqual(item.internal_hours, 1.0)
        self.assertEqual(item.total_used_seconds, 0)
        self.assertEqual(item.total_tracked_seconds, 3600)

    def test_a_default_task_name_on_another_project_does_not_bleed_over(self):
        """The internal bucket is matched per project: a default-named task
        on project 2 must not classify project 1's time."""
        self.db.add_all([_project(1), _project(2)])
        self.db.add_all([
            Task(id=1, organization_id=ORG, project_id=2, task_name="Internal Discussion", created_by=ADMIN),
        ])
        self.db.add(_entry(1, 1, 3600, task_id=99))  # project 1, unknown task
        self.db.commit()

        summaries = {row["project_id"]: row for row in ProjectManagementService.hours_summary(self.db, _admin())}
        self.assertEqual(summaries[1]["total_used_seconds"], 3600)
        self.assertEqual(summaries[1]["internal_seconds"], 0)

    def test_started_at_is_the_earliest_tracked_session_not_the_project_created_date(self):
        # Project row created 2026-01-01 (see _project's default created_at),
        # but the first actual tracked session is two days later -- Started
        # must read that, not Project.created_at.
        self.db.add(_project(1))
        self.db.add_all([
            _entry(1, 1, 3600, start=datetime(2026, 1, 3, 9, 0, tzinfo=timezone.utc)),
            _entry(2, 1, 3600, start=datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc)),
        ])
        self.db.commit()

        # SQLite drops tz-awareness on read (unlike Postgres' real timestamptz),
        # so the comparison is against a naive instant here.
        [summary] = ProjectManagementService.hours_summary(self.db, _admin())
        self.assertEqual(summary["started_at"], datetime(2026, 1, 3, 9, 0))

    def test_started_at_is_none_when_nothing_has_ever_been_tracked(self):
        self.db.add(_project(1, billing_type="free"))
        self.db.commit()

        [summary] = ProjectManagementService.hours_summary(self.db, _admin())
        self.assertIsNone(summary["started_at"])


if __name__ == "__main__":
    unittest.main()

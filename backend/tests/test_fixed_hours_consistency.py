"""One definition of a fixed-hours project's Remaining, on every surface.

The authoritative definition (see `app.services.project_hours`):

    Internal  = time on the project's four default tasks
    Used      = every other task's time
    Total     = Used + Internal
    Remaining = fixed allocation - Used        (internal does NOT consume it)

The Dashboard billing card and the client Billing page once measured
Remaining against *all* tracked time, so a project with internal time showed
three different Remainings on three screens. These tests pin every surface to
the same numbers from the same real rows — a SQLite database, not a mocked
session, because the figures under test are SQL sums.

Fixture: a 10h fixed project with 3h on "Internal Discussion", 1h on "Send
Client Update" and 8h on a work task. Used 8h, Internal 4h, Total 12h,
Remaining +2h. Counting internal time would have said -2h.
"""
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models.manual_time_entry import ManualTimeEntry
from app.models.project import Project
from app.models.project_member import ProjectMember
from app.models.task import Task
from app.models.time_entry import TimeEntry
from app.models.time_entry_adjustment import TimeEntryAdjustment
from app.models.user import User
from app.services.project_hours import (
    DEFAULT_PROJECT_TASKS, all_time_project_hours, remaining_seconds,
)
from tests.test_project_hours_summary import (
    ADMIN, ORG, _admin, _entry, _project, _sqlite_schema,
)

HOUR = 3600
WORK_TASK = 30


def _seed(db, *, work_hours=8, fixed_hours=10, billing_type="fixed"):
    db.add(_project(1, fixed_hours=fixed_hours, billing_type=billing_type))
    db.add_all([
        Task(id=10, organization_id=ORG, project_id=1, task_name="Internal Discussion", created_by=ADMIN),
        Task(id=11, organization_id=ORG, project_id=1, task_name="Send Client Update", created_by=ADMIN),
        Task(id=WORK_TASK, organization_id=ORG, project_id=1, task_name="Build checkout", created_by=ADMIN),
    ])
    db.add_all([
        _entry(1, 1, 3 * HOUR, task_id=10),
        _entry(2, 1, 1 * HOUR, task_id=11),
        _entry(3, 1, work_hours * HOUR, task_id=WORK_TASK),
    ])
    db.commit()


class _Base(unittest.TestCase):
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

    # -- each surface, read exactly as its screen reads it --------------------

    def project_management(self):
        """Project Management: hours-summary Used, and the table's Remaining
        cell (allocation - Used)."""
        from app.services.project_management import ProjectManagementService

        [row] = ProjectManagementService.hours_summary(self.db, _admin())
        project = self.db.get(Project, 1)
        return {
            "used": row["total_used_seconds"],
            "internal": row["internal_seconds"],
            "total": row["total_tracked_seconds"],
            "remaining": remaining_seconds(project.billing_type, project.fixed_hours, row["total_used_seconds"]),
        }

    def dashboard(self):
        """Dashboard billing card: the service's item for this project."""
        from app.react_apis.dashboard.repository import DashboardRepository
        from app.react_apis.dashboard.service import DashboardService
        from app.react_apis.reports_page.repository import ReportFilters

        # The card's *range* half (tracked hours this range) is not what is
        # under test and its entry-grain SQL is Postgres-only; the all-time
        # half is computed for real.
        real = DashboardRepository.billing_progress

        def rows(db, filters):
            project = db.get(Project, 1)
            split = all_time_project_hours(db, ORG, [1])[1]
            return [{"project": project, "completed_seconds": split.used_seconds,
                     "internal_seconds": split.internal_seconds,
                     "tracked_seconds": 0, "avg_activity": None}]

        # Prove the repository really does read the shared function before
        # standing in for it.
        with patch("app.react_apis.dashboard.repository.all_time_project_hours",
                   wraps=all_time_project_hours) as shared, \
                patch("app.react_apis.dashboard.repository.ReportsPageRepository.entry_grain_subquery",
                      side_effect=RuntimeError("range half not under test")):
            with self.assertRaises(RuntimeError):
                real(self.db, SimpleNamespace(organization_id=ORG, project_ids=()))
        self.assertTrue(shared.called, "the dashboard must read the shared calculation")

        with patch.object(DashboardRepository, "billing_progress", side_effect=rows):
            result = DashboardService.billing_progress(self.db, ReportFilters(
                organization_id=ORG, start_date=datetime(2026, 1, 1).date(), end_date=datetime(2026, 1, 1).date(),
                start_time=datetime(2026, 1, 1, tzinfo=timezone.utc), end_time=datetime(2026, 1, 2, tzinfo=timezone.utc),
            ))
        items = result["billable_projects"] + result["internal_projects"]
        [item] = [item for item in items if item["project_id"] == 1]
        return {
            "used": item["completed_seconds"],
            "internal": item["internal_seconds"],
            "total": item["completed_seconds"] + item["internal_seconds"],
            "remaining": item["remaining_seconds"],
        }

    def client_billing(self):
        """Client portal Billing page: the project item's used/remaining."""
        from app.services.client_portal_service import ClientPortalService

        client = SimpleNamespace(
            id=1, organization_id=ORG, share_billing=True, share_member_details=False,
            share_timing=True, share_tasks=True,
        )
        with patch.object(ClientPortalService, "_client_for", return_value=client), \
                patch.object(ClientPortalService, "_shared_project_ids", return_value=[1]), \
                patch("app.services.client_portal_service._permissions_payload", return_value={}):
            result = ClientPortalService.list_billing(self.db, _admin())
        if not result["items"]:
            return None
        [item] = result["items"]
        remaining_hours = item["remaining_hours"]
        return {
            "used": item["used_seconds"],
            "internal": item["internal_seconds"],
            "total": item["used_seconds"] + item["internal_seconds"],
            # The client payload carries hours at 2dp; convert back exactly.
            "remaining": None if remaining_hours is None else round(remaining_hours * HOUR),
        }


class TestEverySurfaceAgrees(_Base):

    def test_the_split_and_the_remaining_are_identical_on_every_surface(self):
        _seed(self.db)
        expected = {"used": 8 * HOUR, "internal": 4 * HOUR, "total": 12 * HOUR, "remaining": 2 * HOUR}
        for name, reader in (("Project Management", self.project_management),
                             ("Dashboard billing", self.dashboard),
                             ("Client Billing", self.client_billing)):
            with self.subTest(surface=name):
                self.assertEqual(reader(), expected)

    def test_internal_time_does_not_consume_the_fixed_budget(self):
        """The regression itself: with internal counted, Remaining would be -2h."""
        _seed(self.db)
        for reader in (self.project_management, self.dashboard, self.client_billing):
            with self.subTest(surface=reader.__name__):
                self.assertEqual(reader()["remaining"], 2 * HOUR)
                self.assertNotEqual(reader()["remaining"], -2 * HOUR)

    def test_an_overspend_is_negative_everywhere_and_never_clamped(self):
        _seed(self.db, work_hours=11)   # 11h used of 10h
        for reader in (self.project_management, self.dashboard, self.client_billing):
            with self.subTest(surface=reader.__name__):
                self.assertEqual(reader()["remaining"], -1 * HOUR)

    def test_a_flexible_project_has_no_remaining_anywhere(self):
        _seed(self.db, billing_type="free", fixed_hours=None)
        self.assertIsNone(self.project_management()["remaining"])
        self.assertIsNone(self.dashboard()["remaining"])
        # Client Billing lists fixed projects only; a flexible one is absent.
        self.assertIsNone(self.client_billing())


class TestTheSharedDefinition(_Base):

    def test_used_plus_internal_is_total(self):
        _seed(self.db)
        split = all_time_project_hours(self.db, ORG, [1])[1]
        self.assertEqual(split.used_seconds + split.internal_seconds, split.total_seconds)

    def test_the_four_default_tasks_are_the_internal_ones(self):
        self.assertEqual(set(DEFAULT_PROJECT_TASKS), {
            "Project Setup / Understanding", "Review Client Update",
            "Send Client Update", "Internal Discussion",
        })

    def test_project_creation_still_seeds_exactly_those_tasks(self):
        from app.services import project_management

        self.assertIs(project_management.DEFAULT_PROJECT_TASKS, DEFAULT_PROJECT_TASKS)

    def test_a_zero_or_missing_allocation_has_no_remaining(self):
        self.assertIsNone(remaining_seconds("fixed", None, 100))
        self.assertIsNone(remaining_seconds("fixed", 0, 100))
        self.assertIsNone(remaining_seconds("free", 10, 100))
        self.assertEqual(remaining_seconds("fixed", 10, 11 * HOUR), -HOUR)

    def test_every_surface_imports_the_shared_calculation(self):
        """Structural: a future 'quick fix' that recomputes Used inline on one
        screen fails here rather than drifting silently."""
        import inspect

        from app.react_apis.dashboard import repository as dashboard_repository
        from app.react_apis.dashboard import service as dashboard_service
        from app.services import client_portal_service, project_management

        self.assertIn("all_time_project_hours", inspect.getsource(project_management.ProjectManagementService.hours_summary))
        self.assertIn("all_time_project_hours", inspect.getsource(dashboard_repository.DashboardRepository.billing_progress))
        self.assertIn("remaining_seconds", inspect.getsource(dashboard_service.DashboardService.billing_progress))
        self.assertIn("all_time_project_hours", inspect.getsource(client_portal_service.ClientPortalService.list_billing))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

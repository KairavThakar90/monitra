"""Which *projects* a leader may see, and which leader a leader may assign.

The companion to ``test_leader_scope.py``. That file pins whose recorded work a
leader may read; this one pins which projects and tasks that leader may open,
and the two have to agree -- a leader whose member directory is one team while
their project list is the whole company has been told two different things
about their own authority.

Four rules, each one wrong before:

1. **A leader's project list is their own projects** -- the ones they lead plus
   any they were staffed onto. It used to be every project in the organization
   on ``/api/v1/projects``, and, on the older ``/projects`` route, an
   unconditional empty list: that ``else`` branch named admins and employees and
   returned ``[]`` for everybody it did not name.
2. **Their tasks follow their projects.** The Task Listing screen is answered by
   ``/reports/project-task-summary``, which paged through every project in the
   organization for anyone holding ``time_entries:view_all``.
3. **A leader leads what they create.** ``create`` pins ``leader_id`` to the
   caller and ``update`` keeps whoever owns the project, so a leader can neither
   hand a project away -- and lose sight of it the moment they save -- nor take
   over somebody else's.
4. **Staffing is still not scoped.** ``/projects/assignable-employees`` offers
   the whole organization, exactly as ``member_scope`` promises; it is
   ``assignable-leaders`` that narrows, because there is only one leader a
   leader may pick.
"""

import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

from app.api.project_management import assignable_employees, assignable_leaders
from app.core.permissions import ROLE_PERMISSIONS
from app.schemas.project_management import BillingType, ProjectCreate, ProjectUpdate
from app.services.project import ProjectService
from app.services.project_management import ProjectManagementService
from app.services.project_scope import may_view_project, visible_project_ids
from app.services.reports import ReportsService
from app.services.task import TaskService
from app.services.time_entry_screenshot import TimeEntryScreenshotService

LEADER_ID = 42
LED = {11, 12}
STAFFED = {13}
OUTSIDER_PROJECT = 99


def _leader(role="leader"):
    return SimpleNamespace(
        id=LEADER_ID, organization_id=1, role_name=role, name="Lee", email="lee@example.com",
        permissions={p: True for p in ROLE_PERMISSIONS[role]},
    )


def _admin():
    return SimpleNamespace(
        id=1, organization_id=1, role_name="administrator", name="Ada", email="ada@example.com",
        permissions={p: True for p in ROLE_PERMISSIONS["administrator"]},
    )


def _db_returning(*batches):
    """A session whose successive ``scalars()`` calls yield the given batches.

    ``visible_project_ids`` asks twice -- projects led, then projects staffed
    onto -- so the two are supplied separately and a test can tell the union
    apart from either half.
    """
    db = MagicMock()
    db.scalars.return_value.all.side_effect = [list(batch) for batch in batches]
    return db


def _scope_db():
    return _db_returning(LED, STAFFED)


class ProjectScopeTests(unittest.TestCase):
    def test_an_admin_is_unrestricted(self):
        self.assertIsNone(visible_project_ids(MagicMock(), _admin()))
        self.assertTrue(may_view_project(MagicMock(), _admin(), OUTSIDER_PROJECT))

    def test_a_leaders_scope_is_what_they_lead_plus_what_they_are_staffed_onto(self):
        self.assertEqual(visible_project_ids(_scope_db(), _leader()), LED | STAFFED)

    def test_both_leader_spellings_are_scoped(self):
        self.assertEqual(visible_project_ids(_scope_db(), _leader("project_leader")), LED | STAFFED)

    def test_a_leader_with_no_projects_sees_none(self):
        self.assertEqual(visible_project_ids(_db_returning([], []), _leader()), set())

    def test_no_session_narrows_rather_than_widens(self):
        # Matching visible_member_ids: a missing session must never turn a
        # leader into an org-wide reader.
        self.assertEqual(visible_project_ids(None, _leader()), set())
        self.assertIsNone(visible_project_ids(None, _admin()))

    def test_may_view_project_follows_the_scope(self):
        self.assertTrue(may_view_project(_scope_db(), _leader(), 11))
        self.assertTrue(may_view_project(_scope_db(), _leader(), 13))
        self.assertFalse(may_view_project(_scope_db(), _leader(), OUTSIDER_PROJECT))


class ProjectReadTests(unittest.TestCase):
    """/api/v1/projects -- the list and the single-project routes behind it."""

    def _project_db(self, project, *batches):
        db = MagicMock()
        db.scalar.return_value = project
        db.scalars.return_value.all.side_effect = [list(batch) for batch in batches]
        return db

    def test_a_leader_cannot_open_somebody_elses_project(self):
        project = SimpleNamespace(id=OUTSIDER_PROJECT, organization_id=1, status="active")
        db = self._project_db(project, LED, STAFFED)
        with self.assertRaises(HTTPException) as error:
            ProjectManagementService._project(db, OUTSIDER_PROJECT, _leader())
        # 404, not 403: the existence of another team's project is not this
        # caller's to learn.
        self.assertEqual(error.exception.status_code, 404)

    def test_a_leader_can_open_their_own_project(self):
        project = SimpleNamespace(id=11, organization_id=1, status="active")
        db = self._project_db(project, LED, STAFFED)
        self.assertIs(ProjectManagementService._project(db, 11, _leader()), project)

    def test_an_admin_opens_any_project_without_a_scope_query(self):
        project = SimpleNamespace(id=OUTSIDER_PROJECT, organization_id=1, status="active")
        db = MagicMock()
        db.scalar.return_value = project
        self.assertIs(ProjectManagementService._project(db, OUTSIDER_PROJECT, _admin()), project)
        db.scalars.assert_not_called()

    def test_the_leaders_list_is_filtered_and_the_admins_is_not(self):
        for user, expect_scope_query in ((_leader(), True), (_admin(), False)):
            with self.subTest(role=user.role_name):
                db = MagicMock()
                db.scalar.return_value = 0
                db.scalars.return_value.all.side_effect = [list(LED), list(STAFFED), []]
                with patch.object(ProjectManagementService, "_detail_payloads", return_value=[]):
                    ProjectManagementService.list(db, user, 1, 20, None, None, None, None)
                # The scope query runs only for the role that is scoped: one
                # scalars() call for the page itself, three for a leader.
                self.assertEqual(db.scalars.call_count > 1, expect_scope_query)


class LegacyProjectRouteTests(unittest.TestCase):
    """The older /projects routes, which the desktop client also reaches."""

    def test_a_leaders_list_is_no_longer_empty(self):
        db = MagicMock()
        db.scalars.return_value.all.side_effect = [list(LED), list(STAFFED), ["p11", "p12", "p13"]]
        self.assertEqual(ProjectService.list_projects(db, _leader()), ["p11", "p12", "p13"])

    def test_a_leader_cannot_fetch_a_project_outside_their_scope(self):
        project = SimpleNamespace(id=OUTSIDER_PROJECT, organization_id=1)
        db = _db_returning(LED, STAFFED)
        with patch("app.services.project.ProjectRepository.get_by_id", return_value=project):
            with self.assertRaises(HTTPException) as error:
                ProjectService.get_project(db, OUTSIDER_PROJECT, _leader())
        self.assertEqual(error.exception.status_code, 404)

    def test_a_leader_can_fetch_their_own_project(self):
        project = SimpleNamespace(id=11, organization_id=1)
        db = _db_returning(LED, STAFFED)
        with patch("app.services.project.ProjectRepository.get_by_id", return_value=project):
            self.assertIs(ProjectService.get_project(db, 11, _leader()), project)


class LegacyTaskListingTests(unittest.TestCase):
    """A leader's tasks, on the /projects/{id}/tasks route the desktop reads."""

    def _tasks(self, user, tasks):
        db = MagicMock()
        db.scalars.return_value.all.side_effect = [list(tasks), []]
        with patch("app.services.task.ProjectService.get_project"):
            return TaskService.list_tasks(db, 11, user)

    def test_a_leaders_project_is_no_longer_taskless(self):
        # The `else` branch returned [] for every role it did not name, so a
        # leader opened their own project and found nothing in it.
        task = SimpleNamespace(id=5, assignees=None)
        self.assertEqual(self._tasks(_leader(), [task]), [task])

    def test_an_admin_still_sees_the_projects_tasks(self):
        task = SimpleNamespace(id=5, assignees=None)
        self.assertEqual(self._tasks(_admin(), [task]), [task])


class ScreenshotScopeTests(unittest.TestCase):
    """Screenshots are somebody's recorded work, so they take the member scope."""

    def test_a_leaders_listing_is_restricted_to_their_team(self):
        db = MagicMock()
        db.scalars.return_value.all.return_value = [7, 8]
        with patch("app.services.time_entry_screenshot.TimeEntryScreenshotRepository.list_screenshots",
                   return_value=[]) as listed:
            TimeEntryScreenshotService.list_screenshots(db, None, None, 100, _leader())
        self.assertEqual(listed.call_args.kwargs["user_ids"], {7, 8, LEADER_ID})

    def test_an_admins_listing_is_unrestricted(self):
        with patch("app.services.time_entry_screenshot.TimeEntryScreenshotRepository.list_screenshots",
                   return_value=[]) as listed:
            TimeEntryScreenshotService.list_screenshots(MagicMock(), None, None, 100, _admin())
        self.assertIsNone(listed.call_args.kwargs["user_ids"])


class LeaderAssignmentTests(unittest.TestCase):
    """Who ends up leading a project the caller creates or edits."""

    def _create(self, user):
        payload = ProjectCreate(
            project_name="Nova", description=None, status_id=1, leader_id=999,
            employee_ids=[], deadline=date(2099, 1, 1), billing_type=BillingType.free,
            fixed_hours=None,
        )
        # Stop at validation: the argument it is handed is the whole assertion.
        with patch.object(ProjectManagementService, "_validate_project_fields",
                          side_effect=HTTPException(418, "stop")) as validated:
            with self.assertRaises(HTTPException):
                ProjectManagementService.create(MagicMock(), user, payload)
        # _validate_project_fields(db, user, status_id, leader_id, ...)
        return validated.call_args.args[3]

    def test_a_leader_leads_the_project_they_create(self):
        self.assertEqual(self._create(_leader()), LEADER_ID)

    def test_the_project_leader_spelling_is_pinned_too(self):
        self.assertEqual(self._create(_leader("project_leader")), LEADER_ID)

    def test_an_admin_assigns_whoever_they_chose(self):
        self.assertEqual(self._create(_admin()), 999)

    def _update(self, user):
        project = SimpleNamespace(id=11, organization_id=1, status="active", status_id=1,
                                  leader_id=500, billing_type="free", fixed_hours=None,
                                  deadline=None)
        db = MagicMock()
        db.scalar.return_value = project
        # A leader's update spends two scalars() calls on the scope lookup
        # before the third, which reads the project's current members; an
        # admin's goes straight to that third one.
        batches = [list(LED), list(STAFFED), []] if user.role_name == "leader" else [[]]
        db.scalars.return_value.all.side_effect = batches
        with patch.object(ProjectManagementService, "_validate_project_fields",
                          side_effect=HTTPException(418, "stop")) as validated:
            with self.assertRaises(HTTPException):
                ProjectManagementService.update(db, user, 11, ProjectUpdate(leader_id=999))
        return validated.call_args.args[3]

    def test_a_leader_cannot_hand_their_project_to_somebody_else(self):
        self.assertEqual(self._update(_leader()), 500)

    def test_an_admin_may_reassign_a_project(self):
        self.assertEqual(self._update(_admin()), 999)

    def test_the_leader_picker_offers_a_leader_only_themselves(self):
        offered = assignable_leaders(None, _leader(), MagicMock())
        self.assertEqual([item["id"] for item in offered], [LEADER_ID])

    def test_the_leader_picker_is_a_query_for_everybody_else(self):
        db = MagicMock()
        db.scalars.return_value.all.return_value = []
        assignable_leaders(None, _admin(), db)
        db.scalars.assert_called_once()


class TaskSummaryScopeTests(unittest.TestCase):
    """/reports/project-task-summary -- the Task Listing screen."""

    def _summary(self, user, project_ids):
        db = MagicMock()
        db.scalars.return_value.all.side_effect = [list(LED), list(STAFFED)]
        with patch("app.services.reports.ReportsRepository.existing_project_ids",
                   side_effect=lambda _db, _org, ids: set(ids)), \
             patch("app.services.reports.ReportsRepository.paginated_projects",
                   return_value=([], 0)) as paged, \
             patch("app.services.reports.ReportsRepository.session_seconds_by", return_value={}), \
             patch("app.services.reports.ReportsRepository.active_tasks_by_project", return_value={}), \
             patch("app.services.reports.ReportsRepository.project_statuses_lookup", return_value={}):
            ReportsService.build_project_task_summary(db, user, 1, 5, project_ids, None, None, None)
        return paged.call_args.args[2]

    def test_an_unfiltered_leader_pages_through_their_own_projects(self):
        self.assertEqual(self._summary(_leader(), None), sorted(LED | STAFFED))

    def test_a_project_filter_is_intersected_with_the_scope(self):
        self.assertEqual(self._summary(_leader(), [11, OUTSIDER_PROJECT]), [11])

    def test_asking_only_for_somebody_elses_project_returns_an_empty_page(self):
        # [] must reach the repository as "none of them" rather than decaying
        # into "no filter" -- that decay would make the whole scope a no-op.
        self.assertEqual(self._summary(_leader(), [OUTSIDER_PROJECT]), [])

    def test_an_admin_is_unfiltered(self):
        self.assertIsNone(self._summary(_admin(), None))


class ScopedFilterHelperTests(unittest.TestCase):
    def test_no_restriction_leaves_the_request_alone(self):
        self.assertIsNone(ReportsService._scoped(None, None))
        self.assertEqual(ReportsService._scoped([9], None), [9])

    def test_an_absent_filter_becomes_the_scope(self):
        self.assertEqual(ReportsService._scoped(None, {2, 1}), [1, 2])

    def test_a_supplied_filter_is_intersected_and_may_be_empty(self):
        self.assertEqual(ReportsService._scoped([1, 9], {1, 2}), [1])
        self.assertEqual(ReportsService._scoped([9], {1, 2}), [])


class StaffingIsNotScopedTests(unittest.TestCase):
    def test_the_employee_picker_still_offers_the_organization(self):
        # A leader building a team chooses freely; narrowing this would leave a
        # leader with no team unable to acquire one.
        db = MagicMock()
        db.scalars.return_value.all.return_value = []
        assignable_employees(None, _leader(), db)
        db.scalars.assert_called_once()

    def test_the_picker_offers_every_role_the_write_path_accepts(self):
        """The picker must not be narrower than the endpoint behind it.

        ``ProjectMemberService.add_members`` attaches any *active user in the
        organization* and checks no role at all, so a leader, an HR member or an
        admin is a perfectly valid project member. This picker used to filter on
        ``role_name == "employee"``, so those people could not be chosen -- the
        supported action showed up to the user as "the app won't let me add
        them". The scoping this query must keep is the organization and the
        active flag; the role is not its business.
        """
        db = MagicMock()
        db.scalars.return_value.all.return_value = []
        assignable_employees(None, _admin(), db)
        # Only the WHERE clause is the subject here -- `role_name` appears in
        # every SELECT of this model as a column, which says nothing about
        # filtering.
        where_clause = str(db.scalars.call_args[0][0]).split("WHERE", 1)[1]
        self.assertNotIn("role_name", where_clause)
        self.assertIn("organization_id", where_clause)
        self.assertIn("is_active", where_clause)


if __name__ == "__main__":
    unittest.main()

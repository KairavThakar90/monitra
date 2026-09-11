"""What a leader may see, and what a leader may not write.

Three rules, pinned here because each one used to be wrong:

1. **A leader reads their own team, not the organization.** They hold
   ``time_entries:view_all`` and ``view_employees``, which used to mean
   "everybody"; ``app/services/member_scope.py`` now answers *whom*, and every
   read surface asks it — the member directory, the dashboard and reports
   filters, time tracking, and the manual time entry listings.
2. **A leader may not file manual time for a member.** Reading somebody's time
   and writing hours onto their timesheet are now separate permissions;
   ``manual_time_entries:create_for_others`` is the second one, and a leader
   does not have it.
3. **Staffing is not scoped.** A leader building a project chooses from the
   whole organization — ``/projects/assignable-employees`` is untouched by the
   scope helper, or a leader with no team could never acquire one.
"""

import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

from app.core.permissions import ROLE_PERMISSIONS
from app.react_apis.reports_page.service import ReportsPageService
from app.schemas.manual_time_entry import ManualTimeEntryCreate
from app.services.manual_time_entry import ManualTimeEntryService
from app.services.member_scope import is_team_scoped, may_view_member, visible_member_ids
from app.services.employee_service import EmployeeService
from app.services.member_service import MemberService
from app.services.time_tracking import TimeTrackingService

LEADER_ID = 42
TEAM = {7, 8}


def _leader(role="leader"):
    return SimpleNamespace(
        id=LEADER_ID, organization_id=1, role_name=role,
        permissions={p: True for p in ROLE_PERMISSIONS[role]},
    )


def _admin():
    return SimpleNamespace(
        id=1, organization_id=1, role_name="administrator",
        permissions={p: True for p in ROLE_PERMISSIONS["administrator"]},
    )


def _db_returning(member_ids):
    """A session whose one scalars() call yields the leader's project members."""
    db = MagicMock()
    db.scalars.return_value.all.return_value = list(member_ids)
    return db


class PermissionTableTests(unittest.TestCase):
    def test_a_leader_may_not_log_time_for_somebody_else(self):
        for role in ("leader", "project_leader", "employee"):
            with self.subTest(role=role):
                self.assertNotIn("manual_time_entries:create_for_others", ROLE_PERMISSIONS[role])

    def test_a_leader_still_approves_and_reads_time(self):
        for role in ("leader", "project_leader"):
            with self.subTest(role=role):
                self.assertIn("manual_time_entries:approve", ROLE_PERMISSIONS[role])
                self.assertIn("time_entries:view_all", ROLE_PERMISSIONS[role])
                # Their own manual entry is still theirs to file.
                self.assertIn("time_entries:manage_own", ROLE_PERMISSIONS[role])

    def test_a_leader_runs_projects(self):
        for role in ("leader", "project_leader"):
            with self.subTest(role=role):
                for permission in ("projects:create", "project_members:manage", "tasks:create"):
                    self.assertIn(permission, ROLE_PERMISSIONS[role])

    def test_the_roles_that_may_file_for_others_are_the_org_wide_ones(self):
        for role in ("administrator", "org_admin", "super_admin", "manager", "hr"):
            with self.subTest(role=role):
                self.assertIn("manual_time_entries:create_for_others", ROLE_PERMISSIONS[role])


class MemberScopeTests(unittest.TestCase):
    def test_both_leader_spellings_are_team_scoped(self):
        self.assertTrue(is_team_scoped(_leader("leader")))
        self.assertTrue(is_team_scoped(_leader("project_leader")))

    def test_nobody_else_is_scoped(self):
        self.assertFalse(is_team_scoped(_admin()))
        self.assertIsNone(visible_member_ids(MagicMock(), _admin()))

    def test_the_team_is_the_members_of_the_projects_they_lead_plus_themselves(self):
        self.assertEqual(visible_member_ids(_db_returning(TEAM), _leader()), TEAM | {LEADER_ID})

    def test_a_leader_with_no_project_still_sees_themselves(self):
        self.assertEqual(visible_member_ids(_db_returning([]), _leader()), {LEADER_ID})

    def test_no_session_narrows_rather_than_widens(self):
        # A missing session must never turn a leader into an org-wide reader.
        self.assertEqual(visible_member_ids(None, _leader()), {LEADER_ID})

    def test_may_view_member_follows_the_team(self):
        db = _db_returning(TEAM)
        self.assertTrue(may_view_member(db, _leader(), 7))
        self.assertFalse(may_view_member(_db_returning(TEAM), _leader(), 99))
        self.assertTrue(may_view_member(MagicMock(), _admin(), 99))


class MemberDirectoryTests(unittest.TestCase):
    def test_a_leaders_directory_is_their_team(self):
        db = _db_returning(TEAM)
        with patch("app.services.member_service.MemberRepository.list_by_organization",
                   return_value=([], 0)) as listed:
            MemberService.list(db, _leader(), None, None, None, 1, 20)
        self.assertEqual(listed.call_args.args[-1], TEAM | {LEADER_ID})

    def test_an_admins_directory_is_unrestricted(self):
        with patch("app.services.member_service.MemberRepository.list_by_organization",
                   return_value=([], 0)) as listed:
            MemberService.list(MagicMock(), _admin(), None, None, None, 1, 20)
        self.assertIsNone(listed.call_args.args[-1])

    def test_a_member_off_the_team_reads_as_missing(self):
        db = _db_returning(TEAM)
        with patch("app.services.member_service.MemberRepository.get_by_id_and_organization",
                   return_value=SimpleNamespace(id=99, organization_id=1)):
            with self.assertRaises(HTTPException) as error:
                MemberService.get(db, _leader(), 99)
        self.assertEqual(error.exception.status_code, 404)

    def test_a_member_on_the_team_is_returned(self):
        db = _db_returning(TEAM)
        member = SimpleNamespace(id=7, organization_id=1)
        with patch("app.services.member_service.MemberRepository.get_by_id_and_organization",
                   return_value=member):
            self.assertIs(MemberService.get(db, _leader(), 7), member)


class LegacyEmployeeRosterTests(unittest.TestCase):
    """The older /employees roster reads people too, so it takes the same scope."""

    def test_a_leader_sees_only_their_team(self):
        people = [SimpleNamespace(id=7), SimpleNamespace(id=99), SimpleNamespace(id=LEADER_ID)]
        with patch("app.services.employee_service.UserRepository.list_by_organization",
                   return_value=people):
            result = EmployeeService.list_employees(_db_returning(TEAM), _leader())
        self.assertEqual([person.id for person in result], [7, LEADER_ID])

    def test_an_outsider_reads_as_missing(self):
        with patch("app.services.employee_service.UserRepository.get_by_id_and_organization",
                   return_value=SimpleNamespace(id=99)):
            with self.assertRaises(HTTPException) as error:
                EmployeeService.get_employee(_db_returning(TEAM), 99, _leader())
        self.assertEqual(error.exception.status_code, 404)

    def test_an_admin_sees_everyone(self):
        people = [SimpleNamespace(id=7), SimpleNamespace(id=99)]
        with patch("app.services.employee_service.UserRepository.list_by_organization",
                   return_value=people):
            self.assertEqual(EmployeeService.list_employees(MagicMock(), _admin()), people)


class ReportFilterScopeTests(unittest.TestCase):
    def test_an_unfiltered_leader_report_covers_the_team_only(self):
        filters = ReportsPageService.resolve_filters(
            _leader(), date(2026, 9, 1), date(2026, 9, 7), None, None, None, _db_returning(TEAM)
        )
        self.assertEqual(set(filters.member_ids), TEAM | {LEADER_ID})

    def test_a_member_id_outside_the_team_is_dropped(self):
        filters = ReportsPageService.resolve_filters(
            _leader(), date(2026, 9, 1), date(2026, 9, 7), None, None, [7, 99], _db_returning(TEAM)
        )
        self.assertEqual(set(filters.member_ids), {7})

    def test_asking_only_for_somebody_else_falls_back_to_the_leader(self):
        filters = ReportsPageService.resolve_filters(
            _leader(), date(2026, 9, 1), date(2026, 9, 7), None, None, [99], _db_returning(TEAM)
        )
        self.assertEqual(set(filters.member_ids), {LEADER_ID})

    def test_an_admin_is_still_unfiltered(self):
        filters = ReportsPageService.resolve_filters(
            _admin(), date(2026, 9, 1), date(2026, 9, 7), None, None, None, MagicMock()
        )
        self.assertEqual(filters.member_ids, ())


class TimeTrackingScopeTests(unittest.TestCase):
    def test_an_unfiltered_leader_listing_covers_the_team_only(self):
        ids = TimeTrackingService._effective_user_ids(_leader(), None, _db_returning(TEAM))
        self.assertEqual(set(ids), TEAM | {LEADER_ID})

    def test_another_teams_employee_is_refused(self):
        with self.assertRaises(HTTPException) as error:
            TimeTrackingService._ensure_employees_access(_leader(), [99], _db_returning(TEAM))
        self.assertEqual(error.exception.status_code, 403)

    def test_their_own_team_is_allowed(self):
        TimeTrackingService._ensure_employees_access(_leader(), [7], _db_returning(TEAM))
        TimeTrackingService._ensure_employee_access(_leader(), 7, _db_returning(TEAM))

    def test_the_detail_view_of_an_outsider_is_not_that_employee(self):
        effective = TimeTrackingService._effective_user_id(_leader(), 99, _db_returning(TEAM))
        self.assertNotEqual(effective, 99)

    def test_an_admin_is_unaffected(self):
        self.assertEqual(TimeTrackingService._effective_user_ids(_admin(), [99], MagicMock()), [99])
        TimeTrackingService._ensure_employees_access(_admin(), [99], MagicMock())


class ManualTimeCreationTests(unittest.TestCase):
    def _create(self, user, target_id, db=None):
        payload = ManualTimeEntryCreate(project_id=1, task_id=2, work_date=date(2026, 8, 10),
                                        total_seconds=3600, user_id=target_id)
        with patch("app.services.manual_time_entry.TaskService.get_task"), \
             patch("app.services.manual_time_entry.ManualTimeEntryRepository.find_overlapping_time_entries",
                   return_value=[]), \
             patch("app.services.manual_time_entry.ManualTimeEntryRepository.find_overlapping_manual_entries",
                   return_value=[]), \
             patch("app.services.manual_time_entry.ManualTimeEntryRepository.create",
                   return_value="created") as create:
            return ManualTimeEntryService.create_manual_entry(db, payload, user), create

    def test_a_leader_cannot_file_time_for_a_member_of_their_own_team(self):
        with self.assertRaises(HTTPException) as error:
            self._create(_leader(), 7, _db_returning(TEAM))
        self.assertEqual(error.exception.status_code, 403)
        self.assertIn("create_for_others", error.exception.detail)

    def test_a_leader_can_still_file_their_own_manual_time(self):
        result, create = self._create(_leader(), LEADER_ID, _db_returning(TEAM))
        self.assertEqual(result, "created")
        self.assertEqual(create.call_args.kwargs["user_id"], LEADER_ID)

    def test_an_admin_can_file_for_a_member(self):
        result, create = self._create(_admin(), 7, MagicMock())
        self.assertEqual(result, "created")
        self.assertEqual(create.call_args.kwargs["user_id"], 7)


class ManualTimeReviewScopeTests(unittest.TestCase):
    def test_a_leaders_review_queue_is_their_team(self):
        db = _db_returning(TEAM)
        with patch("app.services.manual_time_entry.ManualTimeEntryRepository.search_by_filters",
                   return_value=([], 0)) as searched:
            ManualTimeEntryService.list_for_review(
                db, _leader(), None, None, None, None, None, None, None, 1, 20
            )
        self.assertEqual(searched.call_args.kwargs["user_ids"], TEAM | {LEADER_ID})

    def test_filtering_to_an_outsider_returns_nothing(self):
        with patch("app.services.manual_time_entry.ManualTimeEntryRepository.search_by_filters") as searched:
            result = ManualTimeEntryService.list_for_review(
                _db_returning(TEAM), _leader(), None, None, None, 99, None, None, None, 1, 20
            )
        self.assertEqual(result["total"], 0)
        searched.assert_not_called()

    def test_a_leader_cannot_approve_an_outsiders_request(self):
        entry = SimpleNamespace(id=3, organization_id=1, user_id=99, deleted_at=None,
                                approval_status="pending")
        with patch("app.services.manual_time_entry.ManualTimeEntryRepository.get_by_id",
                   return_value=entry):
            with self.assertRaises(HTTPException) as error:
                ManualTimeEntryService.update_approval(_db_returning(TEAM), 3, "approved", _leader())
        self.assertEqual(error.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()

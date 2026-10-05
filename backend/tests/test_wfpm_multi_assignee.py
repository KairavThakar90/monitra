"""WFPM -> Monitra: several assignees per task (`/WFPM/sync/tasks/{id}/assignees`).

A WFPM task can have several assignees, one of them primary. These tests run
`WfpmSyncService` against real SQLite rows -- what is defended is what ends up
in `tasks.assignee_id` and `task_assignees`, and a mocked session would agree
with whatever it was scripted to say -- and the routes through `TestClient`,
so a permission or validation rule missing from a decorator shows up here.

What each group maps to in production:

* **The list is the whole set, in order.** WFPM sends its current list on every
  sync. If a repeat changed anything, or the first id did not become the
  primary, the two systems would disagree about who owns the task.
* **All or nothing.** A bad id must leave the task exactly as it was, or WFPM
  believes a list was applied that Monitra only half wrote.
* **Existing callers keep working.** `assignee_id` / `assignee` stay the
  primary, and the single-assignee route still means "just this person".
* **Assigning is not owning the history.** Every assignee can track time on
  the task, and removing one never deletes the time they already booked.
"""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select

from app.core.database import get_db
from app.core.permissions import ROLE_PERMISSIONS
from app.core.security import get_current_user
from app.main import app
from app.models.project_member import ProjectMember
from app.models.task_assignee import TaskAssignee
from app.models.time_entry import TimeEntry
from app.models.user import User
from app.services.activity_log import ActivityLogService
from app.services.project_management import ProjectManagementService
from app.WFPM.schemas import (
    MAX_ASSIGNEES, WfpmTaskAssigneesAdd, WfpmTaskAssigneesSet, WfpmTaskSyncCreate,
)
from app.WFPM.service import WfpmSyncService
from tests.test_project_hours_summary import ORG, _sqlite_schema
from tests.test_wfpm_sync_service import (
    ADMIN, EMPLOYEE, LEADER, OTHER_EMPLOYEE, OUTSIDER, _Db,
)

THIRD_EMPLOYEE = 103
UTC = timezone.utc


def _set(*ids, add_missing_members=False):
    return WfpmTaskAssigneesSet(assignee_ids=list(ids), add_missing_members=add_missing_members)


class _AssigneeDb(_Db):
    """A linked project (WFPM 55) with EMPLOYEE and OTHER_EMPLOYEE as members,
    and one linked task (WFPM 900) that nobody holds yet. THIRD_EMPLOYEE is an
    employee of the organization who is *not* a member of the project."""

    def setUp(self):
        super().setUp()
        self._user(THIRD_EMPLOYEE, "employee")
        # Real users carry their role's permissions; Monitra's own rule for
        # several assignees reads them.
        self.admin.permissions = {name: True for name in ROLE_PERMISSIONS["administrator"]}
        self.db.commit()
        # The trail is not under test, and a repeated call must not write to it.
        self.trail = patch.object(ActivityLogService, "capture", return_value=None)
        self.capture = self.trail.start()
        self.addCleanup(self.trail.stop)
        self._linked_project(employee_ids=[EMPLOYEE, OTHER_EMPLOYEE])
        self._linked_task()
        self.capture.reset_mock()

    def holders(self):
        """`(tasks.assignee_id, task_assignees user ids in insertion order)`."""
        [task] = self._tasks()
        self.db.refresh(task)
        rows = list(self.db.scalars(
            select(TaskAssignee.user_id).where(TaskAssignee.task_id == task.id).order_by(TaskAssignee.id)
        ).all())
        return task.assignee_id, rows

    def assign(self, *ids, **kwargs):
        return WfpmSyncService.set_task_assignees(self.db, self.admin, "900", _set(*ids, **kwargs))

    def members(self):
        return set(self.db.scalars(select(ProjectMember.user_id)).all())

    @staticmethod
    def ids(task):
        return [person["id"] for person in task["assignees"]]


# ── setting the list ────────────────────────────────────────────────────────

class SetAssigneesTests(_AssigneeDb):
    def test_setting_a_list_writes_both_representations_with_the_first_as_primary(self):
        task = self.assign(EMPLOYEE, OTHER_EMPLOYEE)

        self.assertEqual(self.holders(), (EMPLOYEE, [EMPLOYEE, OTHER_EMPLOYEE]))
        self.assertEqual(self.ids(task), [EMPLOYEE, OTHER_EMPLOYEE])
        self.assertEqual(task["wfpm_task_id"], "900")

    def test_assignee_id_and_assignee_stay_the_first_assignee(self):
        task = self.assign(OTHER_EMPLOYEE, EMPLOYEE)

        self.assertEqual(task["assignee_id"], OTHER_EMPLOYEE)
        self.assertEqual(task["assignee"]["id"], OTHER_EMPLOYEE)
        self.assertEqual(
            {key: task["assignee"][key] for key in ("name", "email", "role")},
            {"name": f"User {OTHER_EMPLOYEE}", "email": f"u{OTHER_EMPLOYEE}@example.com", "role": "employee"},
        )
        self.assertEqual(self.ids(task), [OTHER_EMPLOYEE, EMPLOYEE])

    def test_a_new_list_replaces_the_old_one_entirely(self):
        self.assign(EMPLOYEE, OTHER_EMPLOYEE)
        replaced = self.assign(OTHER_EMPLOYEE)

        self.assertEqual(self.holders(), (OTHER_EMPLOYEE, [OTHER_EMPLOYEE]))
        self.assertEqual(self.ids(replaced), [OTHER_EMPLOYEE])

    def test_the_order_decides_the_primary_even_between_people_already_assigned(self):
        self.assign(EMPLOYEE, OTHER_EMPLOYEE)
        reordered = self.assign(OTHER_EMPLOYEE, EMPLOYEE)

        self.assertEqual(self.holders(), (OTHER_EMPLOYEE, [OTHER_EMPLOYEE, EMPLOYEE]))
        self.assertEqual(self.ids(reordered), [OTHER_EMPLOYEE, EMPLOYEE])
        self.assertEqual(reordered["assignee_id"], OTHER_EMPLOYEE)
        # And it reads back in that order.
        got = WfpmSyncService.get_task_assignees(self.db, self.admin, "900")
        self.assertEqual([person["id"] for person in got], [OTHER_EMPLOYEE, EMPLOYEE])

    def test_an_empty_list_leaves_the_task_unassigned(self):
        self.assign(EMPLOYEE, OTHER_EMPLOYEE)
        cleared = self.assign()

        self.assertEqual(self.holders(), (None, []))
        self.assertEqual((cleared["assignee_id"], cleared["assignee"], cleared["assignees"]), (None, None, []))

    def test_repeating_the_same_call_changes_nothing(self):
        self.assign(EMPLOYEE, OTHER_EMPLOYEE)
        [task] = self._tasks()
        row_ids = list(self.db.scalars(select(TaskAssignee.id).order_by(TaskAssignee.id)).all())
        self.capture.reset_mock()

        again = self.assign(EMPLOYEE, OTHER_EMPLOYEE)

        self.assertEqual(self.holders(), (EMPLOYEE, [EMPLOYEE, OTHER_EMPLOYEE]))
        self.assertEqual(self.ids(again), [EMPLOYEE, OTHER_EMPLOYEE])
        # Not a single row was rewritten, and nothing was added to the trail.
        self.assertEqual(list(self.db.scalars(select(TaskAssignee.id).order_by(TaskAssignee.id)).all()), row_ids)
        self.capture.assert_not_called()

    def test_repeating_an_empty_list_changes_nothing(self):
        self.assign()
        self.assertEqual(self.assign()["assignees"], [])
        self.assertEqual(self.holders(), (None, []))

    def test_a_change_is_recorded_in_the_trail_once(self):
        self.assign(EMPLOYEE)
        self.assertEqual(self.capture.call_count, 1)

    def test_duplicates_are_ignored(self):
        task = self.assign(EMPLOYEE, EMPLOYEE, OTHER_EMPLOYEE, EMPLOYEE)
        self.assertEqual(self.ids(task), [EMPLOYEE, OTHER_EMPLOYEE])
        self.assertEqual(self.holders(), (EMPLOYEE, [EMPLOYEE, OTHER_EMPLOYEE]))

    def test_the_single_assignee_route_means_just_this_one_person(self):
        self.assign(EMPLOYEE, OTHER_EMPLOYEE)
        single = WfpmSyncService.assign_task(self.db, self.admin, "900", OTHER_EMPLOYEE)

        self.assertEqual(self.holders(), (OTHER_EMPLOYEE, [OTHER_EMPLOYEE]))
        self.assertEqual(self.ids(single), [OTHER_EMPLOYEE])
        self.assertEqual(single["assignee_id"], OTHER_EMPLOYEE)

    def test_the_task_read_carries_the_whole_set(self):
        self.assign(EMPLOYEE, OTHER_EMPLOYEE)
        got = WfpmSyncService.get_task(self.db, self.admin, "900")
        self.assertEqual(self.ids(got), [EMPLOYEE, OTHER_EMPLOYEE])
        self.assertEqual(got["assignee_id"], EMPLOYEE)

    def test_the_project_read_carries_each_tasks_whole_set(self):
        self.assign(EMPLOYEE, OTHER_EMPLOYEE)
        project = WfpmSyncService.get_project(self.db, self.admin, "55")
        [wfpm_task] = [task for task in project["tasks"] if task["wfpm_task_id"] == "900"]
        self.assertEqual(self.ids(wfpm_task), [EMPLOYEE, OTHER_EMPLOYEE])


# ── all or nothing ──────────────────────────────────────────────────────────

class InvalidAssigneeTests(_AssigneeDb):
    def refused(self, *ids, **kwargs):
        with self.assertRaises(HTTPException) as ctx:
            self.assign(*ids, **kwargs)
        return ctx.exception

    def test_an_employee_who_is_not_on_the_project_changes_nothing_and_is_named(self):
        self.assign(EMPLOYEE)
        error = self.refused(OTHER_EMPLOYEE, THIRD_EMPLOYEE)

        self.assertEqual(error.status_code, 400)
        self.assertIn(str(THIRD_EMPLOYEE), error.detail)
        self.assertNotIn(str(OTHER_EMPLOYEE), error.detail)       # only the offenders
        self.assertEqual(self.holders(), (EMPLOYEE, [EMPLOYEE]))   # not even the valid one was added

    def test_someone_who_is_not_an_employee_is_refused_and_named(self):
        error = self.refused(EMPLOYEE, LEADER)
        self.assertEqual(error.status_code, 400)
        self.assertIn(str(LEADER), error.detail)
        self.assertEqual(self.holders(), (None, []))

    def test_an_unknown_id_and_one_from_another_organization_are_refused_and_named(self):
        for bad in (99999, OUTSIDER):
            with self.subTest(user=bad):
                error = self.refused(EMPLOYEE, bad)
                self.assertEqual(error.status_code, 400)
                self.assertIn(str(bad), error.detail)
        self.assertEqual(self.holders(), (None, []))

    def test_every_offending_id_is_listed(self):
        self._user(104, "employee")
        self.db.commit()
        error = self.refused(EMPLOYEE, THIRD_EMPLOYEE, 104)
        self.assertEqual(error.status_code, 400)
        self.assertIn(str([THIRD_EMPLOYEE, 104]), error.detail)
        self.assertEqual(self.holders(), (None, []))

    def test_someone_already_on_the_task_who_has_left_the_project_may_stay(self):
        """Only people being *added* are checked -- Monitra's own rule for the
        Assign Tasks screen -- so repeating a list never fails because of a
        person the call did not touch."""
        self.assign(EMPLOYEE, OTHER_EMPLOYEE)
        self.db.execute(ProjectMember.__table__.delete().where(ProjectMember.user_id == OTHER_EMPLOYEE))
        self.db.commit()

        again = self.assign(EMPLOYEE, OTHER_EMPLOYEE)

        self.assertEqual(self.ids(again), [EMPLOYEE, OTHER_EMPLOYEE])


class MalformedListTests(unittest.TestCase):
    def test_a_list_above_the_maximum_is_refused(self):
        WfpmTaskAssigneesSet(assignee_ids=list(range(1, MAX_ASSIGNEES + 1)))
        with self.assertRaises(ValidationError):
            WfpmTaskAssigneesSet(assignee_ids=list(range(1, MAX_ASSIGNEES + 2)))

    def test_a_malformed_list_is_refused(self):
        for bad in (None, 101, "101", {"id": 101}, [0], [-3], ["abc"], [True], [1.5], [None]):
            with self.subTest(value=bad), self.assertRaises(ValidationError):
                WfpmTaskAssigneesSet(assignee_ids=bad)

    def test_the_list_is_required_on_the_set_and_add_bodies(self):
        with self.assertRaises(ValidationError):
            WfpmTaskAssigneesSet()
        with self.assertRaises(ValidationError):
            WfpmTaskAssigneesAdd(assignee_ids=[])

    def test_duplicates_are_dropped_in_order(self):
        self.assertEqual(WfpmTaskAssigneesSet(assignee_ids=[5, 3, 5, 9, 3]).assignee_ids, [5, 3, 9])


# ── not found ───────────────────────────────────────────────────────────────

class NotFoundTests(_AssigneeDb):
    def test_a_task_that_is_not_linked_is_404_on_every_assignee_route(self):
        calls = (
            lambda: WfpmSyncService.set_task_assignees(self.db, self.admin, "nope", _set(EMPLOYEE)),
            lambda: WfpmSyncService.get_task_assignees(self.db, self.admin, "nope"),
            lambda: WfpmSyncService.add_task_assignees(self.db, self.admin, "nope", WfpmTaskAssigneesAdd(assignee_ids=[EMPLOYEE])),
            lambda: WfpmSyncService.remove_task_assignee(self.db, self.admin, "nope", EMPLOYEE),
        )
        for call in calls:
            with self.assertRaises(HTTPException) as ctx:
                call()
            self.assertEqual(ctx.exception.status_code, 404)

    def test_a_task_the_caller_cannot_see_is_404_and_untouched(self):
        self.assign(OTHER_EMPLOYEE)
        with self.assertRaises(HTTPException) as ctx:
            WfpmSyncService.set_task_assignees(self.db, self.db.get(User, EMPLOYEE), "900", _set(EMPLOYEE))
        self.assertEqual(ctx.exception.status_code, 404)
        with self.assertRaises(HTTPException) as ctx:
            WfpmSyncService.get_task_assignees(self.db, self.db.get(User, EMPLOYEE), "900")
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertEqual(self.holders(), (OTHER_EMPLOYEE, [OTHER_EMPLOYEE]))

    def test_an_archived_task_is_404(self):
        [task] = self._tasks()
        task.status = "archived"
        self.db.commit()
        with self.assertRaises(HTTPException) as ctx:
            self.assign(EMPLOYEE)
        self.assertEqual(ctx.exception.status_code, 404)


# ── add one / remove one ────────────────────────────────────────────────────

class AddRemoveTests(_AssigneeDb):
    def test_adding_keeps_whoever_holds_the_task_and_their_primary(self):
        self.assign(EMPLOYEE)
        added = WfpmSyncService.add_task_assignees(
            self.db, self.admin, "900", WfpmTaskAssigneesAdd(assignee_ids=[OTHER_EMPLOYEE]))

        self.assertEqual(self.ids(added), [EMPLOYEE, OTHER_EMPLOYEE])
        self.assertEqual(added["assignee_id"], EMPLOYEE)
        self.assertEqual(self.holders(), (EMPLOYEE, [EMPLOYEE, OTHER_EMPLOYEE]))

    def test_adding_someone_already_assigned_is_not_an_error(self):
        self.assign(EMPLOYEE, OTHER_EMPLOYEE)
        again = WfpmSyncService.add_task_assignees(
            self.db, self.admin, "900", WfpmTaskAssigneesAdd(assignee_ids=[OTHER_EMPLOYEE, EMPLOYEE]))
        self.assertEqual(self.ids(again), [EMPLOYEE, OTHER_EMPLOYEE])
        self.assertEqual(self.holders(), (EMPLOYEE, [EMPLOYEE, OTHER_EMPLOYEE]))

    def test_adding_to_an_unassigned_task_makes_the_first_the_primary(self):
        added = WfpmSyncService.add_task_assignees(
            self.db, self.admin, "900", WfpmTaskAssigneesAdd(assignee_ids=[OTHER_EMPLOYEE, EMPLOYEE]))
        self.assertEqual((added["assignee_id"], self.ids(added)), (OTHER_EMPLOYEE, [OTHER_EMPLOYEE, EMPLOYEE]))

    def test_adding_an_invalid_id_changes_nothing(self):
        self.assign(EMPLOYEE)
        with self.assertRaises(HTTPException) as ctx:
            WfpmSyncService.add_task_assignees(
                self.db, self.admin, "900", WfpmTaskAssigneesAdd(assignee_ids=[OTHER_EMPLOYEE, LEADER]))
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn(str(LEADER), ctx.exception.detail)
        self.assertEqual(self.holders(), (EMPLOYEE, [EMPLOYEE]))
        # A person who is merely not on the project yet is a different case:
        # POST puts them on it (AddMissingMembersByDefaultTests). Someone who
        # could never hold a task at all (a leader) is refused, and not added.
        self.assertNotIn(LEADER, self.members())

    def test_removing_one_leaves_the_rest_in_order(self):
        self.assign(EMPLOYEE, OTHER_EMPLOYEE)
        WfpmSyncService.remove_task_assignee(self.db, self.admin, "900", EMPLOYEE)
        # The primary went, so the next in line takes the role.
        self.assertEqual(self.holders(), (OTHER_EMPLOYEE, [OTHER_EMPLOYEE]))

    def test_removing_the_last_one_leaves_the_task_unassigned(self):
        self.assign(EMPLOYEE)
        WfpmSyncService.remove_task_assignee(self.db, self.admin, "900", EMPLOYEE)
        self.assertEqual(self.holders(), (None, []))

    def test_removing_someone_who_is_not_assigned_is_404(self):
        self.assign(EMPLOYEE)
        with self.assertRaises(HTTPException) as ctx:
            WfpmSyncService.remove_task_assignee(self.db, self.admin, "900", OTHER_EMPLOYEE)
        self.assertEqual(ctx.exception.status_code, 404)
        # So a repeated removal is distinguishable, and harmless.
        WfpmSyncService.remove_task_assignee(self.db, self.admin, "900", EMPLOYEE)
        with self.assertRaises(HTTPException) as ctx:
            WfpmSyncService.remove_task_assignee(self.db, self.admin, "900", EMPLOYEE)
        self.assertEqual(ctx.exception.status_code, 404)


# ── time ────────────────────────────────────────────────────────────────────

class TimeTrackingTests(_AssigneeDb):
    def setUp(self):
        super().setUp()
        from app.models.activity_log import ActivityLog
        from app.WFPM.models import WfpmTimerEvent
        _sqlite_schema(self.engine, TimeEntry, ActivityLog, WfpmTimerEvent)
        # Nothing here may reach a real WFPM.
        off = patch.multiple("app.core.config.settings", WFPM_TIMER_START_URL="", WFPM_TIMER_STOP_URL="")
        off.start()
        self.addCleanup(off.stop)

    def book(self, user_id, minutes=30):
        [task] = self._tasks()
        start = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
        entry = TimeEntry(
            organization_id=ORG, user_id=user_id, project_id=task.project_id, task_id=task.id,
            start_time=start, end_time=start + timedelta(minutes=minutes), total_seconds=minutes * 60,
            status="stopped", is_manual=False, is_billable=False,
        )
        self.db.add(entry)
        self.db.commit()
        return entry.id

    def entry_ids(self):
        return set(self.db.scalars(select(TimeEntry.id)).all())

    def test_every_assignee_can_start_a_timer_on_the_task_not_only_the_primary(self):
        from app.services.time_entry import TimeEntryService
        [task] = self._tasks()
        self.assign(EMPLOYEE, OTHER_EMPLOYEE)

        for person in (EMPLOYEE, OTHER_EMPLOYEE):
            with self.subTest(user=person):
                entry, created = TimeEntryService.start_timer(
                    self.db, task.project_id, task.id, None, None, self.db.get(User, person),
                )
                self.assertTrue(created)
                self.assertEqual((entry.user_id, entry.task_id), (person, task.id))

    def test_someone_not_assigned_cannot_start_a_timer_on_it(self):
        from app.services.time_entry import TimeEntryService
        [task] = self._tasks()
        self.assign(EMPLOYEE)
        with self.assertRaises(HTTPException) as ctx:
            TimeEntryService.start_timer(
                self.db, task.project_id, task.id, None, None, self.db.get(User, OTHER_EMPLOYEE),
            )
        self.assertEqual(ctx.exception.status_code, 404)

    def test_removing_an_assignee_keeps_their_time_entries(self):
        self.assign(EMPLOYEE, OTHER_EMPLOYEE)
        kept = {self.book(EMPLOYEE), self.book(OTHER_EMPLOYEE, minutes=45)}

        WfpmSyncService.remove_task_assignee(self.db, self.admin, "900", EMPLOYEE)
        self.assertEqual(self.entry_ids(), kept)

        self.assign()   # and clearing the whole list
        self.assertEqual(self.entry_ids(), kept)
        self.assertEqual(
            {entry.user_id: entry.total_seconds for entry in self.db.scalars(select(TimeEntry)).all()},
            {EMPLOYEE: 1800, OTHER_EMPLOYEE: 2700},
        )

    def test_replacing_the_list_keeps_the_time_entries_of_everyone_dropped(self):
        self.assign(EMPLOYEE)
        entry = self.book(EMPLOYEE)
        self.assign(OTHER_EMPLOYEE)
        self.assertEqual(self.entry_ids(), {entry})


# ── create ──────────────────────────────────────────────────────────────────

class CreateWithAssigneesTests(_AssigneeDb):
    def create(self, wfpm_task_id="901", **fields):
        return WfpmSyncService.create_task(
            self.db, self.admin, "55",
            WfpmTaskSyncCreate(wfpm_task_id=wfpm_task_id, name="Write the copy", **fields),
        )

    def held(self, wfpm_task_id):
        task = next(item for item in self._tasks() if item.wfpm_task_id == wfpm_task_id)
        self.db.refresh(task)
        rows = list(self.db.scalars(
            select(TaskAssignee.user_id).where(TaskAssignee.task_id == task.id).order_by(TaskAssignee.id)
        ).all())
        return task.assignee_id, rows

    def test_a_task_can_be_created_with_several_assignees(self):
        task, created = self.create(assignee_ids=[OTHER_EMPLOYEE, EMPLOYEE])

        self.assertTrue(created)
        self.assertEqual(self.ids(task), [OTHER_EMPLOYEE, EMPLOYEE])
        self.assertEqual((task["assignee_id"], task["assignee"]["id"]), (OTHER_EMPLOYEE, OTHER_EMPLOYEE))
        self.assertEqual(self.held("901"), (OTHER_EMPLOYEE, [OTHER_EMPLOYEE, EMPLOYEE]))

    def test_assignee_ids_wins_when_both_are_sent(self):
        task, _ = self.create(assignee_id=EMPLOYEE, assignee_ids=[OTHER_EMPLOYEE])
        self.assertEqual((task["assignee_id"], self.ids(task)), (OTHER_EMPLOYEE, [OTHER_EMPLOYEE]))
        self.assertEqual(self.held("901"), (OTHER_EMPLOYEE, [OTHER_EMPLOYEE]))

    def test_the_single_assignee_still_works_and_is_reported_as_a_list_of_one(self):
        task, _ = self.create(assignee_id=EMPLOYEE)
        self.assertEqual((task["assignee_id"], self.ids(task)), (EMPLOYEE, [EMPLOYEE]))

    def test_an_empty_assignee_ids_means_nobody_even_beside_an_assignee_id(self):
        task, _ = self.create(assignee_id=EMPLOYEE, assignee_ids=[])
        self.assertEqual((task["assignee_id"], task["assignees"]), (None, []))

    def test_a_repeated_create_is_a_replay_that_reports_the_set_and_adds_nothing(self):
        self.create(assignee_ids=[EMPLOYEE, OTHER_EMPLOYEE])
        before = len(self._tasks())

        again, created = self.create(assignee_ids=[OTHER_EMPLOYEE])    # a different list is not applied

        self.assertFalse(created)
        self.assertEqual(len(self._tasks()), before)
        self.assertEqual(self.ids(again), [EMPLOYEE, OTHER_EMPLOYEE])
        self.assertEqual(self.held("901"), (EMPLOYEE, [EMPLOYEE, OTHER_EMPLOYEE]))

    def test_an_invalid_assignee_creates_no_task(self):
        before = len(self._tasks())
        with self.assertRaises(HTTPException) as ctx:
            # `false` is the strict mode: never add anyone to the project.
            self.create(assignee_ids=[EMPLOYEE, THIRD_EMPLOYEE], add_missing_members=False)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn(str(THIRD_EMPLOYEE), ctx.exception.detail)
        self.assertEqual(len(self._tasks()), before)

    def test_an_employee_may_not_hand_a_new_task_to_others(self):
        """Monitra's rule for several assignees (`task_assignees:manage`),
        unchanged: the same refusal as `POST /api/v1/.../tasks`."""
        employee = self.db.get(User, EMPLOYEE)
        employee.permissions = {name: True for name in ROLE_PERMISSIONS["employee"]}
        with self.assertRaises(HTTPException) as ctx:
            WfpmSyncService.create_task(
                self.db, employee, "55",
                WfpmTaskSyncCreate(wfpm_task_id="902", name="x", assignee_ids=[EMPLOYEE, OTHER_EMPLOYEE]),
            )
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertEqual(len(self._tasks()), 1)


# ── add_missing_members ─────────────────────────────────────────────────────

class AddMissingMembersTests(_AssigneeDb):
    def test_without_the_flag_a_non_member_is_refused(self):
        with self.assertRaises(HTTPException) as ctx:
            self.assign(EMPLOYEE, THIRD_EMPLOYEE)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertNotIn(THIRD_EMPLOYEE, self.members())

    def test_with_the_flag_the_missing_member_is_added_and_assigned(self):
        task = self.assign(THIRD_EMPLOYEE, EMPLOYEE, add_missing_members=True)

        self.assertIn(THIRD_EMPLOYEE, self.members())
        self.assertEqual(self.ids(task), [THIRD_EMPLOYEE, EMPLOYEE])
        self.assertEqual(self.holders(), (THIRD_EMPLOYEE, [THIRD_EMPLOYEE, EMPLOYEE]))

    def test_the_flag_needs_the_members_permission_and_changes_nothing_without_it(self):
        self.admin.permissions = {}
        with self.assertRaises(HTTPException) as ctx:
            self.assign(THIRD_EMPLOYEE, add_missing_members=True)
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertNotIn(THIRD_EMPLOYEE, self.members())
        self.assertEqual(self.holders(), (None, []))

    def test_a_manager_who_does_not_lead_the_project_cannot_add_members_through_it(self):
        """The members route's own rule (an admin or the project's leader)
        still applies, because the flag goes through the same service."""
        leader = self.db.get(User, LEADER)
        leader.permissions = {name: True for name in ROLE_PERMISSIONS["leader"]}
        project_id = self._tasks()[0].project_id
        self.db.execute(
            ProjectMember.__table__.delete().where(ProjectMember.user_id == LEADER))
        self.db.commit()
        from app.models.project import Project
        self.assertNotEqual(self.db.get(Project, project_id).leader_id, LEADER)

        with patch.object(ProjectManagementService, "_project"):
            with self.assertRaises(HTTPException) as ctx:
                WfpmSyncService.set_task_assignees(
                    self.db, leader, "900", _set(THIRD_EMPLOYEE, add_missing_members=True))
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertNotIn(THIRD_EMPLOYEE, self.members())

    def test_someone_who_cannot_hold_a_task_is_not_added_either(self):
        """Checked before the first write: a bad id must not leave a stray
        membership behind."""
        before = self.members()
        with self.assertRaises(HTTPException) as ctx:
            self.assign(THIRD_EMPLOYEE, LEADER, add_missing_members=True)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn(str(LEADER), ctx.exception.detail)
        self.assertEqual(self.members(), before)
        self.assertEqual(self.holders(), (None, []))

    def test_an_empty_list_with_the_flag_is_just_an_empty_list(self):
        self.assign(EMPLOYEE)
        self.assertEqual(self.assign(add_missing_members=True)["assignees"], [])

    def test_create_with_the_flag_adds_the_missing_members_first(self):
        task, created = WfpmSyncService.create_task(
            self.db, self.admin, "55",
            WfpmTaskSyncCreate(
                wfpm_task_id="903", name="x", assignee_ids=[THIRD_EMPLOYEE, EMPLOYEE], add_missing_members=True),
        )
        self.assertTrue(created)
        self.assertIn(THIRD_EMPLOYEE, self.members())
        self.assertEqual(self.ids(task), [THIRD_EMPLOYEE, EMPLOYEE])

    def test_a_replayed_create_with_the_flag_adds_no_members(self):
        WfpmSyncService.create_task(
            self.db, self.admin, "55",
            WfpmTaskSyncCreate(wfpm_task_id="903", name="x", assignee_ids=[EMPLOYEE]),
        )
        _, created = WfpmSyncService.create_task(
            self.db, self.admin, "55",
            WfpmTaskSyncCreate(wfpm_task_id="903", name="x", assignee_ids=[THIRD_EMPLOYEE], add_missing_members=True),
        )
        self.assertFalse(created)
        self.assertNotIn(THIRD_EMPLOYEE, self.members())


# ── add_missing_members omitted: the case WFPM actually sends ───────────────

class AddMissingMembersByDefaultTests(_AssigneeDb):
    """A person assigned in WFPM must reach Monitra's project or the task never
    reaches them. With the flag omitted, the caller who may add members does
    that; everyone else keeps Monitra's own refusal."""

    def create(self, caller=None, wfpm_task_id="910", **fields):
        return WfpmSyncService.create_task(
            self.db, caller or self.admin, "55",
            WfpmTaskSyncCreate(wfpm_task_id=wfpm_task_id, name="Write the copy", **fields),
        )

    def visible_to(self, user_id):
        """The names `GET /api/v1/projects/{id}/tasks` shows this person."""
        project_id = self._tasks()[0].project_id
        rows = ProjectManagementService.tasks(self.db, self.db.get(User, user_id), project_id, None, None, None)
        return {row["name"] for row in rows}

    def test_create_with_a_non_member_assignee_adds_them_and_the_task_reaches_them(self):
        task, created = self.create(assignee_id=THIRD_EMPLOYEE)
        self.assertTrue(created)
        self.assertIn(THIRD_EMPLOYEE, self.members())
        self.assertEqual(self.ids(task), [THIRD_EMPLOYEE])
        self.assertIn("Write the copy", self.visible_to(THIRD_EMPLOYEE))

    def test_create_with_several_non_member_assignees_adds_each(self):
        task, _ = self.create(assignee_ids=[THIRD_EMPLOYEE, EMPLOYEE])
        self.assertEqual(self.ids(task), [THIRD_EMPLOYEE, EMPLOYEE])
        self.assertIn(THIRD_EMPLOYEE, self.members())

    def test_put_and_post_add_the_missing_member_too(self):
        task = WfpmSyncService.set_task_assignees(
            self.db, self.admin, "900", WfpmTaskAssigneesSet(assignee_ids=[THIRD_EMPLOYEE]))
        self.assertEqual(self.ids(task), [THIRD_EMPLOYEE])
        self.assertIn(THIRD_EMPLOYEE, self.members())

        fourth = 104
        self._user(fourth, "employee")
        self.db.commit()
        task = WfpmSyncService.add_task_assignees(
            self.db, self.admin, "900", WfpmTaskAssigneesAdd(assignee_ids=[fourth]))
        self.assertEqual(self.ids(task), [THIRD_EMPLOYEE, fourth])
        self.assertIn(fourth, self.members())

    def test_false_still_means_never_add(self):
        with self.assertRaises(HTTPException) as ctx:
            self.create(assignee_id=THIRD_EMPLOYEE, add_missing_members=False)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertNotIn(THIRD_EMPLOYEE, self.members())

    def test_a_caller_who_may_not_add_members_gets_the_usual_400_and_adds_nobody(self):
        self.admin.permissions = {
            name: True for name in ROLE_PERMISSIONS["administrator"] if name != "project_members:manage"
        }
        with self.assertRaises(HTTPException) as ctx:
            self.create(assignee_id=THIRD_EMPLOYEE)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertNotIn(THIRD_EMPLOYEE, self.members())

    def test_someone_who_could_never_hold_a_task_is_not_added_either(self):
        before = self.members()
        with self.assertRaises(HTTPException) as ctx:
            self.create(assignee_ids=[THIRD_EMPLOYEE, LEADER])
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(self.members(), before)
        self.assertEqual(len(self._tasks()), 1)

    def test_an_employee_naming_several_people_is_refused_before_anyone_is_added(self):
        employee = self.db.get(User, EMPLOYEE)
        employee.permissions = {name: True for name in ROLE_PERMISSIONS["employee"]}
        with self.assertRaises(HTTPException) as ctx:
            self.create(caller=employee, assignee_ids=[EMPLOYEE, THIRD_EMPLOYEE])
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertNotIn(THIRD_EMPLOYEE, self.members())

    def test_a_member_already_on_the_project_is_not_re_judged(self):
        """Only people not on the project yet are checked. One who is on it keeps
        the task even if their role has changed since."""
        self.db.get(User, OTHER_EMPLOYEE).role_name = "manager"
        self.db.commit()
        task = WfpmSyncService.set_task_assignees(
            self.db, self.admin, "900", WfpmTaskAssigneesSet(assignee_ids=[EMPLOYEE, THIRD_EMPLOYEE]))
        self.assertEqual(self.ids(task), [EMPLOYEE, THIRD_EMPLOYEE])


# ── the routes ──────────────────────────────────────────────────────────────

TASK_BODY = {
    "id": 1, "project_id": 7, "name": "Design", "assignee_id": 101,
    "assignee": {"id": 101, "name": "A", "email": "a@example.com", "role": "employee"},
    "assignees": [{"id": 101, "name": "A", "email": "a@example.com", "role": "employee"}],
    "status": {"id": 1, "name": "Todo", "color": "#CBD5E1"}, "estimated_hours": None,
    "created_at": "2026-10-01T00:00:00Z", "updated_at": "2026-10-01T00:00:00Z", "wfpm_task_id": "900",
}


def _as(role, user_id=501):
    user = User()
    user.id, user.organization_id, user.role_name = user_id, 1, role
    user.permissions = {name: True for name in ROLE_PERMISSIONS.get(role, ())}
    user.is_active = True
    app.dependency_overrides[get_current_user] = lambda: user


class RouteTests(unittest.TestCase):
    PATH = "/WFPM/sync/tasks/900/assignees"

    def setUp(self):
        app.dependency_overrides[get_db] = lambda: None
        self.addCleanup(app.dependency_overrides.clear)
        self.client = TestClient(app)

    def test_put_hands_the_list_to_the_service_and_returns_the_task(self):
        _as("administrator")
        with patch.object(WfpmSyncService, "set_task_assignees", return_value=TASK_BODY) as service:
            response = self.client.put(self.PATH, json={"assignee_ids": [101, 102, 101], "add_missing_members": True})
        self.assertEqual(response.status_code, 200, response.text)
        payload = service.call_args.args[3]
        self.assertEqual((service.call_args.args[2], payload.assignee_ids, payload.add_missing_members),
                         ("900", [101, 102], True))
        body = response.json()
        self.assertEqual((body["assignee_id"], body["assignee"]["id"], [p["id"] for p in body["assignees"]]), (101, 101, [101]))

    def test_a_malformed_list_is_422_and_never_reaches_the_service(self):
        _as("administrator")
        too_many = list(range(1, MAX_ASSIGNEES + 2))
        with patch.object(WfpmSyncService, "set_task_assignees") as service:
            for bad in ({}, {"assignee_ids": None}, {"assignee_ids": "101"}, {"assignee_ids": [0]},
                        {"assignee_ids": ["x"]}, {"assignee_ids": too_many}):
                with self.subTest(body=str(bad)[:40]):
                    self.assertEqual(self.client.put(self.PATH, json=bad).status_code, 422)
            self.assertEqual(self.client.post(self.PATH, json={"assignee_ids": []}).status_code, 422)
        service.assert_not_called()

    def test_put_post_delete_need_tasks_update_and_get_needs_tasks_view(self):
        # `hr` may view tasks but not change them; `client` may do neither.
        with patch.object(WfpmSyncService, "set_task_assignees", return_value=TASK_BODY), \
                patch.object(WfpmSyncService, "add_task_assignees", return_value=TASK_BODY), \
                patch.object(WfpmSyncService, "remove_task_assignee", return_value=None), \
                patch.object(WfpmSyncService, "get_task_assignees", return_value=[]):
            _as("hr")
            self.assertEqual(self.client.get(self.PATH).status_code, 200)
            self.assertEqual(self.client.put(self.PATH, json={"assignee_ids": [1]}).status_code, 403)
            self.assertEqual(self.client.post(self.PATH, json={"assignee_ids": [1]}).status_code, 403)
            self.assertEqual(self.client.delete(f"{self.PATH}/1").status_code, 403)
            _as("client")
            self.assertEqual(self.client.get(self.PATH).status_code, 403)
            self.assertEqual(self.client.put(self.PATH, json={"assignee_ids": [1]}).status_code, 403)
            for role in ("employee", "leader", "manager", "administrator"):
                _as(role)
                with self.subTest(role=role):
                    self.assertEqual(self.client.put(self.PATH, json={"assignee_ids": [1]}).status_code, 200)
                    self.assertEqual(self.client.post(self.PATH, json={"assignee_ids": [1]}).status_code, 200)
                    self.assertEqual(self.client.delete(f"{self.PATH}/1").status_code, 204)
                    self.assertEqual(self.client.get(self.PATH).status_code, 200)

    def test_get_returns_the_ordered_list_of_people(self):
        _as("employee")
        people = [{"id": 102, "name": "B", "email": "b@example.com", "role": "employee"},
                  {"id": 101, "name": "A", "email": "a@example.com", "role": "employee"}]
        with patch.object(WfpmSyncService, "get_task_assignees", return_value=people):
            response = self.client.get(self.PATH)
        self.assertEqual(response.json(), people)

    def test_delete_answers_204_with_no_body(self):
        _as("administrator")
        with patch.object(WfpmSyncService, "remove_task_assignee", return_value=None) as service:
            response = self.client.delete(f"{self.PATH}/102")
        self.assertEqual((response.status_code, response.content), (204, b""))
        self.assertEqual(service.call_args.args[2:], ("900", 102))

    def test_the_single_assignee_routes_are_unchanged(self):
        _as("administrator")
        with patch.object(WfpmSyncService, "assign_task", return_value=TASK_BODY) as assign, \
                patch.object(WfpmSyncService, "unassign_task", return_value=TASK_BODY):
            self.assertEqual(self.client.put("/WFPM/sync/tasks/900/assignee", json={"assignee_id": 101}).status_code, 200)
            self.assertEqual(self.client.delete("/WFPM/sync/tasks/900/assignee").status_code, 200)
        assign.assert_called_once()

    def test_create_accepts_assignee_ids_and_reports_them(self):
        _as("administrator")
        with patch.object(WfpmSyncService, "create_task", return_value=(TASK_BODY, True)) as service:
            response = self.client.post(
                "/WFPM/sync/projects/55/tasks",
                json={"wfpm_task_id": 900, "name": "Design", "assignee_ids": [101, 102], "add_missing_members": True},
            )
        self.assertEqual(response.status_code, 201, response.text)
        payload = service.call_args.args[3]
        self.assertEqual((payload.assignee_ids, payload.assignee_id, payload.add_missing_members), ([101, 102], None, True))
        self.assertIn("assignees", response.json())

    def test_create_with_a_malformed_assignee_list_is_422(self):
        _as("administrator")
        response = self.client.post(
            "/WFPM/sync/projects/55/tasks",
            json={"wfpm_task_id": 900, "name": "Design", "assignee_ids": list(range(1, MAX_ASSIGNEES + 2))},
        )
        self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main()

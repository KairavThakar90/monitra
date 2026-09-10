"""
Coverage for who may be assigned a project task, and for the endpoint that
tells a client who those people are.

POST /api/v1/projects/{id}/tasks accepts an assignee only if they are an
active employee who is a member of the project. That rule is correct; making
the field *required* was not, because the desktop then had to invent a value
and the only id it held was the signed-in user's -- which works for an
employee and is refused with HTTP 400 for an admin or a leader. The field is
now optional and the rule applies only when a value is given.
"""
import unittest
from unittest.mock import MagicMock

from fastapi import HTTPException

from app.models.project import Project
from app.models.task import Task
from app.models.task_assignee import TaskAssignee
from app.models.user import User
from app.schemas.project_management import TaskCreate
from app.services.project_management import ProjectManagementService


def _user(role="admin", user_id=1):
    return User(id=user_id, organization_id=1, role_name=role, permissions={})


def _project():
    return Project(id=7, organization_id=1, status="active")


class TestTaskAssigneeRule(unittest.TestCase):
    """The assignee validation, exercised with the caller as an admin -- the
    account for which task creation failed in production."""

    def setUp(self):
        self.db = MagicMock()
        self.payload = TaskCreate(name="Write the report", assignee_id=1, status_id=1)

    def _create(self, member, assignee, user=None):
        # _project, then the membership lookup, then the assignee lookup.
        self.db.scalar.side_effect = [_project(), member, assignee]
        self.db.get.return_value = MagicMock(id=1)
        return ProjectManagementService.create_task(
            self.db, user or _user(), 7, self.payload
        )

    def test_an_admin_cannot_be_the_assignee_even_as_a_project_member(self):
        """The exact production failure: the desktop self-assigned, so an
        admin's own id arrived as assignee_id and was refused."""
        with self.assertRaises(HTTPException) as ctx:
            self._create(member=MagicMock(), assignee=_user(role="admin"))

        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("active employee", ctx.exception.detail)

    def test_an_employee_who_is_not_a_project_member_is_refused(self):
        with self.assertRaises(HTTPException) as ctx:
            self._create(member=None, assignee=_user(role="employee", user_id=2))

        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("assigned to this project", ctx.exception.detail)

    def test_the_refusal_is_a_400_with_an_explanation_a_user_can_act_on(self):
        """The desktop surfaces `detail` verbatim, so it has to say what to
        do about it, not just that something was wrong."""
        with self.assertRaises(HTTPException) as ctx:
            self._create(member=None, assignee=None)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertTrue(ctx.exception.detail.strip().endswith("."))

    def test_the_creator_is_taken_from_the_authenticated_user(self):
        """Not from the payload -- TaskCreate has no creator field at all, so
        a client cannot create a task as somebody else."""
        self.assertNotIn("created_by", TaskCreate.model_fields)
        self.assertNotIn("user_id", TaskCreate.model_fields)
        self.assertNotIn("organization_id", TaskCreate.model_fields)


class TestUnassignedTask(unittest.TestCase):
    """An admin creates a task without naming an assignee -- the flow that
    replaced the self-assignment that produced the 400."""

    def test_the_assignee_is_optional(self):
        payload = TaskCreate(name="Write the report", status_id=1)
        self.assertIsNone(payload.assignee_id)

    def test_a_task_with_no_assignee_is_created_unassigned(self):
        db = MagicMock()
        db.scalar.return_value = _project()
        db.get.return_value = MagicMock(id=1, name="Todo", color="#CBD5E1")

        ProjectManagementService.create_task(
            db, _user(role="admin"), 7,
            TaskCreate(name="Write the report", status_id=1),
        )

        added = [call.args[0] for call in db.add.call_args_list]
        tasks = [item for item in added if isinstance(item, Task)]
        self.assertEqual(len(tasks), 1)
        self.assertIsNone(tasks[0].assignee_id)
        self.assertEqual(tasks[0].created_by, 1)
        # No assignment row is written for a task nobody holds yet.
        self.assertFalse([item for item in added if isinstance(item, TaskAssignee)])
        db.commit.assert_called_once()

    def test_omitting_the_assignee_skips_the_membership_lookup_entirely(self):
        """The validation is not merely tolerated when there is no assignee;
        it is not run, so it cannot reject a task that names nobody."""
        db = MagicMock()
        db.scalar.return_value = _project()
        db.get.return_value = MagicMock(id=1)

        ProjectManagementService.create_task(
            db, _user(role="admin"), 7, TaskCreate(name="Write the report", status_id=1)
        )

        # One scalar() call only: the project lookup in _project.
        self.assertEqual(db.scalar.call_count, 1)


class ProjectMembershipRoleTests(unittest.TestCase):
    """Who may be a *member* of a project, as opposed to its leader.

    Creating a project used to require every selected member to be
    ``role_name == "employee"``, while ``ProjectMemberService.add_members``
    accepted any active user in the organization. The same action therefore
    succeeded or failed depending only on whether it was done in the create
    drawer or afterwards, and the drawer answered
    "Selected employees have an invalid role." for a perfectly valid choice.
    """

    @staticmethod
    def _db_returning(users):
        db = MagicMock()
        db.scalars.return_value.all.return_value = users
        return db

    def test_a_non_employee_may_be_a_project_member(self):
        members = [
            _user(role="hr", user_id=2),
            _user(role="leader", user_id=3),
            _user(role="administrator", user_id=4),
        ]
        resolved = ProjectManagementService._users(
            self._db_returning(members), _user(), [2, 3, 4], None, "employees"
        )
        self.assertEqual([item.id for item in resolved], [2, 3, 4])

    def test_a_role_restriction_is_still_enforced_when_one_is_given(self):
        # The leader slot still passes a role set, and it must keep rejecting.
        with self.assertRaises(HTTPException) as caught:
            ProjectManagementService._users(
                self._db_returning([_user(role="employee", user_id=2)]),
                _user(), [2], {"leader"}, "leader",
            )
        self.assertEqual(caught.exception.status_code, 400)
        self.assertIn("invalid role", caught.exception.detail)

    def test_someone_outside_the_organization_is_still_refused(self):
        # Widening the role check must not weaken the membership check.
        with self.assertRaises(HTTPException) as caught:
            ProjectManagementService._users(
                self._db_returning([]), _user(), [99], None, "employees"
            )
        self.assertEqual(caught.exception.status_code, 400)
        self.assertIn("do not belong to this organization", caught.exception.detail)

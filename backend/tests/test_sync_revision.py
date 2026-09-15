"""`GET /api/v1/sync/revision` -- the fingerprint the desktop polls.

The contract under test is narrow and worth stating: the revision must move
when, and only when, something the caller can see has changed. So the
components are computed under exactly the scope the list endpoints apply
(an employee's fingerprint is over their projects, not the organization's),
and every kind of change -- insert, update, delete -- has to reach it.
"""
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock

from app.models.user import User
from app.services import sync_revision


def _user(role="administrator", user_id=1):
    return User(id=user_id, organization_id=1, role_name=role, permissions={})


class _Answers:
    """A session whose `execute(...).one()` answers from a scripted list of
    `(count, max_timestamp, max_id)` tuples, recording every statement."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.statements = []

    def execute(self, statement):
        self.statements.append(statement)
        result = MagicMock()
        result.one.return_value = self.answers.pop(0)
        return result

    def scalars(self, statement):
        # `visible_project_ids` asks for led/staffed projects for a leader.
        result = MagicMock()
        result.all.return_value = [7]
        return result


STAMP = datetime(2026, 9, 15, 10, 0, tzinfo=timezone.utc)
LATER = datetime(2026, 9, 15, 10, 5, tzinfo=timezone.utc)

#: projects, memberships, tasks, assignments, time_entries -- in that order.
BASELINE = [(3, STAMP, 30), (5, STAMP, 50), (12, STAMP, 120), (4, STAMP, 40), (9, STAMP, 90)]


def _revision(answers, user=None):
    return sync_revision.scope_revision(_Answers(answers), user or _user())["revision"]


class TestRevisionMovesOnEveryKindOfChange(unittest.TestCase):
    def test_is_stable_for_unchanged_state(self):
        self.assertEqual(_revision(BASELINE), _revision(BASELINE))

    def test_an_insert_moves_it(self):
        changed = list(BASELINE)
        changed[2] = (13, LATER, 121)          # one more task
        self.assertNotEqual(_revision(BASELINE), _revision(changed))

    def test_an_update_moves_it_even_when_the_count_does_not(self):
        changed = list(BASELINE)
        changed[2] = (12, LATER, 120)          # a task renamed: only updated_at
        self.assertNotEqual(_revision(BASELINE), _revision(changed))

    def test_a_delete_moves_it(self):
        changed = list(BASELINE)
        changed[1] = (4, STAMP, 50)            # a membership removed
        self.assertNotEqual(_revision(BASELINE), _revision(changed))

    def test_a_delete_and_insert_in_the_same_instant_still_moves_it(self):
        """The count is unchanged and the timestamp may collide; MAX(id) is
        what distinguishes a reassignment from nothing having happened."""
        changed = list(BASELINE)
        changed[3] = (4, STAMP, 41)
        self.assertNotEqual(_revision(BASELINE), _revision(changed))

    def test_the_callers_own_time_entries_are_part_of_it(self):
        changed = list(BASELINE)
        changed[4] = (9, LATER, 90)            # an entry stopped or approved
        self.assertNotEqual(_revision(BASELINE), _revision(changed))

    def test_every_component_is_named(self):
        components = sync_revision.scope_components(_Answers(BASELINE), _user())
        self.assertEqual(
            set(components), {"projects", "memberships", "tasks", "assignments", "time_entries"}
        )
        self.assertEqual(components["projects"], f"3:{STAMP.isoformat()}:30")

    def test_an_empty_scope_is_a_real_answer(self):
        """A user with nothing to see gets a fingerprint, not an error, and
        one that differs from a user who can see something."""
        empty = [(0, None, None)] * 5
        self.assertNotEqual(_revision(empty), _revision(BASELINE))
        self.assertEqual(_revision(empty), _revision(empty))


class TestScope(unittest.TestCase):
    """The fingerprint is computed over the caller's rows, not the table."""

    def _sql(self, user):
        session = _Answers(BASELINE)
        sync_revision.scope_components(session, user)
        return [str(statement.compile(compile_kwargs={"literal_binds": False}))
                for statement in session.statements]

    def test_an_employee_is_fingerprinted_over_their_memberships(self):
        projects_sql = self._sql(_user(role="employee"))[0]
        self.assertIn("project_members", projects_sql)
        self.assertIn("organization_id", projects_sql)

    def test_an_administrator_is_fingerprinted_over_the_organization(self):
        projects_sql = self._sql(_user(role="administrator"))[0]
        self.assertNotIn("project_members", projects_sql)
        self.assertIn("organization_id", projects_sql)

    def test_archived_rows_are_outside_the_fingerprint(self):
        """Archiving is how projects and tasks are deleted; the lists drop
        them, so the fingerprint must too or a deletion would never show."""
        statements = self._sql(_user())
        self.assertIn("status", statements[0])   # projects
        self.assertIn("status", statements[2])   # tasks

    def test_time_entries_are_the_callers_own(self):
        entries_sql = self._sql(_user(user_id=42))[4]
        self.assertIn("user_id", entries_sql)

    def test_an_employees_tasks_carry_the_task_scope(self):
        """The same narrowing `GET /projects/{id}/tasks` applies: their own,
        assigned to them, or unassigned -- not every task in the project."""
        tasks_sql = self._sql(_user(role="employee"))[2]
        self.assertIn("task_assignees", tasks_sql)
        self.assertIn("created_by", tasks_sql)

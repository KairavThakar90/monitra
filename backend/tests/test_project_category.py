"""The optional project category: 'kyle' | 'st' | none.

A project may be tagged with a category when it is created (and changed on an
edit), but never has to be:

* the schema accepts the two categories and nothing else, and a project created
  without one is simply uncategorised -- no default is invented;
* on an edit, an omitted `category` leaves it alone (the inline status and team
  edits never send it) and an explicit null clears it;
* the list can be filtered by it and every row says what it is;
* a change is recorded in the activity trail, from what to what.

The list and update tests run against a real SQLite database for the same
reason `test_project_member_filter.py` documents: they assert what a `WHERE`
clause and a write actually do, which a mocked session cannot show.
"""
import unittest
from datetime import date, timedelta
from unittest.mock import MagicMock

from pydantic import ValidationError
from sqlalchemy import BigInteger, create_engine, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.permissions import ROLE_PERMISSIONS
from app.models.activity_log import ActivityLog, ActivityLogAction
from app.models.project import Project
from app.models.project_member import ProjectMember
from app.models.task import Task
from app.models.task_assignee import TaskAssignee
from app.models.user import User
from app.schemas.project_management import ProjectCategory, ProjectCreate, ProjectRead, ProjectUpdate
from app.services.project_management import ProjectManagementService
from tests.status_catalog_stub import rows, status_catalog
from tests.test_project_hours_summary import _sqlite_schema


@compiles(JSONB, "sqlite")
def _jsonb_is_json_on_sqlite(type_, compiler, **kw):  # pragma: no cover - dialect shim
    return "JSON"


@compiles(BigInteger, "sqlite")
def _bigint_is_integer_on_sqlite(type_, compiler, **kw):  # pragma: no cover - dialect shim
    return "INTEGER"


ORG = 1
ADMIN, LEADER, OWNER = 1, 2, 9
PROJECT_STATUSES = rows((1, "Active"), (2, "Paused"))
TASK_STATUSES = rows((1, "Todo"))


def _payload(**overrides) -> ProjectCreate:
    fields = dict(
        project_name="Website", status_id=1, owner_id=OWNER, leader_id=LEADER, employee_ids=[],
        deadline=date.today() + timedelta(days=20), billing_type="free",
    )
    fields.update(overrides)
    return ProjectCreate(**fields)


class CategorySchemaTests(unittest.TestCase):
    def test_the_two_categories(self):
        self.assertEqual({category.value for category in ProjectCategory}, {"kyle", "st"})

    def test_a_project_needs_no_category(self):
        self.assertIsNone(_payload().category)

    def test_either_category_is_accepted(self):
        self.assertEqual(_payload(category="kyle").category, ProjectCategory.kyle)
        self.assertEqual(_payload(category="st").category, ProjectCategory.st)

    def test_an_explicit_null_is_the_same_as_none(self):
        self.assertIsNone(_payload(category=None).category)

    def test_any_other_value_is_refused(self):
        for value in ("other", "KYLE", "", "Kyle project"):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                _payload(category=value)

    def test_an_update_distinguishes_omitted_from_cleared(self):
        self.assertNotIn("category", ProjectUpdate(project_name="X").model_dump(exclude_unset=True))
        self.assertIn("category", ProjectUpdate(category=None).model_dump(exclude_unset=True))
        self.assertEqual(ProjectUpdate(category="st").category, ProjectCategory.st)
        with self.assertRaises(ValidationError):
            ProjectUpdate(category="other")

    def test_the_response_carries_it_and_an_older_payload_still_reads(self):
        self.assertIn("category", ProjectRead.model_fields)
        self.assertIsNone(ProjectRead.model_fields["category"].default)


class CategoryCreateTests(unittest.TestCase):
    def _create(self, **overrides) -> Project:
        """Run `create()` against a stubbed session and return the Project it added."""
        db = MagicMock()
        db.scalar.return_value = User(
            id=OWNER, organization_id=ORG, role_name="administrator", permissions={},
            is_active=True, can_own_projects=True,
        )
        leader = User(id=LEADER, organization_id=ORG, role_name="project_leader", permissions={})
        answers = [[leader], [], []]
        db.scalars.return_value.all.side_effect = lambda: answers.pop(0) if answers else []
        actor = User(id=ADMIN, organization_id=ORG, role_name="administrator", permissions={})
        with status_catalog(project_statuses=rows((1, "Active")), task_statuses=rows((1, "Todo"))):
            ProjectManagementService.create(db, actor, _payload(**overrides))
        return next(call.args[0] for call in db.add.call_args_list if isinstance(call.args[0], Project))

    def test_a_chosen_category_is_stored(self):
        self.assertEqual(self._create(category="kyle").category, "kyle")
        self.assertEqual(self._create(category="st").category, "st")

    def test_no_category_is_stored_as_none_not_a_default(self):
        self.assertIsNone(self._create().category)


class _World(unittest.TestCase):
    """An administrator and three projects: one of each category and one
    uncategorised."""

    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        _sqlite_schema(self.engine, User, Project, ProjectMember, Task, TaskAssignee, ActivityLog)
        self.db = Session(self.engine)
        self.admin = User(
            id=ADMIN, organization_id=ORG, username="grace", email="grace@example.com", name="Grace",
            role_name="administrator", permissions={p: True for p in ROLE_PERMISSIONS.get("administrator", set())},
            status="active", is_active=True, idle_enabled=True, idle_minutes=5, capture_frequency=10,
        )
        self.leader = User(
            id=LEADER, organization_id=ORG, username="hank", email="hank@example.com", name="Hank",
            role_name="leader", permissions={}, status="active", is_active=True,
            idle_enabled=True, idle_minutes=5, capture_frequency=10,
        )
        self.db.add_all([self.admin, self.leader])
        for project_id, name, category in ((10, "Kyle one", "kyle"), (11, "ST one", "st"), (12, "Plain", None)):
            self.db.add(Project(
                id=project_id, organization_id=ORG, project_name=name, status="active", status_id=1,
                leader_id=LEADER, billing_type="free", category=category, created_by=ADMIN,
            ))
        self.db.commit()
        catalog = status_catalog(project_statuses=PROJECT_STATUSES, task_statuses=TASK_STATUSES)
        catalog.__enter__()
        self.addCleanup(catalog.__exit__, None, None, None)

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def trail(self):
        self.db.expire_all()
        return [(row.action, row.description) for row in self.db.scalars(select(ActivityLog).order_by(ActivityLog.id)).all()]


class CategoryListTests(_World):
    def _names(self, **kwargs):
        result = ProjectManagementService.list(
            self.db, self.admin, page=1, limit=20, search=None, status_id=None,
            leader_id=None, billing_type=None, include_tasks=False, **kwargs,
        )
        return sorted(item["project_name"] for item in result["items"])

    def test_no_filter_returns_every_project(self):
        self.assertEqual(self._names(), ["Kyle one", "Plain", "ST one"])

    def test_filtering_on_a_category_returns_only_that_category(self):
        self.assertEqual(self._names(category=ProjectCategory.kyle), ["Kyle one"])
        self.assertEqual(self._names(category=ProjectCategory.st), ["ST one"])

    def test_an_uncategorised_project_is_in_neither_category(self):
        self.assertNotIn("Plain", self._names(category=ProjectCategory.kyle))
        self.assertNotIn("Plain", self._names(category=ProjectCategory.st))

    def test_every_row_says_what_its_category_is(self):
        result = ProjectManagementService.list(
            self.db, self.admin, page=1, limit=20, search=None, status_id=None,
            leader_id=None, billing_type=None, include_tasks=False,
        )
        self.assertEqual(
            {item["project_name"]: item["category"] for item in result["items"]},
            {"Kyle one": "kyle", "ST one": "st", "Plain": None},
        )

    def test_the_detail_payload_carries_it_too(self):
        self.assertEqual(ProjectManagementService.get(self.db, self.admin, 10)["category"], "kyle")
        self.assertIsNone(ProjectManagementService.get(self.db, self.admin, 12)["category"])


class CategoryUpdateTests(_World):
    def _update(self, project_id=12, **fields):
        return ProjectManagementService.update(self.db, self.admin, project_id, ProjectUpdate(**fields))

    def test_a_category_can_be_set_on_an_uncategorised_project(self):
        self.assertEqual(self._update(category="st")["category"], "st")
        self.assertEqual(self.db.get(Project, 12).category, "st")

    def test_a_category_can_be_changed(self):
        self.assertEqual(self._update(10, category="st")["category"], "st")

    def test_an_explicit_null_clears_it(self):
        self.assertIsNone(self._update(10, category=None)["category"])
        self.db.expire_all()
        self.assertIsNone(self.db.get(Project, 10).category)

    def test_an_edit_that_does_not_send_it_leaves_it_alone(self):
        # What the inline status and team edits on the list do.
        self._update(10, status_id=2)
        self._update(10, project_name="Kyle renamed")
        self.db.expire_all()
        self.assertEqual(self.db.get(Project, 10).category, "kyle")

    def test_an_unknown_category_is_refused_before_anything_is_written(self):
        with self.assertRaises(ValidationError):
            ProjectUpdate(category="other")
        self.assertEqual(self.db.get(Project, 10).category, "kyle")


class CategoryActivityTests(_World):
    def _update(self, project_id, **fields):
        return ProjectManagementService.update(self.db, self.admin, project_id, ProjectUpdate(**fields))

    def test_a_change_says_from_what_to_what(self):
        self._update(10, category="st")
        self.assertEqual(self.trail(), [(
            ActivityLogAction.PROJECT_UPDATED, 'Updated the project "Kyle one" (category: Kyle project → ST project)',
        )])

    def test_setting_one_on_an_uncategorised_project_says_none_before(self):
        self._update(12, category="kyle")
        self.assertEqual(self.trail(), [(
            ActivityLogAction.PROJECT_UPDATED, 'Updated the project "Plain" (category: none → Kyle project)',
        )])

    def test_clearing_it_says_none_after(self):
        self._update(11, category=None)
        self.assertEqual(self.trail(), [(
            ActivityLogAction.PROJECT_UPDATED, 'Updated the project "ST one" (category: ST project → none)',
        )])

    def test_re_sending_the_same_category_records_nothing(self):
        self._update(10, category="kyle")
        self.assertEqual(self.trail(), [])


if __name__ == "__main__":
    unittest.main()

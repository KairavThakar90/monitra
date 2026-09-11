"""
Creating a project must not insert a membership row that already exists.

The production failure: POST /projects answered HTTP 500 whenever the person
creating the project was also among the selected members -- which the "select
all" control in the create drawer does by default. Some databases carry a
hand-added trigger on `projects`::

    CREATE TRIGGER trg_project_creator_member AFTER INSERT ON public.projects
    FOR EACH ROW EXECUTE FUNCTION add_project_creator_as_member()

whose function inserts (organization_id, id, created_by, created_by) into
`project_members`. It is in no Alembic migration, so it exists in some
deployments and not others. Where it exists, `create` then inserted the creator
a second time, violated `uq_project_member`, and the IntegrityError surfaced as
a 500 -- while the identical request succeeded against a database without the
trigger. That is why this reproduced only in production.

`create` now reads back the memberships the flush actually left behind and adds
only what is missing, so it is correct with the trigger and without it.
"""
import unittest
from datetime import date, timedelta
from unittest.mock import MagicMock

from app.models.project import Project
from app.models.project_member import ProjectMember
from app.models.user import User
from app.schemas.project_management import BillingType, ProjectCreate
from app.services.project_management import ProjectManagementService
from tests.status_catalog_stub import rows, status_catalog

ACTOR_ID = 1
LEADER_ID = 2
OTHER_ID = 3


def _actor():
    return User(id=ACTOR_ID, organization_id=1, role_name="administrator", permissions={})


def _create(existing_member_ids, employee_ids):
    """Run create() with the database reporting `existing_member_ids` already
    present on the new project -- which is what the trigger leaves behind."""
    db = MagicMock()

    leader = User(id=LEADER_ID, organization_id=1, role_name="project_leader", permissions={})
    employees = [
        User(id=item_id, organization_id=1, role_name="employee", permissions={})
        for item_id in employee_ids
    ]
    # In order: _users(leader), _users(employees), then the membership
    # read-back. Both status tables come from the catalogue rather than from
    # this script. Everything after is _detail_payload, which has nothing to
    # find.
    answers = [[leader], employees, list(existing_member_ids)]
    db.scalars.return_value.all.side_effect = lambda: answers.pop(0) if answers else []

    payload = ProjectCreate(
        project_name="Beta launch", status_id=1, leader_id=LEADER_ID,
        employee_ids=list(employee_ids),
        deadline=date.today() + timedelta(days=20),
        billing_type=BillingType.free,
    )
    with status_catalog(
        project_statuses=rows((1, "Active")), task_statuses=rows((1, "Todo"))
    ):
        ProjectManagementService.create(db, _actor(), payload)

    added = [call.args[0] for call in db.add.call_args_list]
    members = [item for item in added if isinstance(item, ProjectMember)]
    project = next(item for item in added if isinstance(item, Project))
    return project, members


class CreatorAlreadyAMemberTests(unittest.TestCase):
    def test_the_creator_the_trigger_added_is_not_inserted_again(self):
        """The exact production 500: the creator is in the member list and the
        trigger has already written their row."""
        _, members = _create(
            existing_member_ids={ACTOR_ID},
            employee_ids=[ACTOR_ID, OTHER_ID],
        )

        user_ids = [m.user_id for m in members]
        self.assertNotIn(ACTOR_ID, user_ids, "would violate uq_project_member")
        self.assertEqual(user_ids, [OTHER_ID])

    def test_no_duplicate_rows_are_ever_written(self):
        _, members = _create(
            existing_member_ids={ACTOR_ID},
            employee_ids=[ACTOR_ID, OTHER_ID, 4, 5],
        )
        user_ids = [m.user_id for m in members]
        self.assertEqual(len(user_ids), len(set(user_ids)))

    def test_without_the_trigger_every_selected_member_is_written(self):
        """A database with no trigger must be unaffected: the fix must not
        quietly drop members that genuinely need inserting."""
        _, members = _create(
            existing_member_ids=set(),
            employee_ids=[ACTOR_ID, OTHER_ID],
        )
        self.assertEqual(sorted(m.user_id for m in members), [ACTOR_ID, OTHER_ID])

    def test_the_project_itself_is_still_created_normally(self):
        project, _ = _create(
            existing_member_ids={ACTOR_ID}, employee_ids=[ACTOR_ID, OTHER_ID]
        )
        self.assertEqual(project.project_name, "Beta launch")
        self.assertEqual(project.status, "active")
        self.assertEqual(project.leader_id, LEADER_ID)


if __name__ == "__main__":
    unittest.main()

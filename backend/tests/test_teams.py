import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.schemas.teams import TeamMemberCardResponse, TeamSummaryResponse
from app.services.teams import TeamsService, _initials, _percent, _status_key


class TeamsTests(unittest.TestCase):
    def test_progress_handles_empty_projects(self):
        self.assertEqual(_percent(0, 0), 0)
        self.assertEqual(_percent(2, 5), 40)

    def test_initials_and_status_keys(self):
        self.assertEqual(_initials("Alice Cooper"), "AC")
        self.assertEqual(_initials("Single"), "S")
        self.assertEqual(_status_key("To Do"), "todo")
        self.assertEqual(_status_key("In Progress"), "in_progress")

    def test_summary_response_shape(self):
        response = TeamSummaryResponse(
            team_leaders=3,
            employees=8,
            total_projects=35,
            active_projects=8,
        )
        self.assertEqual(response.model_dump(), {
            "team_leaders": 3,
            "employees": 8,
            "total_projects": 35,
            "active_projects": 8,
        })

    def test_the_two_people_tiles_partition_the_directory(self):
        """Team Leaders + Team Members must account for every active member.

        The Members screen lists the whole directory, so when these two tiles
        are read next to it they are being used as a breakdown of it. Counting
        `role_name == "employee"` in the Team Members tile left HR — and any
        manager — in neither tile, so the tiles under-reported the organization
        against a directory the same admin could see in full.
        """
        db = MagicMock()
        db.scalar.return_value = 0
        db.execute.return_value.all.return_value = []
        TeamsService.summary(db, SimpleNamespace(id=1, organization_id=1, role_name="admin"))

        # The first two counts are the leader tile and the member tile.
        leaders_where = str(db.scalar.call_args_list[0][0][0]).split("WHERE", 1)[1]
        members_where = str(db.scalar.call_args_list[1][0][0]).split("WHERE", 1)[1]
        self.assertIn("role_name IN", leaders_where)
        self.assertIn("role_name NOT IN", members_where)
        # Complementary sets over the same population: both stay scoped to the
        # organization and to active members.
        for clause in (leaders_where, members_where):
            self.assertIn("organization_id", clause)
            self.assertIn("is_active", clause)

    def test_member_card_serializes_task_status(self):
        member = SimpleNamespace(id=7, name="Alice Cooper", designation="Engineer", role_name="employee")
        task = SimpleNamespace(id=15, task_name="Implement fix", assignee_id=7, status_id=2)
        task_status = SimpleNamespace(id=2, name="In Progress", color="#2563EB")

        response = TeamsService._member_card(
            None,
            member,
            13,
            [task],
            {2: task_status},
            set(),
        )

        validated = TeamMemberCardResponse.model_validate(response)
        self.assertEqual(validated.tasks[0].status.model_dump(), {
            "id": 2,
            "name": "In Progress",
            "color": "#2563EB",
        })


if __name__ == "__main__":
    unittest.main()

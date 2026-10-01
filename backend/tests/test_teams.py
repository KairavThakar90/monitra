import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from fastapi import HTTPException

from app.schemas.teams import TeamMemberCardResponse, TeamProjectCardResponse, TeamSummaryResponse
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
        TeamsService.summary(db, SimpleNamespace(id=1, organization_id=1, role_name="administrator"))

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

    def test_only_leader_roles_are_listed_as_team_leaders(self):
        """The Teams directory lists leaders, not administrators.

        An administrator can still lead a project (LEADER_ROLE_NAMES), but the
        list, the Team Leaders tile and the leader lookup all use the narrower
        TEAM_LEADER_ROLE_NAMES, so they cannot disagree with each other.
        """
        from app.core.permissions import LEADER_ROLE_NAMES, TEAM_LEADER_ROLE_NAMES

        self.assertIn("administrator", LEADER_ROLE_NAMES)
        self.assertNotIn("administrator", TEAM_LEADER_ROLE_NAMES)
        self.assertEqual(set(TEAM_LEADER_ROLE_NAMES), {"leader", "project_leader"})

        admin = SimpleNamespace(id=1, organization_id=1, role_name="administrator")

        db = MagicMock()
        db.scalar.return_value = 0
        db.scalars.return_value.all.return_value = []
        TeamsService.leaders(db, admin, 1, 20, None)
        listing = db.scalar.call_args_list[0][0][0].compile().params
        self.assertNotIn("administrator", [v for p in listing.values() for v in (p if isinstance(p, (list, tuple, set)) else [p])])
        self.assertIn("leader", [v for p in listing.values() for v in (p if isinstance(p, (list, tuple, set)) else [p])])

        db = MagicMock()
        db.scalar.return_value = 0
        TeamsService.summary(db, admin)
        tile = db.scalar.call_args_list[0][0][0].compile().params
        self.assertNotIn("administrator", [v for p in tile.values() for v in (p if isinstance(p, (list, tuple, set)) else [p])])

        # Opening an administrator's team page directly is "not found" too.
        db = MagicMock()
        db.scalar.return_value = None
        with self.assertRaises(HTTPException) as caught:
            TeamsService.leader_detail(db, admin, 1)
        self.assertEqual(caught.exception.status_code, 404)
        lookup = db.scalar.call_args_list[0][0][0].compile().params
        self.assertNotIn("administrator", [v for p in lookup.values() for v in (p if isinstance(p, (list, tuple, set)) else [p])])

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

    def test_project_status_falls_back_to_the_legacy_status_string(self):
        # Production holds projects with `status_id` NULL and only the legacy
        # `status` string; the lookup by id alone returned None and 500'd the
        # leader's project list.
        statuses = {
            1: SimpleNamespace(id=1, name="Active", color="#16A34A"),
            2: SimpleNamespace(id=2, name="Paused", color="#F59E0B"),
        }
        by_id = SimpleNamespace(status_id=2, status="active")
        legacy_only = SimpleNamespace(status_id=None, status="active")
        unknown = SimpleNamespace(status_id=None, status="planning")

        self.assertEqual(TeamsService._project_status(by_id, statuses).id, 2)
        self.assertEqual(TeamsService._project_status(legacy_only, statuses).id, 1)
        self.assertIsNone(TeamsService._project_status(unknown, statuses))

    def test_project_card_accepts_a_project_with_no_resolvable_status(self):
        card = {
            "id": 1, "project_name": "P", "description": None, "status": None,
            "created_at": "2026-01-01T00:00:00", "deadline": None, "member_count": 0,
            "members_preview": [],
            "task_progress": {"completed": 0, "total": 0, "percentage": 0},
        }
        self.assertIsNone(TeamProjectCardResponse.model_validate(card).status)


if __name__ == "__main__":
    unittest.main()

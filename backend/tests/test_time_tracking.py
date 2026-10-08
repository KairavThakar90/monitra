import unittest
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

from app.core.time_format import ist_today
from app.schemas.time_tracking import (
    ActiveTimeTrackingResponse,
    TimeTrackingDetailResponse,
    TimeTrackingListResponse,
)
from app.services.time_tracking import TimeTrackingService


class TimeTrackingTests(unittest.TestCase):
    def setUp(self):
        self.user = SimpleNamespace(
            id=10,
            organization_id=3,
            name="Employee",
            email="employee@example.com",
            designation="Developer",
            role_name="employee",
            permissions={"time_entries:view_all": True},
        )

    def test_date_bounds(self):
        # "today" is the IST calendar day, not the server's UTC day.
        self.assertEqual(TimeTrackingService.date_bounds("today", None, None, None), (ist_today(), ist_today()))
        self.assertEqual(
            TimeTrackingService.date_bounds(None, None, date(2026, 8, 1), date(2026, 8, 26)),
            (date(2026, 8, 1), date(2026, 8, 26)),
        )

    def test_invalid_date_filter(self):
        with self.assertRaises(HTTPException) as error:
            TimeTrackingService.date_bounds(None, None, date(2026, 8, 27), date(2026, 8, 26))
        self.assertEqual(error.exception.status_code, 400)

    def test_list_serializes_aggregated_duration(self):
        row = {
            "employee_id": 10,
            "name": "Employee",
            "email": "employee@example.com",
            "designation": "Developer",
            "work_date": date(2026, 8, 26),
            "start_time": datetime(2026, 8, 26, 10, tzinfo=timezone.utc),
            "end_time": datetime(2026, 8, 26, 16, tzinfo=timezone.utc),
            "total_seconds": 5 * 3600,
        }
        with patch("app.services.time_tracking.TimeTrackingRepository.list_daily_totals", return_value=([row], 1)):
            response = TimeTrackingService.list_daily(
                None, self.user, None, date(2026, 8, 26), None, None, None, None, 1, 50
            )
        validated = TimeTrackingListResponse.model_validate(response)
        self.assertEqual(validated.items[0].total_hours, "5h 0m")
        self.assertEqual(validated.items[0].total_time, "05:00:00")
        self.assertEqual(validated.items[0].total_seconds, 5 * 3600)

    def test_detail_aggregates_multiple_tasks_and_running_entry(self):
        project = SimpleNamespace(id=92, project_name="Alpha")
        task = SimpleNamespace(id=185, task_name="Setup")
        project_status = SimpleNamespace(id=1, name="Active", color="#3B82F6")
        task_status = SimpleNamespace(id=2, name="In Progress", color="#F59E0B")
        first = SimpleNamespace(
            id=1001,
            start_time=datetime(2026, 8, 26, 10, tzinfo=timezone.utc),
            end_time=datetime(2026, 8, 26, 12, tzinfo=timezone.utc),
            is_manual=False,
        )
        running = SimpleNamespace(
            id=1002,
            start_time=datetime(2026, 8, 26, 13, tzinfo=timezone.utc),
            end_time=None,
            is_manual=True,
        )
        rows = [(first, project, task, project_status, task_status, 7200), (running, project, task, project_status, task_status, 3600)]
        with patch("app.services.time_tracking.TimeTrackingRepository.get_employee", return_value=self.user), \
             patch("app.services.time_tracking.TimeTrackingRepository.detail_entries", return_value=rows):
            response = TimeTrackingService.detail(
                None, self.user, 10, None, date(2026, 8, 26), None, None
            )
        validated = TimeTrackingDetailResponse.model_validate(response)
        self.assertEqual(validated.summary.total_seconds, 10800)
        self.assertEqual(validated.projects[0].tasks[0].total_seconds, 10800)
        self.assertTrue(validated.projects[0].tasks[0].entries[1].is_running)
        self.assertFalse(validated.projects[0].tasks[0].entries[0].is_manual)
        self.assertTrue(validated.projects[0].tasks[0].entries[1].is_manual)

    def test_unprivileged_employee_filter_is_forbidden(self):
        self.user.permissions = {}
        with self.assertRaises(HTTPException) as error:
            TimeTrackingService.list_daily(None, self.user, "today", None, None, None, [11], None, 1, 50)
        self.assertEqual(error.exception.status_code, 403)

    def test_multiple_employee_ids_accepted_when_privileged(self):
        with patch("app.services.time_tracking.TimeTrackingRepository.list_daily_totals", return_value=([], 0)) as repo:
            TimeTrackingService.list_daily(None, self.user, "today", None, None, None, [10, 11], "ada", 1, 50)
        self.assertEqual(repo.call_args.args[4], [10, 11])
        self.assertEqual(repo.call_args.args[5], "ada")

    def test_unprivileged_user_forced_to_own_id_regardless_of_filter(self):
        self.user.permissions = {}
        with patch("app.services.time_tracking.TimeTrackingRepository.list_daily_totals", return_value=([], 0)) as repo:
            TimeTrackingService.list_daily(None, self.user, "today", None, None, None, None, None, 1, 50)
        self.assertEqual(repo.call_args.args[4], [self.user.id])


class ActiveTimeTrackingTests(unittest.TestCase):
    """`GET /time-tracking/active`: who has a timer running right now."""

    def setUp(self):
        self.user = SimpleNamespace(
            id=10,
            organization_id=3,
            role_name="administrator",
            permissions={"time_entries:view_all": True},
        )
        # Today's activity per member; no database here. Tests that care set `.return_value`.
        patcher = patch(
            "app.services.time_tracking.TimeEntryActivityRepository.get_day_totals_for_users",
            return_value={},
        )
        self.day_totals = patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def _row(entry_id=501, member_id=11, elapsed=3725.4):
        entry = SimpleNamespace(id=entry_id, start_time=datetime(2026, 9, 30, 4, 30, tzinfo=timezone.utc))
        member = SimpleNamespace(id=member_id, name="Asha Patel", email="asha@example.com", designation="Developer")
        project = SimpleNamespace(id=92, project_name="Alpha")
        task = SimpleNamespace(id=185, task_name="Build login screen")
        return (entry, member, project, task, elapsed)

    def test_each_item_names_the_member_project_and_task(self):
        with patch("app.services.time_tracking.TimeTrackingRepository.list_active", return_value=[self._row()]):
            response = TimeTrackingService.list_active(None, self.user)
        validated = ActiveTimeTrackingResponse.model_validate(response)
        item = validated.items[0]
        self.assertEqual(validated.total, 1)
        self.assertEqual(item.name, "Asha Patel")
        self.assertEqual(item.project_name, "Alpha")
        self.assertEqual(item.task_name, "Build login screen")
        self.assertEqual(item.time_entry_id, 501)

    def test_elapsed_is_whole_seconds_rendered_as_hms(self):
        with patch("app.services.time_tracking.TimeTrackingRepository.list_active", return_value=[self._row(elapsed=3725.9)]):
            validated = ActiveTimeTrackingResponse.model_validate(TimeTrackingService.list_active(None, self.user))
        self.assertEqual(validated.items[0].elapsed_seconds, 3725)
        self.assertEqual(validated.items[0].elapsed_time, "01:02:05")

    def test_net_elapsed_is_floored_at_zero(self):
        # Deductions can exceed what has elapsed so far; the page never shows a negative clock.
        with patch("app.services.time_tracking.TimeTrackingRepository.list_active", return_value=[self._row(elapsed=-40.0)]):
            validated = ActiveTimeTrackingResponse.model_validate(TimeTrackingService.list_active(None, self.user))
        self.assertEqual(validated.items[0].elapsed_seconds, 0)
        self.assertEqual(validated.items[0].elapsed_time, "00:00:00")

    def test_activity_is_the_members_duration_weighted_figure_for_today(self):
        # 80% for 120 s and 0% for 10 s: weighted 9600 / 130 s = 73.8 -> 74 (a per-window mean says 40).
        self.day_totals.return_value = {11: (9600.0, 130)}
        with patch("app.services.time_tracking.TimeTrackingRepository.list_active", return_value=[self._row()]):
            validated = ActiveTimeTrackingResponse.model_validate(TimeTrackingService.list_active(None, self.user))
        self.assertEqual(validated.items[0].activity_percentage, 74)

    def test_activity_is_unknown_not_zero_when_nothing_was_measured(self):
        with patch("app.services.time_tracking.TimeTrackingRepository.list_active", return_value=[self._row()]):
            validated = ActiveTimeTrackingResponse.model_validate(TimeTrackingService.list_active(None, self.user))
        self.assertIsNone(validated.items[0].activity_percentage)

    def test_a_measured_zero_is_zero(self):
        self.day_totals.return_value = {11: (0.0, 600)}
        with patch("app.services.time_tracking.TimeTrackingRepository.list_active", return_value=[self._row()]):
            validated = ActiveTimeTrackingResponse.model_validate(TimeTrackingService.list_active(None, self.user))
        self.assertEqual(validated.items[0].activity_percentage, 0)

    def test_activity_is_looked_up_once_for_all_members_in_the_callers_organization(self):
        rows = [self._row(entry_id=1, member_id=12), self._row(entry_id=2, member_id=11)]
        with patch("app.services.time_tracking.TimeTrackingRepository.list_active", return_value=rows):
            TimeTrackingService.list_active(None, self.user)
        self.assertEqual(self.day_totals.call_count, 1)
        args = self.day_totals.call_args.args
        self.assertEqual(args[1], 3)
        self.assertEqual(args[2], [11, 12])

    def test_nobody_running_is_an_empty_list(self):
        with patch("app.services.time_tracking.TimeTrackingRepository.list_active", return_value=[]):
            validated = ActiveTimeTrackingResponse.model_validate(TimeTrackingService.list_active(None, self.user))
        self.assertEqual(validated.items, [])
        self.assertEqual(validated.total, 0)

    def test_organization_wide_caller_is_not_narrowed(self):
        with patch("app.services.time_tracking.TimeTrackingRepository.list_active", return_value=[]) as repo:
            TimeTrackingService.list_active(None, self.user)
        self.assertEqual(repo.call_args.args[1], 3)
        self.assertIsNone(repo.call_args.args[2])

    def test_caller_without_view_all_sees_only_themselves(self):
        self.user.permissions = {}
        with patch("app.services.time_tracking.TimeTrackingRepository.list_active", return_value=[]) as repo:
            TimeTrackingService.list_active(None, self.user)
        self.assertEqual(repo.call_args.args[2], [self.user.id])

    def test_leader_is_narrowed_to_their_team(self):
        self.user.role_name = "leader"
        with patch("app.services.time_tracking.visible_member_ids", return_value={10, 11, 12}), \
             patch("app.services.time_tracking.TimeTrackingRepository.list_active", return_value=[]) as repo:
            TimeTrackingService.list_active(object(), self.user)
        self.assertEqual(repo.call_args.args[2], [10, 11, 12])

    def test_query_filters_running_entries_in_the_callers_organization(self):
        from sqlalchemy.dialects import postgresql

        from app.repositories.time_tracking import TimeTrackingRepository

        org_wide = TimeTrackingRepository.active_entries_query(3, None)
        sql = str(org_wide.compile(dialect=postgresql.dialect()))
        self.assertIn("time_entries.end_time IS NULL", sql)
        self.assertIn("time_entries.organization_id =", sql)
        self.assertNotIn("time_entries.user_id IN", sql)

        narrowed = TimeTrackingRepository.active_entries_query(3, [10, 11])
        self.assertIn("time_entries.user_id IN", str(narrowed.compile(dialect=postgresql.dialect())))

    def test_active_route_is_registered_before_the_employee_id_route(self):
        # `/{employee_id}` would otherwise swallow "active" and answer 422.
        from app.api.time_tracking import router

        paths = [route.path for route in router.routes]
        self.assertIn("/api/v1/time-tracking/active", paths)
        self.assertLess(
            paths.index("/api/v1/time-tracking/active"),
            paths.index("/api/v1/time-tracking/{employee_id}"),
        )


if __name__ == "__main__":
    unittest.main()

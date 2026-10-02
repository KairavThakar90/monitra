"""
Task Listing (`GET /reports/project-task-summary`): project type and budget usage.

Two additions to the report behind the Task Listing screen:

* a `billing_type` filter -- fixed, free (flexible time) or non_billing, and any
  combination of them -- applied before the page is cut, so a filtered list
  pages and counts truthfully;
* each project reports its billing type and, for a fixed-hours project, how much
  of its budget it has spent. That figure comes from the same shared calculation
  the dashboard uses, so a project lands in the same colour band on both.
"""
import unittest
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from app.repositories.reports import ReportsRepository
from app.schemas.reports import ProjectTaskSummaryResponse
from app.services.project_hours import ProjectHours, usage_percentage
from app.services.reports import ReportsService

HOUR = 3600


def _project(pid, billing_type, fixed_hours=None):
    return SimpleNamespace(
        id=pid, project_name=f"P{pid}", created_at=datetime(2026, 8, 1), status_id=None,
        billing_type=billing_type, fixed_hours=fixed_hours,
    )


def _summary(projects, used_hours_by_id=None, billing_types=None):
    """Run the service with every repository read stubbed; returns (response, mocks)."""
    used = {pid: ProjectHours(total_seconds=int(h * HOUR)) for pid, h in (used_hours_by_id or {}).items()}
    user = SimpleNamespace(id=54, organization_id=1)
    with patch("app.services.reports.ReportsRepository.project_ids_tracked_between", return_value={p.id for p in projects}), \
         patch("app.services.reports.ReportsRepository.paginated_projects",
               return_value=(projects, len(projects))) as paginated, \
         patch("app.services.reports.ReportsRepository.session_seconds_by", return_value={}), \
         patch("app.services.reports.ReportsRepository.active_tasks_by_project", return_value={}), \
         patch("app.services.reports.ReportsRepository.tasks_touched_today", return_value=set()), \
         patch("app.services.reports.ReportsRepository.project_statuses_lookup", return_value={}), \
         patch("app.services.reports.all_time_project_hours", return_value=used) as hours:
        response = ReportsService.build_project_task_summary(
            None, user, 1, 5, None, None, None, None, billing_types,
        )
    return response, paginated, hours


class UsagePercentageTests(unittest.TestCase):
    """The one definition of "how much of its budget": same figure, same colour, every screen."""

    def test_used_over_fixed(self):
        self.assertEqual(usage_percentage("fixed", 1200, 1080 * HOUR), 90.0)
        self.assertEqual(usage_percentage("fixed", 40, 40 * HOUR), 100.0)
        self.assertEqual(usage_percentage("fixed", 1200, 1300 * HOUR), round(1300 / 1200 * 100, 2))

    def test_nothing_used_is_zero_not_none(self):
        self.assertEqual(usage_percentage("fixed", 40, 0), 0.0)

    def test_used_hours_are_rounded_to_two_decimals_before_dividing(self):
        # 3 h 59 m 59 s is 3.9997 h, which the dashboard has always printed as 4.00 h -> exactly 100%.
        self.assertEqual(usage_percentage("fixed", 4, 4 * HOUR - 1), 100.0)

    def test_only_a_fixed_budget_has_a_percentage(self):
        self.assertIsNone(usage_percentage("free", None, 10 * HOUR))
        self.assertIsNone(usage_percentage("non_billing", None, 10 * HOUR))
        self.assertIsNone(usage_percentage("fixed", None, 10 * HOUR))  # fixed, but no budget set
        self.assertIsNone(usage_percentage("fixed", 0, 10 * HOUR))


class ProjectBillingFieldsTests(unittest.TestCase):
    def test_a_fixed_project_reports_its_budget_and_usage(self):
        response, _, _ = _summary([_project(1, "fixed", 40)], {1: 32})
        item = response["projects"][0]
        self.assertEqual(item["billing_type"], "fixed")
        self.assertEqual(item["fixed_hours"], 40.0)
        self.assertEqual(item["used_seconds"], 32 * HOUR)
        self.assertEqual(item["remaining_seconds"], 8 * HOUR)
        self.assertEqual(item["usage_percentage"], 80.0)

    def test_over_budget_reports_a_negative_remaining_and_over_100_percent(self):
        response, _, _ = _summary([_project(1, "fixed", 40)], {1: 44})
        item = response["projects"][0]
        self.assertEqual(item["remaining_seconds"], -4 * HOUR)
        self.assertEqual(item["usage_percentage"], 110.0)

    def test_flexible_and_non_billing_projects_have_nothing_to_measure(self):
        response, _, hours = _summary([_project(1, "free"), _project(2, "non_billing")])
        free, non_billing = response["projects"]
        self.assertEqual((free["billing_type"], non_billing["billing_type"]), ("free", "non_billing"))
        for item in (free, non_billing):
            self.assertIsNone(item["fixed_hours"])
            self.assertIsNone(item["used_seconds"])
            self.assertIsNone(item["remaining_seconds"])
            self.assertIsNone(item["usage_percentage"])
        hours.assert_not_called()  # a page without a fixed project costs no extra query

    def test_only_fixed_projects_are_looked_up_for_usage(self):
        _, _, hours = _summary([_project(1, "fixed", 40), _project(2, "free"), _project(3, "non_billing")], {1: 5})
        self.assertEqual(hours.call_args.args[2], [1])

    def test_the_response_matches_its_schema(self):
        response, _, _ = _summary([_project(1, "fixed", 40), _project(2, "free"), _project(3, "non_billing")], {1: 40})
        parsed = ProjectTaskSummaryResponse.model_validate(response)
        self.assertEqual([p.billing_type for p in parsed.projects], ["fixed", "free", "non_billing"])
        self.assertEqual(parsed.projects[0].usage_percentage, 100.0)


class BillingTypeFilterTests(unittest.TestCase):
    def test_the_filter_reaches_the_paged_query(self):
        _, paginated, _ = _summary([_project(1, "fixed", 40)], {1: 1}, billing_types=["fixed", "free"])
        self.assertEqual(paginated.call_args.kwargs["billing_types"], ["fixed", "free"])

    def test_no_filter_means_every_type(self):
        _, paginated, _ = _summary([_project(1, "free")])
        self.assertIsNone(paginated.call_args.kwargs["billing_types"])

    def test_the_query_filters_on_billing_type_before_paging(self):
        captured = {}

        class _Session:
            def execute(self, statement):
                captured["sql"] = str(statement.compile(dialect=postgresql.dialect()))
                return SimpleNamespace(all=lambda: [])

            def scalar(self, _statement):
                return 0

        ReportsRepository.paginated_projects(_Session(), 1, [1, 2], 1, 5, billing_types=["non_billing"])
        sql = captured["sql"]
        self.assertIn("projects.billing_type IN", sql)
        # The filter is part of the same statement that cuts the page.
        self.assertLess(sql.index("projects.billing_type IN"), sql.index("LIMIT"))

    def test_the_query_has_no_billing_clause_without_a_filter(self):
        captured = {}

        class _Session:
            def execute(self, statement):
                captured["sql"] = str(statement.compile(dialect=postgresql.dialect()))
                return SimpleNamespace(all=lambda: [])

            def scalar(self, _statement):
                return 0

        ReportsRepository.paginated_projects(_Session(), 1, None, 1, 5)
        self.assertNotIn("projects.billing_type IN", captured["sql"])
        ReportsRepository.paginated_projects(_Session(), 1, None, 1, 5, billing_types=[])
        self.assertNotIn("projects.billing_type IN", captured["sql"])

    def test_the_route_accepts_repeated_billing_types_and_refuses_unknown_ones(self):
        from fastapi import Query  # noqa: F401  (route signature is exercised below)
        from fastapi.testclient import TestClient
        from types import SimpleNamespace as NS

        from app.core.database import get_db
        from app.core.security import get_current_user
        from app.main import app

        seen = {}

        def fake_build(db, user, page, limit, project_ids, single_date, start_date, end_date, billing_types=None):
            seen["billing_types"] = billing_types
            return {"projects": [], "pagination": {"page": 1, "limit": 5, "total_projects": 0, "total_pages": 0}}

        admin = NS(id=1, organization_id=1, role_name="administrator", permissions={"time_entries:view_all": True})
        app.dependency_overrides[get_current_user] = lambda: admin
        app.dependency_overrides[get_db] = lambda: iter([None])
        try:
            with patch("app.react_apis.reports.ReportsService.build_project_task_summary", side_effect=fake_build):
                client = TestClient(app)
                ok = client.get("/api/v1/reports/project-task-summary?billing_type=free&billing_type=fixed")
                self.assertEqual(ok.status_code, 200, ok.text)
                self.assertEqual(seen["billing_types"], ["fixed", "free"])  # de-duplicated and sorted

                seen.clear()
                none = client.get("/api/v1/reports/project-task-summary")
                self.assertEqual(none.status_code, 200)
                self.assertIsNone(seen["billing_types"])

                bad = client.get("/api/v1/reports/project-task-summary?billing_type=billable")
                self.assertEqual(bad.status_code, 422)
        finally:
            app.dependency_overrides.clear()


if __name__ == "__main__":
    unittest.main()

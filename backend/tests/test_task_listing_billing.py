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
    response, paginated, hours, _ = _summary_with_reads(projects, used_hours_by_id, billing_types)
    return response, paginated, hours


def _summary_with_reads(projects, used_hours_by_id=None, billing_types=None, member_ids=None, user=None, allowed_members=None):
    """As `_summary`, also returning the repository reads that carry the member filter.

    `allowed_members` stands in for `visible_member_ids`: None is "no restriction"
    (everyone but a leader), a set is a leader's team.
    """
    used = {pid: ProjectHours(total_seconds=int(h * HOUR)) for pid, h in (used_hours_by_id or {}).items()}
    user = user or SimpleNamespace(id=54, organization_id=1)
    reads = {}
    with patch("app.services.reports.ReportsRepository.project_ids_tracked_between",
               return_value={p.id for p in projects}) as reads["tracked"], \
         patch("app.services.reports.ReportsRepository.paginated_projects",
               return_value=(projects, len(projects))) as paginated, \
         patch("app.services.reports.ReportsRepository.session_seconds_by", return_value={}) as reads["seconds"], \
         patch("app.services.reports.ReportsRepository.active_tasks_by_project", return_value={}), \
         patch("app.services.reports.ReportsRepository.tasks_touched_today", return_value=set()) as reads["touched"], \
         patch("app.services.reports.ReportsRepository.project_statuses_lookup", return_value={}), \
         patch("app.services.reports.visible_member_ids", return_value=allowed_members), \
         patch("app.services.reports.all_time_project_hours", return_value=used) as hours:
        response = ReportsService.build_project_task_summary(
            None, user, 1, 5, None, None, None, None, billing_types, member_ids,
        )
    return response, paginated, hours, reads


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

        def fake_build(db, user, page, limit, project_ids, single_date, start_date, end_date, billing_types=None, member_ids=None):
            seen["billing_types"] = billing_types
            seen["member_ids"] = member_ids
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

                seen.clear()
                members = client.get("/api/v1/reports/project-task-summary?member_id=7&member_id=9")
                self.assertEqual(members.status_code, 200, members.text)
                self.assertEqual(seen["member_ids"], [7, 9])

                seen.clear()
                client.get("/api/v1/reports/project-task-summary")
                self.assertIsNone(seen["member_ids"])
        finally:
            app.dependency_overrides.clear()


class MemberFilterTests(unittest.TestCase):
    """The Task Listing's member filter: what those people did today, with their hours."""

    def test_the_filter_reaches_every_read_that_decides_what_is_shown(self):
        _, _, _, reads = _summary_with_reads([_project(1, "free")], member_ids=[9, 7])
        self.assertEqual(reads["tracked"].call_args.kwargs["member_ids"], [7, 9])   # which projects appear
        self.assertEqual(reads["touched"].call_args.kwargs["member_ids"], [7, 9])   # which tasks appear
        for call in reads["seconds"].call_args_list:                                # the hours shown
            self.assertEqual(call.args[3], [7, 9])

    def test_no_filter_leaves_everyone_in(self):
        _, _, _, reads = _summary_with_reads([_project(1, "free")])
        self.assertIsNone(reads["tracked"].call_args.kwargs["member_ids"])
        self.assertIsNone(reads["touched"].call_args.kwargs["member_ids"])
        for call in reads["seconds"].call_args_list:
            self.assertIsNone(call.args[3])

    def test_duplicates_are_collapsed(self):
        _, _, _, reads = _summary_with_reads([_project(1, "free")], member_ids=[3, 3, 3])
        self.assertEqual(reads["tracked"].call_args.kwargs["member_ids"], [3])

    def test_a_budget_is_spent_by_everyone_so_it_is_not_narrowed_to_the_members(self):
        # Used-vs-fixed must read the same however the page is filtered, or the colour would lie.
        _, _, hours, _ = _summary_with_reads([_project(1, "fixed", 40)], {1: 10}, member_ids=[7])
        args, kwargs = hours.call_args
        self.assertEqual(len(args), 3)  # (db, organization_id, fixed project ids) -- no member argument
        self.assertNotIn("member_ids", kwargs)

    def test_it_combines_with_the_billing_type_filter(self):
        _, paginated, _, reads = _summary_with_reads(
            [_project(1, "fixed", 40)], {1: 5}, billing_types=["fixed"], member_ids=[7],
        )
        self.assertEqual(paginated.call_args.kwargs["billing_types"], ["fixed"])
        self.assertEqual(reads["tracked"].call_args.kwargs["member_ids"], [7])

    def test_a_leader_is_held_to_their_team(self):
        leader = SimpleNamespace(id=5, organization_id=1)
        _, _, _, reads = _summary_with_reads(
            [_project(1, "free")], member_ids=[7, 8, 99], user=leader, allowed_members={5, 7, 8},
        )
        self.assertEqual(reads["tracked"].call_args.kwargs["member_ids"], [7, 8])

    def test_a_leader_asking_only_for_outsiders_gets_nobody_not_everybody(self):
        leader = SimpleNamespace(id=5, organization_id=1)
        response, paginated, _, reads = _summary_with_reads(
            [_project(1, "free")], member_ids=[99], user=leader, allowed_members={5, 7},
        )
        self.assertEqual(response["projects"], [])
        self.assertEqual(response["pagination"]["total_projects"], 0)
        # Answered before any read: an empty list must never decay into "no filter".
        reads["tracked"].assert_not_called()
        paginated.assert_not_called()

    def test_without_a_filter_a_leader_is_not_narrowed_to_their_team(self):
        # Their projects are already scoped; narrowing the *people* too would hide an
        # admin's time on a project the leader leads.
        leader = SimpleNamespace(id=5, organization_id=1)
        _, _, _, reads = _summary_with_reads([_project(1, "free")], user=leader, allowed_members={5, 7})
        self.assertIsNone(reads["tracked"].call_args.kwargs["member_ids"])

    def test_the_queries_filter_on_the_member(self):
        captured = []

        class _Session:
            def scalars(self, statement):
                captured.append(str(statement.compile(dialect=postgresql.dialect())))
                return SimpleNamespace(all=lambda: [])

        with_members = ReportsRepository.project_ids_tracked_between(
            _Session(), 1, None, datetime(2026, 8, 1), datetime(2026, 8, 2), date(2026, 8, 1), date(2026, 8, 1),
            member_ids=[7],
        )
        self.assertEqual(with_members, set())
        self.assertTrue(all("user_id IN" in sql for sql in captured), captured)

        captured.clear()
        ReportsRepository.tasks_touched_today(
            _Session(), 1, [1], datetime(2026, 8, 1), datetime(2026, 8, 2), date(2026, 8, 1), date(2026, 8, 1),
            member_ids=[7],
        )
        self.assertEqual(len(captured), 2)
        self.assertTrue(all("user_id IN" in sql for sql in captured), captured)

        captured.clear()
        ReportsRepository.tasks_touched_today(
            _Session(), 1, [1], datetime(2026, 8, 1), datetime(2026, 8, 2), date(2026, 8, 1), date(2026, 8, 1),
        )
        self.assertFalse(any("user_id IN" in sql for sql in captured), captured)


if __name__ == "__main__":
    unittest.main()

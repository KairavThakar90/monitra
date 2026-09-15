"""Tests for the React Dashboard APIs (app/react_apis/dashboard).

The dashboard reuses the Reports page's filter resolution and entry-grain
aggregation, so the tests here concentrate on what is actually new: the
active-project count, the gap-filled time series, the donut chart's
percentage denominator, and the guarantee that every section is computed from
one shared filter set.
"""

import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy.dialects import postgresql

from app.core.time_format import ist_day_end_utc, ist_day_start_utc, ist_today
from app.react_apis.dashboard.repository import DashboardRepository
from app.react_apis.dashboard.service import DEFAULT_TOP_N, DashboardService
from app.react_apis.reports_page.repository import ReportFilters, ReportsPageRepository
from app.react_apis.reports_page.service import ReportsPageService


def _filters(**overrides):
    base = dict(
        organization_id=1,
        start_date=date(2026, 9, 1),
        end_date=date(2026, 9, 7),
        start_time=ist_day_start_utc(date(2026, 9, 1)),
        end_time=ist_day_end_utc(date(2026, 9, 7)),
    )
    base.update(overrides)
    return ReportFilters(**base)


def _sql(statement) -> str:
    return str(statement.compile(dialect=postgresql.dialect()))


class RecordingSession:
    def __init__(self, rows=None, one=None, scalar=0):
        self.statements = []
        self._rows = rows or []
        # `_paginate` reads its row count and its scope-wide seconds out of
        # one aggregate row, so the default `.one()` is a two-value row.
        self._one = (0, 0.0) if one is None else one
        self._scalar = scalar

    def execute(self, statement):
        self.statements.append(statement)
        return SimpleNamespace(all=lambda: self._rows, one=lambda: self._one)

    def scalar(self, statement):
        self.statements.append(statement)
        return self._scalar


class SharedFilterTests(unittest.TestCase):
    def test_dashboard_reuses_the_reports_filter_resolver(self):
        # Not "behaves the same as" -- literally the same function, so the two
        # pages cannot drift apart.
        self.assertIs(DashboardService.resolve_filters, ReportsPageService.resolve_filters)

    def test_default_range_is_last_seven_ist_calendar_days(self):
        user = SimpleNamespace(id=1, organization_id=1)
        filters = DashboardService.resolve_filters(user, None, None, None, None, None)
        self.assertEqual(filters.end_date, ist_today())
        self.assertEqual((filters.end_date - filters.start_date).days, 6)


class TimeSeriesTests(unittest.TestCase):
    def test_days_without_tracking_are_returned_as_zero(self):
        with patch.object(DashboardRepository, "time_series",
                          return_value={date(2026, 9, 2): (30600.0, 4320.0)}):
            result = DashboardService.time_tracked(None, _filters())
        self.assertEqual(result["interval"], "day")
        # Every day of the inclusive range is present, not just the one with data.
        self.assertEqual([point["date"] for point in result["data"]],
                         [date(2026, 9, day) for day in range(1, 8)])
        self.assertEqual(result["data"][0], {"date": date(2026, 9, 1), "tracked_hours": 0.0, "manual_hours": 0.0})
        self.assertEqual(result["data"][1],
                         {"date": date(2026, 9, 2), "tracked_hours": 8.5, "manual_hours": 1.2})

    def test_single_day_range_yields_one_point(self):
        filters = _filters(start_date=date(2026, 9, 7), end_date=date(2026, 9, 7))
        with patch.object(DashboardRepository, "time_series", return_value={}):
            result = DashboardService.time_tracked(None, filters)
        self.assertEqual(len(result["data"]), 1)

    def test_interval_widens_only_for_ranges_longer_than_the_ui_presets(self):
        cases = [
            (date(2026, 9, 1), date(2026, 9, 7), "day"),      # last 7 days
            (date(2026, 8, 1), date(2026, 8, 31), "day"),     # a month
            (date(2026, 7, 1), date(2026, 8, 31), "day"),     # 62 days, still daily
            (date(2026, 6, 1), date(2026, 8, 31), "week"),
            (date(2020, 1, 1), date(2026, 8, 31), "month"),
        ]
        for start, end, expected in cases:
            filters = _filters(start_date=start, end_date=end,
                               start_time=ist_day_start_utc(start), end_time=ist_day_end_utc(end))
            self.assertEqual(DashboardService.choose_interval(filters), expected, (start, end))

    def test_long_range_returns_bounded_buckets_starting_on_the_period(self):
        start, end = date(2026, 1, 1), date(2026, 12, 31)
        filters = _filters(start_date=start, end_date=end,
                           start_time=ist_day_start_utc(start), end_time=ist_day_end_utc(end))
        with patch.object(DashboardRepository, "time_series", return_value={}) as series:
            result = DashboardService.time_tracked(None, filters)
        self.assertEqual(result["interval"], "week")
        self.assertEqual(series.call_args[0][2], "week")
        self.assertLess(len(result["data"]), 60)
        # date_trunc('week') is Monday-based, so the first bucket is the Monday
        # on or before start_date -- otherwise buckets and labels would drift.
        self.assertEqual(result["data"][0]["date"], date(2025, 12, 29))

    def test_series_is_grouped_by_ist_day_in_sql(self):
        db = RecordingSession()
        DashboardRepository.time_series(db, _filters())
        sql = _sql(db.statements[-1])
        self.assertIn("GROUP BY report_entries.work_date", sql)
        # work_date is the IST calendar day, so buckets line up with the IST
        # window the filters were resolved into.
        entries = ReportsPageRepository.entry_grain_subquery(_filters()).original
        compiled = entries.compile(dialect=postgresql.dialect())
        self.assertIn("date(timezone(", str(compiled))
        self.assertIn("Asia/Kolkata", compiled.params.values())


class SummaryTests(unittest.TestCase):
    def test_active_projects_excludes_archived_and_activity_is_aliased(self):
        row = SimpleNamespace(total_seconds=278100, avg_activity=73.4231,
                              total_members=24, total_tasks=48, active_projects=17)
        with patch.object(DashboardRepository, "summary", return_value=row):
            summary = DashboardService.summary(None, _filters())
        self.assertEqual(summary["total_hours"], 77.25)
        # The exact figure travels beside the rounded one: the "Time Worked"
        # card renders seconds, so a 10-second session is not shown as 0.00h.
        self.assertEqual(summary["total_seconds"], 278100)
        self.assertEqual(summary["active_projects"], 17)
        self.assertEqual(summary["team_members"], 24)
        # The card is labelled "Monthly Activity" but carries the selected range.
        self.assertEqual(summary["activity"], 73.42)
        self.assertEqual(summary["monthly_activity"], summary["activity"])

    def test_null_activity_is_not_reported_as_zero(self):
        row = SimpleNamespace(total_seconds=0, avg_activity=None, total_members=0,
                              total_tasks=0, active_projects=0)
        with patch.object(DashboardRepository, "summary", return_value=row):
            summary = DashboardService.summary(None, _filters())
        self.assertIsNone(summary["monthly_activity"])
        self.assertIsNone(summary["activity"])

    def test_summary_sql_counts_non_archived_projects_distinctly(self):
        db = RecordingSession(one=None)
        DashboardRepository.summary(db, _filters())
        sql = _sql(db.statements[-1])
        self.assertIn("count(DISTINCT CASE WHEN (projects.status !=", sql)
        self.assertIn("count(DISTINCT report_entries.user_id)", sql)
        # Joining projects must not fan the entry rows out into extra hours.
        self.assertIn("JOIN projects ON projects.id = report_entries.project_id", sql)


class TopListTests(unittest.TestCase):
    def _row(self, id_, name, seconds=3600, activity=65.0):
        return SimpleNamespace(id=id_, name=name, total_seconds=seconds,
                               avg_activity=activity, total_members=1, total_tasks=1)

    def test_top_projects_shape(self):
        with patch.object(ReportsPageRepository, "projects",
                          return_value=([self._row(12, "Mobile Time Tracker", 1765080, 65.8)], 47, 4_000_000.0)):
            result = DashboardService.top_projects(None, _filters(), None, "total_hours", "desc", 1, 10)
        self.assertEqual(result["items"][0], {
            "project_id": 12, "project_name": "Mobile Time Tracker",
            "total_hours": 490.3, "avg_activity": 65.8,
        })
        self.assertEqual((result["total"], result["pages"], result["limit"]), (47, 5, 10))

    def test_top_members_shape(self):
        with patch.object(ReportsPageRepository, "members",
                          return_value=([self._row(102, "John Doe", 605880, 79.0)], 1, 605880.0)):
            result = DashboardService.top_members(None, _filters(), None, "total_hours", "desc", 1, 10)
        self.assertEqual(result["items"][0], {
            "member_id": 102, "member_name": "John Doe",
            "total_hours": 168.3, "avg_activity": 79.0,
        })

    def test_members_group_by_user_and_join_users(self):
        db = RecordingSession()
        ReportsPageRepository.members(db, _filters(), None, "total_hours", "desc", 1, 10)
        sql = _sql(db.statements[-1])
        self.assertIn("GROUP BY report_entries.user_id", sql)
        self.assertIn("JOIN users", sql)


class TopAppsTests(unittest.TestCase):
    def test_percentage_is_a_share_of_the_whole_scope_not_of_the_page(self):
        rows = [SimpleNamespace(id=15, name="Google Chrome", total_seconds=81000)]
        # The scope-wide seconds arrive with the rows, out of the same
        # aggregate as the row count -- no second query for the denominator.
        with patch.object(DashboardRepository, "top_apps", return_value=(rows, 30, 249840.0)):
            result = DashboardService.top_apps(None, _filters(), None, "total_hours", "desc", 1, 10)
        item = result["items"][0]
        self.assertEqual(item["total_hours"], 22.5)
        self.assertEqual(item["app_id"], 15)
        # 22.5 / 69.4 -- the page's own single row would have given 100%.
        self.assertEqual(item["percentage"], 32.42)
        self.assertEqual(result["total_app_hours"], 69.4)

    def test_no_app_usage_gives_null_percentages_rather_than_a_divide_by_zero(self):
        rows = [SimpleNamespace(id=1, name="chrome", total_seconds=0)]
        with patch.object(DashboardRepository, "top_apps", return_value=(rows, 1, 0.0)):
            result = DashboardService.top_apps(None, _filters(), None, "total_hours", "desc", 1, 10)
        self.assertIsNone(result["items"][0]["percentage"])
        self.assertEqual(result["total_app_hours"], 0.0)

    def test_denominator_query_is_filtered_and_ungrouped(self):
        db = RecordingSession(scalar=0)
        DashboardRepository.total_app_seconds(
            db, _filters(project_ids=(12,), member_ids=(102, 103)), None
        )
        sql = _sql(db.statements[-1])
        self.assertIn("sum(time_entry_app_usage.duration_seconds)", sql)
        self.assertNotIn("GROUP BY time_entry_app_usage.application_name", sql)
        self.assertIn("time_entries.project_id IN", sql)
        self.assertIn("time_entries.user_id IN", sql)
        self.assertIn("time_entry_app_usage.recorded_at >=", sql)


class FilterPropagationTests(unittest.TestCase):
    def test_every_section_receives_the_same_filters(self):
        filters = _filters(project_ids=(12, 13), task_ids=(101,), member_ids=(102,))
        seen = []

        def record(name):
            def inner(db, f, *args, **kwargs):
                seen.append((name, f))
                return {} if name == "summary" else {"items": []}
            return inner

        with patch.object(DashboardService, "summary", record("summary")), \
             patch.object(DashboardService, "time_tracked", record("time_tracked")), \
             patch.object(DashboardService, "top_projects", record("top_projects")), \
             patch.object(DashboardService, "top_members", record("top_members")), \
             patch.object(DashboardService, "top_apps", record("top_apps")):
            response = DashboardService.dashboard(None, filters)

        self.assertEqual({name for name, _ in seen},
                         {"summary", "time_tracked", "top_projects", "top_members", "top_apps"})
        for _, used in seen:
            self.assertIs(used, filters)
        # Echoed back as lists: the filters are multi-select on both pages.
        self.assertEqual(response["filters"]["project_id"], [12, 13])
        self.assertEqual(response["filters"]["task_id"], [101])
        self.assertEqual(response["filters"]["member_id"], [102])

    def test_top_lists_default_to_ten_rows_sorted_by_hours(self):
        calls = {}

        def capture(name):
            def inner(db, f, search, sort_by, sort_order, page, limit):
                calls[name] = (search, sort_by, sort_order, page, limit)
                return {"items": []}
            return inner

        with patch.object(DashboardService, "summary", lambda db, f: {}), \
             patch.object(DashboardService, "time_tracked", lambda db, f: {}), \
             patch.object(DashboardService, "top_projects", capture("projects")), \
             patch.object(DashboardService, "top_members", capture("members")), \
             patch.object(DashboardService, "top_apps", capture("apps")):
            DashboardService.dashboard(None, _filters())

        self.assertEqual(DEFAULT_TOP_N, 10)
        for name in ("projects", "members", "apps"):
            self.assertEqual(calls[name], (None, "total_hours", "desc", 1, 10))


class RouterTests(unittest.TestCase):
    def test_endpoints_and_parameters_are_documented(self):
        from app.main import app

        schema = app.openapi()
        for path in ("/api/v1/react/dashboard", "/api/v1/react/dashboard/projects",
                     "/api/v1/react/dashboard/members", "/api/v1/react/dashboard/apps"):
            self.assertIn(path, schema["paths"])

        dashboard_params = {
            p["name"] for p in schema["paths"]["/api/v1/react/dashboard"]["get"]["parameters"]
        }
        self.assertLessEqual({"start_date", "end_date", "project_id", "task_id", "member_id", "top_n"},
                             dashboard_params)

        list_params = {
            p["name"] for p in schema["paths"]["/api/v1/react/dashboard/members"]["get"]["parameters"]
        }
        self.assertLessEqual({"start_date", "end_date", "project_id", "task_id", "member_id",
                              "page", "limit", "sort_by", "sort_order", "search"}, list_params)


if __name__ == "__main__":
    unittest.main()

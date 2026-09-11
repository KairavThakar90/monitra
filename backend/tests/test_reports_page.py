"""Tests for the React Reports page APIs (app/react_apis/reports_page).

Two kinds of coverage here:

* Filter/shaping unit tests, matching the mock-based style of
  tests/test_reports.py -- no database required.
* SQL compilation tests, which render every report query against the
  PostgreSQL dialect. These are what catch the failure mode this feature is
  most exposed to: a join that fans rows out and silently inflates
  SUM(seconds). They assert that activity and adjustments are pre-aggregated
  to one row per time entry before being joined.
"""

import unittest
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

from app.core.time_format import ist_day_end_utc, ist_day_start_utc
from app.react_apis.reports_page.repository import ReportFilters, ReportsPageRepository
from app.react_apis.reports_page.service import ReportsPageService


def _user(organization_id=1):
    """A privileged caller -- ``time_entries:view_all``, so the member filter
    is whatever the request asked for."""
    return SimpleNamespace(
        id=54,
        organization_id=organization_id,
        permissions={"time_entries:view_all": True},
    )


def _member(organization_id=1, user_id=54):
    """The employee role the member-side pages run as: no
    ``time_entries:view_all``, so the report is pinned to their own rows."""
    return SimpleNamespace(id=user_id, organization_id=organization_id, permissions={})


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


class ResolveFiltersTests(unittest.TestCase):
    def test_default_range_is_last_seven_calendar_days_including_today(self):
        today = date.today()
        filters = ReportsPageService.resolve_filters(_user(), None, None, None, None, None)
        self.assertEqual(filters.end_date, today)
        self.assertEqual(filters.start_date, today - timedelta(days=6))
        # 7 calendar days inclusive, not 7 completed 24h periods.
        self.assertEqual((filters.end_date - filters.start_date).days, 6)

    def test_default_start_is_derived_from_an_explicit_end_date(self):
        filters = ReportsPageService.resolve_filters(_user(), None, date(2026, 9, 7), None, None, None)
        self.assertEqual(filters.start_date, date(2026, 9, 1))

    def test_end_date_bound_is_exclusive_start_of_the_next_day(self):
        filters = ReportsPageService.resolve_filters(
            _user(), date(2026, 9, 1), date(2026, 9, 7), None, None, None
        )
        self.assertEqual(filters.start_time, ist_day_start_utc(date(2026, 9, 1)))
        self.assertEqual(filters.end_time, ist_day_start_utc(date(2026, 9, 8)))

    def test_inverted_range_raises_400(self):
        with self.assertRaises(HTTPException) as error:
            ReportsPageService.resolve_filters(
                _user(), date(2026, 9, 7), date(2026, 9, 1), None, None, None
            )
        self.assertEqual(error.exception.status_code, 400)

    def test_equal_start_and_end_is_a_valid_single_day(self):
        filters = ReportsPageService.resolve_filters(
            _user(), date(2026, 9, 7), date(2026, 9, 7), None, None, None
        )
        self.assertEqual(filters.start_date, filters.end_date)

    def test_a_member_without_view_all_is_pinned_to_their_own_rows(self):
        filters = ReportsPageService.resolve_filters(_member(user_id=77), None, None, None, None, None)
        self.assertEqual(filters.member_ids, (77,))

    def test_a_member_cannot_widen_the_scope_with_an_explicit_member_id(self):
        # The member-side Dashboard and Reports run against the same endpoints
        # as the admin ones; a hand-crafted ?member_id= must not read anyone
        # else's time.
        filters = ReportsPageService.resolve_filters(
            _member(user_id=77), None, None, None, None, [102, 103]
        )
        self.assertEqual(filters.member_ids, (77,))

    def test_a_member_still_gets_the_project_and_task_filters_they_asked_for(self):
        filters = ReportsPageService.resolve_filters(
            _member(user_id=77), None, None, [12], [101], None
        )
        self.assertEqual((filters.project_ids, filters.task_ids), ((12,), (101,)))

    def test_organization_comes_from_the_authenticated_user(self):
        filters = ReportsPageService.resolve_filters(_user(organization_id=99), None, None, None, None, None)
        self.assertEqual(filters.organization_id, 99)

    def test_entity_filters_are_carried_through(self):
        filters = ReportsPageService.resolve_filters(_user(), None, None, [12], [101], [102])
        self.assertEqual((filters.project_ids, filters.task_ids, filters.member_ids),
                         ((12,), (101,), (102,)))

    def test_repeated_entity_filters_are_all_kept(self):
        # The frontend's member/project pickers are multi-select; every chosen
        # id has to reach the query, not just the last one.
        filters = ReportsPageService.resolve_filters(_user(), None, None, [12, 13], None, [102, 103])
        self.assertEqual(filters.project_ids, (12, 13))
        self.assertEqual(filters.member_ids, (102, 103))

    def test_duplicate_ids_are_collapsed_in_order(self):
        filters = ReportsPageService.resolve_filters(_user(), None, None, [13, 12, 13], None, None)
        self.assertEqual(filters.project_ids, (13, 12))

    def test_omitted_entity_filters_mean_no_restriction(self):
        filters = ReportsPageService.resolve_filters(_user(), None, None, None, None, None)
        self.assertEqual((filters.project_ids, filters.task_ids, filters.member_ids), ((), (), ()))


class ShapingTests(unittest.TestCase):
    def test_metrics_convert_seconds_to_hours_and_round_activity(self):
        row = SimpleNamespace(total_seconds=153900, avg_activity=71.3216, total_members=5, total_tasks=14)
        self.assertEqual(
            ReportsPageService._metrics(row),
            {"total_seconds": 153900, "total_hours": 42.75, "avg_activity": 71.32,
             "total_members": 5, "total_tasks": 14},
        )

    def test_null_activity_stays_null_rather_than_becoming_zero(self):
        row = SimpleNamespace(total_seconds=3600, avg_activity=None, total_members=1, total_tasks=1)
        self.assertIsNone(ReportsPageService._metrics(row)["avg_activity"])

    def test_page_math(self):
        self.assertEqual(
            ReportsPageService._page([], 1, 20, 100, 154800.0),
            {"items": [], "page": 1, "limit": 20, "total": 100, "pages": 5,
             "total_seconds": 154800, "total_hours": 43.0},
        )
        self.assertEqual(ReportsPageService._page([], 1, 20, 41, 0.0)["pages"], 3)
        self.assertEqual(ReportsPageService._page([], 1, 20, 0, 0.0)["pages"], 0)

    def test_a_page_reports_the_seconds_of_every_matching_row_not_just_the_page(self):
        """The denominator a part-to-whole chart divides by.

        Summing the visible rows would make every page add up to 100%, and
        taking the summary strip's total instead is what drew every
        unattributed second of the day as one unnamed slice on the App and
        URL tabs.
        """
        page = ReportsPageService._page([], 2, 5, 87, 154800.0)
        self.assertEqual(page["total_seconds"], 154800)
        self.assertEqual(page["total_hours"], 43.0)

    def test_project_rows_are_named_with_project_fields(self):
        row = SimpleNamespace(id=12, name="Website Redesign", total_seconds=3600,
                              avg_activity=70.0, total_members=5, total_tasks=14)
        with patch.object(ReportsPageRepository, "projects", return_value=([row], 1, 3600.0)):
            response = ReportsPageService.projects(None, _filters(), None, "total_hours", "desc", 1, 20)
        self.assertEqual(response["items"][0]["project_id"], 12)
        self.assertEqual(response["items"][0]["project_name"], "Website Redesign")
        self.assertEqual(response["total"], 1)

    def test_task_rows_report_one_task_each(self):
        row = SimpleNamespace(id=101, name="Implement Login API", total_seconds=30600,
                              avg_activity=76.21, total_members=2, total_tasks=1)
        with patch.object(ReportsPageRepository, "tasks", return_value=([row], 1, 30600.0)):
            response = ReportsPageService.tasks(None, _filters(), None, "total_hours", "desc", 1, 20)
        item = response["items"][0]
        self.assertEqual(item["task_id"], 101)
        self.assertEqual(item["total_tasks"], 1)
        self.assertEqual(item["total_hours"], 8.5)

    def test_app_and_url_ids_are_usage_table_row_ids(self):
        app_row = SimpleNamespace(id=15, name="Google Chrome", total_seconds=3600,
                                  avg_activity=64.75, total_members=6, total_tasks=11)
        with patch.object(ReportsPageRepository, "usage", return_value=([app_row], 1, 3600.0)) as usage:
            response = ReportsPageService.apps(None, _filters(), None, "total_hours", "desc", 1, 20)
        self.assertEqual(usage.call_args[0][2], "app")
        self.assertEqual(response["items"][0]["app_id"], 15)
        self.assertEqual(response["items"][0]["app_name"], "Google Chrome")

        url_row = SimpleNamespace(id=32, name="https://github.com", total_seconds=3600,
                                  avg_activity=69.44, total_members=4, total_tasks=7)
        with patch.object(ReportsPageRepository, "usage", return_value=([url_row], 1, 3600.0)) as usage:
            response = ReportsPageService.urls(None, _filters(), None, "total_hours", "desc", 1, 20)
        self.assertEqual(usage.call_args[0][2], "url")
        self.assertEqual(response["items"][0]["url_id"], 32)
        self.assertEqual(response["items"][0]["url_name"], "https://github.com")


class TrendShapingTests(unittest.TestCase):
    """The trend feeds a chart, so a missing day must become a real zero rather
    than a hole that silently shifts every later point one step left."""

    def _trend(self, rows, start=date(2026, 9, 1), end=date(2026, 9, 3)):
        with patch.object(ReportsPageRepository, "daily_trend", return_value=rows):
            return ReportsPageService.trend(None, _filters(start_date=start, end_date=end,
                                                           start_time=ist_day_start_utc(start),
                                                           end_time=ist_day_end_utc(end)))

    def test_every_day_in_range_gets_a_point_even_with_no_rows(self):
        response = self._trend([])
        self.assertEqual([point["date"] for point in response["points"]],
                         [date(2026, 9, 1), date(2026, 9, 2), date(2026, 9, 3)])
        self.assertEqual({point["total_seconds"] for point in response["points"]}, {0})

    def test_a_gap_day_is_zero_hours_with_null_activity_not_a_missing_point(self):
        rows = [SimpleNamespace(day=date(2026, 9, 1), total_seconds=27000, avg_activity=74.5,
                                total_members=2, total_tasks=3)]
        points = self._trend(rows)["points"]
        self.assertEqual(len(points), 3)
        self.assertEqual((points[0]["total_seconds"], points[0]["total_hours"]), (27000, 7.5))
        self.assertEqual(points[0]["avg_activity"], 74.5)
        self.assertEqual(points[1]["total_seconds"], 0)
        # No samples is not 0% activity.
        self.assertIsNone(points[1]["avg_activity"])

    def test_rows_outside_the_range_are_not_emitted(self):
        rows = [SimpleNamespace(day=date(2026, 8, 30), total_seconds=3600, avg_activity=50.0,
                                total_members=1, total_tasks=1)]
        response = self._trend(rows)
        self.assertEqual(len(response["points"]), 3)
        self.assertEqual({point["total_seconds"] for point in response["points"]}, {0})

    def test_a_single_day_range_is_one_point(self):
        response = self._trend([], start=date(2026, 9, 1), end=date(2026, 9, 1))
        self.assertEqual(len(response["points"]), 1)
        self.assertEqual(response["start_date"], date(2026, 9, 1))
        self.assertEqual(response["end_date"], date(2026, 9, 1))

    def test_tracked_activity_of_zero_percent_stays_zero(self):
        rows = [SimpleNamespace(day=date(2026, 9, 1), total_seconds=3600, avg_activity=0.0,
                                total_members=1, total_tasks=1)]
        self.assertEqual(self._trend(rows)["points"][0]["avg_activity"], 0.0)


class EntryGrainSqlTests(unittest.TestCase):
    """The entry-grain subquery is the one place a fan-out bug could inflate
    every report's hours at once."""

    def setUp(self):
        self.sql = _sql(ReportsPageRepository.entry_grain_subquery(_filters()).original)

    def test_activity_is_pre_aggregated_per_time_entry_before_joining(self):
        self.assertIn("GROUP BY time_entry_activity.time_entry_id", self.sql)
        self.assertIn("LEFT OUTER JOIN (SELECT time_entry_activity.time_entry_id", self.sql)

    def test_adjustments_are_pre_aggregated_per_time_entry_before_joining(self):
        self.assertIn("GROUP BY time_entry_adjustments.time_entry_id", self.sql)

    def test_adjusted_seconds_are_floored_at_zero(self):
        self.assertIn("greatest", self.sql.lower())

    def test_approved_unmirrored_manual_entries_are_unioned_in(self):
        self.assertIn("UNION ALL", self.sql)
        self.assertIn("manual_time_entries.mirrored_time_entry_id IS NULL", self.sql)

    def test_date_window_is_applied_to_the_entry_scan(self):
        self.assertIn("time_entries.start_time >=", self.sql)
        self.assertIn("time_entries.start_time <", self.sql)

    def test_organization_scope_is_always_present(self):
        self.assertIn("time_entries.organization_id =", self.sql)
        self.assertIn("manual_time_entries.organization_id =", self.sql)

    def test_entity_filters_reach_both_branches(self):
        sql = _sql(
            ReportsPageRepository.entry_grain_subquery(
                _filters(project_ids=(12, 13), task_ids=(101,), member_ids=(102, 103))
            ).original
        )
        for column in ("time_entries.project_id IN", "time_entries.task_id IN",
                       "time_entries.user_id IN", "manual_time_entries.project_id IN",
                       "manual_time_entries.task_id IN", "manual_time_entries.user_id IN"):
            self.assertIn(column, sql)

    def test_empty_entity_filters_add_no_clause(self):
        sql = _sql(ReportsPageRepository.entry_grain_subquery(_filters()).original)
        self.assertNotIn("time_entries.project_id IN", sql)
        self.assertNotIn("time_entries.user_id IN", sql)

    def test_the_day_bucket_is_the_ist_calendar_date_on_both_branches(self):
        sql = _sql(ReportsPageRepository.entry_grain_subquery(_filters()).original)
        # Converted through the real zone name, never a hard-coded +05:30.
        self.assertIn("timezone(%(timezone_1)s, time_entries.start_time)", sql)
        self.assertIn("manual_time_entries.work_date", sql)

    def test_there_is_exactly_one_day_column_per_branch(self):
        # A merge once left two day columns side by side (`day` and
        # `work_date`) computing the same IST date. One bucket, one name.
        sql = _sql(ReportsPageRepository.entry_grain_subquery(_filters()).original)
        self.assertEqual(sql.count("AS work_date"), 2)
        self.assertNotIn("AS day", sql)


class GroupedQuerySqlTests(unittest.TestCase):
    """Compile the queries the four tabs actually run, through a fake Session
    that records the statements instead of executing them."""

    class _RecordingSession:
        def __init__(self):
            self.statements = []

        def execute(self, statement):
            self.statements.append(statement)
            # `_paginate` reads its row count and its scope-wide seconds out
            # of one aggregate row, so `.one()` has to be a two-value row.
            return SimpleNamespace(all=lambda: [], one=lambda: (0, 0.0))

        def scalar(self, statement):
            self.statements.append(statement)
            return 0

    def _run(self, call):
        session = self._RecordingSession()
        call(session)
        return [_sql(statement) for statement in session.statements]

    def test_projects_query_groups_and_counts_distinctly(self):
        sql = self._run(lambda db: ReportsPageRepository.projects(
            db, _filters(), None, "total_hours", "desc", 1, 20
        ))[-1]
        self.assertIn("GROUP BY report_entries.project_id", sql)
        self.assertIn("count(DISTINCT report_entries.user_id)", sql)
        self.assertIn("count(DISTINCT report_entries.task_id)", sql)
        self.assertIn("JOIN projects", sql)

    def test_tasks_query_groups_by_task(self):
        sql = self._run(lambda db: ReportsPageRepository.tasks(
            db, _filters(), None, "name", "asc", 1, 20
        ))[-1]
        self.assertIn("GROUP BY report_entries.task_id", sql)
        self.assertIn("JOIN tasks", sql)

    def test_search_becomes_a_bound_ilike_not_interpolated_sql(self):
        sql = self._run(lambda db: ReportsPageRepository.projects(
            db, _filters(), "website'; DROP TABLE projects;--", "total_hours", "desc", 1, 20
        ))[-1]
        self.assertIn("ILIKE", sql.upper())
        self.assertNotIn("DROP TABLE", sql)

    def test_pagination_applies_limit_and_offset(self):
        sql = self._run(lambda db: ReportsPageRepository.projects(
            db, _filters(), None, "total_hours", "desc", 3, 20
        ))[-1]
        self.assertIn("LIMIT", sql)
        self.assertIn("OFFSET", sql)

    def test_unknown_sort_key_falls_back_to_total_seconds(self):
        sql = self._run(lambda db: ReportsPageRepository.projects(
            db, _filters(), None, "; DROP TABLE projects", "desc", 1, 20
        ))[-1]
        self.assertNotIn("DROP TABLE", sql)
        self.assertIn("ORDER BY report_rows.total_seconds DESC", sql)

    def test_sort_order_is_honoured_for_each_whitelisted_field(self):
        for field, column in (
            ("total_hours", "total_seconds"),
            ("avg_activity", "avg_activity"),
            ("total_members", "total_members"),
            ("total_tasks", "total_tasks"),
            ("name", "name"),
        ):
            sql = self._run(lambda db: ReportsPageRepository.projects(
                db, _filters(), None, field, "asc", 1, 20
            ))[-1]
            self.assertIn(f"ORDER BY report_rows.{column} ASC", sql)

    def test_app_usage_sums_usage_duration_and_joins_activity_once(self):
        sql = self._run(lambda db: ReportsPageRepository.usage(
            db, _filters(), "app", None, "total_hours", "desc", 1, 20
        ))[-1]
        self.assertIn("sum(time_entry_app_usage.duration_seconds)", sql)
        self.assertIn("min(time_entry_app_usage.id)", sql)
        self.assertIn("GROUP BY time_entry_app_usage.application_name", sql)
        self.assertIn("GROUP BY time_entry_activity.time_entry_id", sql)
        self.assertIn("JOIN time_entries", sql)

    def test_url_usage_groups_by_url_falling_back_to_domain(self):
        sql = self._run(lambda db: ReportsPageRepository.usage(
            db, _filters(), "url", None, "total_hours", "desc", 1, 20
        ))[-1]
        self.assertIn("sum(time_entry_url_usage.duration_seconds)", sql)
        self.assertIn("min(time_entry_url_usage.id)", sql)
        # url is nullable; falling back to domain keeps those rows' time visible.
        self.assertIn(
            "GROUP BY coalesce(time_entry_url_usage.url, time_entry_url_usage.domain)", sql
        )

    def test_usage_filters_apply_to_the_parent_time_entry(self):
        sql = self._run(lambda db: ReportsPageRepository.usage(
            db, _filters(project_ids=(12,), task_ids=(101,), member_ids=(102, 103)), "app", None,
            "total_hours", "desc", 1, 20,
        ))[-1]
        self.assertIn("time_entries.project_id IN", sql)
        self.assertIn("time_entries.task_id IN", sql)
        self.assertIn("time_entries.user_id IN", sql)
        # The usage rows themselves are date-filtered too -- not just the
        # sessions they hang off.
        self.assertIn("time_entry_app_usage.recorded_at >=", sql)
        self.assertIn("time_entry_app_usage.recorded_at <", sql)

    def test_trend_groups_by_the_ist_day_in_order(self):
        sql = self._run(lambda db: ReportsPageRepository.daily_trend(db, _filters()))[-1]
        # The trend buckets on the entry-grain subquery's single IST day column
        # -- the same one the dashboard's time series groups by. There is not a
        # second day expression for the trend to drift away on.
        self.assertIn("GROUP BY report_entries.work_date", sql)
        self.assertIn("ORDER BY report_entries.work_date", sql)
        # Same metric columns as every other report -- one definition, not two.
        self.assertIn("count(DISTINCT report_entries.user_id)", sql)

    def test_summary_aggregates_without_grouping(self):
        sql = self._run(lambda db: ReportsPageRepository.summary(db, _filters()))[-1]
        self.assertNotIn("GROUP BY report_entries", sql)
        self.assertIn("count(DISTINCT report_entries.user_id)", sql)


class RouterRegistrationTests(unittest.TestCase):
    def test_all_six_endpoints_are_mounted_under_the_react_prefix(self):
        from app.main import app

        # app.routes carries _IncludedRouter entries alongside real routes in
        # this FastAPI version, so read the paths off the generated schema.
        paths = set(app.openapi()["paths"])
        for suffix in ("summary", "trend", "projects", "tasks", "apps", "urls"):
            self.assertIn(f"/api/v1/react/reports/{suffix}", paths)

    def test_openapi_schema_builds_with_the_report_endpoints(self):
        from app.main import app

        schema = app.openapi()
        self.assertIn("/api/v1/react/reports/summary", schema["paths"])
        params = {
            param["name"]
            for param in schema["paths"]["/api/v1/react/reports/projects"]["get"]["parameters"]
        }
        self.assertLessEqual(
            {"start_date", "end_date", "project_id", "task_id", "member_id",
             "page", "limit", "sort_by", "sort_order", "search"},
            params,
        )

    def test_date_range_is_advertised_as_start_date_and_end_date_on_every_tab(self):
        # The web client once sent ?from=&to= here -- names the schema does not
        # declare -- so FastAPI dropped them and every report silently answered
        # for the default window. Pin the names the contract actually exposes.
        from app.main import app

        schema = app.openapi()
        for suffix in ("summary", "trend", "projects", "tasks", "apps", "urls"):
            params = {
                param["name"]
                for param in schema["paths"][f"/api/v1/react/reports/{suffix}"]["get"]["parameters"]
            }
            self.assertIn("start_date", params, suffix)
            self.assertIn("end_date", params, suffix)
            self.assertNotIn("from", params, suffix)
            self.assertNotIn("to", params, suffix)

    def test_member_and_project_filters_accept_repeated_values(self):
        from app.main import app

        schema = app.openapi()
        params = {
            param["name"]: param["schema"]
            for param in schema["paths"]["/api/v1/react/reports/projects"]["get"]["parameters"]
        }
        for name in ("member_id", "project_id", "task_id"):
            # Optional[List[int]] renders as an anyOf of an array and null.
            rendered = str(params[name])
            self.assertIn("array", rendered, name)


if __name__ == "__main__":
    unittest.main()

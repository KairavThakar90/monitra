import unittest
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

from app.schemas.reports import BillableFilter, ReportDimension, UsageType
from app.services.reports import ReportsService, _weighted_average


class WeightedAverageTests(unittest.TestCase):
    def test_weighted_by_sample_count(self):
        # (80*1 + 40*9) / 10 = 44.0, not the naive per-item average of 60.0
        self.assertEqual(_weighted_average([(80.0, 1), (40.0, 9)]), 44.0)

    def test_none_when_no_samples(self):
        self.assertIsNone(_weighted_average([(None, 0), (None, 0)]))

    def test_skips_zero_count_pairs(self):
        self.assertEqual(_weighted_average([(50.0, 0), (70.0, 2)]), 70.0)


class ReportsServiceCommonTests(unittest.TestCase):
    def setUp(self):
        self.user = SimpleNamespace(id=54, organization_id=1)

    def test_invalid_date_range_raises_400(self):
        with self.assertRaises(HTTPException) as error:
            ReportsService.build_grouped(
                None, self.user, ReportDimension.projects, date(2026, 8, 27), date(2026, 8, 1),
                None, None, None, UsageType.app,
            )
        self.assertEqual(error.exception.status_code, 400)

    def test_detailed_logs_invalid_date_range_raises_400(self):
        with self.assertRaises(HTTPException) as error:
            ReportsService.build_detailed_logs(
                None, self.user, ReportDimension.projects, date(2026, 8, 27), date(2026, 8, 1),
                None, None, None, UsageType.app, None, "date", True, 1, 50,
            )
        self.assertEqual(error.exception.status_code, 400)


class GroupedProjectsTests(unittest.TestCase):
    def setUp(self):
        self.user = SimpleNamespace(id=54, organization_id=1)

    def test_zero_data_project_excluded_and_meta_label_from_triples(self):
        with patch("app.services.reports.ReportsRepository.eligible_projects",
                   return_value={1: "Alpha", 2: "Silent"}), \
             patch("app.services.reports.ReportsRepository.session_seconds_by",
                   side_effect=[{1: 3600}, {}]), \
             patch("app.services.reports.ReportsRepository.session_activity_by",
                   return_value={1: (80.0, 4)}), \
             patch("app.services.reports.ReportsRepository.session_triples",
                   return_value={(1, 10, 100), (1, 11, 101)}), \
             patch("app.services.reports.ReportsRepository.session_entry_count", return_value=7):
            response = ReportsService.build_grouped(
                None, self.user, ReportDimension.projects, date(2026, 8, 1), date(2026, 8, 27),
                None, None, None, UsageType.app,
            )
        self.assertEqual(len(response["grouped_data"]), 1)
        item = response["grouped_data"][0]
        self.assertEqual(item["id"], 1)
        self.assertEqual(item["meta_label"], "2 members · 2 tasks")
        self.assertEqual(response["summary"]["total_projects"], 1)
        self.assertEqual(response["summary"]["total_members"], 2)
        self.assertEqual(response["summary"]["total_entries"], 7)
        self.assertEqual(response["summary"]["average_activity_percentage"], 80.0)

    def test_session_seconds_by_called_with_project_id_grouping(self):
        with patch("app.services.reports.ReportsRepository.eligible_projects", return_value={1: "Alpha"}), \
             patch("app.services.reports.ReportsRepository.session_seconds_by", return_value={}) as seconds, \
             patch("app.services.reports.ReportsRepository.session_activity_by", return_value={}), \
             patch("app.services.reports.ReportsRepository.session_triples", return_value=set()), \
             patch("app.services.reports.ReportsRepository.session_entry_count", return_value=0):
            ReportsService.build_grouped(
                None, self.user, ReportDimension.projects, date(2026, 8, 1), date(2026, 8, 27),
                None, None, None, UsageType.app,
            )
        self.assertEqual(seconds.call_args.args[-1], "project_id")


class GroupedMembersTests(unittest.TestCase):
    def setUp(self):
        self.user = SimpleNamespace(id=54, organization_id=1)

    def test_member_meta_label_and_name_lookup(self):
        with patch("app.services.reports.ReportsRepository.eligible_projects", return_value={1: "Alpha", 2: "Beta"}), \
             patch("app.services.reports.ReportsRepository.session_seconds_by", return_value={10: 7200}), \
             patch("app.services.reports.ReportsRepository.session_activity_by", return_value={10: (70.0, 2)}), \
             patch("app.services.reports.ReportsRepository.session_triples",
                   return_value={(1, 10, 100), (2, 10, 101), (2, 10, 102)}), \
             patch("app.services.reports.ReportsRepository.users_lookup", return_value={10: ("Ada", "employee")}), \
             patch("app.services.reports.ReportsRepository.session_entry_count", return_value=3):
            response = ReportsService.build_grouped(
                None, self.user, ReportDimension.members, date(2026, 8, 1), date(2026, 8, 27),
                None, None, None, UsageType.app,
            )
        item = response["grouped_data"][0]
        self.assertEqual(item["name"], "Ada")
        self.assertEqual(item["meta_label"], "2 projects, 3 tasks")
        self.assertEqual(response["summary"]["total_members"], 1)


class GroupedAppsTests(unittest.TestCase):
    def setUp(self):
        self.user = SimpleNamespace(id=54, organization_id=1)

    def test_uses_usage_repository_methods_for_apps_dimension(self):
        with patch("app.services.reports.ReportsRepository.eligible_projects", return_value={1: "Alpha"}), \
             patch("app.services.reports.ReportsRepository.app_usage_seconds_by_name",
                   return_value={"VS Code": 600}) as seconds, \
             patch("app.services.reports.ReportsRepository.app_usage_activity_by_name",
                   return_value={"VS Code": (90.0, 1)}), \
             patch("app.services.reports.ReportsRepository.app_usage_member_counts_by_name",
                   return_value={"VS Code": 2}), \
             patch("app.services.reports.ReportsRepository.app_usage_distinct_member_ids",
                   return_value={10, 11}), \
             patch("app.services.reports.ReportsRepository.app_usage_entry_count", return_value=5):
            response = ReportsService.build_grouped(
                None, self.user, ReportDimension.apps, date(2026, 8, 1), date(2026, 8, 27),
                None, None, None, UsageType.app,
            )
        self.assertEqual(seconds.call_args.args[-1], "app")
        item = response["grouped_data"][0]
        self.assertEqual(item["id"], "VS Code")
        self.assertEqual(item["meta_label"], "2 members")
        self.assertEqual(response["summary"]["total_apps"], 1)

    def test_url_usage_type_forwarded(self):
        with patch("app.services.reports.ReportsRepository.eligible_projects", return_value={1: "Alpha"}), \
             patch("app.services.reports.ReportsRepository.app_usage_seconds_by_name", return_value={}) as seconds, \
             patch("app.services.reports.ReportsRepository.app_usage_activity_by_name", return_value={}), \
             patch("app.services.reports.ReportsRepository.app_usage_member_counts_by_name", return_value={}), \
             patch("app.services.reports.ReportsRepository.app_usage_distinct_member_ids", return_value=set()), \
             patch("app.services.reports.ReportsRepository.app_usage_entry_count", return_value=0):
            ReportsService.build_grouped(
                None, self.user, ReportDimension.apps, date(2026, 8, 1), date(2026, 8, 27),
                None, None, None, UsageType.url,
            )
        self.assertEqual(seconds.call_args.args[-1], "url")


class DetailedLogsTests(unittest.TestCase):
    def setUp(self):
        self.user = SimpleNamespace(id=54, organization_id=1)

    def test_session_dimension_maps_rows_with_null_app_and_url(self):
        row = SimpleNamespace(
            id="te-1", work_date=date(2026, 8, 20), member_id=10, member_name="Ada", role="employee",
            project_id=1, project_name="Alpha", task_id=100, task_name="Design",
            tracked_seconds=3600, activity_percentage=72.5,
        )
        with patch("app.services.reports.ReportsRepository.eligible_projects", return_value={1: "Alpha"}), \
             patch("app.services.reports.ReportsRepository.session_detailed_logs", return_value=([row], 1)):
            response = ReportsService.build_detailed_logs(
                None, self.user, ReportDimension.projects, date(2026, 8, 1), date(2026, 8, 27),
                None, None, None, UsageType.app, None, "date", True, 1, 50,
            )
        item = response["items"][0]
        self.assertIsNone(item["app"])
        self.assertIsNone(item["url"])
        self.assertEqual(item["tracked_hours"], 1.0)
        self.assertEqual(response["pagination"], {"page": 1, "limit": 50, "total": 1, "total_pages": 1})

    def test_apps_dimension_maps_name_onto_app_field(self):
        row = SimpleNamespace(
            id="au-1", work_date=date(2026, 8, 20), member_id=10, member_name="Ada", role="employee",
            project_id=1, project_name="Alpha", task_id=100, task_name="Design",
            name="VS Code", tracked_seconds=120, activity_percentage=None,
        )
        with patch("app.services.reports.ReportsRepository.eligible_projects", return_value={1: "Alpha"}), \
             patch("app.services.reports.ReportsRepository.app_usage_detailed_logs", return_value=([row], 1)):
            response = ReportsService.build_detailed_logs(
                None, self.user, ReportDimension.apps, date(2026, 8, 1), date(2026, 8, 27),
                None, None, None, UsageType.app, None, "date", True, 1, 50,
            )
        item = response["items"][0]
        self.assertEqual(item["app"], "VS Code")
        self.assertIsNone(item["url"])

    def test_apps_dimension_url_usage_maps_name_onto_url_field(self):
        row = SimpleNamespace(
            id="uu-1", work_date=date(2026, 8, 20), member_id=10, member_name="Ada", role="employee",
            project_id=1, project_name="Alpha", task_id=100, task_name="Design",
            name="github.com", tracked_seconds=60, activity_percentage=None,
        )
        with patch("app.services.reports.ReportsRepository.eligible_projects", return_value={1: "Alpha"}), \
             patch("app.services.reports.ReportsRepository.app_usage_detailed_logs", return_value=([row], 1)):
            response = ReportsService.build_detailed_logs(
                None, self.user, ReportDimension.apps, date(2026, 8, 1), date(2026, 8, 27),
                None, None, None, UsageType.url, None, "date", True, 1, 50,
            )
        item = response["items"][0]
        self.assertIsNone(item["app"])
        self.assertEqual(item["url"], "github.com")

    def test_pagination_offset_computed_from_page_and_limit(self):
        with patch("app.services.reports.ReportsRepository.eligible_projects", return_value={1: "Alpha"}), \
             patch("app.services.reports.ReportsRepository.session_detailed_logs", return_value=([], 0)) as query:
            ReportsService.build_detailed_logs(
                None, self.user, ReportDimension.projects, date(2026, 8, 1), date(2026, 8, 27),
                None, None, None, UsageType.app, None, "date", True, 3, 20,
            )
        self.assertEqual(query.call_args.args[-2], 40)  # offset = (3-1)*20
        self.assertEqual(query.call_args.args[-1], 20)  # limit


class ProjectTaskSummaryTests(unittest.TestCase):
    def setUp(self):
        self.user = SimpleNamespace(id=54, organization_id=1)
        self.project = SimpleNamespace(
            id=1, project_name="Alpha", created_at=datetime(2026, 8, 1), status_id=2,
            billing_type="free", fixed_hours=None,
        )
        self.tracked_task = SimpleNamespace(
            id=100, task_name="Design", created_at=datetime(2026, 8, 2), project_id=1,
            estimated_hours=None,
        )
        self.untracked_task = SimpleNamespace(
            id=101, task_name="Untouched", created_at=datetime(2026, 8, 3), project_id=1,
            estimated_hours=None,
        )

    def test_date_and_date_range_together_raises_400(self):
        with self.assertRaises(HTTPException) as error:
            ReportsService.build_project_task_summary(
                None, self.user, 1, 5, None, date(2026, 8, 1), date(2026, 8, 1), date(2026, 8, 5),
            )
        self.assertEqual(error.exception.status_code, 400)

    def test_start_date_without_end_date_raises_400(self):
        with self.assertRaises(HTTPException) as error:
            ReportsService.build_project_task_summary(
                None, self.user, 1, 5, None, None, date(2026, 8, 1), None,
            )
        self.assertEqual(error.exception.status_code, 400)

    def test_start_date_after_end_date_raises_400(self):
        with self.assertRaises(HTTPException) as error:
            ReportsService.build_project_task_summary(
                None, self.user, 1, 5, None, None, date(2026, 8, 27), date(2026, 8, 1),
            )
        self.assertEqual(error.exception.status_code, 400)

    def test_invalid_project_id_raises_400(self):
        with patch("app.services.reports.ReportsRepository.existing_project_ids", return_value={1}):
            with self.assertRaises(HTTPException) as error:
                ReportsService.build_project_task_summary(
                    None, self.user, 1, 5, [1, 999], None, None, None,
                )
        self.assertEqual(error.exception.status_code, 400)

    def test_no_date_filter_uses_all_time_range(self):
        with patch("app.services.reports.ReportsRepository.project_ids_tracked_between", return_value={1}), \
             patch("app.services.reports.ReportsRepository.paginated_projects", return_value=([self.project], 1)), \
             patch("app.services.reports.ReportsRepository.session_seconds_by", return_value={}) as seconds, \
             patch("app.services.reports.ReportsRepository.active_tasks_by_project", return_value={}), \
             patch("app.services.reports.ReportsRepository.tasks_touched_today", return_value=set()), \
             patch("app.services.reports.ReportsRepository.project_statuses_lookup", return_value={}):
            ReportsService.build_project_task_summary(None, self.user, 1, 5, None, None, None, None)
        start_date, end_date = seconds.call_args_list[0].args[6], seconds.call_args_list[0].args[7]
        self.assertEqual(start_date, ReportsService._EPOCH_DATE)
        self.assertEqual(end_date, ReportsService._FAR_FUTURE_DATE)

    def test_only_tasks_touched_today_appear(self):
        # Task Listing shows what's actively being worked on today, not every
        # task the project has ever had -- the untracked task never appears
        # because tasks_touched_today never names it, regardless of what
        # total_tracked_hours it would otherwise show.
        with patch("app.services.reports.ReportsRepository.project_ids_tracked_between", return_value={1}), \
             patch("app.services.reports.ReportsRepository.paginated_projects", return_value=([self.project], 1)), \
             patch("app.services.reports.ReportsRepository.session_seconds_by",
                   side_effect=[{1: 7200}, {100: 7200}]), \
             patch("app.services.reports.ReportsRepository.active_tasks_by_project",
                   return_value={1: [self.tracked_task, self.untracked_task]}), \
             patch("app.services.reports.ReportsRepository.tasks_touched_today", return_value={100}), \
             patch("app.services.reports.ReportsRepository.project_statuses_lookup", return_value={}):
            response = ReportsService.build_project_task_summary(
                None, self.user, 1, 5, None, date(2026, 8, 1), None, None,
            )

        project_item = response["projects"][0]
        self.assertEqual(project_item["total_task_count"], 1)
        self.assertEqual(len(project_item["tasks"]), 1)
        self.assertEqual(project_item["tasks"][0]["id"], 100)
        self.assertEqual(response["pagination"], {"page": 1, "limit": 5, "total_projects": 1, "total_pages": 1})

    def test_a_task_touched_today_appears_even_with_zero_hours_in_the_selected_range(self):
        # "Touched today" and "the selected date range" are independent: a
        # task worked on today can still show 0 hours if the admin is viewing
        # a different range (e.g. last month), and it must not be dropped for
        # that -- the range only affects the displayed total, never inclusion.
        with patch("app.services.reports.ReportsRepository.project_ids_tracked_between", return_value={1}), \
             patch("app.services.reports.ReportsRepository.paginated_projects", return_value=([self.project], 1)), \
             patch("app.services.reports.ReportsRepository.session_seconds_by", return_value={}), \
             patch("app.services.reports.ReportsRepository.active_tasks_by_project",
                   return_value={1: [self.tracked_task, self.untracked_task]}), \
             patch("app.services.reports.ReportsRepository.tasks_touched_today", return_value={100}), \
             patch("app.services.reports.ReportsRepository.project_statuses_lookup", return_value={}):
            response = ReportsService.build_project_task_summary(
                None, self.user, 1, 5, None, date(2020, 1, 1), None, None,
            )

        tasks = response["projects"][0]["tasks"]
        self.assertEqual([task["id"] for task in tasks], [100])
        self.assertEqual(tasks[0]["total_tracked_hours"], 0.0)

    def test_a_project_with_no_tracking_today_is_hidden_entirely(self):
        # The Task Listing is "what is being worked on today": a project
        # nobody has started today does not appear at all -- not even as an
        # empty shell -- and the page never asks the database to paginate it.
        with patch("app.services.reports.ReportsRepository.project_ids_tracked_between",
                   return_value=set()), \
             patch("app.services.reports.ReportsRepository.paginated_projects") as paginated:
            response = ReportsService.build_project_task_summary(None, self.user, 1, 5, None, None, None, None)

        self.assertEqual(response["projects"], [])
        self.assertEqual(response["pagination"], {"page": 1, "limit": 5, "total_projects": 0, "total_pages": 0})
        paginated.assert_not_called()

    def test_pagination_covers_only_todays_active_projects(self):
        # The paginated set *is* the active set: total_projects counts only
        # projects with tracking today, so page counts stay truthful.
        with patch("app.services.reports.ReportsRepository.project_ids_tracked_between",
                   return_value={1, 7}), \
             patch("app.services.reports.ReportsRepository.paginated_projects",
                   return_value=([self.project], 2)) as paginated, \
             patch("app.services.reports.ReportsRepository.session_seconds_by", return_value={}), \
             patch("app.services.reports.ReportsRepository.active_tasks_by_project", return_value={}), \
             patch("app.services.reports.ReportsRepository.tasks_touched_today", return_value=set()), \
             patch("app.services.reports.ReportsRepository.project_statuses_lookup", return_value={}):
            response = ReportsService.build_project_task_summary(None, self.user, 1, 1, None, None, None, None)

        self.assertEqual(paginated.call_args.args[2], [1, 7])
        self.assertEqual(response["pagination"]["total_projects"], 2)

    def test_tasks_touched_today_is_asked_for_todays_boundary_not_the_requested_range(self):
        # The admin can be viewing any range (or all-time); which tasks show
        # must still be resolved against today, never the requested range.
        with patch("app.services.reports.ReportsRepository.project_ids_tracked_between", return_value={1}), \
             patch("app.services.reports.ReportsRepository.paginated_projects", return_value=([self.project], 1)), \
             patch("app.services.reports.ReportsRepository.session_seconds_by", return_value={}), \
             patch("app.services.reports.ReportsRepository.active_tasks_by_project", return_value={}), \
             patch("app.services.reports.ReportsRepository.tasks_touched_today",
                   return_value=set()) as touched, \
             patch("app.services.reports.ReportsRepository.project_statuses_lookup", return_value={}), \
             patch("app.services.reports.ist_today", return_value=date(2026, 9, 28)):
            ReportsService.build_project_task_summary(
                None, self.user, 1, 5, None, None, date(2020, 1, 1), date(2020, 1, 31),
            )
        _db, _org, _page_ids, start_time, end_time, start_date, end_date = touched.call_args.args
        self.assertEqual(start_date, date(2026, 9, 28))
        self.assertEqual(end_date, date(2026, 9, 28))

    def test_no_projects_on_page_returns_empty_list_not_error(self):
        with patch("app.services.reports.ReportsRepository.project_ids_tracked_between", return_value={1}), \
             patch("app.services.reports.ReportsRepository.paginated_projects", return_value=([], 0)), \
             patch("app.services.reports.ReportsRepository.session_seconds_by", return_value={}), \
             patch("app.services.reports.ReportsRepository.active_tasks_by_project", return_value={}), \
             patch("app.services.reports.ReportsRepository.tasks_touched_today", return_value=set()), \
             patch("app.services.reports.ReportsRepository.project_statuses_lookup", return_value={}):
            response = ReportsService.build_project_task_summary(None, self.user, 1, 5, None, None, None, None)
        self.assertEqual(response["projects"], [])
        self.assertEqual(response["pagination"]["total_pages"], 0)


# ---------------------------------------------------------------------------
# ReportsRepository.tasks_touched_today, against a real SQLite database.
#
# The day-boundary conversion (IST midnight -> UTC instants) is exactly the
# kind of thing a mock cannot catch a mistake in: a mock returns whatever the
# test tells it to, whichever instant the real WHERE clause actually used.
# ---------------------------------------------------------------------------

from datetime import timedelta  # noqa: E402

from sqlalchemy import BigInteger as _BigInteger, create_engine, select  # noqa: E402
from sqlalchemy.ext.compiler import compiles  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.core.database import Base  # noqa: E402
from app.core.time_format import ist_day_end_utc, ist_day_start_utc  # noqa: E402
from app.models.manual_time_entry import ManualTimeEntry  # noqa: E402
from app.models.project import Project  # noqa: E402
from app.models.task import Task  # noqa: E402
from app.models.time_entry import TimeEntry  # noqa: E402
from app.repositories.reports import ReportsRepository  # noqa: E402


@compiles(_BigInteger, "sqlite")
def _bigint_is_integer_on_sqlite(type_, compiler, **kw):
    """INTEGER PRIMARY KEY is SQLite's autoincrementing rowid alias; Postgres's
    Identity(always=True) has no SQLite equivalent, and BIGINT stays a 64-bit
    integer in both engines regardless of which one assigns the id."""
    return "INTEGER"


class TasksTouchedTodayTests(unittest.TestCase):
    ORG = 1
    PROJECT = 1

    def setUp(self):
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(self.engine, tables=[
            Project.__table__, Task.__table__, TimeEntry.__table__, ManualTimeEntry.__table__,
        ])
        self.db = Session(self.engine)
        self.db.add(Project(id=self.PROJECT, organization_id=self.ORG, project_name="Alpha", created_by=1))
        self.today = date(2026, 9, 28)
        self.task_ids = {}
        for offset, label in enumerate((
            "touched", "untouched", "yesterday_only", "manual_pending", "manual_approved",
        )):
            task_id = 100 + offset
            self.db.add(Task(id=task_id, organization_id=self.ORG, project_id=self.PROJECT,
                              task_name=label, created_by=1))
            self.task_ids[label] = task_id
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def _entry(self, task_label, start_time, user_id=10, end_time=None):
        self.db.add(TimeEntry(
            organization_id=self.ORG, user_id=user_id, project_id=self.PROJECT,
            task_id=self.task_ids[task_label], start_time=start_time,
            end_time=end_time, status="running" if end_time is None else "completed",
        ))
        self.db.commit()

    def _touched(self):
        start_time = ist_day_start_utc(self.today)
        end_time = ist_day_end_utc(self.today)
        return ReportsRepository.tasks_touched_today(
            self.db, self.ORG, [self.PROJECT], start_time, end_time, self.today, self.today,
        )

    def test_a_task_with_a_running_entry_today_is_touched(self):
        self._entry("touched", ist_day_start_utc(self.today) + timedelta(hours=9), end_time=None)
        self.assertIn(self.task_ids["touched"], self._touched())

    def test_a_task_with_no_entry_at_all_is_not_touched(self):
        self.assertNotIn(self.task_ids["untouched"], self._touched())

    def test_a_task_only_worked_on_yesterday_is_not_touched(self):
        yesterday_start = ist_day_start_utc(self.today - timedelta(days=1))
        self._entry("yesterday_only", yesterday_start + timedelta(hours=10),
                    end_time=yesterday_start + timedelta(hours=11))
        self.assertNotIn(self.task_ids["yesterday_only"], self._touched())

    def test_the_instant_ist_midnight_rolls_into_today_counts(self):
        # The first instant of today's IST calendar day, not a moment before it.
        self._entry("touched", ist_day_start_utc(self.today))
        self.assertIn(self.task_ids["touched"], self._touched())

    def test_one_second_before_ist_midnight_does_not_count(self):
        self._entry("touched", ist_day_start_utc(self.today) - timedelta(seconds=1))
        self.assertNotIn(self.task_ids["touched"], self._touched())

    def test_the_end_boundary_is_exclusive(self):
        # ist_day_end_utc(today) is the first instant of *tomorrow*.
        self._entry("touched", ist_day_end_utc(self.today))
        self.assertNotIn(self.task_ids["touched"], self._touched())

    def test_two_different_users_each_active_on_a_different_task_both_count(self):
        self._entry("touched", ist_day_start_utc(self.today) + timedelta(hours=9), user_id=10)
        self._entry("untouched", ist_day_start_utc(self.today) + timedelta(hours=9), user_id=11)
        touched = self._touched()
        self.assertIn(self.task_ids["touched"], touched)
        self.assertIn(self.task_ids["untouched"], touched)

    def test_a_pending_manual_entry_does_not_count(self):
        self.db.add(ManualTimeEntry(
            organization_id=self.ORG, user_id=10, project_id=self.PROJECT,
            task_id=self.task_ids["manual_pending"], work_date=self.today,
            start_time=ist_day_start_utc(self.today) + timedelta(hours=9),
            end_time=ist_day_start_utc(self.today) + timedelta(hours=10),
            total_seconds=3600, approval_status="pending",
        ))
        self.db.commit()
        self.assertNotIn(self.task_ids["manual_pending"], self._touched())

    def test_an_approved_manual_entry_for_today_counts(self):
        self.db.add(ManualTimeEntry(
            organization_id=self.ORG, user_id=10, project_id=self.PROJECT,
            task_id=self.task_ids["manual_approved"], work_date=self.today,
            start_time=ist_day_start_utc(self.today) + timedelta(hours=9),
            end_time=ist_day_start_utc(self.today) + timedelta(hours=10),
            total_seconds=3600, approval_status="approved",
        ))
        self.db.commit()
        self.assertIn(self.task_ids["manual_approved"], self._touched())

    def test_no_project_ids_short_circuits_to_an_empty_set(self):
        self.assertEqual(ReportsRepository.tasks_touched_today(
            self.db, self.ORG, [], ist_day_start_utc(self.today), ist_day_end_utc(self.today),
            self.today, self.today,
        ), set())


if __name__ == "__main__":
    unittest.main()

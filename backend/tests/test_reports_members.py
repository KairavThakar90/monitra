"""The Reports page's per-member activity: ``GET /api/v1/react/reports/members``.

The page shows a member's activity percentage when their name is hovered. That number
has to be the *same* number the "Avg. Activity" tile shows for the same filters, or
hovering one member of a one-member report would contradict the tile above it. So it
comes from the same entry-level query, with the same definition, and what is pinned
here is that definition and its edges:

* **Duration-weighted, not a mean of percentages.** A 60-second window at 80% and a
  180-second window at 20% make 35%, not 50%.
* **Nothing sampled is null, not 0%.** A member with manual time only, or a timer entry
  with no activity windows, has no activity figure; it must not read as "0% active"
  and it must not drag anyone else's average down.
* **Same filters as every tab:** the date range (an IST calendar range, whole end day
  included), projects, members and search; another organization's rows never appear.
* **A caller without ``time_entries:view_all`` sees only themself,** whatever
  ``member_id`` they send.
* **It agrees with the summary strip** for the same filters.

The query is Postgres SQL, so it runs here on SQLite with stand-ins for the two
Postgres functions it uses (``timezone`` and ``greatest``) -- the real statement, not a
mock of it.
"""
import unittest
from datetime import date, datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import BigInteger, create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.core.security import get_current_user
from app.main import app
from app.models.manual_time_entry import ManualTimeEntry
from app.models.project import Project
from app.models.task import Task
from app.models.time_entry import TimeEntry
from app.models.time_entry_activity import TimeEntryActivity
from app.models.time_entry_adjustment import TimeEntryAdjustment
from app.models.user import User
from app.react_apis.reports_page.service import ReportsPageService


@compiles(JSONB, "sqlite")
def _jsonb_is_json_on_sqlite(type_, compiler, **kw):  # pragma: no cover - dialect shim
    return "JSON"


@compiles(BigInteger, "sqlite")
def _bigint_is_integer_on_sqlite(type_, compiler, **kw):  # pragma: no cover - dialect shim
    return "INTEGER"


def _timezone(zone, value):
    """Postgres ``timezone('Asia/Kolkata', ts)`` for the one zone the reports use: UTC + 5:30."""
    if value is None:
        return None
    parsed = datetime.fromisoformat(str(value))
    return (parsed + timedelta(hours=5, minutes=30)).strftime("%Y-%m-%d %H:%M:%S")


def _sqlite_session() -> Session:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)

    @event.listens_for(engine, "connect")
    def _install_postgres_functions(dbapi_connection, _record):
        dbapi_connection.create_function("timezone", 2, _timezone)
        dbapi_connection.create_function("greatest", 2, lambda a, b: max(a, b))

    tables = [m.__table__ for m in (User, Project, Task, TimeEntry, TimeEntryActivity, TimeEntryAdjustment, ManualTimeEntry)]
    stripped = []
    for table in tables:
        for column in table.columns:
            default = column.server_default
            if default is not None and "::" in str(getattr(default, "arg", "")):
                stripped.append((column, default))
                column.server_default = None
    try:
        Base.metadata.create_all(engine, tables=tables)
    finally:
        for column, default in stripped:
            column.server_default = default
    # Postgres limits a user to ONE running entry with a partial unique index; SQLite would make it
    # unique across all of a user's entries. These fixtures are finished entries, so it does not apply.
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP INDEX IF EXISTS uq_active_time_entry")
    return Session(engine)


VIEW_ALL = {"time_entries:view_all": True}


def _user(id_, name, org=1, role="administrator", permissions=None):
    user = User()
    user.id, user.organization_id, user.username, user.email, user.name = id_, org, f"u{id_}", f"u{id_}@x.test", name
    user.role_name, user.capture_frequency, user.is_active = role, 10, True
    user.permissions = permissions if permissions is not None else {}
    return user


def _at(day: date, hour=9, minute=0) -> datetime:
    """An IST wall-clock time on `day`, as the UTC instant stored in the table."""
    return datetime(day.year, day.month, day.day, hour, minute) - timedelta(hours=5, minutes=30)


D1, D2, D3 = date(2026, 10, 5), date(2026, 10, 6), date(2026, 10, 7)
BEFORE = date(2026, 10, 4)


class Fixture:
    """Four members with activity shaped so every rule above has a distinguishing case."""

    def __init__(self):
        self.db = _sqlite_session()
        db = self.db
        self.ada = _user(1, "Ada Admin", permissions=VIEW_ALL)
        self.bob = _user(2, "Bob Brown", role="employee")
        self.cy = _user(3, "Cy Manual", role="employee")
        self.dee = _user(4, "Dee Quiet", role="employee")
        self.eve = _user(5, "Eve Elsewhere", org=2, role="employee")
        db.add_all([self.ada, self.bob, self.cy, self.dee, self.eve])
        db.add_all([
            Project(id=1, organization_id=1, project_name="Alpha", created_by=1),
            Project(id=2, organization_id=1, project_name="Beta", created_by=1),
            Project(id=3, organization_id=2, project_name="Elsewhere", created_by=5),
        ])
        db.add_all([
            Task(id=1, organization_id=1, project_id=1, task_name="Build", created_by=1),
            Task(id=2, organization_id=1, project_id=2, task_name="Plan", created_by=1),
            Task(id=3, organization_id=2, project_id=3, task_name="Away", created_by=5),
        ])
        db.commit()

        # Ada, 3600 s: a 60 s window at 80% and a 180 s window at 20%  -> weighted 35, a plain mean would say 50.
        self._entry(1, user=1, project=1, task=1, day=D1, seconds=3600, samples=[(80, 60), (20, 180)])
        # Ada, 1800 s: one 60 s window at 100%.  Together: (4800 + 3600 + 6000) / 300 = 48.0
        self._entry(2, user=1, project=1, task=1, day=D2, seconds=1800, samples=[(100, 60)])
        # Ada, outside the range (the day before): 0% that must not be counted.
        self._entry(3, user=1, project=1, task=1, day=BEFORE, seconds=3600, samples=[(0, 600)])
        # Ada, another project, in range: 0% that counts across projects and that a project filter keeps out.
        self._entry(4, user=1, project=2, task=2, day=D3, seconds=300, samples=[(0, 300)])
        # Bob, 7200 s with an adjustment of -1200 s -> reportable 6000 s; one 60 s window at 50%.
        self._entry(5, user=2, project=1, task=1, day=D1, seconds=7200, samples=[(50, 60)])
        db.add(TimeEntryAdjustment(organization_id=1, user_id=2, project_id=1, task_id=1, time_entry_id=5,
                                   adjustment_seconds=-1200, reason="idle"))
        # Dee: a timer entry nobody sampled.
        self._entry(6, user=4, project=1, task=1, day=D2, seconds=600, samples=[])
        # Eve (another organization): must never appear.
        self._entry(7, user=5, project=3, task=3, day=D1, seconds=9999, samples=[(99, 600)])
        # Cy: approved manual time only.
        db.add(ManualTimeEntry(organization_id=1, user_id=3, project_id=1, task_id=1, work_date=D2,
                               start_time=_at(D2), end_time=_at(D2, 10), total_seconds=3600,
                               approval_status="approved"))
        db.commit()

    def _entry(self, id_, user, project, task, day, seconds, samples):
        start = _at(day)
        self.db.add(TimeEntry(
            id=id_, organization_id=1 if user != 5 else 2, user_id=user, project_id=project, task_id=task,
            start_time=start, end_time=start + timedelta(seconds=seconds), total_seconds=seconds, status="stopped",
        ))
        for index, (percent, window) in enumerate(samples):
            self.db.add(TimeEntryActivity(
                organization_id=1 if user != 5 else 2, time_entry_id=id_, activity_percentage=percent,
                window_seconds=window, recorded_at=start + timedelta(minutes=index),
            ))

    def members(self, caller=None, **params):
        caller = caller or self.ada
        filters = ReportsPageService.resolve_filters(
            caller, params.get("start_date", D1), params.get("end_date", D3),
            params.get("project_id"), params.get("task_id"), params.get("member_id"), self.db,
        )
        return ReportsPageService.members(
            self.db, filters, params.get("search"), params.get("sort_by", "total_hours"),
            params.get("sort_order", "desc"), params.get("page", 1), params.get("limit", 50),
        )

    def summary(self, caller=None, **params):
        caller = caller or self.ada
        filters = ReportsPageService.resolve_filters(
            caller, params.get("start_date", D1), params.get("end_date", D3),
            params.get("project_id"), params.get("task_id"), params.get("member_id"), self.db,
        )
        return ReportsPageService.summary(self.db, filters)


def _by_name(page):
    return {item["member_name"]: item for item in page["items"]}


class MemberActivityTests(unittest.TestCase):

    def setUp(self):
        self.f = Fixture()

    def test_each_member_has_a_duration_weighted_activity_and_their_own_hours(self):
        rows = _by_name(self.f.members())

        # (80x60 + 20x180 + 100x60 + 0x300) / 600 = 24.0; a mean of the percentages would say 50.
        self.assertEqual(rows["Ada Admin"]["avg_activity"], 24.0)
        self.assertEqual(rows["Ada Admin"]["total_seconds"], 5700)      # 3600 + 1800 + 300; the day-before entry is outside the range
        self.assertEqual(rows["Bob Brown"]["avg_activity"], 50.0)
        self.assertEqual(rows["Bob Brown"]["total_seconds"], 6000)      # 7200 less the 1200 s adjustment, the same netting as every report

    def test_a_week_of_windows_is_weighted_by_how_long_each_lasted_not_how_many_there_were(self):
        """Ada's first entry alone: 60 s at 80% and 180 s at 20% is 35%, where a plain mean would say 50%."""
        rows = _by_name(self.f.members(start_date=D1, end_date=D1, member_id=[1]))
        self.assertEqual(rows["Ada Admin"]["avg_activity"], 35.0)

    def test_nothing_sampled_is_null_not_zero_and_does_not_drag_anyone_down(self):
        rows = _by_name(self.f.members())

        self.assertIsNone(rows["Cy Manual"]["avg_activity"])            # manual time only
        self.assertIsNone(rows["Dee Quiet"]["avg_activity"])            # a timer entry with no activity windows
        self.assertEqual(rows["Cy Manual"]["total_seconds"], 3600)      # their hours still count
        self.assertEqual(rows["Dee Quiet"]["total_seconds"], 600)
        self.assertEqual(rows["Ada Admin"]["avg_activity"], 24.0)       # and nobody else's figure moved

    def test_another_organizations_members_never_appear(self):
        self.assertNotIn("Eve Elsewhere", _by_name(self.f.members()))
        self.assertEqual(self.f.members()["total"], 4)

    def test_the_date_range_is_an_ist_calendar_range_with_the_whole_end_day(self):
        only_the_first_day = _by_name(self.f.members(start_date=D1, end_date=D1))
        self.assertEqual(set(only_the_first_day), {"Ada Admin", "Bob Brown"})

        day_before = _by_name(self.f.members(start_date=BEFORE, end_date=BEFORE))
        self.assertEqual(set(day_before), {"Ada Admin"})
        self.assertEqual(day_before["Ada Admin"]["avg_activity"], 0.0)  # a real zero: it WAS sampled, at 0%

    def test_the_project_and_member_filters_narrow_the_rows_like_every_tab(self):
        beta = _by_name(self.f.members(project_id=[2]))
        self.assertEqual(set(beta), {"Ada Admin"})
        self.assertEqual(beta["Ada Admin"]["avg_activity"], 0.0)

        alpha = _by_name(self.f.members(project_id=[1]))
        self.assertEqual(alpha["Ada Admin"]["avg_activity"], 48.0)      # the 0% on Beta is out

        just_bob = _by_name(self.f.members(member_id=[2]))
        self.assertEqual(set(just_bob), {"Bob Brown"})

    def test_it_agrees_with_the_summary_strip_for_the_same_filters(self):
        """Hovering the only member in view must show the number the tile above shows."""
        for params in ({"member_id": [1]}, {"member_id": [2]}, {"member_id": [1], "project_id": [1]},
                       {"member_id": [1], "start_date": D1, "end_date": D1}):
            with self.subTest(params=params):
                row = next(iter(self.f.members(**params)["items"]))
                self.assertEqual(row["avg_activity"], self.f.summary(**params)["avg_activity"])
                self.assertEqual(row["total_seconds"], self.f.summary(**params)["total_seconds"])

    def test_across_members_the_tile_is_the_same_weighting_not_an_average_of_the_rows(self):
        everyone = self.f.summary()
        # (14400 + 50x60) / (600 + 60) = 26.36: Ada's 24.0 and Bob's 50.0 averaged as numbers would say 37.0.
        self.assertEqual(everyone["avg_activity"], 26.36)

    def test_search_finds_a_member_by_part_of_their_name(self):
        self.assertEqual(set(_by_name(self.f.members(search="ad"))), {"Ada Admin"})
        self.assertEqual(self.f.members(search="nobody")["items"], [])

    def test_a_percent_sign_in_the_search_is_text_not_a_wildcard(self):
        self.assertEqual(self.f.members(search="%")["items"], [])

    def test_sorting_by_hours_and_by_activity_with_unsampled_members_last(self):
        by_hours = [row["member_name"] for row in self.f.members()["items"]]
        self.assertEqual(by_hours, ["Bob Brown", "Ada Admin", "Cy Manual", "Dee Quiet"])

        by_activity = [row["member_name"] for row in self.f.members(sort_by="avg_activity")["items"]]
        self.assertEqual(by_activity[:2], ["Bob Brown", "Ada Admin"])
        self.assertEqual(set(by_activity[2:]), {"Cy Manual", "Dee Quiet"})   # nulls last, in both directions

        ascending = [row["member_name"] for row in self.f.members(sort_by="avg_activity", sort_order="asc")["items"]]
        self.assertEqual(ascending[:2], ["Ada Admin", "Bob Brown"])
        self.assertEqual(set(ascending[2:]), {"Cy Manual", "Dee Quiet"})

    def test_paging_reports_the_whole_result_not_the_page(self):
        second = self.f.members(limit=2, page=2)

        self.assertEqual((second["total"], second["pages"], second["page"]), (4, 2, 2))
        self.assertEqual([row["member_name"] for row in second["items"]], ["Cy Manual", "Dee Quiet"])
        self.assertEqual(second["total_seconds"], 5700 + 6000 + 3600 + 600)

    def test_a_member_without_view_all_sees_only_themself_whatever_they_ask_for(self):
        rows = self.f.members(caller=self.f.bob, member_id=[1, 2, 3, 4])

        self.assertEqual(set(_by_name(rows)), {"Bob Brown"})
        self.assertEqual(rows["items"][0]["avg_activity"], 50.0)


class RouteTests(unittest.TestCase):

    def setUp(self):
        self.f = Fixture()
        self.caller = self.f.ada
        app.dependency_overrides[get_db] = lambda: self.f.db
        app.dependency_overrides[get_current_user] = lambda: self.caller
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()

    def _get(self, **params):
        query = {"start_date": D1.isoformat(), "end_date": D3.isoformat(), **params}
        return self.client.get("/api/v1/react/reports/members", params=query)

    def test_the_route_returns_the_rows_with_the_ids_and_names_the_page_needs(self):
        response = self._get()

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        ada = next(item for item in body["items"] if item["member_name"] == "Ada Admin")
        self.assertEqual((ada["member_id"], ada["avg_activity"], ada["total_seconds"]), (1, 24.0, 5700))
        cy = next(item for item in body["items"] if item["member_name"] == "Cy Manual")
        self.assertIsNone(cy["avg_activity"])                              # a real null on the wire, not 0

    def test_the_filters_arrive_as_repeated_parameters(self):
        body = self.client.get(
            "/api/v1/react/reports/members",
            params=[("start_date", "2026-10-05"), ("end_date", "2026-10-07"), ("member_id", 1), ("member_id", 2), ("project_id", 1)],
        ).json()

        self.assertEqual({item["member_name"] for item in body["items"]}, {"Ada Admin", "Bob Brown"})

    def test_a_backwards_range_is_a_400(self):
        self.assertEqual(self._get(start_date="2026-10-07", end_date="2026-10-05").status_code, 400)

    def test_the_page_size_is_capped_and_a_bad_sort_is_refused(self):
        self.assertEqual(self._get(limit=201).status_code, 422)
        self.assertEqual(self._get(sort_by="password").status_code, 422)

    def test_it_needs_a_signed_in_client(self):
        app.dependency_overrides.pop(get_current_user)
        self.assertEqual(self._get().status_code, 401)

    def test_an_employee_is_answered_with_their_own_row_only_over_http_too(self):
        self.caller = self.f.bob

        body = self._get(member_id=[1, 3]).json()

        self.assertEqual([item["member_name"] for item in body["items"]], ["Bob Brown"])


if __name__ == "__main__":
    unittest.main()

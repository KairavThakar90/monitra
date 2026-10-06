"""The project list can be filtered by the day a project was created.

The Project Management page has a date range, like the Assign Tasks page's, that
narrows the table to projects created inside it. Because the table is paged by
the server, the range is a query parameter and not a filter over what the
browser happens to hold:

    GET /projects?created_from=2026-10-01&created_to=2026-10-31

The days are **Asia/Kolkata calendar days** -- the same ones the date picker
offers, the Created column displays, and every report is measured on. That is
the whole point of most of these tests: a project created at 00:00 IST is still
the previous day in UTC, so a filter that compared UTC dates would put it on the
wrong day for exactly the people using it.

Both ends are inclusive days, either may be given alone, and a range whose ends
are the wrong way round is a 400, as it is on the Reports endpoints.

Service tests run against a real SQLite database (they assert what the `WHERE`
clause does); route tests go through the real router (they assert how the query
string is parsed). See `test_project_billing_filter.py`, which this mirrors.
"""
import unittest
from datetime import date, datetime, timezone
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.core.database import get_db
from app.core.permissions import ROLE_PERMISSIONS
from app.core.security import get_current_user
from app.main import app
from app.models.project import Project
from app.models.user import User
from app.schemas.project_management import BillingType, ProjectCategory
from app.services.project_management import ProjectManagementService
from tests.test_project_category import ADMIN, ORG, _World


def _utc(*parts) -> datetime:
    return datetime(*parts, tzinfo=timezone.utc)


# (id, name, created_at in UTC) -- the IST day each one falls on is in the comment.
CREATED = (
    (10, "Early", _utc(2026, 9, 30, 12, 0)),         # 30 Sep, 17:30 IST
    (11, "Last minute", _utc(2026, 10, 5, 18, 29, 59)),  # 5 Oct, 23:59:59 IST
    (12, "Midnight", _utc(2026, 10, 5, 18, 30, 0)),  # 6 Oct, 00:00:00 IST -- still 5 Oct in UTC
    (13, "Mid month", _utc(2026, 10, 15, 6, 0)),     # 15 Oct, 11:30 IST
    (14, "Late", _utc(2026, 11, 2, 4, 0)),           # 2 Nov, 09:30 IST
)


class CreatedFilterListTests(_World):
    def setUp(self):
        super().setUp()
        # The base world has three projects; add two more and date all five.
        for project_id, name in ((13, "Mid month"), (14, "Late")):
            self.db.add(Project(
                id=project_id, organization_id=ORG, project_name=name, status="active", status_id=1,
                leader_id=2, billing_type="free", created_by=ADMIN,
            ))
        self.db.flush()
        for project_id, name, created_at in CREATED:
            project = self.db.get(Project, project_id)
            project.project_name = name
            project.created_at = created_at
        self.db.commit()

    def _names(self, created_from=None, created_to=None, **kwargs):
        result = ProjectManagementService.list(
            self.db, self.admin, page=1, limit=20, search=None, status_id=None, leader_id=None,
            billing_type=kwargs.pop("billing_type", None), include_tasks=False,
            created_from=created_from, created_to=created_to, **kwargs,
        )
        return sorted(item["project_name"] for item in result["items"]), result["pagination"]["total"]

    ALL = ["Early", "Last minute", "Late", "Mid month", "Midnight"]

    def test_no_range_returns_every_project(self):
        self.assertEqual(self._names()[0], self.ALL)

    def test_a_range_returns_only_projects_created_inside_it(self):
        self.assertEqual(self._names(date(2026, 10, 1), date(2026, 10, 31))[0], ["Last minute", "Mid month", "Midnight"])

    def test_a_single_day_is_a_range_whose_ends_are_the_same_day(self):
        self.assertEqual(self._names(date(2026, 10, 15), date(2026, 10, 15))[0], ["Mid month"])

    def test_both_ends_are_inclusive(self):
        self.assertEqual(self._names(date(2026, 9, 30), date(2026, 10, 15))[0], ["Early", "Last minute", "Mid month", "Midnight"])

    def test_either_end_may_be_given_alone(self):
        self.assertEqual(self._names(created_from=date(2026, 10, 15))[0], ["Late", "Mid month"])
        self.assertEqual(self._names(created_to=date(2026, 9, 30))[0], ["Early"])

    def test_the_days_are_ist_days_not_utc_days(self):
        # 18:30:00 UTC on 5 Oct is exactly 00:00 IST on 6 Oct: the 6th, though
        # its UTC date is the 5th. A UTC-day filter gets both of these wrong.
        self.assertEqual(self._names(date(2026, 10, 6), date(2026, 10, 6))[0], ["Midnight"])
        self.assertNotIn("Midnight", self._names(date(2026, 10, 5), date(2026, 10, 5))[0])

    def test_one_second_before_midnight_ist_is_still_that_day(self):
        self.assertEqual(self._names(date(2026, 10, 5), date(2026, 10, 5))[0], ["Last minute"])

    def test_the_total_counts_the_filtered_set(self):
        names, total = self._names(date(2026, 10, 1), date(2026, 10, 31))
        self.assertEqual((len(names), total), (3, 3))

    def test_a_range_with_nothing_in_it_is_an_empty_page(self):
        self.assertEqual(self._names(date(2025, 1, 1), date(2025, 1, 31)), ([], 0))

    def test_a_range_the_wrong_way_round_is_a_400_and_never_queries(self):
        with self.assertRaises(HTTPException) as caught:
            self._names(date(2026, 10, 31), date(2026, 10, 1))
        self.assertEqual(caught.exception.status_code, 400)
        self.assertIn("created_from", caught.exception.detail)

    def test_it_composes_with_the_other_filters(self):
        self.assertEqual(
            self._names(date(2026, 10, 1), date(2026, 10, 31), billing_type=[BillingType.free])[0],
            ["Last minute", "Mid month", "Midnight"],
        )
        self.assertEqual(
            self._names(date(2026, 10, 1), date(2026, 10, 31), billing_type=[BillingType.fixed])[0], [],
        )
        # The base world's category tags: project 10 is Kyle ("Early" now), 11 is ST ("Last minute").
        self.assertEqual(
            self._names(date(2026, 9, 1), date(2026, 10, 31), category=ProjectCategory.st)[0], ["Last minute"],
        )

    def test_every_row_still_says_when_it_was_created(self):
        result = ProjectManagementService.list(
            self.db, self.admin, page=1, limit=20, search=None, status_id=None, leader_id=None,
            billing_type=None, include_tasks=False,
        )
        created = {item["project_name"]: item["created_at"] for item in result["items"]}
        self.assertEqual(len(created), 5)
        self.assertTrue(all(value is not None for value in created.values()))


class CreatedFilterRouteTests(unittest.TestCase):
    """What the query string becomes by the time it reaches the service."""

    def setUp(self):
        user = User()
        user.id, user.organization_id, user.role_name = ADMIN, ORG, "administrator"
        user.permissions = {p: True for p in ROLE_PERMISSIONS.get("administrator", set())}
        user.is_active = True
        app.dependency_overrides[get_current_user] = lambda: user
        app.dependency_overrides[get_db] = lambda: None
        self.client = TestClient(app)
        patcher = patch(
            "app.api.project_management.ProjectManagementService.list",
            return_value={"items": [], "pagination": {"page": 1, "limit": 20, "total": 0, "total_pages": 0}},
        )
        self.service = patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        app.dependency_overrides.clear()

    def _sent(self, query):
        response = self.client.get(f"/api/v1/projects?{query}")
        self.assertEqual(response.status_code, 200, response.text)
        # list(db, user, page, limit, search, status_id, leader_id, billing_type,
        #      include_tasks, employee_ids, category, created_from, created_to)
        args = self.service.call_args.args
        return args[11], args[12]

    def test_no_parameters_means_no_date_filter(self):
        self.assertEqual(self._sent("page=1&limit=20"), (None, None))

    def test_both_days_arrive_as_dates(self):
        self.assertEqual(self._sent("created_from=2026-10-01&created_to=2026-10-31"), (date(2026, 10, 1), date(2026, 10, 31)))

    def test_one_end_alone_is_enough(self):
        self.assertEqual(self._sent("created_from=2026-10-01"), (date(2026, 10, 1), None))
        self.assertEqual(self._sent("created_to=2026-10-31"), (None, date(2026, 10, 31)))

    def test_it_sits_beside_the_other_filters(self):
        self.assertEqual(
            self._sent("page=2&limit=50&category=kyle&billing_type=fixed&created_from=2026-10-01&created_to=2026-10-31"),
            (date(2026, 10, 1), date(2026, 10, 31)),
        )
        self.assertEqual(self.service.call_args.args[2:4], (2, 50))
        self.assertEqual(self.service.call_args.args[7], [BillingType.fixed])

    def test_a_malformed_date_is_a_422_and_the_service_never_runs(self):
        for query in ("created_from=yesterday", "created_to=2026-13-45", "created_from=01-10-2026", "created_from="):
            with self.subTest(query=query):
                self.service.reset_mock()
                self.assertEqual(self.client.get(f"/api/v1/projects?{query}").status_code, 422)
                self.service.assert_not_called()


if __name__ == "__main__":
    unittest.main()

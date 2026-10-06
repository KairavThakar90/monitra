"""The project list's billing-type filter takes one type *or several*.

The Project Management page filters the way the Task Listing page does: first
"Billing" or "Non Billing", then -- under Billing -- Fixed Hours or Flexible
Time. "Billing" with no second choice therefore means *both* billed kinds
(`fixed` and `free`), which one `billing_type` value cannot say. The parameter
is now repeatable, exactly like the Reports endpoint's:

    GET /projects?billing_type=fixed&billing_type=free

and everything that sent a single `billing_type=fixed` keeps working, because
one value is simply a list of one.

Two layers, for the usual reason (see `test_project_category.py`): the service
tests run against a real SQLite database because they assert what the `WHERE`
clause does, and the route tests go through the real router because a service
test alone would pass even if the query string were parsed wrongly.
"""
import unittest
from unittest.mock import patch

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


class BillingFilterListTests(_World):
    """Three projects, one of each billing type, each in its own category."""

    def setUp(self):
        super().setUp()
        # The base world has three `free` projects: Kyle one (10), ST one (11)
        # and an uncategorised Plain (12). Give them one type each.
        for project_id, billing_type, hours in ((10, "fixed", 40), (11, "free", None), (12, "non_billing", None)):
            project = self.db.get(Project, project_id)
            project.billing_type = billing_type
            project.fixed_hours = hours
        self.db.commit()

    def _names(self, billing_type, **kwargs):
        result = ProjectManagementService.list(
            self.db, self.admin, page=1, limit=20, search=None, status_id=None,
            leader_id=None, billing_type=billing_type, include_tasks=False, **kwargs,
        )
        return sorted(item["project_name"] for item in result["items"]), result["pagination"]["total"]

    def test_no_filter_returns_every_project(self):
        self.assertEqual(self._names(None)[0], ["Kyle one", "Plain", "ST one"])

    def test_one_type_returns_only_that_type(self):
        self.assertEqual(self._names([BillingType.fixed])[0], ["Kyle one"])
        self.assertEqual(self._names([BillingType.free])[0], ["ST one"])
        self.assertEqual(self._names([BillingType.non_billing])[0], ["Plain"])

    def test_both_billed_kinds_together_exclude_non_billing(self):
        # What "Billing" with no second choice sends.
        self.assertEqual(self._names([BillingType.fixed, BillingType.free])[0], ["Kyle one", "ST one"])

    def test_any_two_types_combine(self):
        self.assertEqual(self._names([BillingType.fixed, BillingType.non_billing])[0], ["Kyle one", "Plain"])

    def test_all_three_types_are_the_same_as_no_filter(self):
        self.assertEqual(self._names(list(BillingType))[0], ["Kyle one", "Plain", "ST one"])

    def test_the_total_counts_the_filtered_set_not_the_whole_list(self):
        # The page's pagination is computed from the same filter, so "2 projects"
        # never sits beside a table that was cut from three.
        names, total = self._names([BillingType.fixed, BillingType.free])
        self.assertEqual((len(names), total), (2, 2))

    def test_a_type_nobody_has_returns_an_empty_page(self):
        for project_id in (10, 11, 12):
            self.db.get(Project, project_id).billing_type = "free"
        self.db.commit()
        self.assertEqual(self._names([BillingType.fixed]), ([], 0))

    def test_it_composes_with_the_category_filter(self):
        self.assertEqual(self._names([BillingType.fixed, BillingType.free], category=ProjectCategory.kyle)[0], ["Kyle one"])
        self.assertEqual(self._names([BillingType.non_billing], category=ProjectCategory.kyle)[0], [])


class BillingFilterRouteTests(unittest.TestCase):
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
        # list(db, user, page, limit, search, status_id, leader_id, billing_type, ...)
        return self.service.call_args.args[7]

    def test_no_parameter_means_no_filter(self):
        self.assertIsNone(self._sent("page=1&limit=20"))

    def test_a_single_value_still_works_as_it_always_did(self):
        self.assertEqual(self._sent("billing_type=fixed"), [BillingType.fixed])
        self.assertEqual(self._sent("billing_type=non_billing"), [BillingType.non_billing])

    def test_a_repeated_parameter_is_a_list_in_order(self):
        self.assertEqual(
            self._sent("billing_type=fixed&billing_type=free"), [BillingType.fixed, BillingType.free],
        )

    def test_it_sits_beside_the_other_filters(self):
        self.assertEqual(
            self._sent("page=2&limit=50&category=kyle&billing_type=free&billing_type=fixed"),
            [BillingType.free, BillingType.fixed],
        )
        self.assertEqual(self.service.call_args.args[2:4], (2, 50))

    def test_an_unknown_type_is_a_422_and_the_service_never_runs(self):
        for query in ("billing_type=gold", "billing_type=fixed&billing_type=gold", "billing_type=FIXED", "billing_type="):
            with self.subTest(query=query):
                self.service.reset_mock()
                response = self.client.get(f"/api/v1/projects?{query}")
                self.assertEqual(response.status_code, 422)
                self.service.assert_not_called()


if __name__ == "__main__":
    unittest.main()

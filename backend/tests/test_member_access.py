"""End-to-end route tests for the member-side Dashboard and Reports.

The member pages in the web client reuse the admin endpoints rather than
having their own. That only works if two things hold at the same time, and
these tests pin both:

 1. A caller *without* ``time_entries:view_all`` is no longer refused. The
    employee role never had that permission, so while the routes required it
    every member-side screen was a 403.
 2. That caller's scope is pinned to themselves in the service, not merely in
    the UI. The request goes through the real router and dependency chain, so
    a ``?member_id=`` naming somebody else must still come out as the caller's
    own id by the time the service sees it.
"""

import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.core.database import get_db
from app.core.security import get_current_user
from app.main import app
from app.models.user import User


def _user(**overrides) -> User:
    user = User()
    user.id = 77
    user.organization_id = 1
    user.role_name = "employee"
    user.permissions = {}
    user.is_active = True
    for key, value in overrides.items():
        setattr(user, key, value)
    return user


class _CapturedFilters:
    """Stands in for the service call, recording the filters it was handed."""

    def __init__(self, payload):
        self.payload = payload
        self.filters = None

    def __call__(self, _db, filters, *args, **kwargs):
        self.filters = filters
        return self.payload


DASHBOARD_PAYLOAD = {
    "filters": {"start_date": "2026-09-01", "end_date": "2026-09-07",
                "project_id": [], "task_id": [], "member_id": [77]},
    "summary": {"activity": None, "monthly_activity": None, "total_hours": 0.0,
                "active_projects": 0, "team_members": 0, "total_tasks": 0},
    "time_tracked": {"interval": "day", "data": []},
    # `total_seconds`/`total_hours` are the scope-wide totals every ranked
    # page now carries -- the denominator a part-to-whole chart divides by.
    "top_projects": {"items": [], "page": 1, "limit": 10, "total": 0, "pages": 0,
                     "total_seconds": 0, "total_hours": 0.0},
    "top_members": {"items": [], "page": 1, "limit": 10, "total": 0, "pages": 0,
                    "total_seconds": 0, "total_hours": 0.0},
    "top_apps": {"items": [], "page": 1, "limit": 10, "total": 0, "pages": 0,
                 "total_seconds": 0, "total_hours": 0.0, "total_app_hours": 0.0},
}

SUMMARY_PAYLOAD = {"total_hours": 0.0, "avg_activity": None, "total_members": 0, "total_tasks": 0}


class MemberRouteAccessTests(unittest.TestCase):
    def setUp(self):
        self.user = _user()
        app.dependency_overrides[get_current_user] = lambda: self.user
        app.dependency_overrides[get_db] = lambda: None
        self.client = TestClient(app)
        self.addCleanup(app.dependency_overrides.clear)

    def test_a_member_can_load_the_dashboard(self):
        capture = _CapturedFilters(DASHBOARD_PAYLOAD)
        with patch("app.react_apis.dashboard.router.DashboardService.dashboard", capture):
            response = self.client.get(
                "/api/v1/react/dashboard", params={"start_date": "2026-09-01", "end_date": "2026-09-07"}
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(capture.filters.member_ids, (77,))

    def test_a_member_cannot_read_another_members_dashboard(self):
        capture = _CapturedFilters(DASHBOARD_PAYLOAD)
        with patch("app.react_apis.dashboard.router.DashboardService.dashboard", capture):
            response = self.client.get(
                "/api/v1/react/dashboard",
                params={"start_date": "2026-09-01", "end_date": "2026-09-07", "member_id": 999},
            )
        self.assertEqual(response.status_code, 200, response.text)
        # The id from the query string is replaced, not merged.
        self.assertEqual(capture.filters.member_ids, (77,))

    def test_a_member_can_load_the_reports_summary_scoped_to_themselves(self):
        capture = _CapturedFilters(SUMMARY_PAYLOAD)
        with patch("app.react_apis.reports_page.router.ReportsPageService.summary", capture):
            response = self.client.get(
                "/api/v1/react/reports/summary",
                params={"start_date": "2026-09-01", "end_date": "2026-09-07", "member_id": 999},
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(capture.filters.member_ids, (77,))

    def test_a_privileged_caller_still_sees_the_whole_organization(self):
        self.user = _user(role_name="manager", permissions={"time_entries:view_all": True})
        app.dependency_overrides[get_current_user] = lambda: self.user
        capture = _CapturedFilters(DASHBOARD_PAYLOAD)
        with patch("app.react_apis.dashboard.router.DashboardService.dashboard", capture):
            response = self.client.get(
                "/api/v1/react/dashboard", params={"start_date": "2026-09-01", "end_date": "2026-09-07"}
            )
        self.assertEqual(response.status_code, 200, response.text)
        # No member filter at all: every member is in scope.
        self.assertEqual(capture.filters.member_ids, ())


if __name__ == "__main__":
    unittest.main()

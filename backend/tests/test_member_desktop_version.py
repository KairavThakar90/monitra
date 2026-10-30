"""The Members directory's desktop version: the column and the Version filter.

What is pinned, on a real (SQLite) database:

* every member row carries the version and OS their desktop last reported, and None for a member whose
  desktop never reported one (never a guessed version);
* the filter keeps only the members on one version, `none` keeps only those who never reported, and no filter
  keeps everyone -- counted and paged on the server, so a filter is right across pages and not only for the
  page on screen;
* the filter combines with the other filters (here: search) instead of replacing them;
* the route accepts a plain version string and refuses anything else.
"""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import BigInteger, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.core.security import get_current_user
from app.main import app
from app.models.desktop_client_version import DesktopClientVersion
from app.models.user import User
from app.repositories.member import MemberRepository
from app.schemas.member import MemberResponse
from app.services.member_service import MemberService


@compiles(JSONB, "sqlite")
def _jsonb_is_json_on_sqlite(type_, compiler, **kw):  # pragma: no cover - dialect shim
    return "JSON"


@compiles(BigInteger, "sqlite")
def _bigint_is_integer_on_sqlite(type_, compiler, **kw):  # pragma: no cover - dialect shim
    return "INTEGER"


def _session() -> Session:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    tables = [User.__table__, DesktopClientVersion.__table__]
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
    return Session(engine, expire_on_commit=False)


def _user(user_id, name, **extra) -> User:
    user = User()
    user.id = user_id
    user.organization_id = 1
    user.name = name
    user.username = name.lower()
    user.email = f"{name.lower()}@example.com"
    user.role_name = "employee"
    user.status = "active"
    user.is_active = True
    user.capture_frequency = 10
    user.permissions = {}
    for key, value in extra.items():
        setattr(user, key, value)
    return user


class MemberVersionTests(unittest.TestCase):

    def setUp(self):
        self.db = _session()
        self.db.add_all([
            _user(1, "Asha"), _user(2, "Bela"), _user(3, "Chitra"), _user(4, "Dev"), _user(5, "Esha", organization_id=2),
        ])
        self.db.add_all([
            DesktopClientVersion(id=1, organization_id=1, user_id=1, app_version="1.3.4", platform="win32"),
            DesktopClientVersion(id=2, organization_id=1, user_id=2, app_version="1.3.4", platform="darwin"),
            DesktopClientVersion(id=3, organization_id=1, user_id=3, app_version="1.2.0", platform=None),
        ])  # Dev never reported one
        self.db.commit()

    def names(self, **kwargs):
        items, total = MemberRepository.list_by_organization(self.db, 1, kwargs.pop("search", None), None, None, 1, 20, **kwargs)
        return [m.name for m in items], total

    def test_no_filter_keeps_everyone_in_the_organization(self):
        self.assertEqual(self.names(), (["Asha", "Bela", "Chitra", "Dev"], 4))

    def test_a_version_keeps_only_the_members_on_it(self):
        self.assertEqual(self.names(desktop_version="1.3.4"), (["Asha", "Bela"], 2))
        self.assertEqual(self.names(desktop_version="1.2.0"), (["Chitra"], 1))

    def test_none_keeps_only_members_whose_desktop_never_reported(self):
        self.assertEqual(self.names(desktop_version="none"), (["Dev"], 1))

    def test_a_version_nobody_is_on_matches_nobody(self):
        self.assertEqual(self.names(desktop_version="9.9.9"), ([], 0))

    def test_another_organizations_report_is_never_counted(self):
        self.db.add(DesktopClientVersion(id=9, organization_id=2, user_id=5, app_version="1.3.4", platform="win32"))
        self.db.commit()
        self.assertEqual(self.names(desktop_version="1.3.4"), (["Asha", "Bela"], 2))

    def test_the_total_and_the_pages_follow_the_filter(self):
        items, total = MemberRepository.list_by_organization(self.db, 1, None, None, None, 2, 1, desktop_version="1.3.4")
        self.assertEqual(([m.name for m in items], total), (["Bela"], 2))

    def test_the_filter_combines_with_search(self):
        self.assertEqual(self.names(desktop_version="1.3.4", search="bel"), (["Bela"], 1))
        self.assertEqual(self.names(desktop_version="none", search="bel"), ([], 0))

    def test_each_row_carries_its_version_and_system_and_none_when_never_reported(self):
        items, _ = MemberRepository.list_by_organization(self.db, 1, None, None, None, 1, 20)
        MemberService._with_desktop_versions(self.db, items)
        got = {m.name: (m.desktop_version, m.desktop_platform) for m in items}
        self.assertEqual(got, {
            "Asha": ("1.3.4", "win32"), "Bela": ("1.3.4", "darwin"), "Chitra": ("1.2.0", None), "Dev": (None, None),
        })

    def test_the_response_model_exposes_them(self):
        items, _ = MemberRepository.list_by_organization(self.db, 1, None, None, None, 1, 20)
        MemberService._with_desktop_versions(self.db, items)
        asha = next(m for m in items if m.name == "Asha")
        asha.created_at = asha.updated_at = __import__("datetime").datetime(2026, 10, 30)
        asha.idle_enabled, asha.idle_minutes = True, 5
        out = MemberResponse.model_validate(asha)
        self.assertEqual((out.desktop_version, out.desktop_platform), ("1.3.4", "win32"))


class MemberVersionRoute(unittest.TestCase):

    def setUp(self):
        admin = SimpleNamespace(id=1, organization_id=1, role_name="administrator", is_active=True,
                                permissions={"view_employees": True}, status="active")
        app.dependency_overrides[get_current_user] = lambda: admin
        app.dependency_overrides[get_db] = lambda: None
        self.addCleanup(app.dependency_overrides.clear)
        self.client = TestClient(app)

    def test_the_filter_reaches_the_service(self):
        empty = {"items": [], "page": 1, "limit": 20, "total": 0, "pages": 0}
        with patch("app.api.members.MemberService.list", return_value=empty) as listing:
            response = self.client.get("/members?desktop_version=1.3.4")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(listing.call_args.kwargs["desktop_version"], "1.3.4")

    def test_no_filter_means_none_is_passed(self):
        empty = {"items": [], "page": 1, "limit": 20, "total": 0, "pages": 0}
        with patch("app.api.members.MemberService.list", return_value=empty) as listing:
            self.client.get("/members")
        self.assertIsNone(listing.call_args.kwargs["desktop_version"])

    def test_anything_but_a_plain_version_is_refused(self):
        for bad in ("1.3.4;drop", "a b", "x" * 40, "%27", "1/2"):
            response = self.client.get("/members", params={"desktop_version": bad})
            self.assertEqual(response.status_code, 422, bad)


if __name__ == "__main__":
    unittest.main()

"""The Members directory can be filtered by the Login and Add Task switches.

``GET /members?can_login=true|false`` and ``?can_add_tasks=true|false`` keep
the members whose switch is on (allowed) or off (excluded); leaving a
parameter out does not filter on it. Run against a real session over an
in-memory database so the SQL is the SQL that ships:

* each filter keeps exactly the right members, alone and together;
* the page and the total both describe the *filtered* list, so paging and the
  "Select all N" walk agree with what is on screen;
* the filters compose with the role, search and status filters and with a
  leader's narrowed directory, and never reach across organizations;
* the route hands the parameters to the service as booleans (and refuses
  anything else), without disturbing the positional arguments other callers
  and tests rely on.
"""
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from sqlalchemy import BigInteger, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.core.permissions import ROLE_PERMISSIONS
from app.core.security import get_current_user
from app.main import app
from app.models.user import User
from app.repositories.member import MemberRepository
from app.services.member_service import MemberService


@compiles(JSONB, "sqlite")
def _jsonb_is_json_on_sqlite(type_, compiler, **kw):  # pragma: no cover - dialect shim
    return "JSON"


@compiles(BigInteger, "sqlite")
def _bigint_is_integer_on_sqlite(type_, compiler, **kw):  # pragma: no cover - dialect shim
    return "INTEGER"


ORG, OTHER_ORG = 7, 8
NOW = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)

#: name -> (can_login, can_add_tasks). Every combination, once.
SWITCHES = {
    "Alice": (True, True),
    "Bob": (False, True),
    "Cara": (True, False),
    "Dan": (False, False),
}


def _database() -> Session:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    tables = [Base.metadata.tables["organizations"], User.__table__]
    # Postgres casts in server defaults ('{}'::jsonb) are not SQLite syntax.
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
    return Session(engine)


def _user(db, user_id, name, can_login=True, can_add_tasks=True, role="employee", status="active", organization_id=ORG):
    user = User(
        id=user_id, organization_id=organization_id, username=name.lower(), email=f"{name.lower()}@example.invalid",
        name=name, role_name=role, password_hash="x", permissions={}, is_active=status == "active", status=status,
        idle_enabled=True, idle_minutes=5, capture_frequency=10, designation="Dev",
        can_login=can_login, can_add_tasks=can_add_tasks, created_at=NOW, updated_at=NOW,
    )
    db.add(user)
    db.flush()
    return user


class _World:
    def __init__(self):
        self.db = _database()
        self.ids = {}
        for i, (name, (login, add_task)) in enumerate(SWITCHES.items(), start=1):
            self.ids[name] = _user(self.db, i, name, can_login=login, can_add_tasks=add_task).id
        self.db.commit()

    def names(self, **filters):
        items, _ = self.list(**filters)
        return [u.name for u in items]

    def list(self, search=None, role=None, status=None, page=1, limit=20, member_ids=None, **switches):
        return MemberRepository.list_by_organization(self.db, ORG, search, role, status, page, limit, member_ids, **switches)


class RepositoryFilterTests(unittest.TestCase):
    def setUp(self):
        self.world = _World()

    def test_no_filter_lists_everyone(self):
        self.assertEqual(self.world.names(), ["Alice", "Bob", "Cara", "Dan"])

    def test_login_allowed_and_excluded(self):
        self.assertEqual(self.world.names(can_login=True), ["Alice", "Cara"])
        self.assertEqual(self.world.names(can_login=False), ["Bob", "Dan"])

    def test_add_task_allowed_and_excluded(self):
        self.assertEqual(self.world.names(can_add_tasks=True), ["Alice", "Bob"])
        self.assertEqual(self.world.names(can_add_tasks=False), ["Cara", "Dan"])

    def test_the_two_filters_combine(self):
        self.assertEqual(self.world.names(can_login=True, can_add_tasks=True), ["Alice"])
        self.assertEqual(self.world.names(can_login=True, can_add_tasks=False), ["Cara"])
        self.assertEqual(self.world.names(can_login=False, can_add_tasks=True), ["Bob"])
        self.assertEqual(self.world.names(can_login=False, can_add_tasks=False), ["Dan"])

    def test_an_unset_filter_does_not_constrain(self):
        self.assertEqual(self.world.names(can_login=None, can_add_tasks=False), ["Cara", "Dan"])

    def test_the_two_filters_partition_the_directory(self):
        allowed, excluded = self.world.names(can_login=True), self.world.names(can_login=False)
        self.assertEqual(sorted(allowed + excluded), ["Alice", "Bob", "Cara", "Dan"])
        self.assertFalse(set(allowed) & set(excluded))

    def test_the_total_and_the_pages_describe_the_filtered_list(self):
        items, total = self.world.list(can_login=False, limit=1, page=1)
        self.assertEqual(([u.name for u in items], total), (["Bob"], 2))
        items, total = self.world.list(can_login=False, limit=1, page=2)
        self.assertEqual(([u.name for u in items], total), (["Dan"], 2))
        items, total = self.world.list(can_login=False, limit=1, page=3)
        self.assertEqual(([u.name for u in items], total), ([], 2))

    def test_it_composes_with_search_role_and_status(self):
        _user(self.world.db, 20, "Zed", can_login=False, role="leader")
        _user(self.world.db, 21, "Yan", can_login=True, status="inactive")
        self.world.db.commit()
        self.assertEqual(self.world.names(can_login=False, search="zed"), ["Zed"])
        self.assertEqual(self.world.names(can_login=False, search="bob"), ["Bob"])
        self.assertEqual(self.world.names(can_login=False, role="leader"), ["Zed"])
        self.assertEqual(self.world.names(can_login=False, role="employee"), ["Bob", "Dan"])
        # The filter follows the switch the column shows, so an Inactive member
        # whose Login switch is on is "allowed" until Status narrows it away.
        self.assertIn("Yan", self.world.names(can_login=True))
        self.assertNotIn("Yan", self.world.names(can_login=True, status="active"))
        self.assertEqual(self.world.names(can_login=True, status="inactive"), ["Yan"])

    def test_a_leaders_narrowed_directory_still_applies(self):
        team = {self.world.ids["Alice"], self.world.ids["Bob"]}
        self.assertEqual(self.world.names(member_ids=team, can_login=False), ["Bob"])
        self.assertEqual(self.world.names(member_ids=set(), can_login=False), [])

    def test_other_organizations_never_appear(self):
        _user(self.world.db, 30, "Outsider", can_login=False, organization_id=OTHER_ORG)
        self.world.db.commit()
        self.assertNotIn("Outsider", self.world.names(can_login=False))

    def test_a_switch_change_moves_the_member_between_lists(self):
        self.world.db.get(User, self.world.ids["Bob"]).can_login = True
        self.world.db.commit()
        self.assertEqual(self.world.names(can_login=False), ["Dan"])
        self.assertIn("Bob", self.world.names(can_login=True))


class ServiceTests(unittest.TestCase):
    def test_the_filters_reach_the_repository_as_keywords_after_the_leader_scope(self):
        with patch("app.services.member_service.MemberRepository.list_by_organization", return_value=([], 0)) as listed, \
                patch("app.services.member_service.visible_directory_ids", return_value=None):
            MemberService.list(MagicMock(), _caller(), None, None, None, 1, 20, can_login=False, can_add_tasks=True)
        self.assertIsNone(listed.call_args.args[-1])   # the leader scope is still the last positional
        self.assertEqual(listed.call_args.kwargs, {"can_login": False, "can_add_tasks": True, "can_add_nonbillable_tasks": None})

    def test_they_default_to_no_filter(self):
        with patch("app.services.member_service.MemberRepository.list_by_organization", return_value=([], 0)) as listed, \
                patch("app.services.member_service.visible_directory_ids", return_value=None):
            MemberService.list(MagicMock(), _caller(), None, None, None, 1, 20)
        self.assertEqual(listed.call_args.kwargs, {"can_login": None, "can_add_tasks": None, "can_add_nonbillable_tasks": None})


def _caller() -> User:
    user = User()
    user.id = 1
    user.organization_id = ORG
    user.role_name = "administrator"
    return user


class RouteTests(unittest.TestCase):
    EMPTY = {"items": [], "page": 1, "limit": 20, "total": 0, "pages": 0}

    def setUp(self):
        admin = _caller()
        admin.permissions = {p: True for p in ROLE_PERMISSIONS["administrator"]}
        admin.is_active = True
        admin.can_login = True
        app.dependency_overrides[get_db] = lambda: MagicMock()
        app.dependency_overrides[get_current_user] = lambda: admin
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()

    def _get(self, params):
        with patch("app.api.members.MemberService.list", return_value=self.EMPTY) as listed:
            response = self.client.get("/api/v1/members", params=params)
        return response, listed

    def test_booleans_are_passed_to_the_service(self):
        response, listed = self._get({"can_login": "true", "can_add_tasks": "false"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(listed.call_args.kwargs, {"can_login": True, "can_add_tasks": False, "can_add_nonbillable_tasks": None})

    def test_omitting_them_means_no_filter(self):
        response, listed = self._get({})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(listed.call_args.kwargs, {"can_login": None, "can_add_tasks": None, "can_add_nonbillable_tasks": None})

    def test_each_filter_works_alone(self):
        self.assertEqual(self._get({"can_login": "false"})[1].call_args.kwargs, {"can_login": False, "can_add_tasks": None, "can_add_nonbillable_tasks": None})
        self.assertEqual(self._get({"can_add_tasks": "true"})[1].call_args.kwargs, {"can_login": None, "can_add_tasks": True, "can_add_nonbillable_tasks": None})

    def test_the_positional_arguments_are_unchanged(self):
        _, listed = self._get({"role": "hr", "search": "ann", "page": 2, "limit": 5, "can_login": "true"})
        self.assertEqual(listed.call_args.args[2:], ("ann", "hr", None, 2, 5))

    def test_anything_but_a_boolean_is_refused(self):
        for value in ("maybe", "allowed", "2"):
            with self.subTest(value=value):
                response, listed = self._get({"can_login": value})
                self.assertEqual(response.status_code, 422)
                listed.assert_not_called()

    def test_the_parameters_are_documented(self):
        params = {p["name"] for p in app.openapi()["paths"]["/api/v1/members"]["get"]["parameters"]}
        self.assertTrue({"can_login", "can_add_tasks", "can_add_nonbillable_tasks"} <= params)


if __name__ == "__main__":
    unittest.main()

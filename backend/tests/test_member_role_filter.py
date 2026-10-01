"""The Members directory's Role filter can pick Client.

Clients are listed in the directory, so the filter has to accept them -- but a
client is an external account, not a role the Add / Edit Member form may assign,
so `MemberRole` (what create and update accept) must stay without it.
"""
import unittest
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.database import get_db
from app.core.permissions import ROLE_PERMISSIONS
from app.core.security import get_current_user
from app.main import app
from app.models.user import User
from app.schemas.member import MemberCreate, MemberRole, MemberRoleFilter, MemberUpdate


def _admin() -> User:
    user = User()
    user.id = 1
    user.organization_id = 1
    user.role_name = "administrator"
    user.permissions = {p: True for p in ROLE_PERMISSIONS["administrator"]}
    user.is_active = True
    user.can_login = True
    return user


class RoleFilterTests(unittest.TestCase):
    def test_the_filter_is_every_assignable_role_plus_client(self):
        self.assertEqual({r.value for r in MemberRoleFilter}, {r.value for r in MemberRole} | {"client"})

    def test_client_is_still_not_an_assignable_role(self):
        self.assertNotIn("client", {r.value for r in MemberRole})
        with self.assertRaises(ValidationError):
            MemberUpdate(role="client")
        with self.assertRaises(ValidationError):
            MemberCreate(name="Cee Lient", email="c@example.com", role="client", status="active")


class ListRouteTests(unittest.TestCase):
    def setUp(self):
        app.dependency_overrides[get_db] = lambda: MagicMock()
        app.dependency_overrides[get_current_user] = _admin
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()

    def _list(self, role):
        empty = {"items": [], "page": 1, "limit": 20, "total": 0, "pages": 0}
        with patch("app.api.members.MemberService.list", return_value=empty) as listed:
            response = self.client.get("/api/v1/members", params={"role": role})
        return response, listed

    def test_the_list_accepts_client_and_passes_it_to_the_service(self):
        response, listed = self._list("client")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(listed.call_args[0][3], "client")

    def test_the_other_roles_still_work_and_junk_is_still_refused(self):
        for role in ("administrator", "hr", "leader", "employee"):
            with self.subTest(role=role):
                self.assertEqual(self._list(role)[0].status_code, 200)
        self.assertEqual(self._list("superhero")[0].status_code, 422)


if __name__ == "__main__":
    unittest.main()

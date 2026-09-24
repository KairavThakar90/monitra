"""The permission table backing the /WFPM API.

`wfpm:projects:create` is deliberately a separate, narrower permission from
`projects:create` (see app/core/permissions.py) -- granting the wide one to
`employee` would also let an employee create/update/delete projects through
`/api/v1/projects`, the Monitra frontend's own route, which was never asked
for. These tests pin that the two stay apart.
"""
import unittest

from app.core.permissions import ROLE_PERMISSIONS


class WfpmPermissionTableTests(unittest.TestCase):
    def test_employee_has_the_narrow_wfpm_permission_but_not_the_wide_one(self):
        self.assertIn("wfpm:projects:create", ROLE_PERMISSIONS["employee"])
        self.assertNotIn("projects:create", ROLE_PERMISSIONS["employee"])

    def test_every_signed_in_role_except_client_and_release_bot_can_use_it(self):
        for role, permissions in ROLE_PERMISSIONS.items():
            if role in {"client", "release_bot"}:
                continue
            with self.subTest(role=role):
                self.assertIn("wfpm:projects:create", permissions)

    def test_client_and_release_bot_cannot_create_wfpm_projects(self):
        self.assertNotIn("wfpm:projects:create", ROLE_PERMISSIONS["client"])
        self.assertNotIn("wfpm:projects:create", ROLE_PERMISSIONS["release_bot"])

    def test_the_project_leader_spelling_inherits_it_from_leader(self):
        # project_leader is `set(ROLE_PERMISSIONS["leader"])` -- pinned so a
        # future edit to `leader` alone cannot silently drop this from the
        # second spelling.
        self.assertIn("wfpm:projects:create", ROLE_PERMISSIONS["project_leader"])

    def test_the_other_four_wfpm_routes_reuse_permissions_employee_already_has(self):
        # list/get project and list/create task use projects:view, tasks:view,
        # tasks:create -- no new permission needed for those.
        for permission in ("projects:view", "tasks:view", "tasks:create"):
            with self.subTest(permission=permission):
                self.assertIn(permission, ROLE_PERMISSIONS["employee"])


if __name__ == "__main__":
    unittest.main()

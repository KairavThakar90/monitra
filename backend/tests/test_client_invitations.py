"""Client invitations: creation, single-use approve/reject, and the access
boundary the client portal rests on.

Runs against a real SQLite database, not a mocked session — the interesting
behaviour here is a database-enforced single-use claim (`mark_approved`/
`mark_rejected`'s conditional UPDATE) and a real WHERE-clause access check
(`ClientProjectRepository.exists`), neither of which a MagicMock can falsify.
"""
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import BigInteger, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session

from app.core.database import Base
from app.core.permissions import ROLE_PERMISSIONS
from app.models.client import Client
from app.models.client_invitation import ClientInvitation
from app.models.client_project import ClientProject
from app.models.manual_time_entry import ManualTimeEntry
from app.models.project import Project
from app.models.project_member import ProjectMember
from app.models.sso_handoff_token import SsoHandoffToken
from app.models.task import Task
from app.models.time_entry import TimeEntry
from app.models.time_entry_adjustment import TimeEntryAdjustment
from app.models.user import User
from app.services.client_invitation_service import ClientInvitationService
from app.services.client_portal_service import ClientPortalService


def _sqlite_schema(engine, *models):
    """Create these models' tables on SQLite (see test_task_isolation.py)."""
    tables = [model.__table__ for model in models]
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


@compiles(JSONB, "sqlite")
def _jsonb_is_json_on_sqlite(type_, compiler, **kw):
    return "JSON"


@compiles(BigInteger, "sqlite")
def _bigint_is_integer_on_sqlite(type_, compiler, **kw):
    return "INTEGER"


ORG = 1


class ClientInvitationCase(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        _sqlite_schema(
            self.engine, User, Project, ProjectMember, Task, TimeEntry,
            ManualTimeEntry, TimeEntryAdjustment, SsoHandoffToken,
            Client, ClientInvitation, ClientProject,
        )
        self.db = Session(self.engine)

        self.admin = User(
            id=1, organization_id=ORG, username="admin", email="admin@example.com",
            name="Admin", role_name="administrator",
            permissions={p: True for p in ROLE_PERMISSIONS["administrator"]},
            is_active=True, status="active", capture_frequency=10,
        )
        self.db.add(self.admin)

        self.project_a = Project(
            id=10, organization_id=ORG, project_name="Alpha", created_by=1,
        )
        self.project_b = Project(
            id=11, organization_id=ORG, project_name="Beta", created_by=1,
        )
        self.db.add_all([self.project_a, self.project_b])
        self.db.commit()

        # Every workflow call under test attempts to send mail; nothing here
        # exercises real SMTP.
        patcher = patch("app.services.email.workflows._send_immediately", return_value=True)
        self.mock_send = patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    # ------------------------------------------------------------- create

    def test_creating_an_invitation_provisions_an_inactive_client_user(self):
        client = ClientInvitationService.create_invitation(
            self.db, self.admin, "client@example.com", [self.project_a.id],
        )
        self.assertEqual(client.status, "pending")
        self.assertIsNotNone(client.user_id)

        user = self.db.get(User, client.user_id)
        self.assertEqual(user.role_name, "client")
        self.assertFalse(user.is_active)
        self.assertEqual(user.status, "pending")
        self.mock_send.assert_called_once()

    def test_an_invalid_project_id_is_refused(self):
        with self.assertRaises(HTTPException) as ctx:
            ClientInvitationService.create_invitation(
                self.db, self.admin, "client@example.com", [999999],
            )
        self.assertEqual(ctx.exception.status_code, 400)

    def test_project_access_is_exactly_what_was_selected(self):
        ClientInvitationService.create_invitation(
            self.db, self.admin, "client@example.com", [self.project_a.id],
        )
        client = self.db.query(Client).filter_by(email="client@example.com").one()
        from app.repositories.client_project import ClientProjectRepository
        self.assertTrue(ClientProjectRepository.exists(self.db, client.id, self.project_a.id))
        self.assertFalse(ClientProjectRepository.exists(self.db, client.id, self.project_b.id))

    def test_inviting_an_email_that_belongs_to_a_staff_account_is_refused(self):
        self.db.add(User(
            id=900, organization_id=ORG, username="staffer", email="staffer@example.com",
            name="Staffer", role_name="employee", permissions={}, is_active=True,
            status="active", capture_frequency=10,
        ))
        self.db.commit()
        with self.assertRaises(HTTPException) as ctx:
            ClientInvitationService.create_invitation(
                self.db, self.admin, "staffer@example.com", [self.project_a.id],
            )
        self.assertEqual(ctx.exception.status_code, 409)

    def test_inviting_an_email_whose_client_row_was_removed_relinks_the_existing_account(self):
        """A `client`-role user can end up with no `Client` row pointing at it
        -- its `Client`/`ClientInvitation` rows removed directly (test data
        cleanup, a manual fix) while the account itself was untouched.
        Re-inviting that email must repair the link, not refuse forever: a
        client-role account is already the smallest permission set this
        table defines, so reusing it is a repair, not a privilege escalation.
        """
        orphaned_user = User(
            id=901, organization_id=ORG, username="orphan", email="orphan@example.com",
            name="Orphan Client", role_name="client", permissions={"clients:view_shared": True},
            is_active=True, status="active", capture_frequency=10,
        )
        self.db.add(orphaned_user)
        self.db.commit()

        client = ClientInvitationService.create_invitation(
            self.db, self.admin, "orphan@example.com", [self.project_a.id],
        )
        self.assertEqual(client.user_id, orphaned_user.id)

    # -------------------------------------------------------- approve/reject

    def _invite(self):
        with patch("app.services.client_invitation_service.secrets.token_urlsafe", return_value="plaintext-token"):
            ClientInvitationService.create_invitation(
                self.db, self.admin, "client@example.com", [self.project_a.id],
            )
        return "plaintext-token"

    def test_approving_activates_the_client_and_the_token_cannot_be_reused(self):
        token = self._invite()

        handoff_token, expires_at = ClientInvitationService.approve_invitation(self.db, token)
        self.assertTrue(handoff_token)

        client = self.db.query(Client).filter_by(email="client@example.com").one()
        self.assertEqual(client.status, "active")
        user = self.db.get(User, client.user_id)
        self.assertTrue(user.is_active)
        self.assertEqual(user.status, "active")

        # A second click on the same link is refused, not re-approved.
        with self.assertRaises(HTTPException) as ctx:
            ClientInvitationService.approve_invitation(self.db, token)
        self.assertEqual(ctx.exception.status_code, 401)

    def test_rejecting_marks_the_invitation_and_leaves_the_account_inactive(self):
        token = self._invite()
        ClientInvitationService.reject_invitation(self.db, token)

        client = self.db.query(Client).filter_by(email="client@example.com").one()
        self.assertEqual(client.status, "rejected")
        user = self.db.get(User, client.user_id)
        self.assertFalse(user.is_active)

        with self.assertRaises(HTTPException):
            ClientInvitationService.reject_invitation(self.db, token)

    def test_an_unknown_token_is_refused(self):
        with self.assertRaises(HTTPException) as ctx:
            ClientInvitationService.approve_invitation(self.db, "not-a-real-token")
        self.assertEqual(ctx.exception.status_code, 401)

    # ------------------------------------------------------------ deactivate

    def test_deactivating_an_active_client_disables_the_account(self):
        token = self._invite()
        ClientInvitationService.approve_invitation(self.db, token)
        client = self.db.query(Client).filter_by(email="client@example.com").one()

        deactivated = ClientInvitationService.deactivate_client(self.db, self.admin, client.id)
        self.assertEqual(deactivated.status, "deactivated")

        user = self.db.get(User, client.user_id)
        self.assertFalse(user.is_active)

    def test_a_non_active_client_cannot_be_deactivated(self):
        self._invite()
        client = self.db.query(Client).filter_by(email="client@example.com").one()
        with self.assertRaises(HTTPException) as ctx:
            ClientInvitationService.deactivate_client(self.db, self.admin, client.id)
        self.assertEqual(ctx.exception.status_code, 409)

    def test_a_deactivated_client_can_be_resent_an_invitation(self):
        token = self._invite()
        ClientInvitationService.approve_invitation(self.db, token)
        client = self.db.query(Client).filter_by(email="client@example.com").one()
        ClientInvitationService.deactivate_client(self.db, self.admin, client.id)

        resent = ClientInvitationService.resend_invitation(self.db, self.admin, client.id)
        self.assertEqual(resent.status, "deactivated")
        self.assertEqual(self.mock_send.call_count, 2)


class ClientPortalAccessCase(unittest.TestCase):
    """The client portal's entire access boundary: a shared project is
    readable, an unshared one in the same organization is not."""

    def setUp(self):
        self.engine = create_engine("sqlite://")
        _sqlite_schema(
            self.engine, User, Project, ProjectMember, Task, TimeEntry,
            ManualTimeEntry, TimeEntryAdjustment, Client, ClientProject,
        )
        self.db = Session(self.engine)

        self.project_shared = Project(id=20, organization_id=ORG, project_name="Shared", created_by=1)
        self.project_hidden = Project(id=21, organization_id=ORG, project_name="Hidden", created_by=1)
        self.db.add_all([self.project_shared, self.project_hidden])

        self.client_user = User(
            id=50, organization_id=ORG, username="client1", email="client1@example.com",
            name="Client One", role_name="client",
            permissions={p: True for p in ROLE_PERMISSIONS["client"]},
            is_active=True, status="active", capture_frequency=10,
        )
        self.db.add(self.client_user)

        self.client_row = Client(
            id=1, organization_id=ORG, user_id=self.client_user.id, name="Client One",
            email="client1@example.com", status="active", invited_by=1,
        )
        self.db.add(self.client_row)
        self.db.add(ClientProject(client_id=self.client_row.id, project_id=self.project_shared.id))
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_a_shared_project_is_readable(self):
        detail = ClientPortalService.get_project_detail(self.db, self.client_user, self.project_shared.id)
        self.assertEqual(detail["id"], self.project_shared.id)
        self.assertEqual(detail["total_tracked_seconds"], 0)

    def test_a_project_in_the_same_org_but_not_shared_is_refused(self):
        with self.assertRaises(HTTPException) as ctx:
            ClientPortalService.get_project_detail(self.db, self.client_user, self.project_hidden.id)
        self.assertEqual(ctx.exception.status_code, 404)

    def test_list_my_projects_returns_only_shared_projects(self):
        result = ClientPortalService.list_my_projects(self.db, self.client_user)
        self.assertEqual([item["id"] for item in result["items"]], [self.project_shared.id])

    def test_member_and_task_hours_are_scoped_to_shared_projects(self):
        members = ClientPortalService.list_member_hours(self.db, self.client_user)
        tasks = ClientPortalService.list_task_hours(self.db, self.client_user)
        self.assertEqual(members["items"], [])
        self.assertEqual(tasks["items"], [])

    def test_a_pending_client_has_no_portal_access(self):
        self.client_row.status = "pending"
        self.db.commit()
        with self.assertRaises(HTTPException) as ctx:
            ClientPortalService.get_project_detail(self.db, self.client_user, self.project_shared.id)
        self.assertEqual(ctx.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()

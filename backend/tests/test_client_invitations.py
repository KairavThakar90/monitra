"""Client invitations: creation, single-use approve/reject, and the access
boundary the client portal rests on.

Runs against a real SQLite database, not a mocked session — the interesting
behaviour here is a database-enforced single-use claim (`mark_approved`/
`mark_rejected`'s conditional UPDATE) and a real WHERE-clause access check
(`ClientProjectRepository.exists`), neither of which a MagicMock can falsify.
"""
import unittest
from datetime import datetime, timezone
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
from app.models.refresh_token import RefreshToken
from app.models.sso_handoff_token import SsoHandoffToken
from app.models.task import Task
from app.models.time_entry import TimeEntry
from app.models.time_entry_activity import TimeEntryActivity
from app.models.time_entry_adjustment import TimeEntryAdjustment
from app.models.time_entry_screenshot import TimeEntryScreenshot
from app.models.user import User
from app.services.auth import AuthService
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
            ManualTimeEntry, TimeEntryAdjustment, TimeEntryActivity, Client, ClientProject,
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
        item = result["items"][0]
        # The export's project facts: created date always present; the
        # first-tracked date honestly None while nothing has been tracked.
        self.assertIsNotNone(item["created_date"])
        self.assertIsNone(item["first_tracked_date"])

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

    def test_a_project_filter_cannot_smuggle_in_an_unshared_project(self):
        """The project filter narrows the client's own shared set; it must
        never widen it. Asking for the hidden project (in the same org, but
        never shared with this client) returns nothing, not that project."""
        result = ClientPortalService.list_my_projects(
            self.db, self.client_user, project_ids=[self.project_hidden.id],
        )
        self.assertEqual(result["items"], [])

    def test_a_project_filter_matching_a_shared_project_narrows_to_it(self):
        result = ClientPortalService.list_my_projects(
            self.db, self.client_user, project_ids=[self.project_shared.id],
        )
        self.assertEqual([item["id"] for item in result["items"]], [self.project_shared.id])


class ClientLoginLinkCase(unittest.TestCase):
    """The client login screen's single email-only path: a real client gets a
    link, anyone else gets a plain "you are not a client" refusal rather than
    a generic non-committal response."""

    def setUp(self):
        self.engine = create_engine("sqlite://")
        _sqlite_schema(self.engine, User, SsoHandoffToken, RefreshToken)
        self.db = Session(self.engine)

        self.active_client = User(
            id=60, organization_id=ORG, username="client2", email="client2@example.com",
            name="Client Two", role_name="client",
            permissions={p: True for p in ROLE_PERMISSIONS["client"]},
            is_active=True, status="active", capture_frequency=10,
        )
        self.pending_client = User(
            id=61, organization_id=ORG, username="client3", email="client3@example.com",
            name="Client Three", role_name="client",
            permissions={p: True for p in ROLE_PERMISSIONS["client"]},
            is_active=False, status="pending", capture_frequency=10,
        )
        self.staff_user = User(
            id=62, organization_id=ORG, username="staffer2", email="staffer2@example.com",
            name="Staffer Two", role_name="employee", permissions={},
            is_active=True, status="active", capture_frequency=10,
        )
        self.db.add_all([self.active_client, self.pending_client, self.staff_user])
        self.db.commit()

        patcher = patch("app.services.email.workflows._send_immediately", return_value=True)
        self.mock_send = patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_an_active_client_receives_a_link(self):
        AuthService.request_client_login_link(self.db, "client2@example.com")
        self.mock_send.assert_called_once()

    def test_an_unknown_email_is_refused_explicitly(self):
        with self.assertRaises(HTTPException) as ctx:
            AuthService.request_client_login_link(self.db, "nobody@example.com")
        self.assertEqual(ctx.exception.status_code, 404)
        self.mock_send.assert_not_called()

    def test_a_pending_not_yet_approved_client_is_refused(self):
        with self.assertRaises(HTTPException) as ctx:
            AuthService.request_client_login_link(self.db, "client3@example.com")
        self.assertEqual(ctx.exception.status_code, 404)
        self.mock_send.assert_not_called()

    def test_a_staff_email_is_refused_not_treated_as_a_client(self):
        """The email field doubles as a client's whole credential, so it must
        never be accepted for an account that is not a client -- confirming
        someone's staff email exists here would be an odd kind of leak, and
        it would be wrong to email them a client sign-in link regardless."""
        with self.assertRaises(HTTPException) as ctx:
            AuthService.request_client_login_link(self.db, "staffer2@example.com")
        self.assertEqual(ctx.exception.status_code, 404)
        self.mock_send.assert_not_called()

    # ---------------------------------------------------- direct login

    def test_an_active_client_is_signed_in_immediately(self):
        pair = AuthService.client_direct_login(self.db, "client2@example.com")
        self.assertEqual(pair.user.id, self.active_client.id)
        self.assertTrue(pair.access_token)
        self.assertTrue(pair.refresh_token)

    def test_direct_login_refuses_a_pending_client(self):
        with self.assertRaises(HTTPException) as ctx:
            AuthService.client_direct_login(self.db, "client3@example.com")
        self.assertEqual(ctx.exception.status_code, 404)

    def test_direct_login_refuses_a_staff_email(self):
        with self.assertRaises(HTTPException) as ctx:
            AuthService.client_direct_login(self.db, "staffer2@example.com")
        self.assertEqual(ctx.exception.status_code, 404)

    def test_direct_login_refuses_an_unknown_email(self):
        with self.assertRaises(HTTPException) as ctx:
            AuthService.client_direct_login(self.db, "nobody@example.com")
        self.assertEqual(ctx.exception.status_code, 404)


class ClientPortalPermissionsCase(unittest.TestCase):
    """The four `share_*` flags: each gates exactly its own section, and
    nothing else is inferred from them (e.g. disabling Timing still shows
    task and member *names*, just no hours)."""

    def setUp(self):
        self.engine = create_engine("sqlite://")
        _sqlite_schema(
            self.engine, User, Project, ProjectMember, Task, TimeEntry,
            ManualTimeEntry, TimeEntryAdjustment, TimeEntryActivity, Client, ClientProject,
            TimeEntryScreenshot,
        )
        self.db = Session(self.engine)

        self.project = Project(id=30, organization_id=ORG, project_name="Permissioned", created_by=1)
        self.db.add(self.project)
        self.member = User(
            id=70, organization_id=ORG, username="member1", email="member1@example.com",
            name="Member One", role_name="employee", permissions={},
            is_active=True, status="active", capture_frequency=10,
        )
        self.db.add(self.member)
        self.task = Task(id=40, organization_id=ORG, project_id=self.project.id, task_name="Do the thing", created_by=1)
        self.db.add(self.task)
        self.db.commit()

        self.db.add(ProjectMember(organization_id=ORG, project_id=self.project.id, user_id=self.member.id, created_by=1))

        self.client_user = User(
            id=71, organization_id=ORG, username="client9", email="client9@example.com",
            name="Client Nine", role_name="client", permissions={"clients:view_shared": True},
            is_active=True, status="active", capture_frequency=10,
        )
        self.db.add(self.client_user)
        # Every flag off except screenshots, so each test below turns on
        # exactly the one it is testing rather than inheriting "everything".
        self.client_row = Client(
            id=9, organization_id=ORG, user_id=self.client_user.id, name="Client Nine",
            email="client9@example.com", status="active", invited_by=1,
            share_member_details=False, share_screenshots=True, share_tasks=False, share_timing=False,
            share_billing=False,
        )
        self.db.add(self.client_row)
        self.db.add(ClientProject(client_id=self.client_row.id, project_id=self.project.id))
        self.db.commit()

        entry = TimeEntry(
            id=100, organization_id=ORG, user_id=self.member.id, project_id=self.project.id,
            task_id=self.task.id, start_time=datetime.now(timezone.utc), end_time=datetime.now(timezone.utc),
            total_seconds=60, status="completed",
        )
        self.db.add(entry)
        self.db.commit()
        self.screenshot = TimeEntryScreenshot(
            id=1, organization_id=ORG, time_entry_id=entry.id, file_path="2026/x.webp",
            monitor_number=1, google_drive_file_id="drive-file-123", file_name="s.webp", mime_type="image/webp",
        )
        self.db.add(self.screenshot)
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_member_details_disabled_hides_the_project_detail_roster(self):
        detail = ClientPortalService.get_project_detail(self.db, self.client_user, self.project.id)
        self.assertEqual(detail["members"], [])
        self.assertFalse(detail["permissions"]["share_member_details"])

    def test_member_hours_endpoint_is_empty_when_disabled(self):
        result = ClientPortalService.list_member_hours(self.db, self.client_user)
        self.assertEqual(result["items"], [])

    def test_tasks_disabled_hides_the_project_detail_task_list(self):
        detail = ClientPortalService.get_project_detail(self.db, self.client_user, self.project.id)
        self.assertEqual(detail["tasks"], [])

    def test_task_hours_endpoint_is_empty_when_disabled(self):
        result = ClientPortalService.list_task_hours(self.db, self.client_user)
        self.assertEqual(result["items"], [])

    def test_timing_disabled_nulls_hours_not_the_whole_section(self):
        self.client_row.share_tasks = True
        self.client_row.share_member_details = True
        self.db.commit()
        detail = ClientPortalService.get_project_detail(self.db, self.client_user, self.project.id)
        self.assertIsNone(detail["total_tracked_seconds"])
        self.assertIsNone(detail["total_tracked_hours"])
        # Task and member names still show -- Timing hides hours, not identity.
        self.assertEqual(len(detail["tasks"]), 1)
        self.assertIsNone(detail["tasks"][0]["total_tracked_hours"])
        self.assertEqual(len(detail["members"]), 1)
        self.assertIsNone(detail["members"][0]["total_tracked_hours"])

    def test_member_hours_name_the_projects_worked_on(self):
        self.client_row.share_member_details = True
        self.client_row.share_timing = True
        self.db.commit()
        result = ClientPortalService.list_member_hours(self.db, self.client_user)
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["project_names"], ["Permissioned"])

    # ---------------------------------------------------------- timesheet

    def _postgres_functions(self):
        """The detailed-log query uses two Postgres functions SQLite lacks. Give
        the test connection stand-ins, so the real query runs rather than a
        patched one."""
        connection = self.engine.raw_connection().driver_connection
        connection.create_function("greatest", 2, lambda a, b: max(a, b))
        connection.create_function("concat", 2, lambda a, b: f"{a}{b}")
        # `organizations` has no ORM model (see MemberRepository.organization_name).
        connection.execute("CREATE TABLE IF NOT EXISTS organizations (id INTEGER PRIMARY KEY, name TEXT)")
        connection.execute(f"INSERT OR REPLACE INTO organizations (id, name) VALUES ({ORG}, 'Acme Co')")
        connection.commit()

    def test_timesheet_is_empty_when_timing_is_not_shared(self):
        """Nothing to report without timing, and no row is invented for it."""
        result = ClientPortalService.list_timesheet(self.db, self.client_user)
        self.assertEqual(result["items"], [])
        self.assertFalse(result["permissions"]["share_timing"])

    def test_timesheet_rows_carry_day_member_project_and_to_do(self):
        self._postgres_functions()
        self.client_row.share_timing = True
        self.client_row.share_member_details = True
        self.client_row.share_tasks = True
        self.db.commit()
        result = ClientPortalService.list_timesheet(self.db, self.client_user)
        self.assertEqual(len(result["items"]), 1)
        row = result["items"][0]
        self.assertEqual(row["member_name"], "Member One")
        self.assertEqual(row["project_name"], "Permissioned")
        self.assertEqual(row["task_name"], "Do the thing")
        self.assertEqual(row["tracked_seconds"], 60)
        self.assertEqual(result["organization"], "Acme Co")
        self.assertRegex(row["date"], r"^\d{4}-\d{2}-\d{2}$")

    def test_timesheet_names_nobody_and_nothing_that_was_not_shared(self):
        """Withheld, not fabricated: the member and the to-do are None, but the
        opaque member id stays so two people are not merged into one row."""
        self._postgres_functions()
        self.client_row.share_timing = True
        self.db.commit()
        row = ClientPortalService.list_timesheet(self.db, self.client_user)["items"][0]
        self.assertIsNone(row["member_name"])
        self.assertIsNone(row["task_name"])
        self.assertEqual(row["member_id"], self.member.id)
        self.assertEqual(row["project_name"], "Permissioned")

    def test_timesheet_filter_can_only_narrow_never_widen(self):
        self._postgres_functions()
        self.client_row.share_timing = True
        self.db.commit()
        # A project that is not this client's cannot be smuggled in.
        outside = ClientPortalService.list_timesheet(self.db, self.client_user, project_ids=[999999])
        self.assertEqual(outside["items"], [])
        # A member filter that excludes the only worker leaves nothing.
        other = ClientPortalService.list_timesheet(self.db, self.client_user, member_ids=[self.member.id + 1000])
        self.assertEqual(other["items"], [])

    # ------------------------------------------------------- task listing

    def test_task_listing_includes_untracked_tasks_with_details(self):
        """The client's task listing is the complete active set -- a task
        nobody worked on in range still appears, with its status, created
        date and (member details permitting) assignee."""
        self.client_row.share_tasks = True
        self.client_row.share_timing = True
        self.client_row.share_member_details = True
        self.task.assignee_id = self.member.id
        untracked = Task(
            id=41, organization_id=ORG, project_id=self.project.id,
            task_name="Not started", created_by=1,
        )
        self.db.add(untracked)
        self.db.commit()

        result = ClientPortalService.list_task_hours(self.db, self.client_user)
        by_name = {item["task_name"]: item for item in result["items"]}
        self.assertIn("Do the thing", by_name)
        self.assertIn("Not started", by_name)
        self.assertEqual(by_name["Do the thing"]["assignee"], "Member One")
        self.assertEqual(by_name["Do the thing"]["total_tracked_seconds"], 60)
        self.assertEqual(by_name["Not started"]["total_tracked_seconds"], 0)
        self.assertIsNotNone(by_name["Do the thing"]["created_date"])
        self.assertEqual(by_name["Do the thing"]["project_name"], "Permissioned")
        # Activity: the plumbing is present, and honestly None when the timer
        # recorded no samples -- never a fabricated percentage.
        self.assertIn("activity_percentage", by_name["Do the thing"])
        self.assertIsNone(by_name["Do the thing"]["activity_percentage"])

    def test_task_listing_withholds_assignee_when_member_details_disabled(self):
        self.client_row.share_tasks = True
        self.task.assignee_id = self.member.id
        self.db.commit()
        result = ClientPortalService.list_task_hours(self.db, self.client_user)
        self.assertIsNone(result["items"][0]["assignee"])

    # ------------------------------------------------------ member detail

    def test_member_detail_refused_when_member_details_disabled(self):
        with self.assertRaises(HTTPException) as ctx:
            ClientPortalService.get_member_detail(self.db, self.client_user, self.member.id)
        self.assertEqual(ctx.exception.status_code, 404)

    def test_member_detail_shows_date_wise_activity(self):
        self.client_row.share_member_details = True
        self.client_row.share_timing = True
        self.db.commit()
        detail = ClientPortalService.get_member_detail(self.db, self.client_user, self.member.id)
        self.assertEqual(detail["name"], "Member One")
        self.assertEqual(detail["total_tracked_seconds"], 60)
        self.assertEqual(detail["days_active"], 1)
        self.assertEqual(len(detail["days"]), 1)
        day = detail["days"][0]
        self.assertEqual(day["total_tracked_seconds"], 60)
        self.assertEqual(day["session_count"], 1)
        self.assertTrue(day["first_activity"])
        self.assertEqual([p["project_name"] for p in detail["projects"]], ["Permissioned"])

    def test_member_detail_withholds_days_when_timing_disabled(self):
        self.client_row.share_member_details = True
        self.db.commit()
        detail = ClientPortalService.get_member_detail(self.db, self.client_user, self.member.id)
        self.assertEqual(detail["name"], "Member One")
        self.assertEqual(detail["days"], [])
        self.assertIsNone(detail["total_tracked_seconds"])
        self.assertIsNone(detail["days_active"])

    def test_member_detail_refuses_a_member_outside_the_shared_projects(self):
        self.client_row.share_member_details = True
        outsider = User(
            id=72, organization_id=ORG, username="outsider", email="outsider@example.com",
            name="Outsider", role_name="employee", permissions={},
            is_active=True, status="active", capture_frequency=10,
        )
        self.db.add(outsider)
        self.db.commit()
        with self.assertRaises(HTTPException) as ctx:
            ClientPortalService.get_member_detail(self.db, self.client_user, outsider.id)
        self.assertEqual(ctx.exception.status_code, 404)

    # ------------------------------------------------------------- billing

    def test_billing_disabled_returns_empty_with_permissions_saying_so(self):
        result = ClientPortalService.list_billing(self.db, self.client_user)
        self.assertEqual(result["items"], [])
        self.assertFalse(result["permissions"]["share_billing"])

    def test_billing_excludes_free_projects(self):
        """A `free` (internal) project has no budget and no billable time --
        it must be absent, not shown with fabricated zeros."""
        self.client_row.share_billing = True
        self.project.billing_type = "free"
        self.db.commit()
        result = ClientPortalService.list_billing(self.db, self.client_user)
        self.assertEqual(result["items"], [])
        self.assertTrue(result["permissions"]["share_billing"])

    def test_billing_reports_budget_used_and_remaining_per_project_and_task(self):
        self.client_row.share_billing = True
        self.project.billing_type = "fixed"
        self.project.fixed_hours = 10
        self.task.estimated_hours = 2
        self.db.commit()

        result = ClientPortalService.list_billing(self.db, self.client_user)
        self.assertEqual(len(result["items"]), 1)
        project = result["items"][0]
        self.assertEqual(project["id"], self.project.id)
        self.assertEqual(project["billing_type"], "fixed")
        self.assertEqual(project["total_hours"], 10.0)
        # The 60-second entry from setUp: all-time used, measured against the budget.
        self.assertEqual(project["used_seconds"], 60)
        self.assertEqual(project["used_hours"], 0.02)
        self.assertEqual(project["remaining_hours"], 9.98)

        self.assertEqual(len(project["tasks"]), 1)
        task = project["tasks"][0]
        self.assertEqual(task["id"], self.task.id)
        self.assertEqual(task["total_hours"], 2.0)
        self.assertEqual(task["used_seconds"], 60)
        self.assertEqual(task["remaining_hours"], 1.98)
        # Member details are off in this fixture, so the who-worked-on-it
        # breakdown is withheld even though billing itself is granted.
        self.assertEqual(task["members"], [])

    def test_billing_breaks_each_task_down_by_member_when_member_details_shared(self):
        self.client_row.share_billing = True
        self.client_row.share_member_details = True
        self.project.billing_type = "fixed"
        self.project.fixed_hours = 10
        self.db.commit()

        result = ClientPortalService.list_billing(self.db, self.client_user)
        task = result["items"][0]["tasks"][0]
        self.assertEqual(len(task["members"]), 1)
        worker = task["members"][0]
        self.assertEqual(worker["id"], self.member.id)
        self.assertEqual(worker["name"], "Member One")
        self.assertEqual(worker["used_seconds"], 60)
        self.assertEqual(worker["used_hours"], 0.02)

    def test_a_task_without_an_estimate_shows_used_hours_but_no_remaining(self):
        """No budget on the task means no `total_hours` and no
        `remaining_hours` -- never a guessed allocation."""
        self.client_row.share_billing = True
        self.project.billing_type = "fixed"
        self.project.fixed_hours = 10
        self.db.commit()

        result = ClientPortalService.list_billing(self.db, self.client_user)
        task = result["items"][0]["tasks"][0]
        self.assertIsNone(task["total_hours"])
        self.assertIsNone(task["remaining_hours"])
        self.assertEqual(task["used_seconds"], 60)

    def test_billing_only_covers_shared_projects(self):
        """A billable project in the same org that was never shared with
        this client must not appear, budget or not."""
        self.client_row.share_billing = True
        self.db.add(Project(
            id=32, organization_id=ORG, project_name="Unshared Billable",
            created_by=1, billing_type="fixed", fixed_hours=50,
        ))
        self.db.commit()
        result = ClientPortalService.list_billing(self.db, self.client_user)
        self.assertEqual([item["id"] for item in result["items"]], [])

    def test_screenshots_are_listed_when_enabled(self):
        result = ClientPortalService.list_project_screenshots(self.db, self.client_user, self.project.id)
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["id"], self.screenshot.id)

    def test_screenshots_are_empty_when_disabled(self):
        self.client_row.share_screenshots = False
        self.db.commit()
        result = ClientPortalService.list_project_screenshots(self.db, self.client_user, self.project.id)
        self.assertEqual(result["items"], [])

    def test_screenshot_bytes_refused_when_disabled(self):
        self.client_row.share_screenshots = False
        self.db.commit()
        with self.assertRaises(HTTPException) as ctx:
            ClientPortalService.get_project_screenshot_bytes(
                self.db, self.client_user, self.project.id, self.screenshot.id,
            )
        self.assertEqual(ctx.exception.status_code, 404)

    def test_screenshot_bytes_stream_when_enabled(self):
        with patch(
            "app.services.client_portal_service.drive_service.download_file",
            return_value=b"fake-image-bytes",
        ):
            content, mime_type, file_name = ClientPortalService.get_project_screenshot_bytes(
                self.db, self.client_user, self.project.id, self.screenshot.id,
            )
        self.assertEqual(content, b"fake-image-bytes")
        self.assertEqual(mime_type, "image/webp")
        self.assertEqual(file_name, "s.webp")

    def test_a_screenshot_from_a_different_project_is_refused(self):
        """The screenshot belongs to a time entry tracked against a
        *different* project than the one in the URL -- refused even though
        the client has screenshot access and the id is real."""
        other_project = Project(id=31, organization_id=ORG, project_name="Other", created_by=1)
        self.db.add(other_project)
        self.db.add(ClientProject(client_id=self.client_row.id, project_id=other_project.id))
        self.db.commit()
        with self.assertRaises(HTTPException) as ctx:
            ClientPortalService.get_project_screenshot_bytes(
                self.db, self.client_user, other_project.id, self.screenshot.id,
            )
        self.assertEqual(ctx.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()

"""A leader's member directory includes the clients of the projects they lead.

An administrator shares a project with a client from the Clients screen
(``client_projects``). The leader of that project then finds the client in
their Members page beside their team. Three things are pinned here, against a
**real SQLite database** -- the rule is a join, and a mocked session would
return whatever list the test handed it whatever the join said:

* the directory is the team **plus** the clients linked, through a project the
  leader leads, by an administrator -- and nobody else's clients;
* that widening is the *directory's alone*. ``visible_member_ids`` -- whose
  recorded work a leader may read: screenshots, logs, timesheets, reports --
  is unchanged, so a client never appears in any of those;
* a leader reads their team's screenshots and logs, and no further.
"""
import unittest
from datetime import date, datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import BigInteger, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.core.permissions import ROLE_PERMISSIONS
from app.models.activity_log import ActivityLog
from app.models.client import Client
from app.models.client_project import ClientProject
from app.models.project import Project
from app.models.project_member import ProjectMember
from app.models.task import Task
from app.models.user import User
from app.services.activity_log import ActivityLogService
from app.services.member_scope import (
    may_view_in_directory, may_view_member, visible_directory_ids, visible_member_ids,
)
from app.services.member_service import MemberService
from app.services.time_entry_screenshot import TimeEntryScreenshotService


@compiles(JSONB, "sqlite")
def _jsonb_is_json_on_sqlite(type_, compiler, **kw):  # pragma: no cover - dialect shim
    return "JSON"


@compiles(BigInteger, "sqlite")
def _bigint_is_integer_on_sqlite(type_, compiler, **kw):  # pragma: no cover - dialect shim
    return "INTEGER"


ORG = 7
ADMIN, LEADER, OTHER_LEADER = 1, 3, 4
ALICE, BOB = 11, 12                      # Alice is on Linus's project; Bob is on nobody's
CLIENT_ACME, CLIENT_OTHER = 21, 22       # client *user* ids
APOLLO, GEMINI = 40, 41                  # Linus leads Apollo; Lena leads Gemini


def _database() -> Session:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    tables = [model.__table__ for model in
              (User, Project, ProjectMember, Task, Client, ClientProject, ActivityLog)]
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


def _add_user(db, user_id, role, name, *, active=True):
    user = User(
        id=user_id, organization_id=ORG, role_name=role, username=name.lower(),
        email=f"{name.lower()}@example.invalid", name=name, password_hash="x",
        permissions={p: True for p in ROLE_PERMISSIONS.get(role, set())},
        is_active=active, status="active" if active else "pending", capture_frequency=10,
    )
    db.add(user)
    return user


class LeaderDirectoryCase(unittest.TestCase):
    """Linus leads Apollo (Alice is on it); Lena leads Gemini. An administrator
    shared Apollo with the client Acme and Gemini with another client, and
    invited a third whose account was never created."""

    def setUp(self):
        self.db = db = _database()
        self.admin = _add_user(db, ADMIN, "administrator", "Grace")
        self.leader = _add_user(db, LEADER, "leader", "Linus")
        self.other_leader = _add_user(db, OTHER_LEADER, "leader", "Lena")
        self.alice = _add_user(db, ALICE, "employee", "Alice")
        self.bob = _add_user(db, BOB, "employee", "Bob")
        self.acme = _add_user(db, CLIENT_ACME, "client", "Acme")
        self.other_client = _add_user(db, CLIENT_OTHER, "client", "Globex")

        db.add(Project(id=APOLLO, organization_id=ORG, project_name="Apollo",
                       status="active", leader_id=LEADER, created_by=ADMIN))
        db.add(Project(id=GEMINI, organization_id=ORG, project_name="Gemini",
                       status="active", leader_id=OTHER_LEADER, created_by=ADMIN))
        db.add(ProjectMember(organization_id=ORG, project_id=APOLLO, user_id=ALICE,
                             created_by=ADMIN, joined_at=date(2026, 9, 1)))

        db.add(Client(id=1, organization_id=ORG, user_id=CLIENT_ACME, name="Acme",
                      email="acme@example.invalid", status="active", invited_by=ADMIN))
        db.add(Client(id=2, organization_id=ORG, user_id=CLIENT_OTHER, name="Globex",
                      email="globex@example.invalid", status="active", invited_by=ADMIN))
        # Invited, never approved: there is no account behind this one.
        db.add(Client(id=3, organization_id=ORG, user_id=None, name="Pending Co",
                      email="pending@example.invalid", status="pending", invited_by=ADMIN))
        db.add(ClientProject(client_id=1, project_id=APOLLO))
        db.add(ClientProject(client_id=2, project_id=GEMINI))
        db.add(ClientProject(client_id=3, project_id=APOLLO))
        db.commit()

    def tearDown(self):
        self.db.close()


class DirectoryScopeTests(LeaderDirectoryCase):

    def test_a_leaders_directory_is_their_team_and_the_clients_of_their_projects(self):
        self.assertEqual(visible_directory_ids(self.db, self.leader), {LEADER, ALICE, CLIENT_ACME})

    def test_the_other_leader_gets_their_own_client_not_this_one(self):
        self.assertEqual(visible_directory_ids(self.db, self.other_leader), {OTHER_LEADER, CLIENT_OTHER})

    def test_an_administrator_and_hr_are_unrestricted(self):
        self.assertIsNone(visible_directory_ids(self.db, self.admin))
        hr = _add_user(self.db, 2, "hr", "Hana")
        self.assertIsNone(visible_directory_ids(self.db, hr))

    def test_unsharing_the_project_removes_the_client_from_the_directory(self):
        """The link is the administrator's to make and to take away."""
        self.db.query(ClientProject).filter(ClientProject.client_id == 1).delete()
        self.db.commit()
        self.assertEqual(visible_directory_ids(self.db, self.leader), {LEADER, ALICE})

    def test_handing_the_project_to_another_leader_moves_the_client_with_it(self):
        self.db.get(Project, APOLLO).leader_id = OTHER_LEADER
        self.db.commit()
        self.assertEqual(visible_directory_ids(self.db, self.leader), {LEADER})
        self.assertIn(CLIENT_ACME, visible_directory_ids(self.db, self.other_leader))

    def test_no_session_narrows_rather_than_widens(self):
        self.assertEqual(visible_directory_ids(None, self.leader), {LEADER})

    def test_membership_checks_follow_the_directory(self):
        self.assertTrue(may_view_in_directory(self.db, self.leader, CLIENT_ACME))
        self.assertFalse(may_view_in_directory(self.db, self.leader, CLIENT_OTHER))
        self.assertFalse(may_view_in_directory(self.db, self.leader, BOB))
        self.assertTrue(may_view_in_directory(self.db, self.admin, CLIENT_OTHER))


class MembersPageTests(LeaderDirectoryCase):
    """What `GET /members` and `GET /members/{id}` answer."""

    def _listed(self, user, **kwargs):
        page = MemberService.list(self.db, user, kwargs.get("search"), kwargs.get("role"), None, 1, 50)
        return [(member.name, member.role_name) for member in page["items"]]

    def test_the_leaders_members_page_lists_their_team_and_their_client(self):
        self.assertEqual(
            self._listed(self.leader),
            [("Acme", "client"), ("Alice", "employee"), ("Linus", "leader")],
        )

    def test_the_client_can_be_found_by_search(self):
        self.assertEqual(self._listed(self.leader, search="acme"), [("Acme", "client")])
        # Somebody else's client is not found, however it is searched for.
        self.assertEqual(self._listed(self.leader, search="globex"), [])

    def test_an_administrator_still_sees_everyone(self):
        self.assertEqual(len(self._listed(self.admin)), 7)

    def test_the_leader_can_open_their_client_and_nobody_elses(self):
        self.assertEqual(MemberService.get(self.db, self.leader, CLIENT_ACME).name, "Acme")
        for outside in (CLIENT_OTHER, BOB):
            with self.assertRaises(HTTPException) as refused:
                MemberService.get(self.db, self.leader, outside)
            # Missing, not forbidden: the existence of somebody else's client
            # is not this leader's to learn.
            self.assertEqual(refused.exception.status_code, 404)

    def test_a_leader_still_cannot_change_the_directory(self):
        """Seeing a client is not managing one: every write route is gated on
        `manage_employees`, which a leader does not hold."""
        for role in ("leader", "project_leader"):
            self.assertNotIn("manage_employees", ROLE_PERMISSIONS[role])
            self.assertNotIn("clients:manage", ROLE_PERMISSIONS[role])


class RecordedWorkScopeTests(LeaderDirectoryCase):
    """The directory widened. Nothing that reads recorded work did."""

    def test_the_team_scope_is_the_team_and_holds_no_client(self):
        self.assertEqual(visible_member_ids(self.db, self.leader), {LEADER, ALICE})
        self.assertFalse(may_view_member(self.db, self.leader, CLIENT_ACME))

    def test_a_leader_reads_their_teams_screenshots_and_no_further(self):
        resolve = TimeEntryScreenshotService._resolve_subject
        self.assertEqual(resolve(self.db, self.leader, ALICE), ALICE)     # on the team
        self.assertEqual(resolve(self.db, self.leader, None), LEADER)     # the Own view
        for outside in (BOB, CLIENT_ACME, OTHER_LEADER):
            with self.assertRaises(HTTPException) as refused:
                resolve(self.db, self.leader, outside)
            self.assertEqual(refused.exception.status_code, 403)

    def test_a_persons_recorded_day_is_opened_through_the_team_not_the_directory(self):
        """The Members page can open the client's profile; nothing that reads a
        tracked day can be pointed at them."""
        self.assertEqual(MemberService.get_team_member(self.db, self.leader, ALICE).name, "Alice")
        for outside in (CLIENT_ACME, BOB):
            with self.assertRaises(HTTPException) as refused:
                MemberService.get_team_member(self.db, self.leader, outside)
            self.assertEqual(refused.exception.status_code, 404)

    def test_a_leader_may_see_screenshots_and_may_not_delete_them(self):
        for role in ("leader", "project_leader"):
            self.assertIn("view_employees", ROLE_PERMISSIONS[role])
            self.assertNotIn("screenshots:delete", ROLE_PERMISSIONS[role])
        for role in ("administrator", "hr"):
            self.assertIn("screenshots:delete", ROLE_PERMISSIONS[role])

    def test_a_leaders_logs_are_their_employees_and_their_own(self):
        now = datetime.now(timezone.utc)
        for user_id, description in (
            (ALICE, "Started the timer on Apollo"),
            (LEADER, "Signed in"),
            (BOB, "Signed in"),                 # off the team
            (CLIENT_ACME, "Signed in"),         # in the directory, not on the team
        ):
            self.db.add(ActivityLog(organization_id=ORG, user_id=user_id, module="auth", action="login",
                                    description=description, created_at=now - timedelta(minutes=5)))
        self.db.commit()

        leaders = ActivityLogService.list_grouped(self.db, self.leader)
        self.assertEqual([group["name"] for group in leaders["members"]], ["Alice", "Linus"])
        # Asking for the client by id learns nothing either.
        self.assertEqual(ActivityLogService.list_grouped(self.db, self.leader, member_id=CLIENT_ACME)["total"], 0)
        # An administrator sees all four.
        self.assertEqual(len(ActivityLogService.list_grouped(self.db, self.admin)["members"]), 4)


if __name__ == "__main__":
    unittest.main()

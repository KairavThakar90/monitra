"""
Project Owner: a project-level relationship, not a global role.

`projects.owner_id` names the member responsible for one project. Who may be
named is the per-member capability `users.can_own_projects`, decided in one
place (`app/services/project_ownership.py`) and read by both the Owner picker
and the create/update validation -- so the picker is a convenience, never the
authorization boundary, and a hand-built request naming anybody else is
refused whatever the client showed.

The rules pinned here:

* A new project made through `/api/v1/projects` must name an eligible owner:
  a member of this organization, active, holding `can_own_projects`.
* WFPM-created projects start without one; that integration has no notion of
  an owner and inventing one would record a decision nobody made.
* Projects that predate owners keep `owner_id = NULL` and stay fully editable.
* Only a *change* of owner is validated, and a leader may not make one.
* Being an owner grants nothing, and no role called "owner" exists.
"""
import importlib.util
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.database import get_db
from app.core.permissions import ROLE_PERMISSIONS
from app.core.security import get_current_user
from app.main import app
from app.models.project import Project
from app.models.user import User
from app.schemas.member import MemberResponse, MemberRole, MemberUpdate
from app.schemas.project_management import BillingType, ProjectCreate, ProjectUpdate
from app.services import project_ownership
from app.services.project_management import ProjectManagementService
from tests.status_catalog_stub import rows, status_catalog

ORG_ID = 1
ACTOR_ID = 1
LEADER_ID = 2
OWNER_ID = 30
OTHER_OWNER_ID = 31


def _person(user_id, role="administrator", **extra):
    return User(id=user_id, organization_id=ORG_ID, role_name=role, permissions={},
                name=f"User {user_id}", email=f"user{user_id}@example.invalid", **extra)


def _admin():
    return _person(ACTOR_ID)


def _leader():
    return _person(LEADER_ID, role="leader")


def _owner(user_id=OWNER_ID, **overrides):
    values = {"is_active": True, "can_own_projects": True, **overrides}
    return _person(user_id, **values)


def _payload(**overrides):
    values = {
        "project_name": "Apollo", "status_id": 1, "owner_id": OWNER_ID, "leader_id": LEADER_ID,
        "employee_ids": [], "deadline": date.today() + timedelta(days=30),
        "billing_type": BillingType.free,
    }
    values.update(overrides)
    return ProjectCreate(**values)


def _create(payload, *, owner_lookup=None, actor=None, owner_required=True):
    """Run `create` against a scripted session.

    `owner_lookup` is what `resolve_owner`'s single `db.scalar` read finds.
    The `scalars` script is the leader lookup, then the membership read-back;
    everything after that is the payload read-back and finds nothing.
    """
    db = MagicMock()
    db.scalar.return_value = owner_lookup
    answers = [[_person(LEADER_ID, role="leader")], []]
    db.scalars.return_value.all.side_effect = lambda: answers.pop(0) if answers else []
    db.get.side_effect = lambda model, user_id: owner_lookup if owner_lookup is not None and user_id == owner_lookup.id else None
    with status_catalog(project_statuses=rows((1, "Active")), task_statuses=rows((1, "Todo"))):
        result = ProjectManagementService.create(db, actor or _admin(), payload, owner_required=owner_required)
    added = [call.args[0] for call in db.add.call_args_list]
    project = next(item for item in added if isinstance(item, Project))
    return project, result, db


def _existing_project(owner_id=None):
    return Project(id=11, organization_id=ORG_ID, project_name="Legacy", status="active", status_id=1,
                   leader_id=LEADER_ID, owner_id=owner_id, billing_type="free", fixed_hours=None,
                   deadline=None)


def _update(project, payload, *, actor=None, lookups=()):
    """Run `update` against a scripted session.

    `db.scalar` answers `_project`'s read first, then each of `lookups` --
    which is `resolve_owner`'s read when the owner is being changed. Field
    validation is patched: it is `_validate_project_fields`, covered on its
    own, and the owner rules must hold whatever it decides.
    """
    db = MagicMock()
    db.scalar.side_effect = [project, *lookups]
    db.scalars.return_value.all.return_value = []
    status_row = SimpleNamespace(id=1, name="Active")
    with patch.object(ProjectManagementService, "_validate_project_fields",
                      return_value=(status_row, _person(LEADER_ID, role="leader"), [])), \
         patch("app.services.project_management.may_view_project", return_value=True), \
         patch.object(ProjectManagementService, "_detail_payload", return_value={"id": project.id}):
        ProjectManagementService.update(db, actor or _admin(), project.id, payload)
    return db


class CreateOwnerTests(unittest.TestCase):
    def test_a_valid_owner_is_stored_and_returned(self):
        """A. The row carries the owner's id and the reply names them."""
        project, result, _ = _create(_payload(), owner_lookup=_owner())
        self.assertEqual(project.owner_id, OWNER_ID)
        self.assertEqual(result["owner"]["id"], OWNER_ID)
        self.assertEqual(set(result["owner"]), {"id", "name", "email", "role"},
                         "owner must be shaped exactly like leader")

    def test_an_owner_outside_the_organization_is_refused(self):
        """B. An id the organization does not have -- nonexistent, or another org's."""
        with self.assertRaises(HTTPException) as ctx:
            _create(_payload(owner_id=987654), owner_lookup=None)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(ctx.exception.detail, "Selected owner does not belong to this organization.")

    def test_a_member_without_the_capability_is_refused(self):
        """C. A real member who was never made eligible."""
        with self.assertRaises(HTTPException) as ctx:
            _create(_payload(), owner_lookup=_owner(can_own_projects=False))
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(ctx.exception.detail, "Selected owner is not eligible to own projects.")

    def test_a_deactivated_owner_is_refused_even_with_the_capability(self):
        with self.assertRaises(HTTPException) as ctx:
            _create(_payload(), owner_lookup=_owner(is_active=False))
        self.assertEqual(ctx.exception.detail, "Selected owner is not eligible to own projects.")

    def test_a_missing_owner_is_refused_with_a_message(self):
        """D. Not a bare 422 -- the drawer shows this text as-is."""
        with self.assertRaises(HTTPException) as ctx:
            _create(_payload(owner_id=None), owner_lookup=None)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(ctx.exception.detail, "Project owner is required.")

    def test_nothing_is_written_when_the_owner_is_refused(self):
        db = MagicMock()
        db.scalar.return_value = _owner(can_own_projects=False)
        with self.assertRaises(HTTPException):
            ProjectManagementService.create(db, _admin(), _payload())
        db.add.assert_not_called()
        db.commit.assert_not_called()

    def test_the_wfpm_path_creates_a_project_without_an_owner(self):
        """WFPM opts out explicitly; the project starts unowned, like a legacy one."""
        project, result, _ = _create(_payload(owner_id=None), owner_lookup=None, owner_required=False)
        self.assertIsNone(project.owner_id)
        self.assertIsNone(result["owner"])

    def test_the_owner_and_leader_are_independent(self):
        """I. The leader rules are untouched: a leader creating a project still
        leads it, and naming an owner does not change who leads."""
        project, result, _ = _create(_payload(), owner_lookup=_owner(), actor=_leader())
        self.assertEqual(project.leader_id, LEADER_ID)
        self.assertEqual(project.owner_id, OWNER_ID)

    def test_the_owner_id_must_be_a_positive_id(self):
        with self.assertRaises(ValidationError):
            _payload(owner_id=0)


class UpdateOwnerTests(unittest.TestCase):
    def test_an_admin_changes_the_owner(self):
        """E."""
        project = _existing_project(owner_id=OWNER_ID)
        _update(project, ProjectUpdate(owner_id=OTHER_OWNER_ID), lookups=[_owner(OTHER_OWNER_ID)])
        self.assertEqual(project.owner_id, OTHER_OWNER_ID)

    def test_an_admin_assigns_an_owner_to_a_legacy_project(self):
        project = _existing_project(owner_id=None)
        _update(project, ProjectUpdate(owner_id=OWNER_ID), lookups=[_owner()])
        self.assertEqual(project.owner_id, OWNER_ID)

    def test_changing_to_an_ineligible_owner_is_refused_and_nothing_is_saved(self):
        project = _existing_project(owner_id=OWNER_ID)
        with self.assertRaises(HTTPException) as ctx:
            _update(project, ProjectUpdate(owner_id=OTHER_OWNER_ID),
                    lookups=[_owner(OTHER_OWNER_ID, can_own_projects=False)])
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(project.owner_id, OWNER_ID)

    def test_changing_to_an_unknown_owner_is_refused(self):
        project = _existing_project(owner_id=OWNER_ID)
        with self.assertRaises(HTTPException) as ctx:
            _update(project, ProjectUpdate(owner_id=987654), lookups=[None])
        self.assertEqual(ctx.exception.detail, "Selected owner does not belong to this organization.")

    def test_the_owner_cannot_be_cleared(self):
        project = _existing_project(owner_id=OWNER_ID)
        with self.assertRaises(HTTPException) as ctx:
            _update(project, ProjectUpdate(owner_id=None))
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(project.owner_id, OWNER_ID)

    def test_a_leader_cannot_change_the_owner(self):
        """K. Refused, not silently dropped -- the caller is told."""
        project = _existing_project(owner_id=OWNER_ID)
        with self.assertRaises(HTTPException) as ctx:
            _update(project, ProjectUpdate(owner_id=OTHER_OWNER_ID), actor=_leader(),
                    lookups=[_owner(OTHER_OWNER_ID)])
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertEqual(project.owner_id, OWNER_ID)

    def test_a_leader_resending_the_current_owner_is_not_a_change(self):
        """The edit form sends every field; an unchanged owner must not 403."""
        project = _existing_project(owner_id=OWNER_ID)
        db = _update(project, ProjectUpdate(owner_id=OWNER_ID, project_name="Renamed"), actor=_leader())
        self.assertEqual(project.project_name, "Renamed")
        db.commit.assert_called_once()

    def test_an_unchanged_owner_who_lost_eligibility_does_not_block_edits(self):
        """Only a change is validated: no eligibility read happens at all."""
        project = _existing_project(owner_id=OWNER_ID)
        db = _update(project, ProjectUpdate(owner_id=OWNER_ID, project_name="Renamed"))
        self.assertEqual(db.scalar.call_count, 1, "only _project's read; resolve_owner was not called")
        self.assertEqual(project.project_name, "Renamed")

    def test_a_legacy_project_without_an_owner_stays_editable(self):
        """H. Editing anything else leaves a NULL owner NULL, and is not refused."""
        project = _existing_project(owner_id=None)
        db = _update(project, ProjectUpdate(project_name="Renamed", status_id=1))
        self.assertIsNone(project.owner_id)
        db.commit.assert_called_once()

    def test_an_explicit_null_on_a_legacy_project_is_a_no_op(self):
        project = _existing_project(owner_id=None)
        db = _update(project, ProjectUpdate(owner_id=None, project_name="Renamed"))
        self.assertIsNone(project.owner_id)
        db.commit.assert_called_once()

    def test_the_leader_reassignment_rules_are_unchanged(self):
        """I. An admin still reassigns the leader in the same request."""
        project = _existing_project(owner_id=OWNER_ID)
        with patch.object(ProjectManagementService, "_validate_project_fields",
                          side_effect=HTTPException(418, "stop")) as validated:
            db = MagicMock()
            db.scalar.side_effect = [project, _owner(OTHER_OWNER_ID)]
            db.scalars.return_value.all.return_value = []
            with patch("app.services.project_management.may_view_project", return_value=True):
                with self.assertRaises(HTTPException):
                    ProjectManagementService.update(db, _admin(), 11, ProjectUpdate(leader_id=77, owner_id=OTHER_OWNER_ID))
        self.assertEqual(validated.call_args.args[3], 77)


class ReadOwnerTests(unittest.TestCase):
    def test_a_single_project_names_its_owner(self):
        """F."""
        db = MagicMock()
        owner = _owner()
        db.get.side_effect = lambda model, user_id: owner if user_id == OWNER_ID else None
        db.scalars.return_value.all.return_value = []
        with status_catalog(project_statuses=rows((1, "Active")), task_statuses=rows((1, "Todo"))):
            payload = ProjectManagementService._detail_payload(db, _existing_project(owner_id=OWNER_ID), _admin())
        self.assertEqual(payload["owner"], {"id": OWNER_ID, "name": owner.name, "email": owner.email, "role": "administrator"})

    def test_a_legacy_project_reads_back_with_no_owner(self):
        """H."""
        db = MagicMock()
        db.get.return_value = None
        db.scalars.return_value.all.return_value = []
        with status_catalog(project_statuses=rows((1, "Active")), task_statuses=rows((1, "Todo"))):
            payload = ProjectManagementService._detail_payload(db, _existing_project(owner_id=None), _admin())
        self.assertIsNone(payload["owner"])

    def test_the_list_names_each_projects_owner_from_the_same_user_read(self):
        """G. No extra query per project: owners join the one users read."""
        owned = _existing_project(owner_id=OWNER_ID)
        legacy = Project(id=12, organization_id=ORG_ID, project_name="Old", status="active", status_id=1,
                         leader_id=LEADER_ID, owner_id=None, billing_type="free")
        db = MagicMock()
        # memberships, then the one users read (leaders + owners + assignees).
        answers = [[], [_owner(), _person(LEADER_ID, role="leader")]]
        db.scalars.return_value.all.side_effect = lambda: answers.pop(0) if answers else []
        db.execute.return_value.all.return_value = []
        with status_catalog(project_statuses=rows((1, "Active")), task_statuses=rows((1, "Todo"))):
            payloads = ProjectManagementService._detail_payloads(db, [owned, legacy], _admin(), include_tasks=False)
        self.assertEqual(payloads[0]["owner"]["id"], OWNER_ID)
        self.assertEqual(payloads[0]["leader"]["id"], LEADER_ID)
        self.assertIsNone(payloads[1]["owner"])
        self.assertEqual(db.scalars.call_count, 2)


class EligibilityTests(unittest.TestCase):
    def test_the_condition_is_organization_active_and_capability(self):
        sql = str(project_ownership.eligible_owner_condition(ORG_ID).compile(compile_kwargs={"literal_binds": True}))
        self.assertIn("users.organization_id = 1", sql)
        self.assertIn("users.is_active IS true", sql)
        self.assertIn("users.can_own_projects IS true", sql)

    def test_the_picker_and_the_validation_share_one_rule(self):
        """The picker's query is built from the same condition `resolve_owner`
        enforces; neither mentions a role."""
        db = MagicMock()
        db.scalars.return_value.all.return_value = []
        project_ownership.assignable_owners(db, _admin())
        sql = str(db.scalars.call_args.args[0].compile(compile_kwargs={"literal_binds": True}))
        where = sql.split("WHERE", 1)[1]
        self.assertIn("users.can_own_projects IS true", where)
        self.assertNotIn("role_name", where)


def _project_read(owner=None):
    now = datetime.now(timezone.utc).isoformat()
    return {"id": 1, "project_name": "X", "description": None, "status": None, "owner": owner,
            "leader": None, "employees": [], "deadline": "2099-01-01", "billing_type": "free",
            "fixed_hours": None, "organization_id": 1, "created_at": now, "updated_at": now, "tasks": []}


class RouteTests(unittest.TestCase):
    """Through the real router, so a missing permission gate would show."""

    def setUp(self):
        app.dependency_overrides[get_db] = lambda: None
        self.addCleanup(app.dependency_overrides.clear)
        self.client = TestClient(app)

    def _as(self, role, user_id=501):
        user = User(id=user_id, organization_id=ORG_ID, role_name=role, is_active=True,
                    permissions={name: True for name in ROLE_PERMISSIONS.get(role, ())})
        app.dependency_overrides[get_current_user] = lambda: user

    def test_the_owner_picker_lists_what_the_eligibility_rule_returns(self):
        self._as("administrator")
        with patch.object(project_ownership, "assignable_owners",
                          return_value=[_owner(OWNER_ID), _owner(OTHER_OWNER_ID)]) as listed:
            response = self.client.get("/api/v1/projects/assignable-owners")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual([item["id"] for item in response.json()], [OWNER_ID, OTHER_OWNER_ID])
        self.assertEqual(set(response.json()[0]), {"id", "name", "email", "role"})
        listed.assert_called_once()

    def test_the_owner_picker_is_not_shadowed_by_the_project_detail_route(self):
        self._as("administrator")
        with patch.object(project_ownership, "assignable_owners", return_value=[]):
            response = self.client.get("/api/v1/projects/assignable-owners")
        self.assertEqual(response.status_code, 200, response.text)

    def test_roles_that_cannot_edit_projects_cannot_list_owners(self):
        for role in ("employee", "hr", "client"):
            with self.subTest(role=role):
                self._as(role)
                response = self.client.get("/api/v1/projects/assignable-owners")
                self.assertEqual(response.status_code, 403)

    def test_roles_that_cannot_edit_projects_cannot_set_an_owner(self):
        """K. The permission gate refuses before the service is reached."""
        for role in ("employee", "hr"):
            with self.subTest(role=role):
                self._as(role)
                with patch.object(ProjectManagementService, "update") as updated, \
                     patch.object(ProjectManagementService, "create") as created:
                    patched = self.client.patch("/api/v1/projects/11", json={"owner_id": OWNER_ID})
                    posted = self.client.post("/api/v1/projects", json={
                        "project_name": "X", "status_id": 1, "owner_id": OWNER_ID, "leader_id": 2,
                        "deadline": "2099-01-01", "billing_type": "free",
                    })
                self.assertEqual(patched.status_code, 403)
                self.assertEqual(posted.status_code, 403)
                updated.assert_not_called()
                created.assert_not_called()

    def test_the_create_route_hands_the_owner_to_the_service(self):
        self._as("administrator")
        reply = _project_read({"id": OWNER_ID, "name": "O", "email": "o@example.invalid", "role": "administrator"})
        with patch.object(ProjectManagementService, "create", return_value=reply) as created:
            response = self.client.post("/api/v1/projects", json={
                "project_name": "X", "status_id": 1, "owner_id": OWNER_ID, "leader_id": 2,
                "deadline": "2099-01-01", "billing_type": "free",
            })
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(created.call_args.args[2].owner_id, OWNER_ID)
        self.assertEqual(len(created.call_args.args), 3, "the API route must not opt out of the owner rule")
        self.assertNotIn("owner_required", created.call_args.kwargs)
        self.assertEqual(response.json()["owner"]["id"], OWNER_ID)

    def test_the_wfpm_route_opts_out_of_the_owner_rule(self):
        self._as("administrator")
        with patch.object(ProjectManagementService, "default_project_status",
                          return_value=SimpleNamespace(id=1)), \
             patch.object(ProjectManagementService, "create", return_value=_project_read()) as created:
            self.client.post("/WFPM/projects", json={
                "project_name": "From WFPM", "deadline": "2099-01-01", "billing_type": "free",
            })
        self.assertIs(created.call_args.kwargs.get("owner_required"), False)
        self.assertIsNone(created.call_args.args[2].owner_id)


class NoOwnerRoleTests(unittest.TestCase):
    """L. Owner is a relationship, so no role or permission was added for it."""

    def test_there_is_no_owner_role(self):
        self.assertFalse([role for role in ROLE_PERMISSIONS if "owner" in role])
        self.assertNotIn("owner", [role.value for role in MemberRole])

    def test_no_permission_mentions_owners(self):
        every = set().union(*ROLE_PERMISSIONS.values())
        self.assertFalse([name for name in every if "owner" in name])

    def test_administrator_permissions_are_unchanged(self):
        self.assertEqual(ROLE_PERMISSIONS["administrator"], {
            "projects:create", "projects:update", "projects:delete", "projects:view",
            "tasks:create", "tasks:update", "tasks:delete", "tasks:view",
            "project_members:manage", "task_assignees:manage", "time_entries:manage_own",
            "time_entries:view_all", "manual_time_entries:approve",
            "manual_time_entries:create_for_others", "view_employees", "manage_employees",
            "screenshots:delete", "manage_desktop_releases", "clients:manage",
            "wfpm:projects:create",
        })


class MemberSwitchTests(unittest.TestCase):
    def test_an_administrator_can_grant_and_withdraw_eligibility(self):
        self.assertIs(MemberUpdate(can_own_projects=True).can_own_projects, True)
        self.assertEqual(MemberUpdate(can_own_projects=False).model_dump(exclude_unset=True), {"can_own_projects": False})

    def test_omitting_the_switch_leaves_it_alone(self):
        self.assertNotIn("can_own_projects", MemberUpdate(name="Someone").model_dump(exclude_unset=True))

    def test_an_explicit_null_is_refused_rather_than_reaching_a_not_null_column(self):
        with self.assertRaises(ValidationError):
            MemberUpdate(can_own_projects=None)

    def test_a_member_is_not_eligible_unless_granted(self):
        now = datetime.now(timezone.utc)
        member = SimpleNamespace(id=5, name="N", email="n@example.invalid", role_name="employee",
                                 status="active", date_of_joining=None, date_of_birth=None,
                                 designation=None, idle_enabled=True, idle_minutes=5,
                                 capture_frequency=10, can_add_tasks=True, can_own_projects=None,
                                 created_at=now, updated_at=now)
        self.assertIs(MemberResponse.model_validate(member).can_own_projects, False)


class MigrationShapeTests(unittest.TestCase):
    """M/N are exercised for real against the development database (upgrade,
    downgrade, re-upgrade); this pins what the file promises."""

    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "7c65a7896bab_add_project_owner.py"
        spec = importlib.util.spec_from_file_location("_project_owner_migration", path)
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def test_it_follows_the_previous_head(self):
        self.assertEqual(self.module.down_revision, "f9a1c3e5b7d2")

    def test_it_is_the_only_head(self):
        from alembic.config import Config
        from alembic.script import ScriptDirectory
        backend = Path(__file__).resolve().parents[1]
        config = Config(str(backend / "alembic.ini"))
        config.set_main_option("script_location", str(backend / "alembic"))
        self.assertEqual(ScriptDirectory.from_config(config).get_heads(), ["7c65a7896bab"])

    def _ops(self, fn):
        recorded = []
        fake_op = MagicMock()
        fake_op.execute.side_effect = lambda statement: recorded.append(("execute", str(statement)))
        for name in ("add_column", "drop_column", "create_foreign_key", "drop_constraint", "create_index", "drop_index"):
            getattr(fake_op, name).side_effect = (lambda n: lambda *a, **k: recorded.append((n, a)))(name)
        with patch.object(self.module, "op", fake_op):
            fn()
        return recorded

    def test_upgrade_adds_nullable_owner_and_never_assigns_existing_projects(self):
        recorded = self._ops(self.module.upgrade)
        owner_column = next(args[1] for name, args in recorded if name == "add_column" and args[0] == "projects")
        self.assertEqual(owner_column.name, "owner_id")
        self.assertTrue(owner_column.nullable)
        statements = [sql for name, sql in recorded if name == "execute"]
        self.assertFalse([sql for sql in statements if "owner_id" in sql.lower() or "update projects" in sql.lower()],
                         "no existing project may be given an owner by migration")

    def test_eligibility_defaults_to_off(self):
        recorded = self._ops(self.module.upgrade)
        column = next(args[1] for name, args in recorded if name == "add_column" and args[0] == "users")
        self.assertEqual(column.name, "can_own_projects")
        self.assertFalse(column.nullable)
        self.assertEqual(str(column.server_default.arg), "false")

    def test_downgrade_removes_everything_upgrade_added(self):
        recorded = self._ops(self.module.downgrade)
        self.assertIn(("drop_column", ("projects", "owner_id")), recorded)
        self.assertIn(("drop_column", ("users", "can_own_projects")), recorded)
        self.assertIn(("drop_constraint", ("fk_projects_owner", "projects")), recorded)
        self.assertIn(("drop_index", ("idx_projects_org_owner",)), recorded)


if __name__ == "__main__":
    unittest.main()

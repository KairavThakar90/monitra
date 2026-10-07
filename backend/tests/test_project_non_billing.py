"""
The Non Billing project type.

A project is created as one of three billing types:

* `fixed`        -- billed against an hour budget, which is required;
* `free`         -- flexible time: no budget, not billed;
* `non_billing`  -- not billed and no budget; its own type so it can be filtered
                    on and shown as "Non Billing".

Both `free` and `non_billing` are stored non-billable (`is_billable = false`),
which is what manual-time defaults, the reports' billable filter and the
dashboard's Billable tab all read.
"""
import unittest
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

from fastapi import HTTPException
from pydantic import ValidationError

from app.models.project import Project
from app.schemas.project_management import BillingType, ProjectCreate, ProjectUpdate
from app.services.project_management import ProjectManagementService
from app.WFPM.schemas import WfpmProjectCreate
from app.models.user import User
from tests.status_catalog_stub import rows, status_catalog

LEADER_ID = 2
OWNER_ID = 9


def _payload(billing_type, fixed_hours=None):
    return ProjectCreate(
        project_name="Internal tooling", status_id=1, owner_id=OWNER_ID, leader_id=LEADER_ID,
        employee_ids=[], deadline=date.today() + timedelta(days=20),
        billing_type=billing_type, fixed_hours=fixed_hours, category="kyle",
    )


def _create(billing_type, fixed_hours=None) -> Project:
    """Run `create()` against a stubbed session and return the Project it added."""
    db = MagicMock()
    db.scalar.return_value = User(
        id=OWNER_ID, organization_id=1, role_name="administrator", permissions={},
        is_active=True, can_own_projects=True,
    )
    leader = User(id=LEADER_ID, organization_id=1, role_name="project_leader", permissions={})
    answers = [[leader], [], []]
    db.scalars.return_value.all.side_effect = lambda: answers.pop(0) if answers else []
    actor = User(id=1, organization_id=1, role_name="administrator", permissions={})
    with status_catalog(project_statuses=rows((1, "Active")), task_statuses=rows((1, "Todo"))):
        ProjectManagementService.create(db, actor, _payload(billing_type, fixed_hours))
    return next(call.args[0] for call in db.add.call_args_list if isinstance(call.args[0], Project))


class NonBillingSchemaTests(unittest.TestCase):
    def test_non_billing_is_a_billing_type(self):
        self.assertEqual(BillingType("non_billing"), BillingType.non_billing)
        self.assertEqual(BillingType.non_billing.value, "non_billing")

    def test_a_non_billing_project_needs_no_hour_budget(self):
        project = _payload("non_billing")
        self.assertEqual(project.billing_type, BillingType.non_billing)
        self.assertIsNone(project.fixed_hours)

    def test_a_non_billing_project_cannot_carry_an_hour_budget(self):
        with self.assertRaises(ValidationError) as error:
            _payload("non_billing", Decimal("40"))
        self.assertIn("non-billing", str(error.exception))

    def test_the_other_billing_rules_are_unchanged(self):
        with self.assertRaises(ValidationError):
            _payload("fixed")  # still needs its hours
        with self.assertRaises(ValidationError):
            _payload("free", Decimal("40"))  # still refuses hours
        self.assertEqual(_payload("fixed", Decimal("40")).fixed_hours, Decimal("40"))
        self.assertIsNone(_payload("free").fixed_hours)

    def test_an_unknown_type_is_still_refused(self):
        with self.assertRaises(ValidationError):
            _payload("billable")

    def test_an_update_accepts_switching_to_non_billing(self):
        update = ProjectUpdate(billing_type="non_billing", fixed_hours=None)
        self.assertEqual(update.billing_type, BillingType.non_billing)
        self.assertIn("fixed_hours", update.model_dump(exclude_unset=True))  # an explicit clear


class NonBillingCreateTests(unittest.TestCase):
    def test_creating_a_non_billing_project_stores_it_non_billable_with_no_budget(self):
        project = _create("non_billing")
        self.assertEqual(project.billing_type, "non_billing")
        self.assertFalse(project.is_billable)
        self.assertIsNone(project.fixed_hours)

    def test_flexible_time_is_stored_non_billable_too(self):
        project = _create("free")
        self.assertEqual(project.billing_type, "free")
        self.assertFalse(project.is_billable)

    def test_a_fixed_hours_project_is_still_billable_with_its_budget(self):
        project = _create("fixed", Decimal("120"))
        self.assertEqual(project.billing_type, "fixed")
        self.assertTrue(project.is_billable)
        self.assertEqual(project.fixed_hours, Decimal("120"))


class NonBillingServiceRuleTests(unittest.TestCase):
    """The service restates the schema's rule, because `update` does not pass
    through `ProjectCreate`: it merges the payload over the stored project."""

    def _validate(self, billing_type, fixed_hours):
        leader = User(id=LEADER_ID, organization_id=1, role_name="project_leader", permissions={})
        actor = User(id=1, organization_id=1, role_name="administrator", permissions={})
        with patch.object(ProjectManagementService, "_status", return_value=MagicMock()), \
             patch.object(ProjectManagementService, "_users", side_effect=[[leader], []]):
            return ProjectManagementService._validate_project_fields(
                MagicMock(), actor, 1, LEADER_ID, [], None, billing_type, fixed_hours
            )

    def test_non_billing_with_hours_is_refused_with_a_400(self):
        with self.assertRaises(HTTPException) as error:
            self._validate(BillingType.non_billing, Decimal("40"))
        self.assertEqual(error.exception.status_code, 400)
        self.assertIn("non-billing", error.exception.detail)

    def test_non_billing_without_hours_passes(self):
        self._validate(BillingType.non_billing, None)

    def test_the_existing_messages_are_unchanged(self):
        with self.assertRaises(HTTPException) as fixed:
            self._validate(BillingType.fixed, None)
        self.assertEqual(fixed.exception.detail, "Fixed hours are required for fixed billing.")
        with self.assertRaises(HTTPException) as free:
            self._validate(BillingType.free, Decimal("40"))
        self.assertEqual(free.exception.detail, "Fixed hours must be empty for free time billing.")


class NonBillingWfpmTests(unittest.TestCase):
    """WFPM's create request reuses `BillingType`, so it accepts the new value
    (documented in docs/WFPM_INTEGRATION.md) and still refuses `fixed`, which
    needs an hours budget that API does not take."""

    def test_wfpm_can_create_a_non_billing_project(self):
        request = WfpmProjectCreate(
            project_name="Ops", employee_ids=[], deadline=date.today() + timedelta(days=5),
            billing_type="non_billing",
        )
        self.assertEqual(request.billing_type, BillingType.non_billing)
        # What the service then builds: no hours, so it validates as a project.
        built = ProjectCreate(
            status_id=1, leader_id=LEADER_ID, fixed_hours=None, **request.model_dump()
        )
        self.assertEqual(built.billing_type, BillingType.non_billing)

    def test_wfpm_still_cannot_create_a_fixed_project(self):
        request = WfpmProjectCreate(
            project_name="Ops", employee_ids=[], deadline=date.today() + timedelta(days=5),
            billing_type="fixed",
        )
        with self.assertRaises(ValidationError):
            ProjectCreate(status_id=1, leader_id=LEADER_ID, fixed_hours=None, **request.model_dump())


if __name__ == "__main__":
    unittest.main()

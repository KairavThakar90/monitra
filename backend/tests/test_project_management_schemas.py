import unittest
from datetime import date, timedelta
from decimal import Decimal

from pydantic import ValidationError

from app.schemas.project_management import ProjectCreate, ProjectUpdate, TaskCreate


def project_values(**overrides):
    values = {
        "project_name": " Website Redesign ",
        "description": " Redesign the website. ",
        "status_id": 1,
        "leader_id": 12,
        "employee_ids": [21, 22],
        "deadline": date.today() + timedelta(days=10),
        "billing_type": "fixed",
        "fixed_hours": Decimal("120"),
    }
    values.update(overrides)
    return values


class ProjectManagementSchemaTests(unittest.TestCase):
    def test_project_values_are_normalized(self):
        project = ProjectCreate(**project_values())
        self.assertEqual(project.project_name, "Website Redesign")
        self.assertEqual(project.description, "Redesign the website.")

    def test_billing_rules(self):
        with self.assertRaises(ValidationError):
            ProjectCreate(**project_values(fixed_hours=None))
        with self.assertRaises(ValidationError):
            ProjectCreate(**project_values(billing_type="free"))

    def test_deadline_and_employee_validation(self):
        with self.assertRaises(ValidationError):
            ProjectCreate(**project_values(deadline=date.today() - timedelta(days=1)))
        with self.assertRaises(ValidationError):
            ProjectCreate(**project_values(employee_ids=[21, 21]))

    def test_a_missing_deadline_is_accepted(self):
        # Deadline is optional: the column is nullable and the app renders
        # "No Deadline" -- only a *past* date is refused, never an absent one.
        project = ProjectCreate(**project_values(deadline=None))
        self.assertIsNone(project.deadline)

    def test_task_name_and_ids_are_validated(self):
        with self.assertRaises(ValidationError):
            TaskCreate(name=" ", assignee_id=21, status_id=1)
        with self.assertRaises(ValidationError):
            TaskCreate(name="Task", assignee_id=0, status_id=1)

    def test_update_allows_explicit_free_billing_clear(self):
        update = ProjectUpdate(billing_type="free", fixed_hours=None)
        self.assertEqual(update.billing_type.value, "free")
        self.assertIsNone(update.fixed_hours)


if __name__ == "__main__":
    unittest.main()
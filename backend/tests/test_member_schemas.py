from datetime import date, timedelta
import unittest

from pydantic import ValidationError

from app.schemas.member import MemberCreate, MemberUpdate


def valid_member(**overrides):
    values = {
        "name": " Ada Lovelace ",
        "email": "ADA@example.com",
        "role": "employee",
        "status": "active",
        "date_of_joining": date(2026, 1, 1),
        "date_of_birth": date(1990, 1, 1),
        "designation": " Engineer ",
    }
    values.update(overrides)
    return values


class MemberSchemaTests(unittest.TestCase):
    def test_member_input_is_normalized(self):
        member = MemberCreate(**valid_member())
        self.assertEqual(member.name, "Ada Lovelace")
        self.assertEqual(member.email, "ada@example.com")
        self.assertEqual(member.designation, "Engineer")


    def test_required_text_fields_reject_whitespace(self):
        for field in ("name", "designation"):
            with self.subTest(field=field), self.assertRaises(ValidationError):
                MemberCreate(**valid_member(**{field: "   "}))


    def test_member_input_rejects_invalid_role_and_future_birth_date(self):
        with self.assertRaises(ValidationError):
            MemberCreate(**valid_member(role="manager"))
        with self.assertRaises(ValidationError):
            MemberCreate(**valid_member(date_of_birth=date.today() + timedelta(days=1)))


class MemberUpdateTrackingSettingsTests(unittest.TestCase):
    """`idle_enabled` / `idle_minutes` / `capture_frequency` are the tracking
    settings an administrator sets per member from the User Management page.
    Omitted fields must stay unset (`exclude_unset` in MemberService.update
    relies on this) rather than default to a value that would silently
    overwrite the member's existing settings."""

    def test_omitted_tracking_fields_stay_unset(self):
        update = MemberUpdate(name="Ada")
        self.assertEqual(update.model_dump(exclude_unset=True), {"name": "Ada"})

    def test_valid_tracking_settings_are_accepted(self):
        update = MemberUpdate(idle_enabled=False, idle_minutes=10, capture_frequency=10)
        self.assertFalse(update.idle_enabled)
        self.assertEqual(update.idle_minutes, 10)
        self.assertEqual(update.capture_frequency, 10)

    def test_idle_minutes_out_of_range_is_rejected(self):
        with self.assertRaises(ValidationError):
            MemberUpdate(idle_minutes=0)
        with self.assertRaises(ValidationError):
            MemberUpdate(idle_minutes=121)

    def test_capture_frequency_has_no_upper_bound(self):
        # The admin has full control over this per-member interval -- only a
        # positive whole number is enforced, never a ceiling.
        with self.assertRaises(ValidationError):
            MemberUpdate(capture_frequency=0)
        update = MemberUpdate(capture_frequency=999999)
        self.assertEqual(update.capture_frequency, 999999)
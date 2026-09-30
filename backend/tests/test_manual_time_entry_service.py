import unittest
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

from app.schemas.manual_time_entry import ManualTimeEntryCreate, ManualTimeEntryUpdate
from app.services.manual_time_entry import ManualTimeEntryService


def make_user(uid=54, org=1, permissions=None):
    return SimpleNamespace(id=uid, organization_id=org, permissions=permissions or {})


def make_entry(**overrides):
    base = dict(
        id=1, organization_id=1, user_id=54, project_id=1272, task_id=239,
        work_date=date(2026, 8, 10), start_time=datetime(2026, 8, 10, 9, tzinfo=timezone.utc),
        end_time=datetime(2026, 8, 10, 10, tzinfo=timezone.utc), total_seconds=3600,
        description="reason", is_billable=True, approval_status="pending",
        approved_by=None, approved_at=None, mirrored_time_entry_id=None, deleted_at=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class SlotResolutionTests(unittest.TestCase):
    def test_backward_compatible_when_no_clock_time_given(self):
        start, end, secs = ManualTimeEntryService._resolve_slot(date(2026, 8, 10), 3600, None, None)
        self.assertEqual(start, datetime(2026, 8, 10, 0, 0, tzinfo=timezone.utc))
        self.assertEqual(end, datetime(2026, 8, 10, 1, 0, tzinfo=timezone.utc))
        self.assertEqual(secs, 3600)

    def test_uses_explicit_slot_when_given(self):
        s = datetime(2026, 8, 10, 14, tzinfo=timezone.utc)
        e = datetime(2026, 8, 10, 15, 30, tzinfo=timezone.utc)
        start, end, secs = ManualTimeEntryService._resolve_slot(date(2026, 8, 10), 999, s, e)
        self.assertEqual((start, end, secs), (s, e, 5400))


def make_project(billing_type="fixed", **overrides):
    base = dict(id=1272, organization_id=1, project_name="Beta Launch", billing_type=billing_type)
    base.update(overrides)
    return SimpleNamespace(**base)


class CreateConflictTests(unittest.TestCase):
    def setUp(self):
        self.user = make_user()
        project_patch = patch(
            "app.services.manual_time_entry.ProjectRepository.get_by_id", return_value=make_project()
        )
        project_patch.start()
        self.addCleanup(project_patch.stop)

    def test_rejects_when_overlapping_time_entry_exists(self):
        payload = ManualTimeEntryCreate(project_id=1272, task_id=239, work_date=date(2026, 8, 10), total_seconds=3600)
        with patch("app.services.manual_time_entry.TaskService.get_task"), \
             patch("app.services.manual_time_entry.ManualTimeEntryRepository.find_overlapping_time_entries",
                   return_value=[SimpleNamespace(id=99)]), \
             patch("app.services.manual_time_entry.ManualTimeEntryRepository.find_overlapping_manual_entries", return_value=[]):
            with self.assertRaises(HTTPException) as error:
                ManualTimeEntryService.create_manual_entry(None, payload, self.user)
        self.assertEqual(error.exception.status_code, 409)

    def test_rejects_when_overlapping_manual_entry_exists(self):
        payload = ManualTimeEntryCreate(project_id=1272, task_id=239, work_date=date(2026, 8, 10), total_seconds=3600)
        with patch("app.services.manual_time_entry.TaskService.get_task"), \
             patch("app.services.manual_time_entry.ManualTimeEntryRepository.find_overlapping_time_entries", return_value=[]), \
             patch("app.services.manual_time_entry.ManualTimeEntryRepository.find_overlapping_manual_entries",
                   return_value=[make_entry(id=2)]):
            with self.assertRaises(HTTPException) as error:
                ManualTimeEntryService.create_manual_entry(None, payload, self.user)
        self.assertEqual(error.exception.status_code, 409)

    def test_succeeds_when_no_conflict(self):
        payload = ManualTimeEntryCreate(project_id=1272, task_id=239, work_date=date(2026, 8, 10), total_seconds=3600)
        with patch("app.services.manual_time_entry.TaskService.get_task"), \
             patch("app.services.manual_time_entry.ManualTimeEntryRepository.find_overlapping_time_entries", return_value=[]), \
             patch("app.services.manual_time_entry.ManualTimeEntryRepository.find_overlapping_manual_entries", return_value=[]), \
             patch("app.services.manual_time_entry.ManualTimeEntryRepository.create", return_value=make_entry()) as create:
            ManualTimeEntryService.create_manual_entry(None, payload, self.user)
        self.assertTrue(create.called)

    def test_future_work_date_rejected(self):
        payload = ManualTimeEntryCreate(project_id=1272, task_id=239, work_date=date(2099, 1, 1), total_seconds=3600)
        with patch("app.services.manual_time_entry.TaskService.get_task"):
            with self.assertRaises(HTTPException) as error:
                ManualTimeEntryService.create_manual_entry(None, payload, self.user)
        self.assertEqual(error.exception.status_code, 400)


class ReasonTests(unittest.TestCase):
    """Why the time is being requested: a fixed set, stored as filed.

    The desktop's Request dialog asks for it in place of the Billable box.
    Billable is then not sent at all and is taken from the project, which
    `BillableTests` below already pins for an omitted `is_billable`.
    """

    def setUp(self):
        self.user = make_user()

    def _create(self, project=None, **fields):
        payload = ManualTimeEntryCreate(
            project_id=1272, task_id=239, work_date=date(2026, 8, 10), total_seconds=3600, **fields
        )
        with patch("app.services.manual_time_entry.TaskService.get_task"), \
             patch("app.services.manual_time_entry.ProjectRepository.get_by_id",
                   return_value=project or make_project("fixed")), \
             patch("app.services.manual_time_entry.ManualTimeEntryRepository.find_overlapping_time_entries", return_value=[]), \
             patch("app.services.manual_time_entry.ManualTimeEntryRepository.find_overlapping_manual_entries", return_value=[]), \
             patch("app.services.manual_time_entry.ManualTimeEntryRepository.create", return_value=make_entry()) as create:
            ManualTimeEntryService.create_manual_entry(None, payload, self.user)
        return create.call_args.kwargs

    def test_the_three_reasons_are_the_whole_set(self):
        from app.schemas.manual_time_entry import MANUAL_ENTRY_REASON_LABELS, ManualEntryReason

        self.assertEqual(
            [member.value for member in ManualEntryReason],
            ["forgot_timer", "wrong_task_project", "other"],
        )
        self.assertEqual(MANUAL_ENTRY_REASON_LABELS, {
            "forgot_timer": "Forgot to start/stop timer",
            "wrong_task_project": "Used wrong task/project",
            "other": "Other",
        })

    def test_each_reason_is_stored_as_its_plain_value(self):
        for value in ("forgot_timer", "wrong_task_project", "other"):
            stored = self._create(reason=value)["reason"]
            self.assertEqual(stored, value)
            self.assertIs(type(stored), str, "the enum member must not reach the column")

    def test_a_request_without_a_reason_is_stored_as_not_given(self):
        """The web form and older desktop builds send none. That is stored as
        null -- never as a default reason nobody chose."""
        self.assertIsNone(self._create()["reason"])

    def test_a_value_outside_the_set_is_refused_and_the_error_names_the_set(self):
        from pydantic import ValidationError

        with self.assertRaises(ValidationError) as error:
            ManualTimeEntryCreate(
                project_id=1272, task_id=239, work_date=date(2026, 8, 10),
                total_seconds=3600, reason="felt like it",
            )
        message = str(error.exception)
        for value in ("forgot_timer", "wrong_task_project", "other"):
            self.assertIn(value, message)

    def test_billable_comes_from_the_project_when_only_a_reason_is_sent(self):
        """What the desktop now sends: a reason and no `is_billable`."""
        fixed = self._create(make_project("fixed"), reason="forgot_timer")
        free = self._create(make_project("free"), reason="forgot_timer")

        self.assertIs(fixed["is_billable"], True)
        self.assertIs(free["is_billable"], False)

    def test_a_pending_request_can_have_its_reason_corrected(self):
        entry = make_entry(reason="other")
        with patch("app.services.manual_time_entry.ManualTimeEntryRepository.get_by_id", return_value=entry), \
             patch("app.services.manual_time_entry.ManualTimeEntryRepository.update_fields",
                   side_effect=lambda db, e, **kw: SimpleNamespace(**{**vars(e), **kw})) as update:
            result = ManualTimeEntryService.update_manual_entry(
                None, 1, ManualTimeEntryUpdate(reason="wrong_task_project"), make_user(uid=54)
            )

        self.assertEqual(result.reason, "wrong_task_project")
        self.assertIs(type(update.call_args.kwargs["reason"]), str)

    def test_the_review_listing_carries_the_reason(self):
        entry = make_entry(reason="wrong_task_project", created_at=None, updated_at=None)
        db = MagicMock()
        db.query.return_value.filter.return_value.all.return_value = []
        with patch("app.services.manual_time_entry.visible_member_ids", return_value=None), \
             patch("app.services.manual_time_entry.ManualTimeEntryRepository.search_by_filters",
                   return_value=([entry], 1)), \
             patch("app.services.manual_time_entry.ManualTimeEntryRepository.find_overlapping_time_entries",
                   return_value=[]):
            page = ManualTimeEntryService.list_for_review(
                db, make_user(permissions={"time_entries:view_all": True}),
                None, None, None, None, None, None, None, 1, 20,
            )

        self.assertEqual(page["items"][0]["reason"], "wrong_task_project")

    def test_the_read_schema_returns_the_reason(self):
        from app.schemas.manual_time_entry import ManualTimeEntryRead

        now = datetime(2026, 8, 10, 9, tzinfo=timezone.utc)
        with_reason = ManualTimeEntryRead.model_validate(
            make_entry(reason="forgot_timer", created_at=now, updated_at=now)
        )
        legacy = ManualTimeEntryRead.model_validate(
            make_entry(reason=None, created_at=now, updated_at=now)
        )

        self.assertEqual(with_reason.reason, "forgot_timer")
        self.assertIsNone(legacy.reason)


class BillableTests(unittest.TestCase):
    """Billable follows the project's billing type, fixed at project creation.

    Only a fixed-hours project bills its time. The desktop dialog showed a
    ticked "Billable" box for every project and the web sent `is_billable:
    true` unconditionally, so free-project time was being recorded as
    billable.
    """

    def setUp(self):
        self.user = make_user()

    def _create(self, project, **fields):
        payload = ManualTimeEntryCreate(
            project_id=1272, task_id=239, work_date=date(2026, 8, 10), total_seconds=3600, **fields
        )
        with patch("app.services.manual_time_entry.TaskService.get_task"),              patch("app.services.manual_time_entry.ProjectRepository.get_by_id", return_value=project),              patch("app.services.manual_time_entry.ManualTimeEntryRepository.find_overlapping_time_entries", return_value=[]),              patch("app.services.manual_time_entry.ManualTimeEntryRepository.find_overlapping_manual_entries", return_value=[]),              patch("app.services.manual_time_entry.ManualTimeEntryRepository.create", return_value=make_entry()) as create:
            ManualTimeEntryService.create_manual_entry(None, payload, self.user)
        return create.call_args.kwargs["is_billable"]

    def test_fixed_project_defaults_to_billable(self):
        self.assertIs(self._create(make_project("fixed")), True)

    def test_fixed_project_honours_an_unticked_box(self):
        self.assertIs(self._create(make_project("fixed"), is_billable=False), False)

    def test_free_project_defaults_to_not_billable(self):
        self.assertIs(self._create(make_project("free")), False)

    def test_free_project_accepts_explicit_not_billable(self):
        self.assertIs(self._create(make_project("free"), is_billable=False), False)

    def test_free_project_refuses_billable_rather_than_scrubbing_it(self):
        with self.assertRaises(HTTPException) as error:
            self._create(make_project("free"), is_billable=True)
        self.assertEqual(error.exception.status_code, 400)
        self.assertIn("Beta Launch", error.exception.detail)
        self.assertIn("not a billable project", error.exception.detail)

    def test_omitted_is_billable_is_left_for_the_project_to_decide(self):
        payload = ManualTimeEntryCreate(project_id=1, task_id=2, work_date=date(2026, 8, 10), total_seconds=60)
        self.assertIsNone(payload.is_billable)

    def _edit(self, entry, update, project):
        with patch("app.services.manual_time_entry.ManualTimeEntryRepository.get_by_id", return_value=entry),              patch("app.services.manual_time_entry.TaskService.get_task"),              patch("app.services.manual_time_entry.ProjectRepository.get_by_id", return_value=project),              patch("app.services.manual_time_entry.ManualTimeEntryRepository.update_fields",
                   side_effect=lambda db, e, **kw: SimpleNamespace(**{**vars(e), **kw})):
            return ManualTimeEntryService.update_manual_entry(None, 1, update, make_user(uid=54))

    def test_edit_cannot_make_free_project_time_billable(self):
        with self.assertRaises(HTTPException) as error:
            self._edit(make_entry(is_billable=False), ManualTimeEntryUpdate(is_billable=True), make_project("free"))
        self.assertEqual(error.exception.status_code, 400)

    def test_moving_an_entry_to_a_free_project_clears_billable(self):
        result = self._edit(make_entry(is_billable=True), ManualTimeEntryUpdate(project_id=2214), make_project("free"))
        self.assertIs(result.is_billable, False)

    def test_moving_an_entry_to_a_fixed_project_defaults_to_billable(self):
        result = self._edit(make_entry(is_billable=False), ManualTimeEntryUpdate(project_id=2215), make_project("fixed"))
        self.assertIs(result.is_billable, True)

    def test_editing_only_the_description_does_not_revisit_billable(self):
        # A pending entry whose project has since become free must still be
        # editable; nothing about its billable flag was asked to change.
        with patch("app.services.manual_time_entry.ManualTimeEntryRepository.get_by_id",
                   return_value=make_entry(is_billable=True)), \
             patch("app.services.manual_time_entry.ProjectRepository.get_by_id",
                   return_value=make_project("free")) as get_project, \
             patch("app.services.manual_time_entry.ManualTimeEntryRepository.update_fields",
                   side_effect=lambda db, e, **kw: SimpleNamespace(**{**vars(e), **kw})):
            result = ManualTimeEntryService.update_manual_entry(
                None, 1, ManualTimeEntryUpdate(description="new"), make_user(uid=54)
            )
        self.assertEqual(result.description, "new")
        self.assertIs(result.is_billable, True)
        self.assertFalse(get_project.called)


class ApprovalMirrorTests(unittest.TestCase):
    def setUp(self):
        self.user = make_user(permissions={"manual_time_entries:approve": True})

    def test_approval_creates_mirror_and_links_it(self):
        entry = make_entry()
        mirror = SimpleNamespace(id=999)
        with patch("app.services.manual_time_entry.ManualTimeEntryRepository.get_by_id", return_value=entry), \
             patch("app.services.manual_time_entry.ManualTimeEntryRepository.find_overlapping_time_entries", return_value=[]), \
             patch("app.services.manual_time_entry.ManualTimeEntryRepository.find_overlapping_manual_entries", return_value=[]), \
             patch("app.services.manual_time_entry.ManualTimeEntryRepository.create_mirrored_time_entry",
                   return_value=mirror) as create_mirror, \
             patch("app.services.manual_time_entry.ManualTimeEntryRepository.update_approval_status",
                   side_effect=lambda **kw: kw) as update_status:
            result = ManualTimeEntryService.update_approval(None, 1, "approved", self.user)
        self.assertTrue(create_mirror.called)
        self.assertEqual(update_status.call_args.kwargs["mirrored_time_entry_id"], 999)
        self.assertEqual(result["approval_status"], "approved")

    def test_rejection_does_not_create_mirror(self):
        entry = make_entry()
        with patch("app.services.manual_time_entry.ManualTimeEntryRepository.get_by_id", return_value=entry), \
             patch("app.services.manual_time_entry.ManualTimeEntryRepository.create_mirrored_time_entry") as create_mirror, \
             patch("app.services.manual_time_entry.ManualTimeEntryRepository.update_approval_status",
                   side_effect=lambda **kw: kw) as update_status:
            ManualTimeEntryService.update_approval(None, 1, "rejected", self.user)
        self.assertFalse(create_mirror.called)
        self.assertIsNone(update_status.call_args.kwargs["mirrored_time_entry_id"])

    def test_approval_conflict_at_decision_time_blocks_and_skips_mirror(self):
        entry = make_entry()
        with patch("app.services.manual_time_entry.ManualTimeEntryRepository.get_by_id", return_value=entry), \
             patch("app.services.manual_time_entry.ManualTimeEntryRepository.find_overlapping_time_entries",
                   return_value=[SimpleNamespace(id=5)]), \
             patch("app.services.manual_time_entry.ManualTimeEntryRepository.create_mirrored_time_entry") as create_mirror:
            with self.assertRaises(HTTPException) as error:
                ManualTimeEntryService.update_approval(None, 1, "approved", self.user)
        self.assertEqual(error.exception.status_code, 409)
        self.assertFalse(create_mirror.called)

    def test_already_decided_entry_is_conflict(self):
        entry = make_entry(approval_status="approved")
        with patch("app.services.manual_time_entry.ManualTimeEntryRepository.get_by_id", return_value=entry):
            with self.assertRaises(HTTPException) as error:
                ManualTimeEntryService.update_approval(None, 1, "approved", self.user)
        self.assertEqual(error.exception.status_code, 409)

    def test_unprivileged_user_forbidden(self):
        with self.assertRaises(HTTPException) as error:
            ManualTimeEntryService.update_approval(None, 1, "approved", make_user(permissions={}))
        self.assertEqual(error.exception.status_code, 403)


class EditTests(unittest.TestCase):
    def test_owner_can_edit_pending_entry(self):
        entry = make_entry()
        user = make_user(uid=54)
        with patch("app.services.manual_time_entry.ManualTimeEntryRepository.get_by_id", return_value=entry), \
             patch("app.services.manual_time_entry.ManualTimeEntryRepository.update_fields",
                   side_effect=lambda db, e, **kw: SimpleNamespace(**{**vars(e), **kw})) as update_fields:
            result = ManualTimeEntryService.update_manual_entry(None, 1, ManualTimeEntryUpdate(description="new"), user)
        self.assertTrue(update_fields.called)
        self.assertEqual(result.description, "new")

    def test_non_owner_forbidden(self):
        entry = make_entry(user_id=54)
        other_user = make_user(uid=99)
        with patch("app.services.manual_time_entry.ManualTimeEntryRepository.get_by_id", return_value=entry):
            with self.assertRaises(HTTPException) as error:
                ManualTimeEntryService.update_manual_entry(None, 1, ManualTimeEntryUpdate(description="x"), other_user)
        self.assertEqual(error.exception.status_code, 403)

    def test_approved_entry_cannot_be_edited(self):
        entry = make_entry(approval_status="approved")
        user = make_user(uid=54)
        with patch("app.services.manual_time_entry.ManualTimeEntryRepository.get_by_id", return_value=entry):
            with self.assertRaises(HTTPException) as error:
                ManualTimeEntryService.update_manual_entry(None, 1, ManualTimeEntryUpdate(description="x"), user)
        self.assertEqual(error.exception.status_code, 409)

    def test_editing_time_reruns_conflict_check(self):
        entry = make_entry()
        user = make_user(uid=54)
        new_start = datetime(2026, 8, 10, 20, tzinfo=timezone.utc)
        new_end = datetime(2026, 8, 10, 21, tzinfo=timezone.utc)
        with patch("app.services.manual_time_entry.ManualTimeEntryRepository.get_by_id", return_value=entry), \
             patch("app.services.manual_time_entry.ManualTimeEntryRepository.find_overlapping_time_entries",
                   return_value=[SimpleNamespace(id=1)]), \
             patch("app.services.manual_time_entry.ManualTimeEntryRepository.find_overlapping_manual_entries", return_value=[]):
            with self.assertRaises(HTTPException) as error:
                ManualTimeEntryService.update_manual_entry(
                    None, 1, ManualTimeEntryUpdate(start_time=new_start, end_time=new_end), user
                )
        self.assertEqual(error.exception.status_code, 409)


class DeleteTests(unittest.TestCase):
    def test_owner_can_delete_pending(self):
        entry = make_entry()
        user = make_user(uid=54, permissions={})
        with patch("app.services.manual_time_entry.ManualTimeEntryRepository.get_by_id", return_value=entry), \
             patch("app.services.manual_time_entry.ManualTimeEntryRepository.soft_delete") as soft_delete:
            ManualTimeEntryService.delete_manual_entry(None, 1, user)
        self.assertTrue(soft_delete.called)

    def test_approver_can_delete_someone_elses_pending_entry(self):
        entry = make_entry(user_id=54)
        approver = make_user(uid=99, permissions={"manual_time_entries:approve": True})
        with patch("app.services.manual_time_entry.ManualTimeEntryRepository.get_by_id", return_value=entry), \
             patch("app.services.manual_time_entry.ManualTimeEntryRepository.soft_delete") as soft_delete:
            ManualTimeEntryService.delete_manual_entry(None, 1, approver)
        self.assertTrue(soft_delete.called)

    def test_unrelated_user_forbidden(self):
        entry = make_entry(user_id=54)
        stranger = make_user(uid=100, permissions={})
        with patch("app.services.manual_time_entry.ManualTimeEntryRepository.get_by_id", return_value=entry):
            with self.assertRaises(HTTPException) as error:
                ManualTimeEntryService.delete_manual_entry(None, 1, stranger)
        self.assertEqual(error.exception.status_code, 403)

    def test_approved_entry_cannot_be_deleted(self):
        entry = make_entry(approval_status="approved")
        user = make_user(uid=54, permissions={})
        with patch("app.services.manual_time_entry.ManualTimeEntryRepository.get_by_id", return_value=entry):
            with self.assertRaises(HTTPException) as error:
                ManualTimeEntryService.delete_manual_entry(None, 1, user)
        self.assertEqual(error.exception.status_code, 409)


if __name__ == "__main__":
    unittest.main()

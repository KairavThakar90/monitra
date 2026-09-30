"""Manual time request emails — the properties that have to hold in production.

1. **Submit.** A new request queues one review email per approver (admins and
   the requester's leaders) plus a receipt to the requester, each keyed so a
   retry can never send a second copy.
2. **Decide.** Approving or rejecting queues exactly one outcome email to the
   requester, and to nobody else.
3. **Content.** Every field of the request appears exactly as it was filed;
   the requester's description is escaped, never interpreted as markup.
4. **Never fails the request.** Email trouble is logged; the request and the
   decision still succeed, and delivery is scheduled right after the response.

No test here sends a real message.
"""
import json
import unittest
from datetime import date, datetime, timezone
from unittest.mock import MagicMock, patch

from app.core.config import settings
from app.services.email import messages
from app.services.email.workflows import (
    _manual_time_payload, manual_time_dedupe_key,
    queue_manual_time_decision_notification, queue_manual_time_request_notifications,
)

WORKFLOWS = "app.services.email.workflows"
APPROVERS = "app.repositories.manual_time_notification.ManualTimeNotificationRepository.list_approvers"


def email_settings(**overrides):
    defaults = {
        "EMAIL_PROVIDER": "smtp",
        "EMAIL_FROM_ADDRESS": "monitra@example.com",
        "EMAIL_FROM_NAME": "Monitra",
        "EMAIL_REPLY_TO": "",
        "SMTP_HOST": "smtp.example.com",
        "MONITRA_APP_URL": "https://staff.peakworkos.com",
        "MONITRA_SUPPORT_EMAIL": "",
        "EMAIL_ASSET_BASE_URL": "",
    }
    defaults.update(overrides)
    return patch.multiple(settings, **defaults)


def _user(user_id, name, email, role="employee", permissions=None):
    user = MagicMock()
    user.id = user_id
    user.name = name
    user.email = email
    user.role_name = role
    user.organization_id = 7
    user.permissions = permissions or {}
    return user


def _entry(**overrides):
    entry = MagicMock()
    entry.id = overrides.get("id", 55)
    entry.organization_id = 7
    entry.user_id = overrides.get("user_id", 42)
    entry.project_id = 3
    entry.task_id = 9
    entry.work_date = date(2026, 9, 15)
    # 09:00–11:30 IST
    entry.start_time = datetime(2026, 9, 15, 3, 30, tzinfo=timezone.utc)
    entry.end_time = datetime(2026, 9, 15, 6, 0, tzinfo=timezone.utc)
    entry.total_seconds = 9000
    entry.is_billable = overrides.get("is_billable", True)
    entry.reason = overrides.get("reason", "forgot_timer")
    entry.description = overrides.get("description", "Client call about the <Q3> rollout\nand follow-up notes")
    entry.approval_status = overrides.get("approval_status", "pending")
    entry.created_at = datetime(2026, 9, 15, 7, 0, tzinfo=timezone.utc)
    entry.approved_at = overrides.get("approved_at")
    return entry


REQUESTER = _user(42, "Smit Prajapati", "smit@example.com")


def _db(requester=REQUESTER):
    """A session whose `get` resolves the requester, the project and the task."""
    project = MagicMock(project_name="Store Revamp", organization_id=7)
    task = MagicMock(task_name="Client onboarding", organization_id=7)
    lookup = {"User": requester, "Project": project, "Task": task}
    db = MagicMock()
    db.get.side_effect = lambda model, _id: lookup[model.__name__]
    return db


def _payload(**overrides):
    entry = _entry(**overrides)
    payload = _manual_time_payload(_db(), entry, REQUESTER)
    payload.update({k: v for k, v in overrides.items() if k in ("reviewer_name", "recipient_name", "status")})
    return payload


def _row(row_id):
    row = MagicMock()
    row.id = row_id
    return row


# ======================================================================
# 1 — submit
# ======================================================================

class TestSubmitQueuesApproversAndReceipt(unittest.TestCase):

    def _queue(self, approvers, enqueue_side_effect=None):
        db = _db()
        with email_settings(), patch(APPROVERS, return_value=approvers) as list_approvers, \
                patch(f"{WORKFLOWS}.EmailOutboxService") as outbox:
            outbox.enqueue.side_effect = enqueue_side_effect or (
                lambda db_, **kw: _row(len(outbox.enqueue.call_args_list))
            )
            ids = queue_manual_time_request_notifications(db, _entry())
        return ids, outbox.enqueue.call_args_list, list_approvers

    def test_every_approver_and_the_requester_get_their_own_row(self):
        admin = _user(1, "Asha Admin", "admin@example.com", "administrator")
        leader = _user(2, "Lalit Leader", "leader@example.com", "leader")
        ids, calls, list_approvers = self._queue([admin, leader])

        self.assertEqual(ids, [1, 2, 3])
        kwargs = [call.kwargs for call in calls]
        self.assertEqual(
            [(k["notification_type"], k["dedupe_key"], k["recipients"]) for k in kwargs],
            [
                ("manual_time_request", "manual:55:approver:1", ["admin@example.com"]),
                ("manual_time_request", "manual:55:approver:2", ["leader@example.com"]),
                ("manual_time_receipt", "manual:55:receipt", ["smit@example.com"]),
            ],
        )
        # The approver lookup is scoped to the request's own org, requester and project.
        self.assertEqual(
            list_approvers.call_args.kwargs,
            {"organization_id": 7, "requester_id": 42, "project_id": 3},
        )
        # Each approver copy greets that approver by name.
        self.assertEqual(kwargs[0]["payload"]["recipient_name"], "Asha Admin")

    def test_the_requester_still_gets_a_receipt_when_no_approver_is_found(self):
        ids, calls, _ = self._queue([])
        self.assertEqual(len(ids), 1)
        self.assertEqual(calls[0].kwargs["notification_type"], "manual_time_receipt")

    def test_one_approvers_failure_does_not_cost_the_others_their_email(self):
        admin = _user(1, "Asha Admin", "admin@example.com", "administrator")
        leader = _user(2, "Lalit Leader", "leader@example.com", "leader")

        def enqueue(db_, **kw):
            if kw["dedupe_key"].endswith("approver:1"):
                raise RuntimeError("row refused")
            return _row(9)

        ids, calls, _ = self._queue([admin, leader], enqueue)
        self.assertEqual(len(calls), 3)
        self.assertEqual(ids, [9, 9])

    def test_it_never_raises_into_the_request(self):
        with patch(APPROVERS, side_effect=RuntimeError("database is on fire")):
            self.assertEqual(queue_manual_time_request_notifications(_db(), _entry()), [])

    def test_the_dedupe_keys_are_stable_per_request(self):
        self.assertEqual(manual_time_dedupe_key(55, "receipt"), "manual:55:receipt")
        self.assertEqual(manual_time_dedupe_key(55, "approved"), manual_time_dedupe_key(55, "approved"))


# ======================================================================
# 2 — decide
# ======================================================================

class TestDecisionQueuesOneEmailToTheRequester(unittest.TestCase):

    def test_an_approval_is_sent_to_the_requester_only(self):
        entry = _entry(approval_status="approved", approved_at=datetime(2026, 9, 16, 5, 0, tzinfo=timezone.utc))
        reviewer = _user(1, "Asha Admin", "admin@example.com", "administrator")
        with email_settings(), patch(f"{WORKFLOWS}.EmailOutboxService") as outbox:
            outbox.enqueue.return_value = _row(77)
            notification_id = queue_manual_time_decision_notification(_db(), entry, reviewer=reviewer)

        self.assertEqual(notification_id, 77)
        kwargs = outbox.enqueue.call_args.kwargs
        self.assertEqual(kwargs["notification_type"], "manual_time_decision")
        self.assertEqual(kwargs["dedupe_key"], "manual:55:approved")
        self.assertEqual(kwargs["recipients"], ["smit@example.com"])
        self.assertEqual(kwargs["payload"]["reviewer_name"], "Asha Admin")
        self.assertEqual(kwargs["payload"]["status"], "approved")

    def test_a_rejection_has_its_own_key(self):
        with email_settings(), patch(f"{WORKFLOWS}.EmailOutboxService") as outbox:
            outbox.enqueue.return_value = _row(78)
            queue_manual_time_decision_notification(_db(), _entry(approval_status="rejected"))
        self.assertEqual(outbox.enqueue.call_args.kwargs["dedupe_key"], "manual:55:rejected")

    def test_an_undecided_request_queues_nothing(self):
        with patch(f"{WORKFLOWS}.EmailOutboxService") as outbox:
            self.assertIsNone(queue_manual_time_decision_notification(_db(), _entry(approval_status="pending")))
        outbox.enqueue.assert_not_called()

    def test_a_requester_without_a_usable_address_is_skipped_not_raised(self):
        requester = _user(42, "No Mail", "")
        with patch(f"{WORKFLOWS}.EmailOutboxService") as outbox:
            self.assertIsNone(queue_manual_time_decision_notification(
                _db(requester), _entry(approval_status="approved"),
            ))
        outbox.enqueue.assert_not_called()


# ======================================================================
# 3 — content
# ======================================================================

class TestContent(unittest.TestCase):

    def test_the_payload_is_the_request_exactly_as_filed(self):
        payload = _payload()
        self.assertEqual(payload["name"], "Smit Prajapati")
        self.assertEqual(payload["email"], "smit@example.com")
        self.assertEqual(payload["project_name"], "Store Revamp")
        self.assertEqual(payload["task_name"], "Client onboarding")
        self.assertEqual(payload["work_date"], "2026-09-15")
        self.assertEqual(payload["total_seconds"], 9000)
        self.assertTrue(payload["is_billable"])
        # The stored value, not its wording: the label is a rendering concern.
        self.assertEqual(payload["reason"], "forgot_timer")
        self.assertEqual(payload["description"], "Client call about the <Q3> rollout\nand follow-up notes")
        self.assertNotIn("permissions", payload)
        json.dumps(payload)  # it must survive the outbox's JSON column

    def test_the_review_email_shows_every_field_and_links_to_the_requests_tab(self):
        with email_settings():
            message = messages.build_manual_time_request_email(
                {**_payload(), "recipient_name": "Asha Admin"}, ["admin@example.com"],
            )
        for expected in ("Hi Asha,", "New manual time request", "Smit Prajapati", "smit@example.com",
                         "Store Revamp", "Client onboarding", "15 September 2026",
                         "9:00 AM – 11:30 AM IST", "2h 30m", "Billable", "Pending approval",
                         "Review Request",
                         "https://staff.peakworkos.com/admin/time-tracking?tab=requests"):
            self.assertIn(expected, message.html, f"missing {expected!r}")
        self.assertIn("Smit Prajapati", message.subject)
        self.assertIn("15 September 2026", message.subject)
        self.assertIn("Client call about the <Q3> rollout", message.text)

    def test_the_reviewer_is_shown_why_the_time_was_requested(self):
        """The reason the requester chose in the desktop's Request dialog, in
        the words they chose it in -- it is what the approver decides on."""
        labels = {
            "forgot_timer": "Forgot to start/stop timer",
            "wrong_task_project": "Used wrong task/project",
            "other": "Other",
        }
        for value, label in labels.items():
            with email_settings():
                message = messages.build_manual_time_request_email(
                    {**_payload(reason=value), "recipient_name": "Asha Admin"}, ["admin@example.com"],
                )
            self.assertIn("Reason", message.html)
            self.assertIn(label, message.html, f"{value} is not shown as {label!r}")
            self.assertIn(label, message.text)

    def test_a_request_with_no_reason_has_no_reason_row(self):
        """Filed before the field existed, or by a client that does not ask
        for one. The row is left out -- never filled with a placeholder."""
        payload = _payload(reason=None)
        self.assertIsNone(payload["reason"])
        with email_settings():
            message = messages.build_manual_time_request_email(
                {**payload, "recipient_name": "Asha Admin"}, ["admin@example.com"],
            )
        self.assertNotIn("Reason", message.html)
        self.assertNotIn("Reason", message.text)
        for label in ("Forgot to start/stop timer", "Used wrong task/project"):
            self.assertNotIn(label, message.html)

    def test_a_stored_email_from_before_the_field_still_renders(self):
        """An outbox row queued by the previous build has no `reason` key."""
        payload = _payload()
        del payload["reason"]
        with email_settings():
            message = messages.build_manual_time_request_email(
                {**payload, "recipient_name": "Asha Admin"}, ["admin@example.com"],
            )
        self.assertIn("Store Revamp", message.html)
        self.assertNotIn("Reason", message.html)

    def test_the_description_is_escaped_not_rendered(self):
        with email_settings():
            message = messages.build_manual_time_request_email(
                _payload(description="<script>alert(1)</script>"), ["admin@example.com"],
            )
        self.assertNotIn("<script>", message.html)
        self.assertIn("&lt;script&gt;", message.html)

    def test_the_receipt_confirms_submission_and_links_to_the_members_own_screen(self):
        with email_settings():
            message = messages.build_manual_time_receipt_email(_payload(), ["smit@example.com"])
        self.assertIn("Your manual time request was submitted", message.html)
        self.assertIn("Hi Smit,", message.html)
        self.assertIn("https://staff.peakworkos.com/member/time-tracking", message.html)
        self.assertIn("Submitted", message.subject)

    def test_an_admin_requester_is_linked_to_the_screen_they_can_open(self):
        payload = {**_payload(), "requester_can_view_directory": True}
        with email_settings():
            self.assertEqual(
                messages.manual_time_requester_url(payload),
                "https://staff.peakworkos.com/admin/time-tracking?tab=requests",
            )

    def test_approved_and_rejected_read_differently(self):
        decided = datetime(2026, 9, 16, 5, 0, tzinfo=timezone.utc).isoformat()
        with email_settings():
            approved = messages.build_manual_time_decision_email(
                {**_payload(), "status": "approved", "decided_at": decided, "reviewer_name": "Asha Admin"},
                ["smit@example.com"],
            )
            rejected = messages.build_manual_time_decision_email(
                {**_payload(), "status": "rejected", "decided_at": decided, "reviewer_name": "Asha Admin"},
                ["smit@example.com"],
            )
        self.assertIn("Your manual time request was approved", approved.html)
        self.assertIn("added to your tracked time", approved.html)
        self.assertIn("Approved", approved.subject)
        self.assertIn("Asha Admin", approved.html)
        self.assertIn("Your manual time request was rejected", rejected.html)
        self.assertIn("has not been added", rejected.html)
        self.assertIn("Rejected", rejected.subject)

    def test_an_unknown_decision_status_is_refused_rather_than_mis_worded(self):
        with email_settings(), self.assertRaises(KeyError):
            messages.build_manual_time_decision_email({**_payload(), "status": "pending"}, ["x@example.com"])

    def test_no_button_for_a_localhost_app_url(self):
        with email_settings(MONITRA_APP_URL="http://localhost:5173"):
            message = messages.build_manual_time_request_email(_payload(), ["admin@example.com"])
        self.assertNotIn("Review Request", message.html)

    def test_both_logos_are_carried(self):
        with email_settings():
            message = messages.build_manual_time_receipt_email(_payload(), ["smit@example.com"])
        self.assertIn("cid:monitra-logo", message.html)
        self.assertIn("cid:store-transform-logo", message.html)

    def test_the_outbox_can_render_every_type_from_its_stored_json(self):
        from app.services.email.messages import BUILDERS

        stored = json.loads(json.dumps({**_payload(), "status": "approved",
                                        "decided_at": "2026-09-16T05:00:00+00:00"}))
        with email_settings():
            for kind in ("manual_time_request", "manual_time_receipt", "manual_time_decision"):
                self.assertIn("Store Revamp", BUILDERS[kind](stored, ["x@example.com"]).html, kind)


# ======================================================================
# 4 — the service never fails over email, and delivers promptly
# ======================================================================

class TestServiceHooks(unittest.TestCase):

    def test_create_schedules_an_immediate_delivery_for_every_queued_email(self):
        from app.services.manual_time_entry import ManualTimeEntryService

        entry = _entry()
        background = MagicMock()
        payload = MagicMock(user_id=None, project_id=3, task_id=9, work_date=date(2026, 9, 15),
                            total_seconds=9000, start_time=entry.start_time, end_time=entry.end_time,
                            description="x", is_billable=None)
        with patch("app.services.manual_time_entry.TaskService.get_task"), \
                patch.object(ManualTimeEntryService, "_resolve_billable", return_value=True), \
                patch.object(ManualTimeEntryService, "_check_no_conflict"), \
                patch("app.services.manual_time_entry.ManualTimeEntryRepository.create", return_value=entry), \
                patch(f"{WORKFLOWS}.queue_manual_time_request_notifications", return_value=[11, 12, 13]):
            result = ManualTimeEntryService.create_manual_entry(MagicMock(), payload, REQUESTER, background)

        self.assertIs(result, entry)
        self.assertEqual([call.args[1] for call in background.add_task.call_args_list], [11, 12, 13])

    def test_approval_schedules_the_decision_email_and_returns_the_decision(self):
        from app.services.manual_time_entry import ManualTimeEntryService

        approver = _user(1, "Asha Admin", "admin@example.com", "administrator",
                         {"manual_time_entries:approve": True})
        entry = _entry()
        decided = _entry(approval_status="rejected")
        background = MagicMock()
        with patch("app.services.manual_time_entry.ManualTimeEntryRepository.get_by_id", return_value=entry), \
                patch("app.services.manual_time_entry.may_view_member", return_value=True), \
                patch("app.services.manual_time_entry.ManualTimeEntryRepository.update_approval_status",
                      return_value=decided), \
                patch(f"{WORKFLOWS}.queue_manual_time_decision_notification", return_value=21) as queue:
            entry.deleted_at = None
            result = ManualTimeEntryService.update_approval(MagicMock(), 55, "rejected", approver, background)

        self.assertIs(result, decided)
        self.assertIs(queue.call_args.kwargs["reviewer"], approver)
        self.assertEqual(background.add_task.call_args.args[1], 21)

    def test_a_request_still_succeeds_when_nothing_could_be_queued(self):
        from app.services.manual_time_entry import ManualTimeEntryService

        entry = _entry()
        payload = MagicMock(user_id=None, project_id=3, task_id=9, work_date=date(2026, 9, 15),
                            total_seconds=9000, start_time=entry.start_time, end_time=entry.end_time,
                            description="x", is_billable=None)
        with patch("app.services.manual_time_entry.TaskService.get_task"), \
                patch.object(ManualTimeEntryService, "_resolve_billable", return_value=True), \
                patch.object(ManualTimeEntryService, "_check_no_conflict"), \
                patch("app.services.manual_time_entry.ManualTimeEntryRepository.create", return_value=entry), \
                patch(APPROVERS, side_effect=RuntimeError("mail is down")):
            self.assertIs(ManualTimeEntryService.create_manual_entry(MagicMock(), payload, REQUESTER), entry)


class TestApproverQuery(unittest.TestCase):
    """The recipient query names exactly the admin and leader roles."""

    def test_the_roles_are_admins_and_leaders_only(self):
        from app.repositories.manual_time_notification import ADMIN_ROLES, LEADER_ROLES

        self.assertEqual(ADMIN_ROLES, {"administrator", "org_admin", "super_admin", "hr", "manager"})
        self.assertEqual(LEADER_ROLES, {"leader", "project_leader"})
        self.assertNotIn("employee", ADMIN_ROLES | LEADER_ROLES)

    def test_every_org_wide_role_that_can_approve_is_notified(self):
        """Anyone who can approve any request in the organisation must be told
        about new ones -- a role added to the approvers must be added here too."""
        from app.core.permissions import ROLE_PERMISSIONS
        from app.repositories.manual_time_notification import ADMIN_ROLES, LEADER_ROLES

        approvers = {role for role, perms in ROLE_PERMISSIONS.items()
                     if "manual_time_entries:approve" in perms}
        self.assertEqual(approvers - LEADER_ROLES, set(ADMIN_ROLES))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

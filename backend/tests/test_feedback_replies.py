"""What an administrator said to the employee shows in the feedback View dialog.

Pressing Resolved on the Feedback page lets the administrator write a note to
the employee. It has always gone out in the status email; it used to be shown
nowhere in the app afterwards, so the administrator who wrote it -- or HR and a
leader reading the same page -- could not see what the employee had been told.

The note is kept once, in the queued email's payload, and the list reads it back
from there (`FeedbackService._replies_for`) instead of copying it onto the
feedback row, where it could disagree with what was sent. These tests run the
real writer (`update_status` -> `queue_feedback_status_notification`) and the
real reader on one in-memory database, so a drift between the key one writes
and the key the other reads is a failing test and not a blank dialog.
"""
import json
import unittest
from unittest.mock import patch

from sqlalchemy import event, select

from app.models.email_notification import STATUS_CANCELLED, EmailNotification
from app.models.feedback_request import FeedbackRequest
from app.schemas.feedback import (
    FeedbackItem, FeedbackListResponse, FeedbackReplyRead, FeedbackStatusAction,
)
from app.services.email.workflows import (
    FEEDBACK_STATUS_NOTE_KEY, feedback_status_dedupe_key,
)
from app.services.feedback import FeedbackService
from tests.test_feedback_attachments import (
    ADMIN, ALICE, BOB, HR, ORG, OTHER_ORG, OUTSIDER, _add_user, _database,
)

RESOLVED = FeedbackStatusAction.resolved
WORKING = FeedbackStatusAction.in_progress


class ReplyCase(unittest.TestCase):
    def setUp(self):
        self.db = _database()
        self.admin = _add_user(self.db, ADMIN, "administrator", "Grace")
        self.hr = _add_user(self.db, HR, "hr", "Hana")
        self.alice = _add_user(self.db, ALICE, "employee", "Alice")
        self.bob = _add_user(self.db, BOB, "employee", "Bob")
        self.outsider = _add_user(self.db, OUTSIDER, "administrator", "Zed", org=OTHER_ORG)
        for user in (self.admin, self.hr, self.alice, self.bob, self.outsider):
            # Deliverable addresses: `.invalid` is refused by the recipient rules.
            user.email = f"{user.name.lower()}@example.com"
        self.db.commit()
        self.addCleanup(self.db.close)

    def feedback(self, user, message="The timer resets after sleep."):
        row = FeedbackRequest(
            organization_id=user.organization_id, user_id=user.id,
            category="report_a_problem", message=message, status="new",
        )
        self.db.add(row)
        self.db.commit()
        return row

    def move(self, feedback, action, note=None):
        return FeedbackService.update_status(
            self.db, self.admin, feedback.id, action, message=note,
        )

    def listed(self, user=None):
        data = FeedbackService.list_all_feedback(self.db, user or self.admin, 1, 100)
        return {item["id"]: item for item in data["items"]}


class AdminNoteIsShownTests(ReplyCase):
    def test_a_resolved_note_comes_back_with_the_feedback(self):
        row = self.feedback(self.alice)
        self.move(row, RESOLVED, "Fixed in 1.3.2 -- thanks for flagging it.")

        replies = self.listed()[row.id]["replies"]

        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0]["message"], "Fixed in 1.3.2 -- thanks for flagging it.")
        self.assertEqual(replies[0]["status"], "resolved")
        self.assertIsNotNone(replies[0]["created_at"])

    def test_the_response_to_the_press_already_carries_it(self):
        row = self.feedback(self.alice)

        result = self.move(row, RESOLVED, "Sorted.")

        self.assertEqual([r["message"] for r in result["replies"]], ["Sorted."])

    def test_a_resolved_feedback_with_no_note_has_no_reply(self):
        row = self.feedback(self.alice)
        self.move(row, RESOLVED)

        self.assertEqual(self.listed()[row.id]["replies"], [])

    def test_a_note_of_only_whitespace_is_no_note(self):
        row = self.feedback(self.alice)
        self.move(row, RESOLVED, "   \n  ")

        self.assertEqual(self.listed()[row.id]["replies"], [])

    def test_a_feedback_nobody_replied_to_has_none(self):
        row = self.feedback(self.alice)

        self.assertEqual(self.listed()[row.id]["replies"], [])

    def test_each_feedback_gets_only_its_own_note(self):
        first, second, third = (self.feedback(self.alice), self.feedback(self.bob), self.feedback(self.alice))
        self.move(first, RESOLVED, "Note for the first.")
        self.move(third, RESOLVED, "Note for the third.")

        listed = self.listed()

        self.assertEqual([r["message"] for r in listed[first.id]["replies"]], ["Note for the first."])
        self.assertEqual(listed[second.id]["replies"], [])
        self.assertEqual([r["message"] for r in listed[third.id]["replies"]], ["Note for the third."])

    def test_working_then_resolved_reads_in_the_order_it_happened(self):
        row = self.feedback(self.alice)
        self.move(row, WORKING, "Looking into it now.")
        self.move(row, RESOLVED, "All done.")

        replies = self.listed()[row.id]["replies"]

        self.assertEqual([(r["status"], r["message"]) for r in replies],
                         [("in_progress", "Looking into it now."), ("resolved", "All done.")])

    def test_pressing_resolved_again_does_not_add_a_second_note(self):
        row = self.feedback(self.alice)
        self.move(row, RESOLVED, "First and only.")
        again = self.move(row, RESOLVED, "A second message that was never sent.")

        self.assertFalse(again["notification_queued"])
        self.assertEqual([r["message"] for r in again["replies"]], ["First and only."])
        self.assertEqual([r["message"] for r in self.listed()[row.id]["replies"]], ["First and only."])


class WhoCanSeeIt(ReplyCase):
    def test_hr_reading_the_same_list_sees_the_note(self):
        row = self.feedback(self.alice)
        self.move(row, RESOLVED, "Done.")

        self.assertEqual([r["message"] for r in self.listed(self.hr)[row.id]["replies"]], ["Done."])

    def test_the_employee_sees_the_note_they_were_sent_on_their_own_list(self):
        row = self.feedback(self.alice)
        self.move(row, RESOLVED, "Done.")

        mine = FeedbackService.list_my_feedback(self.db, self.alice)["items"]
        single = FeedbackService.get_my_feedback(self.db, self.alice, row.id)

        self.assertEqual([r["message"] for r in mine[0]["replies"]], ["Done."])
        self.assertEqual([r["message"] for r in single["replies"]], ["Done."])

    def test_another_organizations_administrator_sees_neither_the_feedback_nor_the_note(self):
        row = self.feedback(self.alice)
        self.move(row, RESOLVED, "Internal detail.")

        self.assertEqual(self.listed(self.outsider), {})

    def test_the_reply_names_no_administrator(self):
        self.assertEqual(set(FeedbackReplyRead.model_fields), {"message", "status", "created_at"})
        row = self.feedback(self.alice)
        self.move(row, RESOLVED, "Done.")
        self.assertNotIn("Grace", json.dumps(self.listed()[row.id]["replies"], default=str))


class WhatIsNotShown(ReplyCase):
    def test_a_note_that_was_never_delivered_because_it_was_cancelled_is_not_shown(self):
        row = self.feedback(self.alice)
        self.move(row, RESOLVED, "Superseded.")
        outbox = self.db.scalar(select(EmailNotification))
        outbox.status = STATUS_CANCELLED
        self.db.commit()

        self.assertEqual(self.listed()[row.id]["replies"], [])

    def test_an_unreadable_payload_is_skipped_and_the_list_still_loads(self):
        row = self.feedback(self.alice)
        self.move(row, RESOLVED, "Readable.")
        broken = self.feedback(self.bob)
        self.move(broken, RESOLVED, "Will be corrupted.")
        outbox = self.db.scalar(
            select(EmailNotification).where(
                EmailNotification.dedupe_key == feedback_status_dedupe_key(broken.id, "resolved")
            )
        )
        outbox.payload = "this is not json"
        self.db.commit()

        listed = self.listed()

        self.assertEqual([r["message"] for r in listed[row.id]["replies"]], ["Readable."])
        self.assertEqual(listed[broken.id]["replies"], [])

    def test_the_other_emails_in_the_outbox_are_not_mistaken_for_replies(self):
        row = self.feedback(self.alice)
        # The admin/HR notification about the submission itself, same feedback id.
        self.db.add(EmailNotification(
            notification_type="feedback", dedupe_key=f"feedback:{row.id}",
            recipients="[]", subject="s", status="pending", max_attempts=1,
            payload=json.dumps({FEEDBACK_STATUS_NOTE_KEY: "not a reply"}),
        ))
        self.db.commit()

        self.assertEqual(self.listed()[row.id]["replies"], [])


class CostAndShape(ReplyCase):
    def test_a_page_of_feedback_costs_one_query_for_notes_however_many_rows(self):
        rows = [self.feedback(self.alice if i % 2 else self.bob) for i in range(12)]
        for row in rows[:5]:
            self.move(row, RESOLVED, "Done.")

        seen = []

        def record(conn, cursor, statement, *rest):
            if "email_notifications" in statement.lower():
                seen.append(statement)

        engine = self.db.get_bind()
        event.listen(engine, "before_cursor_execute", record)
        try:
            self.listed()
        finally:
            event.remove(engine, "before_cursor_execute", record)

        self.assertEqual(len(seen), 1, seen)

    def test_the_response_models_carry_replies_and_default_to_none(self):
        item = FeedbackItem.model_validate({
            "id": 1, "employee_id": 2, "employee_name": "Alice", "category": "other",
            "message": "m", "created_at": "2026-09-01T10:00:00Z",
        })
        self.assertEqual(item.replies, [])
        row = self.feedback(self.alice)
        self.move(row, RESOLVED, "Done.")
        envelope = FeedbackListResponse.model_validate(
            FeedbackService.list_all_feedback(self.db, self.admin, 1, 20)
        )
        self.assertEqual(envelope.items[0].replies[0].message, "Done.")
        self.assertEqual(envelope.items[0].replies[0].status, FeedbackStatusAction.resolved)

    def test_the_writer_and_the_reader_agree_on_where_the_note_lives(self):
        row = self.feedback(self.alice)
        self.move(row, RESOLVED, "Here.")
        outbox = self.db.scalar(select(EmailNotification))

        self.assertEqual(json.loads(outbox.payload)[FEEDBACK_STATUS_NOTE_KEY], "Here.")
        self.assertEqual(outbox.dedupe_key, feedback_status_dedupe_key(row.id, "resolved"))


if __name__ == "__main__":
    unittest.main()

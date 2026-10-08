"""Emails to a member when an administrator moves their Login / Add Task switch,
and the live headcounts shown beside those two columns.

Four things have to hold in production, and every case belongs to one of them:

1. **Exactly the right mail, exactly once.** A switch that moved queues one
   email to that member and to nobody else; pressing a switch that is already
   there queues nothing; excluded -> allowed -> excluded again is three emails,
   not one; a retry of the same transition cannot send twice.
2. **Never the admin's problem.** The change is committed whether or not the
   email can be queued, and nothing here ever raises into the request.
3. **Content.** Each of the four wordings says what the switch really does,
   names nobody but the recipient, and escapes what it is given.
4. **Counts.** The numbers are computed over active members in the caller's own
   scope, in one query, and an explicit False is the only thing that excludes.

No test sends a real message: delivery is only ever *scheduled* here.
"""
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from fastapi import BackgroundTasks
from fastapi.testclient import TestClient
from sqlalchemy import BigInteger, create_engine, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.config import settings
from app.core.database import Base, get_db
from app.core.permissions import ROLE_PERMISSIONS
from app.core.security import get_current_user
from app.main import app
from app.models.activity_log import ActivityLog
from app.models.email_notification import (
    STATUS_CANCELLED, STATUS_PENDING, TYPE_MEMBER_ACCESS, EmailNotification,
)
from app.models.user import User
from app.repositories.member import MemberRepository
from app.schemas.member import MemberAccessUpdate, MemberUpdate
from app.services.email import messages
from app.services.email.outbox import deliver_in_background
from app.services.email.workflows import (
    member_access_dedupe_key, queue_member_access_notification,
)
from app.services.member_service import MemberService

WORKFLOWS = "app.services.email.workflows"
UTC = timezone.utc
T0 = datetime(2026, 10, 2, 9, 30, tzinfo=UTC)


@compiles(JSONB, "sqlite")
def _jsonb_is_json_on_sqlite(type_, compiler, **kw):  # pragma: no cover - dialect shim
    return "JSON"


@compiles(BigInteger, "sqlite")
def _bigint_is_integer_on_sqlite(type_, compiler, **kw):  # pragma: no cover - dialect shim
    return "INTEGER"


def email_settings(**overrides):
    defaults = {
        "EMAIL_PROVIDER": "smtp",
        "EMAIL_FROM_ADDRESS": "monitra@example.com",
        "EMAIL_FROM_NAME": "Monitra",
        "EMAIL_REPLY_TO": "",
        "SMTP_HOST": "smtp.example.com",
        "MONITRA_APP_URL": "https://staff.example.com",
        "MONITRA_SUPPORT_EMAIL": "",
        "EMAIL_ASSET_BASE_URL": "",
        "ACCESS_CHANGE_EMAIL_ENABLED": True,
    }
    defaults.update(overrides)
    return patch.multiple(settings, **defaults)


def _payload(switch, allowed, **overrides):
    return {
        "user_id": 42, "name": "Priya Raman", "switch": switch, "allowed": allowed,
        "changed_at": T0.isoformat(), **overrides,
    }


# ----------------------------------------------------------------------
# 3. Content
# ----------------------------------------------------------------------

class ContentTests(unittest.TestCase):
    def build(self, switch, allowed, **overrides):
        with email_settings(**overrides.pop("settings", {})):
            return messages.build_member_access_email(_payload(switch, allowed, **overrides), ["priya@example.com"])

    def test_every_switch_and_state_has_its_own_subject(self):
        subjects = {
            (switch, allowed): messages.member_access_subject(_payload(switch, allowed))
            for switch in ("login", "add_tasks") for allowed in (True, False)
        }
        self.assertEqual(len(set(subjects.values())), 4)
        self.assertIn("Excluded from Logging In", subjects[("login", False)])
        self.assertIn("Log In Again", subjects[("login", True)])
        self.assertIn("Excluded from Adding Tasks", subjects[("add_tasks", False)])
        self.assertIn("Add Tasks Again", subjects[("add_tasks", True)])

    def test_excluded_login_says_what_exclusion_does(self):
        email = self.build("login", False)
        for fragment in ("excluded your account from logging in", "signed out", "timer", "contact your administrator"):
            self.assertIn(fragment, email.text)
            self.assertIn(fragment, email.html)
        self.assertIn("Excluded", email.html)

    def test_allowed_login_says_the_issue_is_resolved(self):
        email = self.build("login", True)
        self.assertIn("allowed your account to log in", email.text)
        self.assertIn("resolved", email.text)
        self.assertIn("Allowed", email.html)

    def test_excluded_add_task_only_withdraws_adding_tasks(self):
        email = self.build("add_tasks", False)
        self.assertIn("excluded your account from adding tasks", email.text)
        # The honest scope of the switch: sign-in and tracking are untouched.
        self.assertIn("You can still sign in", email.text)

    def test_allowed_add_task_says_the_issue_is_resolved(self):
        email = self.build("add_tasks", True)
        self.assertIn("allowed your account to add tasks", email.text)
        self.assertIn("resolved", email.text)

    def test_allowed_add_task_has_no_button(self):
        email = self.build("add_tasks", True)
        self.assertNotIn("Open Monitra", email.html)
        self.assertNotIn("Open Monitra", email.text)
        self.assertNotIn("/login", email.html)

    def test_the_sign_in_button_only_exists_when_allowed_and_the_url_is_https(self):
        self.assertIn("https://staff.example.com/login", self.build("login", True).html)
        self.assertIn("https://staff.example.com/login", self.build("login", True).text)
        # An exclusion has nowhere to send anybody.
        self.assertNotIn("/login", self.build("login", False).html)
        self.assertNotIn("/login", self.build("add_tasks", False).html)
        # A localhost URL resolves on the reader's machine: no button at all.
        local = self.build("login", True, settings={"MONITRA_APP_URL": "http://localhost:5173"})
        self.assertNotIn("localhost", local.html)
        self.assertNotIn("localhost", local.text)

    def test_the_greeting_uses_the_first_name_and_is_escaped(self):
        self.assertIn("Hi Priya,", self.build("login", True).text)
        hostile = self.build("login", True, name='<script>alert(1)</script> Mallory')
        self.assertNotIn("<script>", hostile.html)

    def test_the_time_is_shown_in_ist(self):
        # 09:30 UTC is 3:00 PM in Asia/Kolkata.
        email = self.build("login", False)
        self.assertIn("3:00 PM IST", email.text)
        self.assertIn("02 October 2026", email.text)

    def test_no_administrator_is_named_and_the_support_address_is_shown_when_set(self):
        email = self.build("login", False, settings={"MONITRA_SUPPORT_EMAIL": "help@example.com"})
        self.assertIn("help@example.com", email.text)
        self.assertNotIn("sender", json.dumps(_payload("login", False)))

    def test_an_unknown_pair_is_refused_rather_than_reworded(self):
        with self.assertRaises(KeyError):
            messages.member_access_subject(_payload("password", True))

    def test_the_outbox_can_render_it(self):
        self.assertIs(messages.BUILDERS[TYPE_MEMBER_ACCESS], messages.build_member_access_email)


# ----------------------------------------------------------------------
# 1. Identity of a transition
# ----------------------------------------------------------------------

class DedupeKeyTests(unittest.TestCase):
    def test_the_same_transition_always_computes_the_same_key(self):
        self.assertEqual(
            member_access_dedupe_key(42, "login", False, T0), member_access_dedupe_key(42, "login", False, T0),
        )

    def test_a_later_transition_is_a_different_event_even_in_the_same_state(self):
        # Excluded, allowed, excluded again: the second exclusion must be sent.
        self.assertNotEqual(
            member_access_dedupe_key(42, "login", False, T0),
            member_access_dedupe_key(42, "login", False, T0 + timedelta(minutes=5)),
        )

    def test_user_switch_and_state_are_all_part_of_the_identity(self):
        base = member_access_dedupe_key(42, "login", False, T0)
        self.assertNotEqual(base, member_access_dedupe_key(43, "login", False, T0))
        self.assertNotEqual(base, member_access_dedupe_key(42, "add_tasks", False, T0))
        self.assertNotEqual(base, member_access_dedupe_key(42, "login", True, T0))

    def test_a_naive_database_timestamp_is_read_as_utc(self):
        self.assertEqual(
            member_access_dedupe_key(42, "login", False, T0.replace(tzinfo=None)),
            member_access_dedupe_key(42, "login", False, T0),
        )

    def test_the_key_fits_the_column(self):
        self.assertLess(len(member_access_dedupe_key(10**12, "add_tasks", False, T0)), 120)


# ----------------------------------------------------------------------
# 1 + 2. The queueing entry point
# ----------------------------------------------------------------------

def _member(**overrides):
    member = MagicMock()
    member.id = overrides.get("id", 42)
    member.name = overrides.get("name", "Priya Raman")
    member.email = overrides.get("email", "priya@example.com")
    member.organization_id = 7
    member.is_active = overrides.get("is_active", True)
    return member


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.enqueue = patch(f"{WORKFLOWS}.EmailOutboxService.enqueue", return_value=MagicMock(id=900, status="pending")).start()
        self.cancel = patch(f"{WORKFLOWS}.EmailNotificationRepository.cancel_pending_by_key_prefix", return_value=0).start()
        self.addCleanup(patch.stopall)

    def queue(self, member=None, **kw):
        kw.setdefault("switch", "login")
        kw.setdefault("allowed", False)
        kw.setdefault("changed_at", T0)
        with email_settings():
            return queue_member_access_notification(MagicMock(), member or _member(), **kw)

    def test_it_queues_one_email_to_the_member_alone(self):
        self.assertEqual(self.queue(), 900)
        kwargs = self.enqueue.call_args.kwargs
        self.assertEqual(kwargs["notification_type"], TYPE_MEMBER_ACCESS)
        self.assertEqual(kwargs["recipients"], ["priya@example.com"])
        self.assertEqual(kwargs["user_id"], 42)
        self.assertEqual(kwargs["organization_id"], 7)
        self.assertEqual(kwargs["dedupe_key"], member_access_dedupe_key(42, "login", False, T0))

    def test_the_payload_holds_only_what_the_email_shows(self):
        self.queue(switch="add_tasks", allowed=True)
        payload = self.enqueue.call_args.kwargs["payload"]
        self.assertEqual(
            set(payload), {"user_id", "name", "switch", "allowed", "changed_at"},
        )
        self.assertEqual((payload["switch"], payload["allowed"]), ("add_tasks", True))

    def test_allowing_withdraws_a_still_pending_exclusion_and_vice_versa(self):
        self.queue(switch="login", allowed=True)
        self.assertEqual(
            self.cancel.call_args.kwargs,
            {"notification_type": TYPE_MEMBER_ACCESS, "key_prefix": "access:42:login:excluded:"},
        )
        self.queue(switch="add_tasks", allowed=False)
        self.assertEqual(self.cancel.call_args.kwargs["key_prefix"], "access:42:add_tasks:allowed:")

    def test_nothing_is_queued_when_the_feature_is_off(self):
        with email_settings(ACCESS_CHANGE_EMAIL_ENABLED=False):
            self.assertIsNone(queue_member_access_notification(
                MagicMock(), _member(), switch="login", allowed=False, changed_at=T0,
            ))
        self.enqueue.assert_not_called()

    def test_a_deactivated_account_is_not_written_to(self):
        self.assertIsNone(self.queue(_member(is_active=False)))
        self.enqueue.assert_not_called()

    def test_a_member_without_a_usable_address_is_skipped_not_failed(self):
        self.assertIsNone(self.queue(_member(email="not-an-address")))
        self.assertIsNone(self.queue(_member(email="")))
        self.enqueue.assert_not_called()

    def test_a_failing_queue_never_raises_into_the_request(self):
        self.enqueue.side_effect = RuntimeError("database went away")
        self.assertIsNone(self.queue())
        self.cancel.side_effect = RuntimeError("database went away")
        self.assertIsNone(self.queue())

    def test_an_unknown_switch_is_refused_quietly(self):
        self.assertIsNone(self.queue(switch="password"))
        self.enqueue.assert_not_called()


# ----------------------------------------------------------------------
# 1 + 2. The whole path on a real SQLite session
# ----------------------------------------------------------------------

TABLES = [
    Base.metadata.tables["organizations"], User.__table__, EmailNotification.__table__, ActivityLog.__table__,
]


def _session() -> Session:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    stripped = []
    for table in TABLES:
        for column in table.columns:
            default = column.server_default
            if default is not None and "::" in str(getattr(default, "arg", "")):
                stripped.append((column, default))
                column.server_default = None
    try:
        Base.metadata.create_all(engine, tables=TABLES)
    finally:
        for column, default in stripped:
            column.server_default = default
    return Session(engine)


def _add_user(db, user_id, role="employee", **overrides):
    user = User(
        id=user_id, organization_id=1, username=f"u{user_id}", email=f"u{user_id}@example.com",
        name=f"User {user_id}", role_name=role,
        permissions={p: True for p in ROLE_PERMISSIONS.get(role, set())},
        is_active=True, idle_enabled=True, idle_minutes=5, capture_frequency=10, status="active",
        can_login=True, can_add_tasks=True, created_at=T0, updated_at=T0,
    )
    for key, value in overrides.items():
        setattr(user, key, value)
    db.add(user)
    db.commit()
    return user


class ServiceTests(unittest.TestCase):
    """`MemberService.update` / `update_access` against real rows."""

    def setUp(self):
        self.db = _session()
        self.addCleanup(self.db.close)
        self.admin = _add_user(self.db, 1, "administrator")
        self.member = _add_user(self.db, 10)
        self.tasks = BackgroundTasks()

        # SQLite's CURRENT_TIMESTAMP has one-second resolution, where Postgres'
        # now() has microseconds. Stamp each save one minute after the last so
        # two transitions inside one test second are still two moments, as they
        # always are in production.
        self.clock = T0
        real_save = MemberRepository.save

        def stamped_save(db, member, data):
            saved = real_save(db, member, data)
            self.clock += timedelta(minutes=1)
            saved.updated_at = self.clock
            return saved

        patch("app.services.member_service.MemberRepository.save", side_effect=stamped_save).start()
        patch("app.repositories.time_entry.TimeEntryRepository.get_active_for_user", return_value=None).start()
        patch("app.services.auth.AuthService.revoke_all_sessions", return_value=0).start()
        email_patch = email_settings()
        email_patch.start()
        self.addCleanup(patch.stopall)
        self.addCleanup(email_patch.stop)

    def outbox(self):
        self.db.expire_all()
        rows = self.db.scalars(select(EmailNotification).order_by(EmailNotification.id)).all()
        return [(r.dedupe_key, json.loads(r.payload), r.status, json.loads(r.recipients)) for r in rows]

    def update(self, member_id=10, **fields):
        return MemberService.update(
            self.db, self.admin, member_id, MemberUpdate(**fields), background_tasks=self.tasks,
        )

    def test_excluding_login_emails_that_member_and_schedules_delivery(self):
        self.update(can_login=False)
        rows = self.outbox()
        self.assertEqual(len(rows), 1)
        _key, payload, status, recipients = rows[0]
        self.assertEqual((payload["switch"], payload["allowed"], status), ("login", False, STATUS_PENDING))
        self.assertEqual(recipients, ["u10@example.com"])
        self.assertEqual([t.func for t in self.tasks.tasks], [deliver_in_background])

    def test_allowing_and_excluding_add_task_each_email(self):
        self.update(can_add_tasks=False)
        self.update(can_add_tasks=True)
        self.assertEqual([(p["switch"], p["allowed"]) for _k, p, _s, _r in self.outbox()],
                         [("add_tasks", False), ("add_tasks", True)])

    def test_pressing_the_position_it_already_holds_sends_nothing(self):
        self.update(can_login=True)
        self.update(can_add_tasks=True)
        self.assertEqual(self.outbox(), [])
        self.assertEqual(self.tasks.tasks, [])

    def test_a_double_click_sends_one_email(self):
        self.update(can_login=False)
        self.update(can_login=False)
        self.assertEqual(len(self.outbox()), 1)
        self.assertEqual(len(self.tasks.tasks), 1)

    def test_excluded_allowed_excluded_again_is_three_emails(self):
        for allowed in (False, True, False):
            self.update(can_login=allowed)
        rows = self.outbox()
        self.assertEqual([p["allowed"] for _k, p, _s, _r in rows], [False, True, False])
        self.assertEqual(len({key for key, *_ in rows}), 3)

    def test_allowing_withdraws_a_exclusion_that_has_not_gone_out_yet(self):
        self.update(can_login=False)
        self.update(can_login=True)
        self.assertEqual([(p["allowed"], s) for _k, p, s, _r in self.outbox()],
                         [(False, STATUS_CANCELLED), (True, STATUS_PENDING)])

    def test_the_two_switches_do_not_withdraw_each_other(self):
        self.update(can_login=False)
        self.update(can_add_tasks=True)  # no-op
        self.update(can_add_tasks=False)
        self.assertEqual([s for _k, _p, s, _r in self.outbox()], [STATUS_PENDING, STATUS_PENDING])

    def test_both_switches_in_one_request_send_one_email_each(self):
        self.update(can_login=False, can_add_tasks=False)
        self.assertEqual(sorted(p["switch"] for _k, p, _s, _r in self.outbox()), ["add_tasks", "login"])
        self.assertEqual(len(self.tasks.tasks), 2)

    def test_editing_anything_else_sends_no_access_email(self):
        self.update(designation="Lead")
        self.assertEqual(self.outbox(), [])

    def test_an_admin_excluding_themselves_is_refused_and_sends_nothing(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException):
            self.update(member_id=1, can_login=False)
        self.assertEqual(self.outbox(), [])

    def test_a_deactivated_member_is_changed_but_not_emailed(self):
        self.member.status = "inactive"
        self.member.is_active = False
        self.db.commit()
        self.update(can_add_tasks=False)
        self.assertFalse(self.db.get(User, 10).can_add_tasks)
        self.assertEqual(self.outbox(), [])

    def test_the_change_stands_when_the_email_cannot_be_queued(self):
        with patch(f"{WORKFLOWS}.EmailOutboxService.enqueue", side_effect=RuntimeError("smtp table gone")):
            saved = self.update(can_login=False)
        self.assertFalse(saved.can_login)
        self.assertFalse(self.db.get(User, 10).can_login)

    def test_it_works_without_a_background_task_runner(self):
        MemberService.update(self.db, self.admin, 10, MemberUpdate(can_login=False))
        self.assertEqual(len(self.outbox()), 1)

    def test_a_bulk_change_emails_exactly_the_members_that_moved(self):
        _add_user(self.db, 11)
        _add_user(self.db, 12, can_add_tasks=False)  # already excluded
        result = MemberService.update_access(
            self.db, self.admin, MemberAccessUpdate(member_ids=[10, 11, 12], can_add_tasks=False),
            background_tasks=self.tasks,
        )
        self.assertEqual([m.id for m in result["updated"]], [10, 11, 12])
        mailed = sorted(r[3][0] for r in self.outbox())
        self.assertEqual(mailed, ["u10@example.com", "u11@example.com"])
        self.assertEqual(len(self.tasks.tasks), 2)

    def test_a_refused_member_in_a_bulk_change_is_not_emailed(self):
        result = MemberService.update_access(
            self.db, self.admin, MemberAccessUpdate(member_ids=[1, 10], can_login=False),
            background_tasks=self.tasks,
        )
        self.assertEqual([f["id"] for f in result["failed"]], [1])
        self.assertEqual([r[3][0] for r in self.outbox()], ["u10@example.com"])


# ----------------------------------------------------------------------
# 4. Counts
# ----------------------------------------------------------------------

class CountTests(unittest.TestCase):
    def setUp(self):
        self.db = _session()
        self.addCleanup(self.db.close)
        _add_user(self.db, 1, "administrator")
        _add_user(self.db, 2)                                              # both allowed
        _add_user(self.db, 3, can_add_tasks=False)                         # login only
        _add_user(self.db, 4, can_login=False)                             # add task only
        _add_user(self.db, 5, can_login=False, can_add_tasks=False)        # neither
        _add_user(self.db, 6, status="inactive", is_active=False)          # deactivated: not counted
        other = _add_user(self.db, 7)
        other.organization_id = 2                                          # another organization
        self.db.commit()

    def counts(self, ids=None):
        return MemberRepository.access_counts(self.db, 1, ids)

    def test_counts_active_members_who_are_allowed(self):
        self.assertEqual(self.counts(), {"add_task_allowed": 3, "login_allowed": 3, "active_members": 5, "add_nonbillable_task_allowed": 0})

    def test_a_deactivated_member_is_not_counted_whatever_their_switch_says(self):
        # Member 6 is deactivated with both switches on: 6 rows in the
        # organization, 5 of them active, and 3 allowed on each switch.
        self.assertEqual(self.counts()["active_members"], 5)
        self.assertEqual(self.counts()["login_allowed"], 3)

    def test_a_scoped_caller_is_counted_over_their_own_set_only(self):
        self.assertEqual(self.counts({2, 3}), {"add_task_allowed": 1, "login_allowed": 2, "active_members": 2, "add_nonbillable_task_allowed": 0})

    def test_an_empty_scope_is_zero_not_everyone(self):
        self.assertEqual(self.counts(set()), {"add_task_allowed": 0, "login_allowed": 0, "active_members": 0, "add_nonbillable_task_allowed": 0})

    def test_the_numbers_follow_a_switch_at_once(self):
        member = self.db.get(User, 2)
        member.can_add_tasks = False
        self.db.commit()
        self.assertEqual(self.counts()["add_task_allowed"], 2)
        member.can_add_tasks = True
        self.db.commit()
        self.assertEqual(self.counts()["add_task_allowed"], 3)

    def test_it_is_one_query(self):
        statements = []
        from sqlalchemy import event
        event.listen(self.db.get_bind(), "before_cursor_execute", lambda *a: statements.append(a[2]))
        self.counts()
        self.assertEqual(len(statements), 1)


class AccessSummaryRouteTests(unittest.TestCase):
    def setUp(self):
        self.user = User()
        self.user.id, self.user.organization_id, self.user.role_name = 1, 1, "administrator"
        self.user.permissions = {p: True for p in ROLE_PERMISSIONS["administrator"]}
        self.user.is_active = True
        app.dependency_overrides[get_current_user] = lambda: self.user
        app.dependency_overrides[get_db] = lambda: None
        self.client = TestClient(app)
        self.addCleanup(app.dependency_overrides.clear)

    def test_it_serves_the_counts_and_is_not_read_as_a_member_id(self):
        counts = {"add_task_allowed": 20, "add_nonbillable_task_allowed": 4, "login_allowed": 15, "active_members": 25}
        with patch("app.api.members.MemberService.access_summary", return_value=counts) as summary:
            response = self.client.get("/api/v1/members/access-summary")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), counts)
        summary.assert_called_once()

    def test_a_role_without_view_employees_is_refused(self):
        self.user.role_name = "employee"
        self.user.permissions = {p: True for p in ROLE_PERMISSIONS["employee"]}
        with patch("app.api.members.MemberService.access_summary") as summary:
            response = self.client.get("/api/v1/members/access-summary")
        self.assertEqual(response.status_code, 403)
        summary.assert_not_called()

    def test_the_bulk_route_hands_the_service_a_background_runner(self):
        with patch("app.api.members.MemberService.update_access", return_value={"updated": [], "failed": []}) as update:
            response = self.client.patch("/api/v1/members/access", json={"member_ids": [10], "can_login": False})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsInstance(update.call_args.kwargs["background_tasks"], BackgroundTasks)


if __name__ == "__main__":
    unittest.main()

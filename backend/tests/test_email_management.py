"""Email Management: who is copied on which email.

What is pinned here, and why each matters:

* **Deploying it changes no email.** With nothing saved, the policy is the
  configured standing CC (`EMAIL_CC_ADDRESSES`) on every email that may be
  copied -- exactly what was sent before.
* **A copy is a decision, per address and per email.** An address is copied on an
  email only when that email is switched on for it. Switching one off leaves the
  address copied on the others; removing an address copies it on nothing.
* **The two emails carrying a private link are never copied** -- not by default, not
  by a policy someone crafts, not by the schema, and not at send time.
* **The send reads the current policy**, at send time, through the real outbox path,
  and a policy that cannot be read is a delivery failure that is retried, not a send
  to the wrong people.
* **Only an administrator reads or changes it, and the backend enforces it.**
* **Rejected, not scrubbed.** A bad address, a duplicate, an email that does not
  exist or one that must not be copied is a 422 -- never quietly dropped.
* **Idempotent and audited**, against a real (SQLite) database; a damaged row cannot
  stop an email.
"""
import email
import json
import smtplib
import unittest
from unittest.mock import MagicMock, patch

from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import BigInteger, create_engine, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.config import settings
from app.core.database import Base, get_db
from app.core import email_catalogue
from app.core.email_catalogue import (
    MAX_CC_ADDRESSES, EmailRegistryError, cc_allowed_keys, check_registry, email_triggers, register_email,
    trigger_keys,
)
from app.core.permissions import ROLE_PERMISSIONS
from app.core.security import get_current_user
from app.main import app
from app.models.activity_log import ActivityLog, ActivityLogAction, ActivityLogModule
from app.models.email_notification import STATUS_PENDING, STATUS_SENT, EmailNotification
from app.models.system_setting import SystemSetting, SystemSettingKey
from app.models.user import User
from app.schemas.email_cc import EmailCcUpdate
from app.services.email import outbox
from app.services.email.messages import BUILDERS
from app.services.email.provider import OutgoingEmail, build_mime_message
from app.services.email.workflows import queue_welcome_email
from app.services.email_cc import EMAIL_CC_MANAGE_ROLES, EmailCcService as Service

ORG = 7
STANDING = ["bharat@storetransform.com", "projectmanager663@gmail.com", "hr@storetransform.com", "piyush@storetransform.com"]
EXEMPT = ["client_invitation", "client_login_link"]
EMAIL_TRIGGERS = email_triggers()
TRIGGER_KEYS = trigger_keys()
CC_ALLOWED_KEYS = cc_allowed_keys()


@compiles(JSONB, "sqlite")
def _jsonb_is_json_on_sqlite(type_, compiler, **kw):  # pragma: no cover - dialect shim
    return "JSON"


@compiles(BigInteger, "sqlite")
def _bigint_is_integer_on_sqlite(type_, compiler, **kw):  # pragma: no cover - dialect shim
    return "INTEGER"


def _admin(role="administrator", user_id=1, username="ada"):
    user = User()
    user.id = user_id
    user.organization_id = ORG
    user.username = username
    user.role_name = role
    user.permissions = {p: True for p in ROLE_PERMISSIONS.get(role, set())}
    user.is_active = True
    return user


def _sqlite_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    tables = [SystemSetting.__table__, ActivityLog.__table__, User.__table__]
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


def _update(*addresses):
    """`_update(("a@b.com", ["welcome"]), ...)`."""
    return EmailCcUpdate(addresses=[{"email": e, "triggers": list(t)} for e, t in addresses])


def _audit(db):
    db.expire_all()
    return list(db.scalars(select(ActivityLog).order_by(ActivityLog.id)).all())


def _env(**overrides):
    return patch.multiple(settings, EMAIL_CC_ADDRESSES=",".join(STANDING), **overrides)


# ── The catalogue ───────────────────────────────────────────────────────────


class CatalogueTests(unittest.TestCase):

    def test_every_email_is_numbered_in_registration_order(self):
        self.assertEqual([t.number for t in EMAIL_TRIGGERS], list(range(1, len(EMAIL_TRIGGERS) + 1)))
        self.assertEqual(len(set(TRIGGER_KEYS)), len(EMAIL_TRIGGERS))
        self.assertGreaterEqual(len(EMAIL_TRIGGERS), 15)

    def test_only_the_two_private_link_emails_are_not_copyable(self):
        self.assertEqual([t.key for t in EMAIL_TRIGGERS if not t.cc_allowed], EXEMPT)
        self.assertEqual(set(CC_ALLOWED_KEYS), set(TRIGGER_KEYS) - set(EXEMPT))

    def test_every_queued_email_is_listed_and_every_listed_email_exists(self):
        # The two private-link emails are sent immediately, never queued, so they
        # have no outbox builder; every other row must be a builder, and vice versa.
        self.assertEqual(set(BUILDERS), set(TRIGGER_KEYS) - set(EXEMPT))
        self.assertEqual({t.key for t in EMAIL_TRIGGERS if not t.queued}, set(EXEMPT))
        self.assertEqual(check_registry(BUILDERS, settings), EMAIL_TRIGGERS)

    def test_the_two_private_link_emails_are_the_builders_that_refuse_a_copy(self):
        from app.services.email import messages

        invitation = messages.build_client_invitation_email(
            {"token": "secret", "project_names": ["Alpha"]}, ["client@example.com"],
        )
        link = messages.build_client_login_link_email({"token": "secret"}, ["client@example.com"])
        for message in (invitation, link):
            with self.subTest(subject=message.subject):
                self.assertTrue(message.copy_exempt)
        for key in EXEMPT:
            self.assertEqual(Service.cc_for(_sqlite_session(), key), [])

    def test_the_configuration_flags_named_by_the_catalogue_exist(self):
        for trigger in EMAIL_TRIGGERS:
            if trigger.enabled_setting:
                self.assertTrue(hasattr(settings, trigger.enabled_setting), trigger.enabled_setting)


class RegistryTests(unittest.TestCase):
    """A new email is one registration beside its builder, and nothing else.

    These register a temporary email the way a developer would, prove it is
    manageable end to end with no other change, and remove it again.
    """

    KEY = "registry_test_email"

    def setUp(self):
        self.addCleanup(self._unregister)

    def _unregister(self):
        email_catalogue._registry.pop(self.KEY, None)
        BUILDERS.pop(self.KEY, None)

    def _register(self, *, builder=True, **metadata):
        from app.services.email import messages

        messages._register(
            self.KEY, (lambda payload, recipients: OutgoingEmail(
                to=tuple(recipients), subject="Registry test", text_body="x", html_body="<p>x</p>",
            )) if builder else None,
            "Registry test email", "Whoever the test names", "Only during this test", **metadata,
        )

    def test_a_registered_email_is_listed_manageable_and_copied_with_no_other_change(self):
        self._register()
        triggers = email_triggers()
        self.assertEqual(triggers[-1].key, self.KEY)
        self.assertEqual(triggers[-1].number, len(triggers))
        self.assertIn(self.KEY, cc_allowed_keys())
        self.assertEqual(check_registry(BUILDERS, settings), triggers)

        db = _sqlite_session()
        admin = _admin(user_id=5, username="grace")
        with _env():
            # The page lists it (the frontend renders what GET returns). An untouched
            # deployment copies its standing list on every copyable email, the new one
            # included -- exactly what the environment list did before the page existed.
            view = Service.get_admin(db, admin)
            self.assertEqual(view["triggers"][-1]["key"], self.KEY)
            self.assertTrue(view["is_default"])
            self.assertTrue(all(self.KEY in a["triggers"] for a in view["addresses"]))
            self.assertEqual(Service.cc_for(db, self.KEY), STANDING)
            # Once a policy has been saved, an email it does not name is copied to nobody:
            # a new email never reaches an address an administrator did not tick for it.
            Service.update(db, admin, _update(("hr@storetransform.com", ["welcome"])))
            self.assertEqual(Service.cc_for(db, self.KEY), [])
            self.assertTrue(all(self.KEY not in a["triggers"] for a in Service.get_admin(db, admin)["addresses"]))
            # The schema accepts it, the policy stores it, and a send reads it back.
            Service.update(db, admin, _update(("hr@storetransform.com", [self.KEY])))
            self.assertEqual(Service.cc_for(db, self.KEY), ["hr@storetransform.com"])
            self.assertEqual(Service.cc_for(db, "welcome"), [])
            # The outbox can dispatch it.
            self.assertIn(self.KEY, BUILDERS)

            # The row outlives the registration (a restart re-registers, the policy is a stored row).
            app.dependency_overrides[get_db] = lambda: db
            app.dependency_overrides[get_current_user] = lambda: admin
            try:
                body = TestClient(app).get("/api/v1/email-cc").json()
            finally:
                app.dependency_overrides.clear()
            self.assertEqual(body["triggers"][-1]["key"], self.KEY)
            self.assertEqual(body["addresses"], [{"email": "hr@storetransform.com", "triggers": [self.KEY]}])

    def test_once_removed_it_is_gone_cleanly(self):
        self._register()
        self._unregister()
        self.assertNotIn(self.KEY, trigger_keys())
        self.assertNotIn(self.KEY, BUILDERS)
        with self.assertRaises(ValidationError):
            _update(("hr@storetransform.com", [self.KEY]))
        with _env():
            self.assertEqual(Service.cc_for(_sqlite_session(), self.KEY), [])

    def test_a_key_registered_twice_is_refused(self):
        self._register()
        with self.assertRaises(EmailRegistryError):
            self._register()

    def test_a_copyable_email_that_is_never_queued_is_refused(self):
        with self.assertRaises(EmailRegistryError):
            self._register(builder=False)
        self.assertNotIn(self.KEY, trigger_keys())

    def test_a_builder_added_without_a_registration_fails_the_start_up_check(self):
        BUILDERS[self.KEY] = lambda payload, recipients: None
        with self.assertRaises(EmailRegistryError) as caught:
            check_registry(BUILDERS, settings)
        self.assertIn(self.KEY, str(caught.exception))

    def test_a_registration_naming_a_flag_that_does_not_exist_fails_the_start_up_check(self):
        self._register(enabled_setting="NO_SUCH_FLAG")
        with self.assertRaises(EmailRegistryError):
            check_registry(BUILDERS, settings)

    def test_a_blank_key_is_refused(self):
        for key in ("", " x", None):
            with self.subTest(key=key), self.assertRaises(EmailRegistryError):
                register_email(key, "n", "g", "t")


# ── The default: unchanged behaviour ────────────────────────────────────────


class DefaultTests(unittest.TestCase):

    def test_an_untouched_deployment_copies_the_standing_list_on_every_copyable_email(self):
        db = _sqlite_session()
        with _env():
            for key in CC_ALLOWED_KEYS:
                with self.subTest(email=key):
                    self.assertEqual(Service.cc_for(db, key), STANDING)

    def test_the_private_link_emails_are_never_copied_even_by_default(self):
        db = _sqlite_session()
        with _env():
            for key in EXEMPT:
                self.assertEqual(Service.cc_for(db, key), [])

    def test_an_unknown_email_is_copied_to_no_one(self):
        with _env():
            self.assertEqual(Service.cc_for(_sqlite_session(), "something_new"), [])

    def test_the_admin_view_shows_the_default_list_with_every_email_switched_on(self):
        with _env():
            view = Service.get_admin(_sqlite_session(), _admin())

        self.assertTrue(view["is_default"])
        self.assertEqual(view["version"], 0)
        self.assertEqual([a["email"] for a in view["addresses"]], STANDING)
        self.assertTrue(all(a["triggers"] == list(CC_ALLOWED_KEYS) for a in view["addresses"]))
        self.assertEqual(view["max_addresses"], MAX_CC_ADDRESSES)
        self.assertEqual(len(view["triggers"]), 15)
        self.assertIsNone(view["updated_at"])

    def test_the_view_says_which_emails_are_switched_off_in_configuration(self):
        with _env(RELEASE_EMAIL_ENABLED=False, WELCOME_EMAIL_ENABLED=True, ACCESS_CHANGE_EMAIL_ENABLED=False):
            view = Service.get_admin(_sqlite_session(), _admin())

        active = {t["key"]: t["active"] for t in view["triggers"]}
        self.assertFalse(active["release"])
        self.assertFalse(active["member_access"])
        self.assertTrue(active["welcome"])
        self.assertTrue(active["weekly_report"])

    def test_no_configured_list_means_no_default_addresses(self):
        with patch.multiple(settings, EMAIL_CC_ADDRESSES=""):
            view = Service.get_admin(_sqlite_session(), _admin())
        self.assertEqual(view["addresses"], [])


# ── Changing it ─────────────────────────────────────────────────────────────


class ChangeTests(unittest.TestCase):

    def setUp(self):
        self.db = _sqlite_session()
        self.admin = _admin(user_id=5, username="grace")

    def test_an_address_is_copied_only_on_the_emails_switched_on_for_it(self):
        with _env():
            Service.update(self.db, self.admin, _update(("hr@storetransform.com", ["welcome", "member_access"])))

            self.assertEqual(Service.cc_for(self.db, "welcome"), ["hr@storetransform.com"])
            self.assertEqual(Service.cc_for(self.db, "member_access"), ["hr@storetransform.com"])
            self.assertEqual(Service.cc_for(self.db, "weekly_report"), [])

    def test_once_saved_the_environment_list_no_longer_applies(self):
        with _env():
            Service.update(self.db, self.admin, _update(("new@example.com", ["welcome"])))

            self.assertEqual(Service.cc_for(self.db, "welcome"), ["new@example.com"])
            self.assertEqual(Service.cc_for(self.db, "feedback"), [])
            self.assertFalse(Service.get_admin(self.db, self.admin)["is_default"])

    def test_the_policy_is_read_in_the_order_it_was_saved(self):
        with _env():
            Service.update(self.db, self.admin, _update(
                ("b@example.com", ["welcome"]), ("a@example.com", ["welcome"]),
            ))
            self.assertEqual(Service.cc_for(self.db, "welcome"), ["b@example.com", "a@example.com"])

    def test_each_address_has_its_own_emails(self):
        with _env():
            Service.update(self.db, self.admin, _update(
                ("a@example.com", ["welcome", "feedback"]), ("b@example.com", ["feedback", "weekly_report"]),
            ))
            self.assertEqual(Service.cc_for(self.db, "welcome"), ["a@example.com"])
            self.assertEqual(Service.cc_for(self.db, "feedback"), ["a@example.com", "b@example.com"])
            self.assertEqual(Service.cc_for(self.db, "weekly_report"), ["b@example.com"])

    def test_leaving_an_address_out_removes_it_everywhere(self):
        with _env():
            Service.update(self.db, self.admin, _update(("a@example.com", ["welcome"]), ("b@example.com", ["welcome"])))
            Service.update(self.db, self.admin, _update(("b@example.com", ["welcome"])))
            self.assertEqual(Service.cc_for(self.db, "welcome"), ["b@example.com"])

    def test_an_empty_list_copies_no_one_and_does_not_fall_back_to_the_default(self):
        with _env():
            Service.update(self.db, self.admin, _update())
            for key in CC_ALLOWED_KEYS:
                self.assertEqual(Service.cc_for(self.db, key), [])
            self.assertFalse(Service.get_admin(self.db, self.admin)["is_default"])

    def test_an_address_with_no_emails_switched_on_is_kept_but_copied_on_nothing(self):
        with _env():
            Service.update(self.db, self.admin, _update(("idle@example.com", [])))
            self.assertEqual([a["email"] for a in Service.get_admin(self.db, self.admin)["addresses"]], ["idle@example.com"])
            self.assertEqual(Service.cc_for(self.db, "welcome"), [])

    def test_a_change_is_stamped_audited_and_names_the_addresses(self):
        with _env():
            saved = Service.update(self.db, self.admin, _update(("new@example.com", ["welcome", "feedback"])))

        self.assertEqual(saved["version"], 1)
        self.assertEqual(saved["updated_by_username"], "grace")
        self.assertIsNotNone(saved["updated_at"])
        rows = _audit(self.db)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].action, ActivityLogAction.EMAIL_CC_UPDATED)
        self.assertEqual(rows[0].module, ActivityLogModule.SYSTEM)
        self.assertEqual(rows[0].user_id, 5)
        self.assertIn("added new@example.com", rows[0].description)
        for gone in STANDING:
            self.assertIn(gone, rows[0].description)  # removed from the default list

    def test_the_audit_names_what_was_switched_on_and_off(self):
        with _env():
            Service.update(self.db, self.admin, _update(("a@example.com", ["welcome", "feedback"])))
            Service.update(self.db, self.admin, _update(("a@example.com", ["feedback", "weekly_report"])))

        description = _audit(self.db)[-1].description
        self.assertIn("a@example.com", description)
        self.assertIn("on for weekly_report", description)
        self.assertIn("off for welcome", description)

    def test_saving_what_is_already_in_force_changes_and_records_nothing(self):
        with _env():
            Service.update(self.db, self.admin, _update(("a@example.com", ["welcome"])))
            again = Service.update(self.db, self.admin, _update(("a@example.com", ["welcome"])))

        self.assertEqual(again["version"], 1)
        self.assertEqual(len(_audit(self.db)), 1)

    def test_the_order_emails_are_listed_in_does_not_make_a_change(self):
        with _env():
            Service.update(self.db, self.admin, _update(("a@example.com", ["welcome", "feedback"])))
            Service.update(self.db, self.admin, _update(("a@example.com", ["feedback", "welcome"])))
        self.assertEqual(len(_audit(self.db)), 1)

    def test_saving_the_untouched_default_writes_no_version(self):
        with _env():
            saved = Service.update(self.db, self.admin, _update(*[(a, CC_ALLOWED_KEYS) for a in STANDING]))
        self.assertEqual(saved["version"], 0)
        self.assertTrue(saved["is_default"])
        self.assertEqual(_audit(self.db), [])

    def test_a_save_over_a_newer_version_is_refused_and_changes_nothing(self):
        from fastapi import HTTPException

        with _env():
            first = Service.update(self.db, self.admin, _update(("hr@storetransform.com", ["welcome"])))
            self.assertEqual(first["version"], 1)
            stale = EmailCcUpdate(addresses=[{"email": "late@example.com", "triggers": ["welcome"]}], version=0)
            with self.assertRaises(HTTPException) as caught:
                Service.update(self.db, self.admin, stale)
            self.assertEqual(caught.exception.status_code, 409)
            self.assertEqual(Service.cc_for(self.db, "welcome"), ["hr@storetransform.com"])
            self.assertEqual(Service.get_admin(self.db, self.admin)["version"], 1)

    def test_a_save_that_names_the_current_version_is_applied(self):
        with _env():
            Service.update(self.db, self.admin, _update(("hr@storetransform.com", ["welcome"])))
            current = EmailCcUpdate(addresses=[{"email": "next@example.com", "triggers": ["welcome"]}], version=1)
            self.assertEqual(Service.update(self.db, self.admin, current)["version"], 2)
            self.assertEqual(Service.cc_for(self.db, "welcome"), ["next@example.com"])

    def test_a_save_without_a_version_is_applied_as_before(self):
        with _env():
            Service.update(self.db, self.admin, _update(("hr@storetransform.com", ["welcome"])))
            self.assertEqual(Service.update(self.db, self.admin, _update(("b@example.com", ["welcome"])))["version"], 2)

    def test_addresses_are_stored_lower_cased(self):
        with _env():
            Service.update(self.db, self.admin, _update(("Mixed.Case@Example.COM", ["welcome"])))
            self.assertEqual(Service.cc_for(self.db, "welcome"), ["mixed.case@example.com"])


# ── Validation ──────────────────────────────────────────────────────────────


class ValidationTests(unittest.TestCase):

    def test_a_bad_address_is_refused(self):
        for bad in ("not-an-address", "", "a@b", "a b@example.com", "x@example.com\r\nBcc: evil@x.com"):
            with self.subTest(address=bad):
                with self.assertRaises(ValidationError):
                    _update((bad, ["welcome"]))

    def test_the_same_address_twice_is_refused_whatever_its_case(self):
        with self.assertRaises(ValidationError):
            _update(("a@example.com", ["welcome"]), ("A@Example.com", ["feedback"]))

    def test_an_email_that_does_not_exist_is_refused(self):
        with self.assertRaises(ValidationError):
            _update(("a@example.com", ["nonsense"]))

    def test_the_private_link_emails_cannot_be_switched_on_for_anyone(self):
        for key in EXEMPT:
            with self.subTest(email=key):
                with self.assertRaises(ValidationError) as caught:
                    _update(("a@example.com", [key]))
                self.assertIn("private link", str(caught.exception))

    def test_too_many_addresses_are_refused(self):
        with self.assertRaises(ValidationError):
            _update(*[(f"user{n}@example.com", []) for n in range(MAX_CC_ADDRESSES + 1)])
        _update(*[(f"user{n}@example.com", []) for n in range(MAX_CC_ADDRESSES)])

    def test_the_wrong_shapes_are_refused(self):
        for body in ({}, {"addresses": "a@example.com"}, {"addresses": [{"email": 5, "triggers": []}]},
                     {"addresses": [{"email": "a@example.com", "triggers": "welcome"}]}):
            with self.subTest(body=body):
                with self.assertRaises(ValidationError):
                    EmailCcUpdate(**body)

    def test_an_email_listed_twice_for_one_address_is_collapsed_not_refused(self):
        update = _update(("a@example.com", ["welcome", "welcome", "feedback"]))
        self.assertEqual(update.addresses[0].triggers, ["welcome", "feedback"])


# ── Who may change it ───────────────────────────────────────────────────────


class PermissionTests(unittest.TestCase):

    def setUp(self):
        self.db = _sqlite_session()

    def test_the_manage_roles_are_the_three_administrator_spellings(self):
        self.assertEqual(EMAIL_CC_MANAGE_ROLES, {"administrator", "org_admin", "super_admin"})

    def test_everyone_else_is_refused_every_call(self):
        service_principal = _admin("release_bot")
        service_principal.is_service_principal = True
        for who in (_admin("hr"), _admin("leader"), _admin("manager"), _admin("employee"), _admin("client"), service_principal):
            for name, call in (
                ("view", lambda w: Service.get_admin(self.db, w)),
                ("change", lambda w: Service.update(self.db, w, _update(("a@example.com", ["welcome"])))),
            ):
                with self.subTest(role=who.role_name, call=name):
                    with self.assertRaises(HTTPException) as caught:
                        call(who)
                    self.assertEqual(caught.exception.status_code, 403)
        self.assertEqual(_audit(self.db), [])
        self.assertIsNone(self.db.get(SystemSetting, SystemSettingKey.EMAIL_CC))

    def test_every_administrator_spelling_may_change_it(self):
        for role in sorted(EMAIL_CC_MANAGE_ROLES):
            with self.subTest(role=role), _env():
                Service.update(self.db, _admin(role), _update(("a@example.com", ["welcome"])))
                Service.update(self.db, _admin(role), _update())


# ── A damaged row cannot stop an email ──────────────────────────────────────


class DamagedRowTests(unittest.TestCase):

    def _db_with(self, value):
        db = _sqlite_session()
        db.add(SystemSetting(key=SystemSettingKey.EMAIL_CC, value=value))
        db.commit()
        return db

    def test_unreadable_values_fall_back_to_the_default_and_never_raise(self):
        for value in ({}, {"version": 0}, {"version": "x", "addresses": []}, {"version": 2}, {"version": 2, "addresses": "no"}, []):
            with self.subTest(value=value), _env():
                self.assertEqual(Service.cc_for(self._db_with(value), "welcome"), STANDING)

    def test_a_bad_entry_is_skipped_and_the_rest_still_apply(self):
        value = {"version": 2, "addresses": [
            "oops", {"email": "not-an-address", "triggers": ["welcome"]}, {"email": 5, "triggers": ["welcome"]},
            {"email": "good@example.com", "triggers": ["welcome", "nonsense", 7] + EXEMPT},
            {"email": "GOOD@example.com", "triggers": ["welcome"]},
        ]}
        with _env():
            db = self._db_with(value)
            self.assertEqual(Service.cc_for(db, "welcome"), ["good@example.com"])
            for key in EXEMPT:
                self.assertEqual(Service.cc_for(db, key), [])


# ── The provider ────────────────────────────────────────────────────────────


def _mail(to=("priya@example.com",), **kw):
    return OutgoingEmail(to=list(to), subject="Hello", html="<p>Hi</p>", text="Hi", **kw)


def _cc_header(message):
    with _env(SMTP_HOST="smtp.example.com", EMAIL_FROM_ADDRESS="monitra@example.com"):
        mime = build_mime_message(message)
    return [a.strip() for a in mime["Cc"].split(",")] if mime["Cc"] else []


class ProviderTests(unittest.TestCase):

    def test_a_decided_cc_is_what_the_header_carries(self):
        self.assertEqual(_cc_header(_mail(cc=("a@example.com", "b@example.com"))), ["a@example.com", "b@example.com"])

    def test_a_decided_empty_cc_means_no_header_not_the_standing_list(self):
        self.assertEqual(_cc_header(_mail(cc=())), [])

    def test_no_decision_still_means_the_standing_list(self):
        self.assertEqual(_cc_header(_mail()), STANDING)

    def test_a_recipient_is_never_copied_on_their_own_email(self):
        self.assertEqual(_cc_header(_mail(to=("A@example.com",), cc=("a@example.com", "b@example.com"))), ["b@example.com"])

    def test_an_exempt_message_is_never_copied_whatever_was_decided(self):
        self.assertEqual(_cc_header(_mail(copy_exempt=True, cc=("a@example.com",))), [])

    def test_a_bad_entry_in_the_decided_cc_is_skipped_and_cannot_inject_a_header(self):
        header = _cc_header(_mail(cc=("good@example.com", "bad", "x@example.com\r\nBcc: evil@x.com")))
        self.assertEqual(header, ["good@example.com"])


# ── The real send path ──────────────────────────────────────────────────────


class SendPathTests(unittest.TestCase):
    """`EmailOutboxService._send_claimed` reads the policy and puts it on the message."""

    def _send(self, db, notification_type="welcome", recipients=("priya@example.com",)):
        sent = []
        provider = MagicMock()
        provider.send.side_effect = sent.append
        row = MagicMock(
            id=1, attempt_count=1, notification_type=notification_type, payload="{}",
            recipients=json.dumps(list(recipients)), max_attempts=3, dedupe_key="k", user_id=2,
        )
        row.notification_type = notification_type
        builders = {notification_type: lambda payload, to: _mail(to=to)}
        with patch.object(outbox, "BUILDERS", builders), \
                patch.object(outbox, "get_email_provider", return_value=provider), \
                patch.object(outbox, "EmailNotificationRepository") as repo, \
                patch.object(outbox, "end_transaction"):
            result = outbox.EmailOutboxService._send_claimed(db, row)
        return result, sent, repo

    def test_the_message_carries_the_addresses_the_policy_chose_for_that_email(self):
        db = _sqlite_session()
        with _env():
            Service.update(db, _admin(), _update(("a@example.com", ["welcome"]), ("b@example.com", ["feedback"])))

            result, sent, _ = self._send(db, "welcome")
            self.assertEqual((result, sent[0].cc), ("sent", ("a@example.com",)))

            result, sent, _ = self._send(db, "feedback")
            self.assertEqual((result, sent[0].cc), ("sent", ("b@example.com",)))

            result, sent, _ = self._send(db, "weekly_report")
            self.assertEqual((result, sent[0].cc), ("sent", ()))

    def test_a_change_applies_to_an_email_that_has_not_gone_out_yet(self):
        db = _sqlite_session()
        with _env():
            _, before, _ = self._send(db, "welcome")
            Service.update(db, _admin(), _update(("late@example.com", ["welcome"])))
            _, after, _ = self._send(db, "welcome")

        self.assertEqual(list(before[0].cc), STANDING)
        self.assertEqual(after[0].cc, ("late@example.com",))

    def test_the_sent_message_really_has_those_addresses_in_its_cc_header(self):
        db = _sqlite_session()
        with _env():
            Service.update(db, _admin(), _update(("a@example.com", ["welcome"])))
            _, sent, _ = self._send(db, "welcome")
        self.assertEqual(_cc_header(sent[0]), ["a@example.com"])

    def test_a_policy_that_cannot_be_read_is_a_retried_failure_not_a_send(self):
        db = _sqlite_session()
        with patch("app.services.email_cc.EmailCcService.cc_for", side_effect=RuntimeError("db down")):
            result, sent, repo = self._send(db, "welcome")

        self.assertEqual(result, "retrying")
        self.assertEqual(sent, [], "nothing may be sent to the wrong people while the policy is unreadable")
        repo.mark_attempt_failed.assert_called_once()
        self.assertIn("CC policy could not be read", repo.mark_attempt_failed.call_args.kwargs["error"])


# ── Over HTTP ───────────────────────────────────────────────────────────────


class RouteTests(unittest.TestCase):

    def setUp(self):
        self.db = _sqlite_session()
        self.user = _admin(user_id=5, username="grace")
        app.dependency_overrides[get_db] = lambda: self.db
        app.dependency_overrides[get_current_user] = lambda: self.user
        self.client = TestClient(app)
        self.addCleanup(app.dependency_overrides.clear)
        patcher = _env()
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_whole_flow_over_http(self):
        read = self.client.get("/api/v1/email-cc")
        self.assertEqual(read.status_code, 200, read.text)
        body = read.json()
        self.assertTrue(body["is_default"])
        self.assertEqual([a["email"] for a in body["addresses"]], STANDING)
        self.assertEqual([t["number"] for t in body["triggers"]], list(range(1, len(EMAIL_TRIGGERS) + 1)))
        locked = {t["key"] for t in body["triggers"] if not t["cc_allowed"]}
        self.assertEqual(locked, set(EXEMPT))

        saved = self.client.put("/api/v1/email-cc", json={"addresses": [
            {"email": "hr@storetransform.com", "triggers": ["welcome", "member_access"]},
            {"email": "new.person@example.com", "triggers": ["welcome"]},
        ]})
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json()["version"], 1)
        self.assertEqual(
            [(a["email"], a["triggers"]) for a in saved.json()["addresses"]],
            [("hr@storetransform.com", ["welcome", "member_access"]), ("new.person@example.com", ["welcome"])],
        )
        self.assertEqual(self.client.get("/api/v1/email-cc").json()["version"], 1)
        self.assertEqual(Service.cc_for(self.db, "welcome"), ["hr@storetransform.com", "new.person@example.com"])

    def test_a_stale_version_is_409_over_http_and_the_list_is_unchanged(self):
        self.client.put("/api/v1/email-cc", json={"addresses": [
            {"email": "hr@storetransform.com", "triggers": ["welcome"]}], "version": 0})
        stale = self.client.put("/api/v1/email-cc", json={"addresses": [], "version": 0})
        self.assertEqual(stale.status_code, 409, stale.text)
        self.assertIn("changed by someone else", stale.json()["detail"])
        body = self.client.get("/api/v1/email-cc").json()
        self.assertEqual(body["version"], 1)
        self.assertEqual([a["email"] for a in body["addresses"]], ["hr@storetransform.com"])
        self.assertEqual(self.client.put("/api/v1/email-cc", json={"addresses": [], "version": -1}).status_code, 422)

    def test_bad_requests_are_422_and_change_nothing(self):
        bodies = (
            {"addresses": [{"email": "nope", "triggers": []}]},
            {"addresses": [{"email": "a@example.com", "triggers": ["client_invitation"]}]},
            {"addresses": [{"email": "a@example.com", "triggers": ["nonsense"]}]},
            {"addresses": [{"email": "a@example.com", "triggers": []}, {"email": "A@example.com", "triggers": []}]},
            {"addresses": "x"}, {},
        )
        for body in bodies:
            with self.subTest(body=body):
                self.assertEqual(self.client.put("/api/v1/email-cc", json=body).status_code, 422)
        self.assertTrue(self.client.get("/api/v1/email-cc").json()["is_default"])

    def test_a_non_administrator_gets_403(self):
        self.user = _admin("hr")
        self.assertEqual(self.client.get("/api/v1/email-cc").status_code, 403)
        self.assertEqual(self.client.put("/api/v1/email-cc", json={"addresses": []}).status_code, 403)


# ── The whole flow, locally ─────────────────────────────────────────────────


class EndToEndTests(unittest.TestCase):
    """Save the policy over HTTP, queue a real email, deliver it, read the SMTP envelope.

    Everything is real except the network: the API, the policy row, the outbox table, the
    builder and the SMTP provider all run, against SQLite, and `smtplib.SMTP` is replaced
    by a recorder, so nothing leaves this machine.
    """

    NEW = "storetranformmanav@gmail.com"

    def setUp(self):
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        tables = [
            Base.metadata.tables["organizations"], SystemSetting.__table__, ActivityLog.__table__,
            User.__table__, EmailNotification.__table__,
        ]
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
        self.db = Session(engine)
        self.addCleanup(self.db.close)

        self.admin = _admin(user_id=5, username="grace")
        self.new_user = User(
            id=77, organization_id=ORG, username="newbie", email="newbie@example.com", name="New Bie",
            role_name="employee", permissions={}, is_active=True, status="active", capture_frequency=10,
        )
        self.db.add(self.new_user)
        self.db.commit()

        app.dependency_overrides[get_db] = lambda: self.db
        app.dependency_overrides[get_current_user] = lambda: self.admin
        self.addCleanup(app.dependency_overrides.clear)
        self.client = TestClient(app)

        self.sent = []
        outer = self

        class RecordingSmtp(smtplib.SMTP):
            def __init__(self, *a, **k):  # no network
                pass

            def ehlo(self, *a, **k):
                return (250, b"ok")

            def starttls(self, *a, **k):
                return (220, b"ok")

            def login(self, *a, **k):
                return (235, b"ok")

            def send_message(self, msg, from_addr=None, to_addrs=None, *a, **k):
                # What smtplib puts on the envelope: every To and Cc address.
                addresses = [addr for _, addr in email.utils.getaddresses(msg.get_all("To", []) + msg.get_all("Cc", []))]
                outer.sent.append({"envelope": addresses, "message": msg})
                return {}

            def quit(self):
                return (221, b"bye")

            def close(self):
                pass

        # The one SQLite-only stand-in: `claim` compares a stored datetime with an aware one,
        # which SQLite (naive datetimes) cannot do in the ORM's in-session evaluation. It is the
        # same statement without that synchronisation; everything else here is the real code.
        def claim(db, *, notification_id, now, retry_at):
            claimed = db.execute(
                update(EmailNotification)
                .where(EmailNotification.id == notification_id, EmailNotification.status == STATUS_PENDING)
                .values(attempt_count=EmailNotification.attempt_count + 1)
                .returning(EmailNotification.id)
                .execution_options(synchronize_session=False)
            ).scalar()
            db.commit()
            return db.get(EmailNotification, notification_id) if claimed is not None else None

        for patcher in (
            patch("app.services.email.outbox.EmailNotificationRepository.claim", staticmethod(claim)),
            _env(SMTP_HOST="smtp.example.com", SMTP_USERNAME="", SMTP_PASSWORD="", EMAIL_PROVIDER="smtp",
                 EMAIL_FROM_ADDRESS="monitra@example.com", WELCOME_EMAIL_ENABLED=True,
                 MONITRA_APP_URL="https://staff.example.com"),
            patch("app.services.email.provider.smtplib.SMTP", RecordingSmtp),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _put(self, addresses):
        response = self.client.put("/api/v1/email-cc", json={"addresses": addresses})
        self.assertEqual(response.status_code, 200, response.text)

    def _deliver_welcome(self):
        row_id = queue_welcome_email(self.db, self.new_user)
        self.assertIsNotNone(row_id)
        self.assertEqual(outbox.EmailOutboxService.deliver_one(self.db, row_id), "sent")
        self.db.expire_all()
        self.assertEqual(self.db.get(EmailNotification, row_id).status, STATUS_SENT)
        return self.sent[-1]

    def test_a_welcome_email_copies_only_the_address_ticked_for_it(self):
        self._put([{"email": self.NEW, "triggers": ["welcome"]}])

        mail = self._deliver_welcome()

        self.assertEqual(mail["message"]["To"], "newbie@example.com")
        self.assertEqual(mail["message"]["Cc"], self.NEW)
        self.assertEqual(sorted(mail["envelope"]), sorted(["newbie@example.com", self.NEW]))
        for standing in STANDING:
            self.assertNotIn(standing, str(mail["message"]["Cc"]))
            self.assertNotIn(standing, mail["envelope"])

    def test_the_same_address_is_not_copied_on_an_email_it_was_not_ticked_for(self):
        self._put([{"email": self.NEW, "triggers": ["feedback"]}])

        mail = self._deliver_welcome()

        self.assertIsNone(mail["message"]["Cc"])
        self.assertEqual(mail["envelope"], ["newbie@example.com"])

    def test_an_untouched_deployment_still_copies_the_standing_four(self):
        mail = self._deliver_welcome()

        self.assertEqual([a.strip() for a in mail["message"]["Cc"].split(",")], STANDING)

    def test_a_change_made_after_the_email_was_queued_applies_when_it_is_sent(self):
        row_id = queue_welcome_email(self.db, self.new_user)
        self.db.expire_all()
        self.assertEqual(self.db.get(EmailNotification, row_id).status, STATUS_PENDING)

        self._put([{"email": self.NEW, "triggers": ["welcome"]}])
        self.assertEqual(outbox.EmailOutboxService.deliver_one(self.db, row_id), "sent")

        self.assertEqual(self.sent[-1]["message"]["Cc"], self.NEW)

    def test_removing_the_address_stops_the_copy(self):
        self._put([{"email": self.NEW, "triggers": ["welcome"]}])
        self._put([])

        mail = self._deliver_welcome()

        self.assertIsNone(mail["message"]["Cc"])
        self.assertEqual(mail["envelope"], ["newbie@example.com"])

    def test_the_change_is_audited_with_the_address(self):
        self._put([{"email": self.NEW, "triggers": ["welcome"]}])

        rows = _audit(self.db)

        self.assertEqual([r.action for r in rows], [ActivityLogAction.EMAIL_CC_UPDATED])
        self.assertIn(self.NEW, rows[0].description)


if __name__ == "__main__":
    unittest.main()

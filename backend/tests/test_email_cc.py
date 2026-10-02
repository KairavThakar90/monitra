"""The standing CC (`EMAIL_CC_ADDRESSES`) on every Monitra email.

It is applied in one place -- `build_mime_message` -- so the properties are
about that place: who is copied, who is never copied twice, which messages are
deliberately left alone (a one-time sign-in link, an invitation's Approve /
Reject links, a release rehearsal) and that the address really reaches the SMTP
envelope, since a header alone delivers nothing.
"""
import smtplib
import unittest
from unittest.mock import patch

from app.core.config import settings
from app.services.email import messages
from app.services.email.provider import (
    OutgoingEmail, SmtpEmailProvider, build_mime_message, standing_cc_addresses,
)

CC = ["bharat@storetransform.com", "projectmanager663@gmail.com", "hr@storetransform.com", "piyush@storetransform.com"]


def cfg(**overrides):
    values = {
        "EMAIL_PROVIDER": "smtp", "EMAIL_FROM_ADDRESS": "monitra@example.com", "EMAIL_FROM_NAME": "Monitra",
        "EMAIL_REPLY_TO": "", "SMTP_HOST": "smtp.example.com", "SMTP_USERNAME": "", "SMTP_PASSWORD": "",
        "MONITRA_APP_URL": "https://staff.example.com", "MONITRA_SUPPORT_EMAIL": "", "EMAIL_ASSET_BASE_URL": "",
        "EMAIL_CC_ADDRESSES": ",".join(CC),
    }
    values.update(overrides)
    return patch.multiple(settings, **values)


def mail(to=("priya@example.com",), **kw):
    return OutgoingEmail(to=list(to), subject="Hello", html="<p>Hi</p>", text="Hi", **kw)


class CcHeaderTests(unittest.TestCase):
    def test_the_shipped_default_is_the_four_requested_addresses(self):
        # The class default, not an environment override: this is what a
        # deployment with nothing set will use.
        default = type(settings).model_fields["EMAIL_CC_ADDRESSES"].default
        self.assertEqual([a.strip() for a in default.split(",")], CC)

    def test_every_message_is_copied_to_all_four(self):
        with cfg():
            mime = build_mime_message(mail())
        self.assertEqual([a.strip() for a in mime["Cc"].split(",")], CC)
        self.assertEqual(mime["To"], "priya@example.com")

    def test_an_address_that_is_already_a_recipient_is_not_copied_twice(self):
        with cfg():
            mime = build_mime_message(mail(to=["ProjectManager663@gmail.com"]))
        self.assertNotIn("projectmanager663@gmail.com", mime["Cc"])
        self.assertEqual(len(mime["Cc"].split(",")), 3)

    def test_if_every_cc_address_is_a_recipient_there_is_no_cc_header(self):
        with cfg():
            mime = build_mime_message(mail(to=CC))
        self.assertIsNone(mime["Cc"])

    def test_an_invalid_entry_is_skipped_not_fatal(self):
        with cfg(EMAIL_CC_ADDRESSES="bharat@storetransform.com, not-an-address ,hr@storetransform.com,\r\nBcc: evil@x.com"):
            self.assertEqual(standing_cc_addresses(), ["bharat@storetransform.com", "hr@storetransform.com"])
            mime = build_mime_message(mail())
        self.assertNotIn("evil", str(mime))

    def test_duplicates_and_case_are_collapsed(self):
        with cfg(EMAIL_CC_ADDRESSES="HR@storetransform.com,hr@storetransform.com"):
            self.assertEqual(standing_cc_addresses(), ["hr@storetransform.com"])

    def test_empty_configuration_switches_it_off(self):
        with cfg(EMAIL_CC_ADDRESSES=""):
            self.assertIsNone(build_mime_message(mail())["Cc"])

    def test_an_exempt_message_is_never_copied(self):
        with cfg():
            self.assertIsNone(build_mime_message(mail(copy_exempt=True))["Cc"])


class EnvelopeTests(unittest.TestCase):
    def test_the_cc_addresses_are_in_the_smtp_envelope(self):
        captured = []

        class FakeSmtp(smtplib.SMTP):
            def __init__(self, *args, **kwargs):  # no network
                pass

            def ehlo(self, *a, **k):
                return (250, b"ok")

            def starttls(self, *a, **k):
                return (220, b"ok")

            def sendmail(self, from_addr, to_addrs, msg, *a, **k):
                captured.append(list(to_addrs))
                return {}

            def quit(self):
                return (221, b"bye")

            def close(self):
                pass

        with cfg(), patch("app.services.email.provider.smtplib.SMTP", FakeSmtp):
            SmtpEmailProvider().send(mail())
        self.assertEqual(captured, [["priya@example.com", *CC]])

    def test_an_exempt_message_reaches_only_its_recipient(self):
        captured = []

        class FakeSmtp(smtplib.SMTP):
            def __init__(self, *a, **k):
                pass

            def ehlo(self, *a, **k):
                return (250, b"ok")

            def starttls(self, *a, **k):
                return (220, b"ok")

            def sendmail(self, from_addr, to_addrs, msg, *a, **k):
                captured.append(list(to_addrs))
                return {}

            def quit(self):
                return (221, b"bye")

            def close(self):
                pass

        with cfg(), patch("app.services.email.provider.smtplib.SMTP", FakeSmtp):
            SmtpEmailProvider().send(mail(copy_exempt=True))
        self.assertEqual(captured, [["priya@example.com"]])


class RealBuilderTests(unittest.TestCase):
    """The exemptions are decided by the real builders, not by this test."""

    def build(self, builder, payload):
        with cfg():
            return builder(payload, ["client@example.com"])

    def test_the_one_time_sign_in_link_is_never_copied(self):
        email = self.build(messages.build_client_login_link_email, {"handoff_token": "tok"})
        self.assertTrue(email.copy_exempt)
        with cfg():
            self.assertIsNone(build_mime_message(email)["Cc"])

    def test_an_invitation_with_approve_reject_links_is_never_copied(self):
        with patch.object(settings, "API_BASE_URL", "https://api.example.com"):
            email = self.build(messages.build_client_invitation_email, {"token": "tok", "project_names": ["P"]})
        self.assertTrue(email.copy_exempt)

    def test_a_release_rehearsal_is_not_copied_but_the_real_announcement_is(self):
        base = {"version": "2.0.0", "name": "Tester", "release_notes": "x"}
        self.assertTrue(self.build(messages.build_release_email, {**base, "test": True}).copy_exempt)
        self.assertFalse(self.build(messages.build_release_email, base).copy_exempt)

    def test_ordinary_emails_are_copied(self):
        welcome = self.build(messages.build_welcome_email, {"name": "Priya"})
        access = self.build(messages.build_member_access_email, {
            "name": "Priya", "switch": "login", "allowed": False, "changed_at": "2026-10-02T09:30:00+00:00",
        })
        for email in (welcome, access):
            self.assertFalse(email.copy_exempt)
            with cfg():
                self.assertEqual(len(build_mime_message(email)["Cc"].split(",")), 4)


if __name__ == "__main__":
    unittest.main()

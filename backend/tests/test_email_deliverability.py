"""What decides whether a message lands in the inbox or in spam, as far as this
code controls it.

Whether a given recipient's server files mail as spam is mostly a property of the
*sender's domain* (SPF, DKIM, DMARC) and of the sender's reputation, neither of
which code can set. These tests pin the parts that code does control, and the
warning that makes the rest visible:

* every message carries a `Date`, which RFC 5322 requires and a missing one is a
  textbook spam signal;
* the shared frame hides its preheader the one standard way, not with the stack
  of tricks (background-coloured text, a 1px font, opacity 0, zero-width
  padding) that spam uses and filters therefore score;
* a free-mail sender address -- which can never be authenticated for our own
  domain -- is reported by `deliverability_warnings`, and so by `/health` and
  the production preflight.
"""
import re
import unittest
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

from app.services.email import messages
from app.services.email.provider import (
    _FREE_MAIL_DOMAINS,
    OutgoingEmail,
    build_mime_message,
    deliverability_warnings,
    describe_configuration,
)
from app.services.email.templates import TEMPLATE_DIR
from tests.test_email_notifications import email_settings

LINKS = dict(API_BASE_URL="https://api.example.com/api/v1", MONITRA_APP_URL="https://app.example.com")

#: Characters a sender uses to push text out of sight in the inbox preview.
ZERO_WIDTH = (" ", "﻿", "͏", "​", "‌", "‍")
ZERO_WIDTH_ENTITIES = ("&#8199;", "&#65279;", "&#847;", "&#8203;", "&zwnj;", "&zwj;")


class TestDateHeader(unittest.TestCase):
    def build(self, message=None):
        with email_settings():
            return build_mime_message(message or OutgoingEmail(
                to=["ops@x.com"], subject="Hello", html="<p>hi</p>", text="hi",
            ))

    def test_every_message_carries_a_date(self):
        self.assertIsNotNone(self.build()["Date"])

    def test_it_is_a_valid_rfc_5322_date_in_utc_and_is_now(self):
        mime = self.build()
        parsed = parsedate_to_datetime(str(mime["Date"]))
        self.assertEqual(parsed.utcoffset(), timedelta(0))
        self.assertLess(abs(datetime.now(timezone.utc) - parsed), timedelta(minutes=1))
        # And on the wire, which is what a receiving server reads (the library
        # writes the zone as "+0000"; "GMT" is the same instant).
        wire = [line for line in mime.as_bytes().decode().splitlines() if line.startswith("Date:")]
        self.assertEqual(len(wire), 1)
        self.assertRegex(wire[0], r"^Date: [A-Z][a-z]{2}, \d{2} [A-Z][a-z]{2} \d{4} \d{2}:\d{2}:\d{2} (\+0000|GMT)$")

    def test_there_is_exactly_one(self):
        self.assertEqual(len(self.build().get_all("Date")), 1)

    def test_the_client_emails_carry_it_too(self):
        with email_settings(**LINKS):
            invitation = build_mime_message(messages.build_client_invitation_email(
                {"token": "t" * 43, "project_names": ["Alpha"]}, ["client@example.com"]))
            sign_in = build_mime_message(messages.build_client_login_link_email(
                {"handoff_token": "h" * 43}, ["client@example.com"]))
        self.assertIsNotNone(invitation["Date"])
        self.assertIsNotNone(sign_in["Date"])


class TestPreheader(unittest.TestCase):
    """The hidden line a client prints beside the subject in the inbox list."""

    def invitation_html(self):
        with email_settings(**LINKS):
            return messages.build_client_invitation_email(
                {"token": "t" * 43, "project_names": ["Alpha"]}, ["client@example.com"]).html

    def hidden_block(self, html):
        match = re.search(r'<div style="[^"]*display:none[^"]*">(.*?)</div>', html, re.S)
        self.assertIsNotNone(match, "the preheader block is missing")
        return match

    def test_the_preheader_text_is_still_there_and_hidden(self):
        html = self.invitation_html()
        block = self.hidden_block(html)
        self.assertIn("You have been invited to view your project information in Monitra.", block.group(1))
        self.assertIn("display:none", block.group(0))

    def test_it_has_no_zero_width_padding(self):
        html = self.invitation_html()
        for character in ZERO_WIDTH:
            self.assertNotIn(character, html)
        for entity in ZERO_WIDTH_ENTITIES:
            self.assertNotIn(entity, html)

    def test_it_is_not_hidden_by_stacking_the_tricks_spam_uses(self):
        style = re.search(r'<div style="([^"]*display:none[^"]*)"', self.invitation_html()).group(1)
        self.assertNotIn("opacity", style)
        self.assertNotIn("font-size:1px", style)
        self.assertNotIn("line-height:1px", style)
        # Text coloured like its background is the other classic: with display:none
        # there is nothing for a colour to do, so its only purpose would be to hide.
        self.assertNotIn("color:", style)

    def test_the_frame_source_itself_stays_clean_for_every_email(self):
        frame = (Path(TEMPLATE_DIR) / "base.html").read_text(encoding="utf-8")
        for entity in ZERO_WIDTH_ENTITIES:
            self.assertNotIn(entity, frame)
        hidden = re.search(r'<div style="[^"]*display:none[^"]*">', frame).group(0)
        self.assertNotIn("opacity", hidden)
        self.assertNotIn("font-size:1px", hidden)


class TestDeliverabilityWarnings(unittest.TestCase):
    def warnings(self, address, name="Monitra"):
        with email_settings(EMAIL_FROM_ADDRESS=address, EMAIL_FROM_NAME=name):
            return deliverability_warnings()

    def test_a_gmail_sender_is_flagged_and_the_warning_says_why_and_where_to_read_more(self):
        [warning] = self.warnings("projectmanager663@gmail.com")
        self.assertIn("gmail.com", warning)
        self.assertIn("DKIM", warning)
        self.assertIn("DMARC", warning)
        self.assertIn("Deliverability", warning)

    def test_it_names_the_display_name_that_is_being_worn(self):
        [warning] = self.warnings("someone@gmail.com", name="Store Transform")
        self.assertIn("'Store Transform'", warning)

    def test_the_address_is_compared_case_and_whitespace_insensitively(self):
        self.assertEqual(len(self.warnings("  Someone@GMAIL.com ")), 1)

    def test_every_listed_consumer_domain_is_flagged(self):
        for domain in sorted(_FREE_MAIL_DOMAINS):
            with self.subTest(domain=domain):
                self.assertEqual(len(self.warnings(f"x@{domain}")), 1)

    def test_a_sender_on_its_own_domain_is_not_flagged(self):
        self.assertEqual(self.warnings("monitra@storetransform.com"), [])
        self.assertEqual(self.warnings("noreply@peakworkos.com"), [])

    def test_a_lookalike_of_a_free_mail_domain_is_not_mistaken_for_one(self):
        # Only the whole domain counts: "notgmail.com" and "gmail.com.example.org" are not Gmail.
        self.assertEqual(self.warnings("x@notgmail.com"), [])
        self.assertEqual(self.warnings("x@gmail.com.example.org"), [])

    def test_no_sender_at_all_is_not_this_warnings_business(self):
        # "EMAIL_FROM_ADDRESS is not set" is already reported by unconfigured_reason().
        self.assertEqual(self.warnings(""), [])

    def test_the_health_summary_carries_the_count_and_never_the_address(self):
        with email_settings(EMAIL_FROM_ADDRESS="projectmanager663@gmail.com"):
            summary = describe_configuration()
        self.assertEqual(summary["deliverability_warnings"], 1)
        self.assertNotIn("projectmanager663", repr(summary))
        with email_settings(EMAIL_FROM_ADDRESS="monitra@storetransform.com"):
            self.assertEqual(describe_configuration()["deliverability_warnings"], 0)


if __name__ == "__main__":
    unittest.main()

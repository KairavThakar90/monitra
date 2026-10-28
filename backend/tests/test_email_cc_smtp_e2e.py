"""Email Management against a real SMTP socket and a real PostgreSQL: who is actually copied.

The unit suite proves the policy with `smtplib.SMTP` replaced by a recorder. This goes one
layer further: the real `SmtpEmailProvider` speaks real SMTP over a real loopback socket to a
sink this module runs, the real outbox queues and dispatches rows in a real PostgreSQL, and
what is asserted is the **envelope the server received** (every `RCPT TO`) and the **`Cc`
header of the message it was given** -- the two things a mail server actually acts on.

Opt-in: it needs a loopback PostgreSQL you do not mind writing to::

    set MONITRA_PG_TEST_URL=postgresql+psycopg2://postgres@127.0.0.1:58432/monitra_e2e
    set MONITRA_E2E=1
    python -m pytest tests/test_email_cc_smtp_e2e.py -q -s         (from backend/)

No email leaves this machine: the provider is pointed at the sink, TLS is off because the
sink is loopback (the provider refuses credentials without TLS, and none are configured),
and every row this writes is removed again.
"""
from __future__ import annotations

import asyncio
import email
import os
import socket
import threading
import unittest
from email import policy
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from tests import pg_support

pytestmark = pytest.mark.skipif(
    os.environ.get("MONITRA_E2E") != "1" or not os.environ.get("MONITRA_PG_TEST_URL"),
    reason="real SMTP + PostgreSQL E2E; set MONITRA_E2E=1 and MONITRA_PG_TEST_URL to run",
)

STANDING = ["bharat@storetransform.com", "projectmanager663@gmail.com", "hr@storetransform.com", "piyush@storetransform.com"]


# ── a loopback SMTP sink ─────────────────────────────────────────────────────

class SmtpSink:
    """Enough of RFC 5321 to receive a message: no TLS, no AUTH, records every envelope."""

    def __init__(self) -> None:
        self.messages: list[dict] = []
        self.port = 0
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._ready = threading.Event()

    def start(self) -> "SmtpSink":
        self._thread.start()
        assert self._ready.wait(5), "the SMTP sink did not start"
        return self

    def stop(self) -> None:
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(5)

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        server = self._loop.run_until_complete(asyncio.start_server(self._serve, "127.0.0.1", 0))
        self.port = server.sockets[0].getsockname()[1]
        self._ready.set()
        self._loop.run_forever()
        server.close()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        async def say(line: str) -> None:
            writer.write((line + "\r\n").encode())
            await writer.drain()

        envelope: dict = {"from": None, "to": [], "data": b""}
        await say("220 sink ESMTP")
        while True:
            raw = await reader.readline()
            if not raw:
                break
            line = raw.decode(errors="replace").rstrip("\r\n")
            verb = line.split(" ", 1)[0].upper()
            if verb in ("EHLO", "HELO"):
                await say("250-sink\r\n250 8BITMIME")
            elif verb == "MAIL":
                envelope["from"] = line.split(":", 1)[1].strip().strip("<>")
                await say("250 OK")
            elif verb == "RCPT":
                envelope["to"].append(line.split(":", 1)[1].strip().strip("<>").lower())
                await say("250 OK")
            elif verb == "DATA":
                await say("354 go ahead")
                chunks = []
                while True:
                    part = await reader.readline()
                    if part in (b".\r\n", b".\n", b""):
                        break
                    chunks.append(part[1:] if part.startswith(b"..") else part)
                envelope["data"] = b"".join(chunks)
                self.messages.append(dict(envelope))
                envelope = {"from": None, "to": [], "data": b""}
                await say("250 queued")
            elif verb == "RSET":
                envelope = {"from": None, "to": [], "data": b""}
                await say("250 OK")
            elif verb == "NOOP":
                await say("250 OK")
            elif verb == "QUIT":
                await say("221 bye")
                break
            else:
                await say("502 not implemented")
        writer.close()

    def parsed(self, index: int):
        return email.message_from_bytes(self.messages[index]["data"], policy=policy.default)


# ── the test ─────────────────────────────────────────────────────────────────

class EmailCcOverRealSmtpTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.engine = pg_support.engine_for(pg_support.pg_url())
        cls.sessions = pg_support.sessions(cls.engine)
        cls.sink = SmtpSink().start()
        cls.env = patch.multiple(
            "app.core.config.settings",
            EMAIL_PROVIDER="smtp", SMTP_HOST="127.0.0.1", SMTP_PORT=cls.sink.port, SMTP_USE_TLS=False,
            SMTP_USE_SSL=False, SMTP_USERNAME="", SMTP_PASSWORD="", EMAIL_FROM_ADDRESS="monitra@example.invalid",
            EMAIL_CC_ADDRESSES=",".join(STANDING), WELCOME_EMAIL_ENABLED=True, EMAIL_DISPATCH_TOKEN="e2e-dispatch",
        )
        cls.env.start()
        from app.services.email import provider as provider_module
        provider_module.get_email_provider.cache_clear() if hasattr(provider_module.get_email_provider, "cache_clear") else None

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()
        cls.sink.stop()
        with cls.engine.begin() as conn:
            conn.execute(text("DELETE FROM email_notifications WHERE notification_type IN ('welcome', 'feedback')"))
            conn.execute(text("DELETE FROM system_settings WHERE key = 'email_cc'"))
            conn.execute(text("DELETE FROM activity_logs WHERE action = 'email_cc_updated'"))
            conn.execute(text("DELETE FROM users WHERE email LIKE '%@cc-e2e.invalid'"))
        cls.engine.dispose()

    def setUp(self):
        self.sink.messages.clear()
        with self.engine.begin() as conn:
            conn.execute(text("DELETE FROM email_notifications WHERE notification_type IN ('welcome', 'feedback')"))
            conn.execute(text("DELETE FROM system_settings WHERE key = 'email_cc'"))
            conn.execute(text("DELETE FROM users WHERE email LIKE '%@cc-e2e.invalid'"))
        self.db = self.sessions()
        self.addCleanup(self.db.close)

    def _member(self, name):
        from app.models.user import User

        user = User(organization_id=1, username=f"cc_{name}", email=f"{name}@cc-e2e.invalid", name=name.title(),
                    role_name="employee", permissions={}, capture_frequency=10, is_active=True, status="active")
        self.db.add(user)
        self.db.commit()
        return user

    def _admin(self):
        from app.models.user import User

        admin = User(organization_id=1, username="cc_admin", email="admin@cc-e2e.invalid", name="Admin",
                     role_name="administrator", permissions={}, capture_frequency=10, is_active=True, status="active")
        self.db.add(admin)
        self.db.commit()
        return admin

    def _welcome(self, user):
        from app.services.email.workflows import queue_welcome_email

        row_id = queue_welcome_email(self.db, user)
        self.assertIsNotNone(row_id)
        return row_id

    def _dispatch(self):
        from app.services.email.outbox import EmailOutboxService

        return EmailOutboxService.dispatch_pending(self.db)

    def _cc_header(self, index):
        message = self.sink.parsed(index)
        return sorted(a.addr_spec.lower() for a in (message["Cc"].addresses if message["Cc"] else ()))

    def test_an_untouched_deployment_copies_the_standing_four_on_the_wire(self):
        user = self._member("alice")
        self._welcome(user)
        self._dispatch()
        self.assertEqual(len(self.sink.messages), 1)
        self.assertEqual(sorted(self.sink.messages[0]["to"]), sorted([user.email] + STANDING))
        self.assertEqual(self._cc_header(0), sorted(STANDING))
        print(f"\nSMTP envelope (default): {self.sink.messages[0]['to']}")

    def test_removing_two_recipients_for_the_welcome_email_changes_the_envelope_and_the_header(self):
        from app.schemas.email_cc import EmailCcUpdate
        from app.services.email_cc import EmailCcService

        admin = self._admin()
        keep = STANDING[:2]
        EmailCcService.update(self.db, admin, EmailCcUpdate(addresses=[
            {"email": keep[0], "triggers": ["welcome", "feedback"]},
            {"email": keep[1], "triggers": ["welcome"]},
            {"email": STANDING[2], "triggers": ["feedback"]},
            {"email": STANDING[3], "triggers": []},
        ]))
        user = self._member("bob")
        self._welcome(user)
        self._dispatch()
        self.assertEqual(self._cc_header(0), sorted(keep))
        self.assertEqual(sorted(self.sink.messages[0]["to"]), sorted([user.email] + keep))
        print(f"\nSMTP envelope (two removed): {self.sink.messages[0]['to']}")

        # A restart of the backend is a new session reading the same row: the policy holds.
        other = self.sessions()
        try:
            self.assertEqual(EmailCcService.cc_for(other, "welcome"), keep)
            self.assertEqual(EmailCcService.cc_for(other, "feedback"), [keep[0], STANDING[2]])
        finally:
            other.close()

    def test_a_new_recipient_added_for_the_welcome_email_only_is_copied_there_and_nowhere_else(self):
        from app.schemas.email_cc import EmailCcUpdate
        from app.services.email_cc import EmailCcService

        admin = self._admin()
        EmailCcService.update(self.db, admin, EmailCcUpdate(addresses=[
            {"email": "New.Person@Example.invalid", "triggers": ["welcome"]},
        ]))
        user = self._member("carol")
        self._welcome(user)
        self._dispatch()
        self.assertEqual(self._cc_header(0), ["new.person@example.invalid"])
        self.assertIn("new.person@example.invalid", self.sink.messages[0]["to"])

        # Another email type is not copied to that person.
        from app.services.email.workflows import queue_feedback_notification

        feedback = SimpleNamespace(id=424242, category="general", message="E2E feedback", created_at=None)
        with patch.multiple("app.core.config.settings", FEEDBACK_ADMIN_EMAIL="admin@cc-e2e.invalid", FEEDBACK_HR_EMAIL=""):
            self.assertIsNotNone(queue_feedback_notification(self.db, feedback, user))
        self._dispatch()
        self.assertEqual(len(self.sink.messages), 2)
        self.assertNotIn("new.person@example.invalid", self.sink.messages[1]["to"])
        self.assertIsNone(self.sink.parsed(1)["Cc"])
        print(f"\nSMTP envelope (feedback, nobody copied): {self.sink.messages[1]['to']}")

    def test_a_change_made_while_the_email_is_queued_applies_when_it_is_sent(self):
        from app.schemas.email_cc import EmailCcUpdate
        from app.services.email_cc import EmailCcService

        admin = self._admin()
        user = self._member("dave")
        self._welcome(user)                       # queued under the default policy
        EmailCcService.update(self.db, admin, EmailCcUpdate(addresses=[{"email": STANDING[2], "triggers": ["welcome"]}]))
        self._dispatch()
        self.assertEqual(self._cc_header(0), [STANDING[2]], "send-time policy, not queue-time")

    def test_an_empty_list_copies_nobody_and_the_envelope_is_the_recipient_alone(self):
        from app.schemas.email_cc import EmailCcUpdate
        from app.services.email_cc import EmailCcService

        admin = self._admin()
        EmailCcService.update(self.db, admin, EmailCcUpdate(addresses=[]))
        user = self._member("erin")
        self._welcome(user)
        self._dispatch()
        self.assertEqual(self.sink.messages[0]["to"], [user.email])
        self.assertIsNone(self.sink.parsed(0)["Cc"])

    def test_a_welcome_email_is_sent_once_however_often_it_is_queued_and_dispatched(self):
        user = self._member("frank")
        self._welcome(user)
        self._welcome(user)
        self._dispatch()
        self._dispatch()
        self.assertEqual(len(self.sink.messages), 1)

    def test_the_api_refuses_bad_input_and_a_stale_version_over_http_against_the_real_database(self):
        from app.core.database import get_db
        from app.core.security import get_current_user
        from app.main import app

        admin = self._admin()
        app.dependency_overrides[get_db] = lambda: self.db
        app.dependency_overrides[get_current_user] = lambda: admin
        try:
            client = TestClient(app)
            first = client.put("/api/v1/email-cc", json={"addresses": [{"email": STANDING[0], "triggers": ["welcome"]}], "version": 0})
            self.assertEqual(first.status_code, 200, first.text)
            self.assertEqual(client.put("/api/v1/email-cc", json={"addresses": [], "version": 0}).status_code, 409)
            for body in ({"addresses": [{"email": "nope", "triggers": []}]},
                         {"addresses": [{"email": "a@example.com", "triggers": ["client_invitation"]}]},
                         {"addresses": [{"email": "a@example.com", "triggers": []}, {"email": "A@example.com", "triggers": []}]}):
                self.assertEqual(client.put("/api/v1/email-cc", json=body).status_code, 422, body)
            self.assertEqual(client.get("/api/v1/email-cc").json()["version"], 1)
        finally:
            app.dependency_overrides.clear()

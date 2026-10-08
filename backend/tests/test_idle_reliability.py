"""
Idle reporting must survive a client whose clock is not the server's, and a
report that races its own retry -- the two ways a user's idle popup could
"never appear" with nothing in any log.

Before this module a desktop whose clock ran even one second ahead of the
server was answered 400 "Idle timestamps cannot be in the future." on every
retry, for ever; the 400 was not logged (``get_db`` skips HTTPExceptions), so
the account looked healthy. These tests pin:

* a client that sends ``client_time`` is placed by age, so its absolute offset
  from the server is irrelevant (the start/stop contract, docs/TIMING_MODEL.md);
* a client that does not is given a bounded lead before it is refused;
* every refusal leaves one ``IDLE_REPORT_REJECTED`` line naming the rule;
* two reports racing for one entry produce one period, not a 500;
* the diagnostics endpoint logs a closed set of scalars and nothing else.
"""
import logging
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

from app.models.time_entry import TimeEntry
from app.models.time_entry_idle_period import IdlePeriodStatus, TimeEntryIdlePeriod
from app.models.user import User
from app.schemas.time_entry_idle_period import IdleClientDiagnostics, IdlePeriodCreate
from app.services.time_entry_idle_period import (
    IDLE_CLIENT_CLOCK_LEAD_SECONDS, TimeEntryIdlePeriodService,
)

SVC = "app.services.time_entry_idle_period"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _user(**overrides) -> User:
    defaults = dict(id=1, organization_id=10, permissions={}, role_name="employee",
                    idle_enabled=True, idle_minutes=5)
    defaults.update(overrides)
    return User(**defaults)


def _entry(started_ago_minutes=60, **overrides) -> TimeEntry:
    defaults = dict(
        id=100, organization_id=10, user_id=1, project_id=5, task_id=7,
        start_time=_now() - timedelta(minutes=started_ago_minutes), end_time=None,
        total_seconds=0, status="running", is_manual=False, is_billable=False,
    )
    defaults.update(overrides)
    return TimeEntry(**defaults)


def _idle(**overrides) -> TimeEntryIdlePeriod:
    defaults = dict(
        id=456, organization_id=10, user_id=1, time_entry_id=100,
        original_project_id=5, original_task_id=7,
        idle_started_at=_now() - timedelta(minutes=7),
        idle_detected_at=_now() - timedelta(minutes=1),
        status=IdlePeriodStatus.PENDING, reassigned=False,
    )
    defaults.update(overrides)
    return TimeEntryIdlePeriod(**defaults)


class _Reporter(unittest.TestCase):
    def setUp(self):
        self.db = MagicMock()

    def report(self, payload, *, entry=None, pending=None, by_key=None, create=None):
        created = {}

        def make(**kw):
            created.update(kw)
            return _idle(**{k: v for k, v in kw.items() if k != "db"})

        with patch(f"{SVC}.TimeEntryIdlePeriodService._owned_entry",
                   return_value=entry or _entry()), \
             patch(f"{SVC}.TimeEntryIdlePeriodRepository.get_pending_for_entry",
                   return_value=pending), \
             patch(f"{SVC}.TimeEntryIdlePeriodRepository.get_by_client_event_id",
                   return_value=by_key), \
             patch(f"{SVC}.TimeEntryIdlePeriodRepository.create",
                   side_effect=create or make), \
             patch(f"{SVC}._with_entry_adjustment", side_effect=lambda db, p: p):
            result = TimeEntryIdlePeriodService.report_idle_period(self.db, payload, _user())
        return result, created


class TestClientClock(_Reporter):
    def test_a_client_ahead_of_the_server_is_placed_by_age_when_it_sends_its_clock(self):
        """The desktop's clock is 90 s fast. It reports a stretch that began
        7 minutes before *its* now. On the server's timeline that is 7 minutes
        before the server's now -- accepted, and the skew is irrelevant."""
        skew = timedelta(seconds=90)
        client_now = _now() + skew
        payload = IdlePeriodCreate(
            time_entry_id=100,
            idle_started_at=client_now - timedelta(minutes=7),
            idle_detected_at=client_now,
            client_time=client_now,
        )
        _, created = self.report(payload)
        observed = (created["idle_detected_at"] - created["idle_started_at"]).total_seconds()
        self.assertAlmostEqual(observed, 7 * 60, delta=1)
        self.assertLessEqual(created["idle_detected_at"], _now())

    def test_a_client_behind_the_server_is_not_pushed_before_the_entry_started(self):
        """A clock 10 minutes slow used to put the idle start before the
        entry's own start (server clock) and be refused for good."""
        entry = _entry(started_ago_minutes=9)
        client_now = _now() - timedelta(minutes=10)
        payload = IdlePeriodCreate(
            time_entry_id=100,
            idle_started_at=client_now - timedelta(minutes=6),
            idle_detected_at=client_now,
            client_time=client_now,
        )
        _, created = self.report(payload, entry=entry)
        self.assertGreaterEqual(created["idle_started_at"], entry.start_time)

    def test_a_long_interruption_gap_is_not_capped_when_the_client_sends_its_clock(self):
        client_now = _now()
        payload = IdlePeriodCreate(
            time_entry_id=100,
            idle_started_at=client_now - timedelta(hours=5),
            idle_detected_at=client_now,
            client_time=client_now,
        )
        _, created = self.report(payload, entry=_entry(started_ago_minutes=60 * 8))
        observed = (created["idle_detected_at"] - created["idle_started_at"]).total_seconds()
        self.assertAlmostEqual(observed, 5 * 3600, delta=1)

    def test_an_older_client_a_few_seconds_fast_is_no_longer_refused_for_good(self):
        """No client_time: a lead inside the allowance is clamped to now."""
        ahead = _now() + timedelta(seconds=30)
        payload = IdlePeriodCreate(
            time_entry_id=100,
            idle_started_at=ahead - timedelta(minutes=6),
            idle_detected_at=ahead,
        )
        _, created = self.report(payload)
        self.assertLessEqual(created["idle_detected_at"], _now())

    def test_an_older_client_beyond_the_allowance_is_still_refused(self):
        far = _now() + timedelta(seconds=IDLE_CLIENT_CLOCK_LEAD_SECONDS + 600)
        with pytest.raises(HTTPException) as exc:
            self.report(IdlePeriodCreate(
                time_entry_id=100, idle_started_at=far, idle_detected_at=far + timedelta(minutes=6),
            ))
        self.assertEqual(exc.value.status_code, 400)

    def test_the_threshold_is_still_enforced_after_the_skew_is_corrected(self):
        client_now = _now() + timedelta(seconds=45)
        with pytest.raises(HTTPException) as exc:
            self.report(IdlePeriodCreate(
                time_entry_id=100,
                idle_started_at=client_now - timedelta(minutes=2),
                idle_detected_at=client_now,
                client_time=client_now,
            ))
        self.assertEqual(exc.value.status_code, 400)


class TestRefusalsAreLogged(_Reporter):
    def test_a_refused_report_leaves_one_warning_naming_the_rule(self):
        far = _now() + timedelta(hours=2)
        with self.assertLogs(SVC, level="WARNING") as captured:
            with pytest.raises(HTTPException):
                self.report(IdlePeriodCreate(
                    time_entry_id=100, idle_started_at=far,
                    idle_detected_at=far + timedelta(minutes=6),
                ))
        line = captured.output[0]
        self.assertIn("IDLE_REPORT_REJECTED", line)
        self.assertIn("reason=future_timestamps", line)
        self.assertIn("user=1", line)
        self.assertIn("entry=100", line)

    def test_a_threshold_refusal_says_how_far_short_it_was(self):
        now = _now()
        with self.assertLogs(SVC, level="WARNING") as captured:
            with pytest.raises(HTTPException):
                self.report(IdlePeriodCreate(
                    time_entry_id=100, idle_started_at=now - timedelta(minutes=2),
                    idle_detected_at=now,
                ))
        self.assertIn("reason=threshold_not_reached", captured.output[0])
        self.assertIn("observed_s=120", captured.output[0])
        self.assertIn("threshold_min=5", captured.output[0])


class TestConcurrentReports(_Reporter):
    def test_the_loser_of_a_race_gets_the_winning_period_not_a_500(self):
        winner = _idle(id=999)
        now = _now()
        payload = IdlePeriodCreate(
            time_entry_id=100, idle_started_at=now - timedelta(minutes=7),
            idle_detected_at=now, client_event_id="evt-race",
        )
        calls = {"n": 0}

        def pending(db, entry_id):
            calls["n"] += 1
            return None if calls["n"] == 1 else winner

        def create(**kw):
            raise IntegrityError("insert", {}, Exception("uq_idle_periods_pending_entry"))

        with patch(f"{SVC}.TimeEntryIdlePeriodService._owned_entry", return_value=_entry()), \
             patch(f"{SVC}.TimeEntryIdlePeriodRepository.get_pending_for_entry", side_effect=pending), \
             patch(f"{SVC}.TimeEntryIdlePeriodRepository.get_by_client_event_id", return_value=None), \
             patch(f"{SVC}.TimeEntryIdlePeriodRepository.create", side_effect=create), \
             patch(f"{SVC}._with_entry_adjustment", side_effect=lambda db, p: p):
            result = TimeEntryIdlePeriodService.report_idle_period(self.db, payload, _user())
        self.assertIs(result, winner)
        self.db.rollback.assert_called()

    def test_an_integrity_error_with_no_winner_is_not_hidden(self):
        now = _now()
        payload = IdlePeriodCreate(
            time_entry_id=100, idle_started_at=now - timedelta(minutes=7), idle_detected_at=now,
        )
        with patch(f"{SVC}.TimeEntryIdlePeriodService._owned_entry", return_value=_entry()), \
             patch(f"{SVC}.TimeEntryIdlePeriodRepository.get_pending_for_entry", return_value=None), \
             patch(f"{SVC}.TimeEntryIdlePeriodRepository.get_by_client_event_id", return_value=None), \
             patch(f"{SVC}.TimeEntryIdlePeriodRepository.create",
                   side_effect=IntegrityError("insert", {}, Exception("something else"))):
            with pytest.raises(IntegrityError):
                TimeEntryIdlePeriodService.report_idle_period(self.db, payload, _user())


class TestClientDiagnostics(unittest.TestCase):
    def test_the_report_is_logged_under_the_authenticated_user_with_the_server_config(self):
        payload = IdleClientDiagnostics(
            event="reading_unavailable", platform="win32", state="MONITORING",
            idle_enabled=True, idle_minutes=5, reading_failure="GetLastInputInfo returned 0",
        )
        with self.assertLogs(SVC, level="INFO") as captured:
            TimeEntryIdlePeriodService.log_client_diagnostics(payload, _user(id=42, idle_minutes=10))
        line = captured.output[0]
        self.assertIn("IDLE_CLIENT event=reading_unavailable user=42", line)
        self.assertIn("server_idle_minutes=10", line)
        self.assertIn("platform=win32", line)
        self.assertEqual(captured.records[0].levelno, logging.WARNING)

    def test_a_stuck_or_failed_event_is_a_warning_and_a_heartbeat_is_info(self):
        with self.assertLogs(SVC, level="INFO") as captured:
            TimeEntryIdlePeriodService.log_client_diagnostics(
                IdleClientDiagnostics(event="health"), _user())
        self.assertEqual(captured.records[0].levelno, logging.INFO)

    def test_fields_the_schema_does_not_name_are_never_logged(self):
        payload = IdleClientDiagnostics.model_validate({
            "event": "health", "url": "https://example.test/?key=SECRET",
            "authorization": "Bearer SECRET", "window_title": "salary.xlsx",
        })
        with self.assertLogs(SVC, level="INFO") as captured:
            TimeEntryIdlePeriodService.log_client_diagnostics(payload, _user())
        text = " ".join(captured.output)
        for leaked in ("SECRET", "salary", "Bearer", "example.test"):
            self.assertNotIn(leaked, text)

    def test_an_event_name_cannot_smuggle_a_line_break_or_a_space(self):
        for bad in ("a b", "a\nIDLE_CLIENT forged", "", "x" * 65):
            with self.assertRaises(ValidationError):
                IdleClientDiagnostics(event=bad)

    def test_free_text_is_bounded(self):
        with self.assertRaises(ValidationError):
            IdleClientDiagnostics(event="health", detail="x" * 201)

    def test_newlines_in_a_value_do_not_start_a_second_log_line(self):
        payload = IdleClientDiagnostics(event="health", detail="a\nIDLE_CLIENT forged=1")
        with self.assertLogs(SVC, level="INFO") as captured:
            TimeEntryIdlePeriodService.log_client_diagnostics(payload, _user())
        self.assertNotIn("\n", captured.records[0].getMessage())


# ── An administrator's idle settings are not undone by the next login ────────

class TestLoginDoesNotOverwriteAdminSettings(unittest.IsolatedAsyncioTestCase):
    async def _login_existing(self, provider_fields, **local):
        from tests.test_auth_flow import FakeAsyncClient, _login_response

        existing = MagicMock(id=7, organization_id=1, role_name="employee", permissions={})
        existing.hubstaff_user_id = "1240560"
        existing.idle_enabled = local.get("idle_enabled", False)
        existing.idle_minutes = local.get("idle_minutes", 20)
        existing.capture_frequency = local.get("capture_frequency", 15)
        response = _login_response(provider_fields | {"roles": ["employee"]})
        db = MagicMock()
        with patch("app.services.external_auth_service.httpx.AsyncClient",
                   return_value=FakeAsyncClient(response)), \
             patch("app.services.auth.UserRepository.get_by_hubstaff_id", return_value=existing), \
             patch("app.services.auth.UserRepository.get_by_normalized_email", return_value=existing), \
             patch("app.services.auth.create_access_token", return_value="tok"), \
             patch("app.services.auth.UserRead") as user_read, \
             patch("app.services.auth.TokenPair", return_value="pair"):
            user_read.model_validate.return_value = "user-read"
            from app.services.auth import AuthService
            await AuthService.login_exchange(db, "user@example.com", "pw")
        return existing

    async def test_a_member_with_idle_switched_off_stays_off_after_login(self):
        user = await self._login_existing({})                      # provider says nothing
        self.assertFalse(user.idle_enabled)
        self.assertEqual(user.idle_minutes, 20)
        self.assertEqual(user.capture_frequency, 15)

    async def test_the_provider_cannot_switch_it_back_on_either(self):
        user = await self._login_existing(
            {"idle_enabled": True, "idle_minutes": 5, "capture_frequency": 10})
        self.assertFalse(user.idle_enabled)
        self.assertEqual(user.idle_minutes, 20)

    async def test_an_unusable_provider_threshold_is_not_provisioned(self):
        from tests.test_auth_flow import FakeAsyncClient, _login_response
        response = _login_response({"roles": ["employee"], "idle_minutes": 0, "idle_enabled": "yes"})
        db = MagicMock()
        db.scalar.side_effect = [None, None]
        db.execute.return_value.scalar.return_value = 1
        with patch("app.services.external_auth_service.httpx.AsyncClient",
                   return_value=FakeAsyncClient(response)), \
             patch("app.services.auth.UserRepository.create") as create_user, \
             patch("app.services.auth.create_access_token", return_value="tok"), \
             patch("app.services.auth.UserRead") as user_read, \
             patch("app.services.auth.TokenPair", return_value="pair"):
            user_read.model_validate.return_value = "user-read"
            create_user.return_value = MagicMock(id=1, organization_id=1, permissions={})
            from app.services.auth import AuthService
            await AuthService.login_exchange(db, "user@example.com", "pw")
        provisioned = create_user.call_args[0][1]
        self.assertEqual(provisioned.idle_minutes, 5)
        self.assertTrue(provisioned.idle_enabled)


class TestMemberPatchNull(unittest.TestCase):
    def test_a_null_idle_field_is_ignored_not_turned_into_a_duplicate_email_error(self):
        from app.schemas.member import MemberUpdate
        from app.services.member_service import MemberService

        member = MagicMock(id=2, date_of_birth=None, date_of_joining=None, can_login=True)
        saved = {}

        def save(db, m, data):
            saved.update(data)
            return m

        payload = MemberUpdate.model_validate(
            {"idle_enabled": None, "idle_minutes": None, "capture_frequency": None, "name": "N"})
        with patch.object(MemberService, "get", return_value=member), \
             patch("app.services.member_service.MemberRepository.save", side_effect=save), \
             patch.object(MemberService, "_notify_access_change"), \
             patch.object(MemberService, "_record"):
            MemberService.update(MagicMock(), MagicMock(id=1), 2, payload)
        self.assertNotIn("idle_enabled", saved)
        self.assertNotIn("idle_minutes", saved)
        self.assertNotIn("capture_frequency", saved)
        self.assertEqual(saved.get("name"), "N")

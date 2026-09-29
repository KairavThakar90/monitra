"""Fixed-hours budget alerts — the properties that must hold in production.

Real SQLite rows for the calculation and the atomic claims; the outbox queue
is recorded rather than written. Allocation is 100h unless stated, so "50h
used" is exactly 50% remaining. The running-timer and true concurrency cases
run against Postgres in the live E2E (SQLite can execute neither the
running-timer expression nor two concurrent writers).
"""
import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.email_notification import TYPE_PROJECT_BUDGET_ALERT
from app.models.manual_time_entry import ManualTimeEntry
from app.models.project import Project
from app.models.project_budget_alert import (
    EVENT_EXHAUSTED, EVENT_REMAINING_10, EVENT_REMAINING_20, EVENT_REMAINING_50, EVENT_START,
    OUTCOME_BASELINE, OUTCOME_NOTIFIED, ProjectBudgetAlert,
)
from app.models.project_member import ProjectMember
from app.models.task import Task
from app.models.time_entry import TimeEntry
from app.models.time_entry_adjustment import TimeEntryAdjustment
from app.models.user import User
from app.repositories.project_budget_alert import ProjectBudgetAlertRepository
from app.services.email import messages
from app.services.project_budget_alerts import (
    ProjectBudgetAlertService, alert_payload, allocation_seconds, crossed_events,
)
from tests.test_project_hours_summary import ADMIN, ORG, _sqlite_schema

H = 3600
WORKFLOWS = "app.services.email.workflows"


def alert_settings(**overrides):
    defaults = {
        "PROJECT_BUDGET_ALERTS_ENABLED": True, "MONITRA_APP_URL": "https://staff.peakworkos.com",
        "EMAIL_PROVIDER": "smtp", "EMAIL_FROM_ADDRESS": "monitra@example.com", "EMAIL_FROM_NAME": "Monitra",
        "EMAIL_REPLY_TO": "", "SMTP_HOST": "smtp.example.com", "MONITRA_SUPPORT_EMAIL": "",
        "EMAIL_ASSET_BASE_URL": "",
    }
    defaults.update(overrides)
    return patch.multiple(settings, **defaults)


class _Db(unittest.TestCase):

    def setUp(self):
        self.engine = create_engine("sqlite://")
        _sqlite_schema(self.engine, Project, ProjectMember, User, Task, TimeEntry, ManualTimeEntry,
                       TimeEntryAdjustment, ProjectBudgetAlert)
        self.db = Session(self.engine)
        self._ids = iter(range(10_000, 1_000_000))
        self.user(ADMIN, "administrator", name="Asha Admin", permissions={"view_employees": True})
        self.queued = []

    def tearDown(self):
        self.db.close()

    # -- rows ------------------------------------------------------------------
    def user(self, user_id, role, *, owner=False, active=True, email=None, name=None, permissions=None):
        row = User(id=user_id, organization_id=ORG, username=f"u{user_id}",
                   email=email if email is not None else f"u{user_id}@example.com",
                   name=name or f"User {user_id}", role_name=role, permissions=permissions or {},
                   status="active" if active else "inactive", is_active=active, idle_enabled=True,
                   idle_minutes=5, capture_frequency=10, can_own_projects=owner)
        self.db.add(row)
        self.db.commit()
        return row

    def project(self, pid=1, *, hours=100, billing="fixed", status="active", version=1, leader_id=None,
                name=None):
        self.db.add(Project(id=pid, organization_id=ORG, project_name=name or f"Project {pid}", description="",
                            status=status, status_id=1, leader_id=leader_id, billing_type=billing,
                            fixed_hours=hours, created_by=ADMIN, budget_version=version,
                            created_at=datetime(2026, 1, 1), updated_at=datetime(2026, 1, 1)))
        self.db.add_all([
            Task(id=pid * 10 + 1, organization_id=ORG, project_id=pid, task_name="Internal Discussion", created_by=ADMIN),
            Task(id=pid * 10 + 2, organization_id=ORG, project_id=pid, task_name="Build", created_by=ADMIN),
        ])
        self.db.commit()

    def work(self, pid, seconds, *, internal=False, start=datetime(2026, 9, 10, tzinfo=timezone.utc)):
        entry_id = next(self._ids)
        self.db.add(TimeEntry(id=entry_id, organization_id=ORG, user_id=entry_id, project_id=pid,
                              task_id=pid * 10 + (1 if internal else 2), start_time=start, end_time=start,
                              total_seconds=seconds, status="stopped", is_manual=False, is_billable=True))
        self.db.commit()

    def use_up_to(self, pid, seconds):
        """Add billable time so the project's Used is exactly `seconds`."""
        from app.services.project_hours import all_time_project_hours
        current = all_time_project_hours(self.db, ORG, [pid])[pid].used_seconds
        if seconds > current:
            self.work(pid, seconds - current)

    # -- running ---------------------------------------------------------------
    def evaluate(self, **kwargs):
        def _queue(db, *, user, project, event, payload):
            self.queued.append((project.id, event, user.id, payload))
            return len(self.queued)

        with alert_settings(), patch(f"{WORKFLOWS}.queue_project_budget_alert", side_effect=_queue):
            return ProjectBudgetAlertService.run(self.db, deliver_now=False, **kwargs)

    def emailed(self, pid=1):
        """Distinct events that produced email for `pid`, in queue order."""
        seen = []
        for project_id, event, _user, _payload in self.queued:
            if project_id == pid and event not in seen:
                seen.append(event)
        return seen

    def rows(self, pid=1):
        return {(r.budget_version, r.event): r.outcome for r in self.db.scalars(
            select(ProjectBudgetAlert).where(ProjectBudgetAlert.project_id == pid))}


# ======================================================================
# The pure rule
# ======================================================================

class TestTheRule(unittest.TestCase):

    def test_boundaries_in_whole_seconds(self):
        A = 100 * H
        self.assertEqual(crossed_events(A, 50 * H - 1), [])
        self.assertEqual(crossed_events(A, 50 * H), [EVENT_REMAINING_50])                        # exactly 50%
        self.assertEqual(crossed_events(A, 80 * H - 1), [EVENT_REMAINING_50])
        self.assertEqual(crossed_events(A, 80 * H), [EVENT_REMAINING_50, EVENT_REMAINING_20])     # exactly 20%
        self.assertEqual(crossed_events(A, 90 * H)[-1], EVENT_REMAINING_10)                      # exactly 10%
        self.assertEqual(crossed_events(A, 100 * H - 1)[-1], EVENT_REMAINING_10)
        self.assertEqual(crossed_events(A, 100 * H)[-1], EVENT_EXHAUSTED)                        # exactly 100%
        self.assertEqual(crossed_events(A, 101 * H)[-1], EVENT_EXHAUSTED)

    def test_the_120h_108h_case_fires_the_10_percent_alert(self):
        """12h of 120h is exactly 10% remaining: the 10% alert fires."""
        self.assertIn(EVENT_REMAINING_10, crossed_events(120 * H, 108 * H))
        self.assertNotIn(EVENT_REMAINING_10, crossed_events(120 * H, 108 * H - 1))

    def test_49_999_hours_is_not_rounded_up_to_50(self):
        self.assertEqual(crossed_events(100 * H, int(49.999 * H)), [])

    def test_allocation_is_exact_in_seconds(self):
        self.assertEqual(allocation_seconds("100.00"), 360000)
        self.assertEqual(allocation_seconds(0.01), 36)


# ======================================================================
# B, C, D, E, F, G — thresholds on a new project (every crossing notified)
# ======================================================================

class TestThresholds(_Db):

    def test_50_20_10_each_once_as_usage_grows(self):
        self.project()
        steps = [(49, []), (50, [EVENT_REMAINING_50]), (51, []), (79, []), (80, [EVENT_REMAINING_20]),
                 (81, []), (89, []), (90, [EVENT_REMAINING_10]), (91, []), (95, [])]
        for used, expected in steps:
            before = len(self.emailed())
            self.use_up_to(1, used * H)
            self.evaluate()
            self.assertEqual(self.emailed()[before:], expected, f"at {used}h")

    def test_exactly_100_percent_is_one_consumed_email_and_101_adds_nothing(self):
        self.project()
        self.use_up_to(1, 99 * H)
        self.evaluate()
        self.use_up_to(1, 100 * H)
        self.evaluate()
        exhausted = [p for pid, e, u, p in self.queued if e == EVENT_EXHAUSTED]
        self.assertEqual(len(exhausted), 1)
        self.assertEqual(exhausted[0]["state"], "consumed")
        self.assertEqual(exhausted[0]["remaining_seconds"], 0)
        self.assertEqual(exhausted[0]["over_budget_seconds"], 0)
        self.use_up_to(1, 101 * H)
        self.evaluate()
        self.assertEqual(len([e for _p, e, _u, _x in self.queued if e == EVENT_EXHAUSTED]), 1, "no second email at 101%")

    def test_over_budget_reports_the_overspend(self):
        self.project()
        self.use_up_to(1, 101 * H)
        self.evaluate()
        [payload] = [p for _pid, e, _u, p in self.queued if e == EVENT_EXHAUSTED]
        self.assertEqual(payload["state"], "over_budget")
        self.assertEqual(payload["over_budget_seconds"], H)
        self.assertEqual(payload["remaining_seconds"], 0)
        self.assertEqual(payload["used_percent"], "101.0")

    def test_a_large_jump_claims_every_crossed_threshold_in_order_once(self):
        self.project()
        self.use_up_to(1, 40 * H)
        self.evaluate()
        self.use_up_to(1, 91 * H)
        self.evaluate()
        self.evaluate()
        self.assertEqual(self.emailed(), [EVENT_REMAINING_50, EVENT_REMAINING_20, EVENT_REMAINING_10])

    def test_a_jump_straight_past_the_budget_sends_all_four_in_order(self):
        self.project()
        self.use_up_to(1, 150 * H)
        self.evaluate()
        self.assertEqual(self.emailed(), [EVENT_REMAINING_50, EVENT_REMAINING_20, EVENT_REMAINING_10, EVENT_EXHAUSTED])

    def test_internal_hours_do_not_consume_the_budget(self):
        self.project()
        self.work(1, 20 * H, internal=True)
        self.work(1, 50 * H)
        self.evaluate()
        [payload] = [p for _pid, e, _u, p in self.queued if e == EVENT_REMAINING_50]
        self.assertEqual(payload["remaining_seconds"], 50 * H, "50h remaining, not 30h")
        self.assertEqual(payload["used_seconds"], 50 * H)
        self.assertEqual(payload["internal_seconds"], 20 * H)
        self.assertEqual(payload["remaining_percent"], "50.0")

    def test_only_internal_time_never_triggers_anything(self):
        self.project()
        self.work(1, 500 * H, internal=True)
        self.evaluate()
        self.assertEqual(self.emailed(), [])

    def test_date_filters_do_not_apply_usage_is_all_time(self):
        self.project()
        self.work(1, 30 * H, start=datetime(2019, 3, 1, tzinfo=timezone.utc))
        self.work(1, 20 * H, start=datetime(2026, 9, 1, tzinfo=timezone.utc))
        self.evaluate()
        self.assertEqual(self.emailed(), [EVENT_REMAINING_50])


# ======================================================================
# A, O, R — which projects are monitored
# ======================================================================

class TestEligibility(_Db):

    def test_flexible_projects_never_alert(self):
        self.project(billing="free", hours=None)
        self.work(1, 10_000 * H)
        tally = self.evaluate()
        self.assertEqual(self.emailed(), [])
        self.assertEqual(self.rows(), {})
        self.assertEqual(tally["projects_scanned"], 0)

    def test_a_fixed_project_without_an_allocation_is_not_monitored(self):
        self.project(hours=0)
        self.work(1, 10 * H)
        self.assertEqual(self.evaluate()["projects_scanned"], 0)

    def test_completed_cancelled_and_archived_are_not_monitored(self):
        for pid, status in ((1, "completed"), (2, "cancelled"), (3, "archived")):
            self.project(pid, status=status)
            self.work(pid, 150 * H)
        self.assertEqual(self.evaluate()["projects_scanned"], 0)
        self.assertEqual(self.queued, [])

    def test_planning_active_todo_and_paused_are_monitored(self):
        for pid, status in ((1, "planning"), (2, "active"), (3, "todo"), (4, "pending")):
            self.project(pid, status=status)
        self.assertEqual(self.evaluate()["projects_scanned"], 4)

    def test_history_is_kept_when_a_project_is_archived(self):
        self.project()
        self.use_up_to(1, 60 * H)
        self.evaluate()
        project = self.db.get(Project, 1)
        project.status = "archived"
        self.db.commit()
        self.use_up_to(1, 95 * H)
        self.evaluate()
        self.assertEqual(self.emailed(), [EVENT_REMAINING_50])
        self.assertIn((1, EVENT_REMAINING_50), self.rows())


# ======================================================================
# C (first deployment) and J (budget changes)
# ======================================================================

class TestFirstDeployment(_Db):
    """Projects that existed when alerts went live carry budget_version 0."""

    def test_70_percent_used_sends_no_historical_50_email(self):
        self.project(version=0)
        self.use_up_to(1, 70 * H)
        self.evaluate()
        self.assertEqual(self.emailed(), [])
        self.assertEqual(self.rows()[(0, EVENT_REMAINING_50)], OUTCOME_BASELINE)

    def test_95_percent_used_sends_no_historical_50_20_10(self):
        self.project(version=0)
        self.use_up_to(1, 95 * H)
        self.evaluate()
        self.assertEqual(self.emailed(), [])
        for event in (EVENT_REMAINING_50, EVENT_REMAINING_20, EVENT_REMAINING_10):
            self.assertEqual(self.rows()[(0, event)], OUTCOME_BASELINE)

    def test_101_percent_used_sends_exactly_one_over_budget_email(self):
        self.project(version=0)
        self.use_up_to(1, 101 * H)
        self.evaluate()
        self.assertEqual(self.emailed(), [EVENT_EXHAUSTED])
        [payload] = [p for _pid, e, _u, p in self.queued]
        self.assertEqual(payload["state"], "over_budget")

    def test_initialisation_is_idempotent(self):
        self.project(version=0)
        self.use_up_to(1, 101 * H)
        self.evaluate()
        self.evaluate()
        self.evaluate()
        self.assertEqual(len(self.queued), 1)

    def test_after_initialisation_later_crossings_are_notified(self):
        self.project(version=0)
        self.use_up_to(1, 60 * H)
        self.evaluate()
        self.use_up_to(1, 85 * H)
        self.evaluate()
        self.assertEqual(self.emailed(), [EVENT_REMAINING_20])

    def test_flexible_and_inactive_projects_are_not_initialised(self):
        self.project(1, version=0, billing="free", hours=None)
        self.project(2, version=0, status="completed")
        self.evaluate()
        self.assertEqual(self.rows(1), {})
        self.assertEqual(self.rows(2), {})

    def test_a_pre_launch_project_reactivated_later_gets_deployment_rules(self):
        self.project(version=0, status="archived")
        self.use_up_to(1, 95 * H)
        self.evaluate()
        project = self.db.get(Project, 1)
        project.status = "active"
        self.db.commit()
        self.evaluate()
        self.assertEqual(self.emailed(), [], "no burst of old thresholds on reactivation")


class TestBudgetChanges(_Db):

    def change_budget(self, hours):
        project = self.db.get(Project, 1)
        project.fixed_hours = hours
        self.db.commit()
        self.db.refresh(project)
        return project.budget_version

    def test_saving_the_same_budget_is_not_a_change(self):
        self.project()
        self.assertEqual(self.change_budget(100), 1)
        self.assertEqual(self.change_budget("100.00"), 1)

    def test_increase_after_the_10_percent_alert(self):
        self.project()
        self.use_up_to(1, 90 * H)
        self.evaluate()
        self.assertEqual(self.emailed(), [EVENT_REMAINING_50, EVENT_REMAINING_20, EVENT_REMAINING_10])
        version = self.change_budget(120)
        self.assertEqual(version, 2)
        self.evaluate()                                    # 90/120 = 25% remaining: 50% already behind
        self.assertEqual(len(self.emailed()), 3, "no email because the budget changed")
        self.assertEqual(self.rows()[(2, EVENT_REMAINING_50)], OUTCOME_BASELINE)
        self.use_up_to(1, 96 * H)                     # 24h of 120h = exactly 20%
        self.evaluate()
        self.use_up_to(1, 108 * H)                    # 12h of 120h = exactly 10%
        self.evaluate()
        v2 = [(e, p["budget_version"]) for _pid, e, _u, p in self.queued if p["budget_version"] == 2]
        self.assertEqual([e for e, _v in dict.fromkeys(v2)], [EVENT_REMAINING_20, EVENT_REMAINING_10])

    def test_decrease_crossing_thresholds_is_silent_then_monitors_normally(self):
        self.project()
        self.use_up_to(1, 70 * H)
        self.evaluate()                                    # v1: 50% notified
        self.change_budget(80)                        # 10/80 = 12.5%: 50 and 20 already behind
        self.evaluate()
        self.assertEqual(self.emailed(), [EVENT_REMAINING_50])
        self.assertEqual(self.rows()[(2, EVENT_REMAINING_20)], OUTCOME_BASELINE)
        self.use_up_to(1, 72 * H)                     # 8/80 = 10%
        self.evaluate()
        self.assertEqual(self.emailed(), [EVENT_REMAINING_50, EVENT_REMAINING_10])

    def test_lowering_below_used_is_silent_and_a_later_version_can_alert_again(self):
        self.project()
        self.use_up_to(1, 101 * H)
        self.evaluate()                                    # v1 exhausted notified
        self.change_budget(90)                        # v2: already exhausted -> silent
        self.evaluate()
        self.assertEqual(self.rows()[(2, EVENT_EXHAUSTED)], OUTCOME_BASELINE)
        self.change_budget(120)                       # v3
        self.evaluate()
        self.use_up_to(1, 121 * H)
        self.evaluate()
        exhausted_versions = [p["budget_version"] for _pid, e, _u, p in self.queued if e == EVENT_EXHAUSTED]
        self.assertEqual(sorted(set(exhausted_versions)), [1, 3])

    def test_each_budget_version_has_independent_state(self):
        self.project()
        self.use_up_to(1, 50 * H)
        self.evaluate()
        self.change_budget(200)                       # 150/200 = 75% remaining: nothing behind
        self.evaluate()
        self.use_up_to(1, 100 * H)                    # 100/200 = 50%
        self.evaluate()
        fifties = [p["budget_version"] for _pid, e, _u, p in self.queued if e == EVENT_REMAINING_50]
        self.assertEqual(sorted(set(fifties)), [1, 2])

    def test_changing_the_project_type_is_a_new_budget(self):
        self.project()
        project = self.db.get(Project, 1)
        project.billing_type = "free"
        project.fixed_hours = None
        self.db.commit()
        self.db.refresh(project)
        self.assertEqual(project.budget_version, 2)


# ======================================================================
# K, L — recipients
# ======================================================================

class TestRecipients(_Db):

    def recipients(self, pid=1):
        return sorted({user for project_id, _e, user, _p in self.queued if project_id == pid})

    def test_admin_owner_and_scoped_leaders_only(self):
        self.user(2, "leader")                      # leads the project
        self.user(3, "leader")                      # staffed on it
        self.user(4, "leader")                      # outside its scope
        self.user(5, "employee", owner=True)        # owner flag
        self.user(6, "employee")                    # worked on it; not a recipient
        self.user(7, "hr")
        self.user(8, "administrator", active=False)
        self.project(leader_id=2)
        self.db.add(ProjectMember(id=1, organization_id=ORG, project_id=1, user_id=3, created_by=ADMIN))
        self.db.add(ProjectMember(id=2, organization_id=ORG, project_id=1, user_id=6, created_by=ADMIN))
        self.db.commit()
        self.use_up_to(1, 50 * H)
        self.evaluate()
        self.assertEqual(self.recipients(), [ADMIN, 2, 3, 5])

    def test_admin_who_is_owner_and_project_leader_gets_one_email(self):
        admin = self.db.get(User, ADMIN)
        admin.can_own_projects = True
        self.db.commit()
        self.project(leader_id=ADMIN)
        self.use_up_to(1, 50 * H)
        self.evaluate()
        self.assertEqual(len([1 for _p, e, u, _x in self.queued if u == ADMIN and e == EVENT_REMAINING_50]), 1)

    def test_two_accounts_sharing_an_address_get_one_email(self):
        # users.email is unique, so the only way two accounts share an address
        # is by case; the recipient dedupe compares case-insensitively.
        self.user(2, "administrator", email=f"U{ADMIN}@EXAMPLE.COM")
        self.project()
        self.use_up_to(1, 50 * H)
        self.evaluate()
        self.assertEqual(len(self.queued), 1)


# ======================================================================
# M, N — idempotency, concurrency and retry
# ======================================================================

class TestIdempotency(_Db):

    def test_the_claim_is_atomic(self):
        self.project()
        claim = dict(organization_id=ORG, project_id=1, budget_version=1, event=EVENT_REMAINING_50,
                     outcome=OUTCOME_NOTIFIED, allocation_seconds=100 * H, used_seconds=50 * H)
        self.assertIsNotNone(ProjectBudgetAlertRepository.claim(self.db, **claim))
        self.db.commit()
        self.assertIsNone(ProjectBudgetAlertRepository.claim(self.db, **claim), "second claimant loses")

    def test_an_evaluation_that_loses_the_start_claim_does_nothing(self):
        """Simulates a concurrent run that initialised the budget first."""
        self.project()
        self.use_up_to(1, 95 * H)
        original = ProjectBudgetAlertRepository.claim

        def lose_start(db, **kwargs):
            if kwargs["event"] == EVENT_START:
                return None
            return original(db, **kwargs)

        with patch.object(ProjectBudgetAlertRepository, "claim", side_effect=lose_start):
            tally = self.evaluate()
        self.assertEqual(tally["skipped"], 1)
        self.assertEqual(self.queued, [])

    def test_a_crash_after_claiming_is_finished_by_the_next_run_without_a_new_claim(self):
        self.user(2, "administrator")
        self.project()
        self.use_up_to(1, 50 * H)
        calls = []

        def flaky(db, *, user, project, event, payload):
            calls.append(user.id)
            if user.id == 2 and len([c for c in calls if c == 2]) == 1:
                raise RuntimeError("mail row refused")
            return len(calls)

        with alert_settings(), patch(f"{WORKFLOWS}.queue_project_budget_alert", side_effect=flaky):
            first = ProjectBudgetAlertService.run(self.db, deliver_now=False)
            [alert] = self.db.scalars(select(ProjectBudgetAlert).where(ProjectBudgetAlert.event == EVENT_REMAINING_50))
            self.assertIsNone(alert.emails_queued_at, "not all recipients queued yet")
            second = ProjectBudgetAlertService.run(self.db, deliver_now=False)
        self.assertEqual(first["failed"], 1)
        self.assertEqual(second["claimed"], 0, "no new claim")
        self.assertEqual(second["requeued"], 1)
        self.db.refresh(alert)
        self.assertIsNotNone(alert.emails_queued_at)

    def test_dry_run_claims_and_queues_nothing(self):
        self.project()
        self.use_up_to(1, 91 * H)
        tally = self.evaluate(dry_run=True)
        self.assertEqual(tally["would_notify"], 3)
        self.assertEqual(self.rows(), {})
        self.assertEqual(self.queued, [])

    def test_the_kill_switch(self):
        self.project()
        self.use_up_to(1, 91 * H)
        with alert_settings(PROJECT_BUDGET_ALERTS_ENABLED=False):
            tally = ProjectBudgetAlertService.run(self.db, deliver_now=False)
        self.assertTrue(tally["disabled"])
        self.assertEqual(self.rows(), {})

    def test_one_failing_project_does_not_stop_the_rest(self):
        self.project(1)
        self.project(2)
        self.use_up_to(1, 50 * H)
        self.use_up_to(2, 50 * H)
        original = ProjectBudgetAlertRepository.claim

        def fail_project_1(db, **kwargs):
            if kwargs["project_id"] == 1:
                raise RuntimeError("boom")
            return original(db, **kwargs)

        with patch.object(ProjectBudgetAlertRepository, "claim", side_effect=fail_project_1):
            tally = self.evaluate()
        self.assertEqual(tally["failed"], 1)
        self.assertEqual(self.emailed(2), [EVENT_REMAINING_50])

    def test_outbox_retry_keeps_one_row_per_recipient(self):
        from app.services.email.outbox import EmailOutboxService
        from app.services.email.provider import EmailDeliveryError
        from app.services.email.workflows import project_budget_alert_dedupe_key

        self.assertEqual(project_budget_alert_dedupe_key(1, 2, EVENT_REMAINING_10, 42), "project:1:v2:remaining_10:user:42")
        row = MagicMock(id=9, notification_type=TYPE_PROJECT_BUDGET_ALERT, attempt_count=1, max_attempts=6,
                        recipients=json.dumps(["a@example.com"]), payload=json.dumps(_payload()), status="pending")
        with alert_settings(), patch("app.services.email.outbox.EmailNotificationRepository") as repository, \
                patch("app.services.email.outbox.get_email_provider") as provider:
            repository.get_by_id.return_value = row
            repository.claim.return_value = row
            provider.return_value.send.side_effect = EmailDeliveryError("mailbox busy")
            self.assertEqual(EmailOutboxService.deliver_one(MagicMock(), 9), "retrying")


# ======================================================================
# P, Q — the email
# ======================================================================

def _payload(**overrides):
    project = MagicMock(id=1, project_name="Project Alpha", status="active", budget_version=1)
    user = MagicMock(id=42, permissions={"view_employees": True})
    user.name = "Asha Admin"
    kwargs = dict(event=EVENT_REMAINING_20, allocation=100 * H, used=80 * H, internal=5 * H,
                  recipient_user=user, generated_at=datetime(2026, 9, 29, 9, 30, tzinfo=timezone.utc))
    kwargs.update(overrides)
    return alert_payload(project, **kwargs)


class TestEmail(unittest.TestCase):

    def render(self, payload=None, **settings_overrides):
        with alert_settings(**settings_overrides):
            return messages.build_project_budget_alert_email(payload or _payload(), ["a@example.com"])

    def test_threshold_email_content(self):
        message = self.render()
        for expected in ("20% Hours Remaining", "Project Alpha", "Fixed Hours", "100h 0m", "80h 0m",
                         "20h 0m", "5h 0m (not counted against the budget)",
                         "29 September 2026, 3:00 PM IST", "Active", "View Project"):
            self.assertIn(expected, message.html, expected)
        # The percentage rows were removed on request; the badge states the threshold.
        for removed in ("Budget used", "20.0%", "80.0%"):
            self.assertNotIn(removed, message.html, removed)
            self.assertNotIn(removed, message.text, removed)
        self.assertEqual(message.subject, "Monitra — 20% Hours Remaining: Project Alpha")

    def test_every_state_has_explicit_text_and_its_own_subject(self):
        cases = {
            (EVENT_REMAINING_50, 50 * H): ("50% Hours Remaining", "Monitra — 50% Hours Remaining: Project Alpha"),
            (EVENT_REMAINING_10, 90 * H): ("10% Hours Remaining", "Monitra — 10% Hours Remaining: Project Alpha"),
            (EVENT_EXHAUSTED, 100 * H): ("100% Hours Consumed", "Monitra — 100% Hours Consumed: Project Alpha"),
            (EVENT_EXHAUSTED, 101 * H): ("OVER BUDGET", "Monitra — Project Over Budget: Project Alpha"),
        }
        for (event, used), (label, subject) in cases.items():
            message = self.render(_payload(event=event, used=used))
            self.assertIn(label, message.html)
            self.assertIn(label.upper(), message.text)
            self.assertEqual(message.subject, subject)
        over = self.render(_payload(event=EVENT_EXHAUSTED, used=101 * H))
        self.assertIn("Over budget by", over.html)
        self.assertIn("1h 0m", over.html)

    def test_the_capsule_is_solid_centred_and_coloured_per_state(self):
        cases = {
            (EVENT_REMAINING_50, 50 * H): ("#FACC15", "color:#1F2937"),   # yellow, dark text
            (EVENT_REMAINING_20, 80 * H): ("#EA580C", "color:#FFFFFF"),   # orange
            (EVENT_REMAINING_10, 90 * H): ("#DC2626", "color:#FFFFFF"),   # red
            (EVENT_EXHAUSTED, 100 * H): ("#991B1B", "color:#FFFFFF"),     # dark red
            (EVENT_EXHAUSTED, 101 * H): ("#991B1B", "color:#FFFFFF"),
        }
        for (event, used), (fill, text_colour) in cases.items():
            html = self.render(_payload(event=event, used=used)).html
            capsule = html[html.index("border-radius:999px") - 200: html.index("border-radius:999px") + 250]
            self.assertIn(f"background-color:{fill}", capsule, event)
            self.assertIn(text_colour, capsule, event)
            self.assertIn("font-weight:800", capsule)
            self.assertIn("font-size:18px", capsule)
            self.assertIn('<td align="center"', html[:html.index("border-radius:999px")][-400:])

    def test_project_names_are_escaped(self):
        message = self.render(_payload(**{}) | {"project_name": "<script>alert(1)</script>"})
        self.assertNotIn("<script>alert(1)", message.html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", message.html)

    def test_cta_points_at_existing_authorised_routes(self):
        with alert_settings():
            self.assertEqual(messages.project_budget_alert_url(_payload()),
                             "https://staff.peakworkos.com/admin/project-management")
            member = _payload() | {"can_view_directory": False}
            self.assertEqual(messages.project_budget_alert_url(member), "https://staff.peakworkos.com/member/projects")
        app = (Path(__file__).resolve().parents[2] / "frontend" / "src" / "App.tsx").read_text(encoding="utf-8")
        self.assertIn('path="/admin/project-management"', app)
        self.assertIn('path="/member/projects"', app)

    def test_no_button_without_a_public_app_url(self):
        self.assertNotIn("View Project", self.render(MONITRA_APP_URL="http://localhost:5173").html)

    def test_shared_frame_and_logos(self):
        message = self.render()
        self.assertIn("cid:monitra-logo", message.html)
        self.assertIn("cid:store-transform-logo", message.html)

    def test_the_outbox_renders_from_stored_json(self):
        with alert_settings():
            html = messages.BUILDERS[TYPE_PROJECT_BUDGET_ALERT](json.loads(json.dumps(_payload())), ["a@example.com"]).html
        self.assertIn("Project Alpha", html)


class TestSharedCalculationAndSecurity(unittest.TestCase):

    def test_the_evaluator_reads_only_the_shared_calculation(self):
        import inspect

        from app.services import project_budget_alerts

        source = inspect.getsource(project_budget_alerts)
        self.assertIn("all_time_project_hours(", source)
        for forbidden in ("session_seconds_by", "TimeEntry", "func.sum"):
            self.assertNotIn(forbidden, source)

    def test_the_cron_runs_every_five_minutes(self):
        config = json.loads((Path(__file__).resolve().parents[2] / "vercel.json").read_text(encoding="utf-8"))
        [entry] = [c for c in config["crons"] if c["path"].startswith("/internal/project-budget-alerts/run")]
        self.assertEqual(entry["schedule"], "*/5 * * * *")

    def test_endpoints_require_the_dispatch_token(self):
        from fastapi.testclient import TestClient

        from app.main import app

        client = TestClient(app)
        with patch.object(settings, "EMAIL_DISPATCH_TOKEN", "secret-token"):
            for path in ("/internal/project-budget-alerts/run?dry_run=true",
                         "/internal/project-budget-alerts/preview?project_id=1&user_id=1"):
                self.assertEqual(client.get(path).status_code, 401, path)

    def test_timer_stop_and_approval_trigger_an_evaluation(self):
        import inspect

        from app.api import time_entry
        from app.services import manual_time_entry, project_management

        self.assertIn("evaluate_project_in_background", inspect.getsource(time_entry.stop_timer))
        self.assertIn("evaluate_project_in_background",
                      inspect.getsource(manual_time_entry.ManualTimeEntryService.update_approval))
        self.assertIn("budget_change", inspect.getsource(project_management.ProjectManagementService.update))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

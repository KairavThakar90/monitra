"""Monthly Project Summary — the properties that must hold in production.

Calculations run against a real SQLite database (the figures are SQL sums);
recipients, idempotency and delivery use mocks where the outbox is involved.
No test sends a message.

Fixture calendar (IST): a timer session is placed by its UTC start, and the
report's month is cut on Asia/Kolkata midnights — 1 Sep 2026 00:00 IST is
2026-08-31T18:30Z.
"""
import json
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.email_notification import STATUS_SENT, TYPE_MONTHLY_PROJECT_SUMMARY
from app.models.manual_time_entry import ManualTimeEntry
from app.models.project import Project
from app.models.project_member import ProjectMember
from app.models.task import Task
from app.models.time_entry import TimeEntry
from app.models.time_entry_adjustment import TimeEntryAdjustment
from app.models.user import User
from app.services.email import messages
from app.services.email.outbox import EmailOutboxService
from app.services.email.provider import EmailDeliveryError
from app.services.email.workflows import queue_monthly_project_summary
from app.services.monthly_project_summary import (
    FIXED, FLEXIBLE, MonthlyProjectSummaryService, Recipient, build_organization_summary,
    resolve_recipients, summary_payload,
)
from app.services.monthly_report import month_containing, monthly_cron_expression, previous_month
from tests.test_project_hours_summary import ADMIN, ORG, _sqlite_schema

HOUR = 3600
SEP = month_containing(date(2026, 9, 15))
SERVICE = "app.services.monthly_project_summary"
WORKFLOWS = "app.services.email.workflows"
OUTBOX = "app.services.email.outbox"


def report_settings(**overrides):
    defaults = {
        "EMAIL_PROVIDER": "smtp", "EMAIL_FROM_ADDRESS": "monitra@example.com",
        "EMAIL_FROM_NAME": "Monitra", "EMAIL_REPLY_TO": "", "SMTP_HOST": "smtp.example.com",
        "SMTP_USERNAME": "", "SMTP_PASSWORD": "", "MONITRA_APP_URL": "https://staff.peakworkos.com",
        "MONITRA_SUPPORT_EMAIL": "", "EMAIL_ASSET_BASE_URL": "", "EMAIL_MAX_ATTEMPTS": 6,
        "WEEKLY_REPORT_TIMEZONE": "Asia/Kolkata", "MONTHLY_REPORT_HOUR": 9, "MONTHLY_REPORT_MINUTE": 0,
        "MONTHLY_PROJECT_SUMMARY_ENABLED": True,
    }
    defaults.update(overrides)
    return patch.multiple(settings, **defaults)


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


# A moment safely inside September in IST.
IN_SEP = utc(2026, 9, 10, 6, 0)


class _Db(unittest.TestCase):
    """A real schema, a helper per row type, and unique ids."""

    def setUp(self):
        self.engine = create_engine("sqlite://")
        _sqlite_schema(self.engine, Project, ProjectMember, User, Task, TimeEntry,
                       ManualTimeEntry, TimeEntryAdjustment)
        self.db = Session(self.engine)
        self._ids = iter(range(1000, 100000))
        self.user(ADMIN, "administrator", name="Asha Admin")

    def tearDown(self):
        self.db.close()

    def user(self, user_id, role, *, name=None, owner=False, active=True, email=None,
             permissions=None, org=ORG):
        row = User(
            id=user_id, organization_id=org, username=f"u{user_id}",
            email=email if email is not None else f"u{user_id}@example.com",
            name=name or f"User {user_id}", role_name=role, permissions=permissions or {},
            status="active" if active else "inactive", is_active=active, idle_enabled=True,
            idle_minutes=5, capture_frequency=10, can_own_projects=owner,
        )
        self.db.add(row)
        self.db.commit()
        return row

    def project(self, project_id, name=None, *, fixed_hours=None, leader_id=None, org=ORG, status="active"):
        self.db.add(Project(
            id=project_id, organization_id=org, project_name=name or f"Project {project_id}",
            description="", status=status, status_id=1, leader_id=leader_id,
            billing_type="fixed" if fixed_hours else "free", fixed_hours=fixed_hours,
            created_by=ADMIN, created_at=datetime(2026, 1, 1), updated_at=datetime(2026, 1, 1),
        ))
        internal = Task(id=project_id * 10 + 1, organization_id=org, project_id=project_id,
                        task_name="Internal Discussion", created_by=ADMIN)
        setup = Task(id=project_id * 10 + 2, organization_id=org, project_id=project_id,
                     task_name="Project Setup / Understanding", created_by=ADMIN)
        work = Task(id=project_id * 10 + 3, organization_id=org, project_id=project_id,
                    task_name="Build the thing", created_by=ADMIN)
        self.db.add_all([internal, setup, work])
        self.db.commit()
        return {"internal": internal.id, "setup": setup.id, "work": work.id}

    def track(self, project_id, task_id, seconds, start=IN_SEP, user_id=None, org=ORG):
        entry_id = next(self._ids)
        self.db.add(TimeEntry(
            id=entry_id, organization_id=org, user_id=user_id or entry_id, project_id=project_id,
            task_id=task_id, start_time=start, end_time=start, total_seconds=seconds,
            status="completed", is_manual=False, is_billable=True,
        ))
        self.db.commit()

    def manual(self, project_id, task_id, seconds, user_id, work_date=date(2026, 9, 12), status="approved"):
        """An approved, unmirrored manual entry -- the second time source the
        shared calculation merges. Also the only way to give one SQLite user a
        second row (the running-timer index is unconditional there)."""
        entry_id = next(self._ids)
        start = datetime.combine(work_date, datetime.min.time()).replace(tzinfo=timezone.utc)
        self.db.add(ManualTimeEntry(
            id=entry_id, organization_id=ORG, user_id=user_id, project_id=project_id, task_id=task_id,
            work_date=work_date, start_time=start, end_time=start, total_seconds=seconds,
            is_billable=True, approval_status=status,
        ))
        self.db.commit()

    def member(self, project_id, user_id):
        self.db.add(ProjectMember(id=next(self._ids), organization_id=ORG, project_id=project_id,
                                  user_id=user_id, created_by=ADMIN))
        self.db.commit()

    def summary(self, period=SEP):
        with report_settings():
            return build_organization_summary(self.db, ORG, period)


# ======================================================================
# A, B, I, S — period and schedule
# ======================================================================

class TestPeriod(unittest.TestCase):

    def test_month_boundaries(self):
        with report_settings():
            cases = {
                date(2026, 10, 1): (date(2026, 9, 1), date(2026, 9, 30)),     # the example
                date(2027, 2, 1): (date(2027, 1, 1), date(2027, 1, 31)),      # January
                date(2027, 3, 1): (date(2027, 2, 1), date(2027, 2, 28)),      # February
                date(2028, 3, 1): (date(2028, 2, 1), date(2028, 2, 29)),      # leap year
                date(2027, 1, 1): (date(2026, 12, 1), date(2026, 12, 31)),    # December → January
            }
            for run_day, expected in cases.items():
                period = previous_month(reference=run_day)
                self.assertEqual((period.start_date, period.end_date), expected, run_day)

    def test_the_window_is_half_open_on_ist_midnights(self):
        with report_settings():
            self.assertEqual(SEP.start_time, utc(2026, 8, 31, 18, 30))
            self.assertEqual(SEP.end_time, utc(2026, 9, 30, 18, 30))

    def test_the_cron_fires_on_the_first_at_the_configured_time(self):
        config = json.loads((Path(__file__).resolve().parents[2] / "vercel.json").read_text(encoding="utf-8"))
        entries = [c for c in config["crons"] if c["path"].startswith("/internal/reports/monthly-projects/run")]
        self.assertEqual(len(entries), 1)
        with report_settings():
            self.assertEqual(entries[0]["schedule"], monthly_cron_expression())
        self.assertEqual(entries[0]["schedule"], "30 3 1 * *")

    def test_a_scheduled_run_reports_the_previous_month(self):
        with report_settings(), patch(f"{SERVICE}.MonthlyProjectSummaryRepository.recipient_candidates",
                                      return_value=[]), \
                patch(f"{SERVICE}.previous_month", return_value=SEP) as previous:
            tally = MonthlyProjectSummaryService.run(MagicMock())
        previous.assert_called_once_with()
        self.assertEqual((tally["month_start"], tally["month_end"]), ("2026-09-01", "2026-09-30"))


class TestTimezoneAndCrossMonth(_Db):

    def test_only_sessions_starting_inside_the_ist_month_count(self):
        tasks = self.project(1, fixed_hours=100)
        self.track(1, tasks["work"], 1 * HOUR, start=utc(2026, 8, 31, 18, 29, 59))   # 31 Aug 23:59:59 IST
        self.track(1, tasks["work"], 2 * HOUR, start=utc(2026, 8, 31, 18, 30))       # 1 Sep 00:00 IST
        self.track(1, tasks["work"], 4 * HOUR, start=utc(2026, 9, 30, 18, 29, 59))   # 30 Sep 23:59:59 IST
        self.track(1, tasks["work"], 8 * HOUR, start=utc(2026, 9, 30, 18, 30))       # 1 Oct 00:00 IST
        [row] = self.summary().projects
        self.assertEqual(row.billable_seconds, 6 * HOUR)

    def test_a_project_worked_only_outside_the_month_is_not_listed(self):
        tasks = self.project(1, fixed_hours=100)
        self.track(1, tasks["work"], HOUR, start=utc(2026, 8, 15))
        self.track(1, tasks["work"], HOUR, start=utc(2026, 10, 15))
        self.assertEqual(self.summary().projects, ())


# ======================================================================
# C, D, E, F, G — the figures
# ======================================================================

class TestFigures(_Db):

    def test_fixed_project_internal_billable_used_and_remaining_as_of_month_end(self):
        tasks = self.project(1, "Alpha", fixed_hours=100)
        self.track(1, tasks["work"], 30 * HOUR, start=utc(2026, 8, 5))     # before the month
        self.track(1, tasks["internal"], 2 * HOUR)                          # this month, internal
        self.track(1, tasks["setup"], 1 * HOUR)                             # this month, internal
        self.track(1, tasks["work"], 6 * HOUR)                              # this month, billable
        self.track(1, tasks["work"], 50 * HOUR, start=utc(2026, 10, 3))    # October: must not count
        [row] = self.summary().projects
        self.assertEqual(row.project_type, FIXED)
        self.assertEqual(row.internal_seconds, 3 * HOUR)
        self.assertEqual(row.billable_seconds, 6 * HOUR)
        self.assertEqual(row.total_seconds, 9 * HOUR)
        self.assertEqual(row.allocation_seconds, 100 * HOUR)
        # Billable used from the start through 30 Sep: 30h + 6h. Internal excluded; October excluded.
        self.assertEqual(row.used_to_date_seconds, 36 * HOUR)
        self.assertEqual(row.remaining_seconds, 64 * HOUR)

    def test_an_over_allocation_project_is_negative_not_zero(self):
        tasks = self.project(1, fixed_hours=100)
        self.track(1, tasks["work"], 108 * HOUR)
        [row] = self.summary().projects
        self.assertEqual(row.remaining_seconds, -8 * HOUR)
        self.assertTrue(row.is_over_allocation)
        self.assertEqual(self.summary().totals()["over_allocation_projects"], 1)

    def test_flexible_project_has_no_remaining(self):
        tasks = self.project(2, "Beta")
        self.track(2, tasks["internal"], 1 * HOUR)
        self.track(2, tasks["work"], 5 * HOUR)
        [row] = self.summary().projects
        self.assertEqual(row.project_type, FLEXIBLE)
        self.assertEqual((row.internal_seconds, row.billable_seconds, row.total_seconds), (HOUR, 5 * HOUR, 6 * HOUR))
        self.assertIsNone(row.remaining_seconds)
        self.assertIsNone(row.allocation_seconds)

    def test_internal_hours_are_the_default_tasks_and_never_double_counted(self):
        tasks = self.project(1, fixed_hours=50)
        self.track(1, tasks["internal"], 2 * HOUR)
        self.track(1, tasks["setup"], 3 * HOUR)
        self.track(1, tasks["work"], 7 * HOUR)
        [row] = self.summary().projects
        self.assertEqual(row.internal_seconds, 5 * HOUR)
        self.assertEqual(row.billable_seconds, 7 * HOUR)
        self.assertEqual(row.internal_seconds + row.billable_seconds, row.total_seconds)
        totals = self.summary().totals()
        self.assertEqual(totals["internal_seconds"] + totals["billable_seconds"], totals["total_seconds"])

    def test_a_default_task_name_on_another_project_is_its_own_projects_internal(self):
        a = self.project(1, fixed_hours=50)
        b = self.project(2)
        self.track(1, a["work"], 4 * HOUR)
        self.track(2, b["internal"], 2 * HOUR)
        rows = {row.project_id: row for row in self.summary().projects}
        self.assertEqual(rows[1].internal_seconds, 0)
        self.assertEqual(rows[2].internal_seconds, 2 * HOUR)

    def test_mixed_three_fixed_and_four_flexible(self):
        for pid in (1, 2, 3):
            tasks = self.project(pid, fixed_hours=10 * pid)
            self.track(pid, tasks["work"], pid * HOUR)
        for pid in (4, 5, 6, 7):
            tasks = self.project(pid)
            self.track(pid, tasks["work"], HOUR)
            self.track(pid, tasks["internal"], HOUR)
        summary = self.summary()
        self.assertEqual(len(summary.fixed), 3)
        self.assertEqual(len(summary.flexible), 4)
        totals = summary.totals()
        self.assertEqual(totals["projects_worked"], 7)
        self.assertEqual(totals["fixed_allocated_seconds"], 60 * HOUR)
        self.assertEqual(totals["fixed_used_to_date_seconds"], 6 * HOUR)
        self.assertEqual(totals["fixed_remaining_seconds"], 54 * HOUR)
        self.assertEqual(totals["flexible_total_seconds"], 8 * HOUR)
        self.assertEqual(totals["total_seconds"], 6 * HOUR + 8 * HOUR)

    def test_contributors_and_highlights(self):
        a = self.project(1, "Alpha", fixed_hours=50)
        b = self.project(2, "Beta")
        self.track(1, a["work"], 4 * HOUR, user_id=501)
        self.track(1, a["internal"], 1 * HOUR, user_id=502)
        self.manual(2, b["internal"], 6 * HOUR, user_id=501)   # 501 again, on another project
        self.manual(2, b["work"], 2 * HOUR, user_id=503, status="pending")   # pending: not counted
        totals = self.summary().totals()
        self.assertEqual(totals["contributors"], 2)
        self.assertEqual(totals["highest_project"]["name"], "Beta")
        self.assertEqual(totals["highest_billable_project"]["name"], "Alpha")
        self.assertEqual(totals["average_seconds_per_project"], int(11 * HOUR / 2))

    def test_an_archived_project_worked_in_the_month_is_still_reported(self):
        tasks = self.project(1, fixed_hours=10, status="archived")
        self.track(1, tasks["work"], HOUR)
        self.assertEqual(len(self.summary().projects), 1)

    def test_another_organisations_projects_never_appear(self):
        tasks = self.project(1)
        self.track(1, tasks["work"], HOUR)
        other = self.project(9, org=2)
        self.track(9, other["work"], HOUR, org=2)
        self.assertEqual([row.project_id for row in self.summary().projects], [1])


class TestQueryCost(_Db):

    def test_the_number_of_aggregate_queries_does_not_grow_with_projects(self):
        from app.repositories.reports import ReportsRepository

        def count_for(n_projects):
            for pid in range(1, n_projects + 1):
                tasks = self.project(pid, fixed_hours=10 if pid % 2 else None)
                self.track(pid, tasks["work"], HOUR)
            with patch.object(ReportsRepository, "session_seconds_by",
                              wraps=ReportsRepository.session_seconds_by) as grouped:
                self.summary()
            return grouped.call_count

        self.assertEqual(count_for(40), 4, "month split (2) + to-date split for fixed projects (2)")


class TestRunningTimer(unittest.TestCase):
    """J. A running session counts its elapsed time so far, bucketed by its
    start — the rule every report uses. Asserted on the shared expression; the
    live E2E exercises it against Postgres."""

    def test_the_shared_duration_rule_counts_a_running_session_as_elapsed_so_far(self):
        from sqlalchemy.dialects import postgresql

        from app.repositories.time_tracking import TimeTrackingRepository

        sql = str(TimeTrackingRepository._duration_expression().compile(dialect=postgresql.dialect()))
        self.assertIn("time_entries.end_time IS NULL", sql)
        self.assertIn("now() - time_entries.start_time", sql)
        self.assertIn("time_entries.total_seconds", sql)

    def test_the_summary_reads_seconds_only_through_that_shared_rule(self):
        """No second engine: the summary sums nothing itself. Its hours come
        from project_hours, which reads session_seconds_by and so inherits
        the running-timer rule above."""
        import inspect

        from app.services import monthly_project_summary, project_hours

        build = inspect.getsource(monthly_project_summary.build_organization_summary)
        self.assertIn("project_hours(", build)
        self.assertIn("all_time_project_hours(", build)
        for forbidden in ("session_seconds_by", "TimeEntry", "func.sum"):
            self.assertNotIn(forbidden, inspect.getsource(monthly_project_summary))
        self.assertIn("session_seconds_by", inspect.getsource(project_hours.project_hours))


# ======================================================================
# K — the email agrees with the application
# ======================================================================

class TestConsistencyWithTheApplication(_Db):

    def test_to_date_figures_equal_project_management_when_nothing_later_exists(self):
        from app.services.project_management import ProjectManagementService

        tasks = self.project(1, fixed_hours=40)
        self.track(1, tasks["work"], 12 * HOUR, start=utc(2026, 7, 1))
        self.track(1, tasks["internal"], 3 * HOUR)
        self.track(1, tasks["work"], 5 * HOUR)
        [row] = self.summary().projects
        [pm] = ProjectManagementService.hours_summary(self.db, self.db.get(User, ADMIN))
        self.assertEqual(row.used_to_date_seconds, pm["total_used_seconds"])
        self.assertEqual(row.remaining_seconds, 40 * HOUR - pm["total_used_seconds"])

    def test_the_month_split_equals_the_shared_calculation_for_the_same_window(self):
        from app.services.project_hours import project_hours

        tasks = self.project(1)
        self.track(1, tasks["internal"], 2 * HOUR)
        self.track(1, tasks["work"], 3 * HOUR)
        shared = project_hours(self.db, ORG, [1], start_time=SEP.start_time, end_time=SEP.end_time,
                               start_date=SEP.start_date, end_date=SEP.end_date)[1]
        [row] = self.summary().projects
        self.assertEqual((row.internal_seconds, row.billable_seconds, row.total_seconds),
                         (shared.internal_seconds, shared.used_seconds, shared.total_seconds))


# ======================================================================
# L, M — recipients and scope
# ======================================================================

class TestRecipients(_Db):

    def _candidates(self):
        from app.repositories.monthly_project_summary import MonthlyProjectSummaryRepository
        return MonthlyProjectSummaryRepository.recipient_candidates(self.db)

    def test_admins_leaders_and_every_owner_are_candidates_and_employees_are_not(self):
        self.user(2, "leader")
        self.user(3, "employee", owner=True)
        self.user(4, "employee", owner=True)
        self.user(5, "manager", owner=True)
        self.user(6, "employee")
        self.user(7, "hr")
        ids = {user.id for user in self._candidates()}
        self.assertEqual(ids, {ADMIN, 2, 3, 4, 5})

    def test_owners_are_resolved_dynamically(self):
        self.user(3, "employee", owner=True)
        self.user(4, "employee", owner=True)
        self.assertEqual(len([u for u in self._candidates() if u.can_own_projects]), 2)
        self.user(5, "employee", owner=True)
        self.assertEqual(len([u for u in self._candidates() if u.can_own_projects]), 3)

    def test_inactive_or_addressless_accounts_are_not_candidates(self):
        self.user(2, "administrator", active=False)
        self.user(3, "leader", active=False)
        self.user(4, "employee", owner=True, active=False)
        self.user(5, "leader", email="")
        self.assertEqual({user.id for user in self._candidates()}, {ADMIN})

    def test_scope_admin_and_owner_company_wide_leader_scoped(self):
        leader = self.user(2, "leader")
        owner = self.user(3, "employee", owner=True)
        recipients = {r.user.id: r for r in resolve_recipients(self.db, [self.db.get(User, ADMIN), leader, owner])}
        self.assertIsNone(recipients[ADMIN].scope)
        self.assertEqual(recipients[ADMIN].audience, "admin")
        self.assertIsNone(recipients[3].scope)
        self.assertEqual(recipients[3].audience, "owner")
        self.assertEqual(recipients[2].audience, "leader")
        self.assertIsNotNone(recipients[2].scope)

    def test_a_leader_who_is_also_an_owner_gets_the_company_summary_once(self):
        both = self.user(2, "leader", owner=True)
        [recipient] = resolve_recipients(self.db, [both])
        self.assertIsNone(recipient.scope)

    def test_a_leader_never_receives_a_project_outside_their_scope(self):
        leader = self.user(2, "leader")
        led = self.project(1, "Led", fixed_hours=20, leader_id=2)
        staffed = self.project(2, "Staffed")
        other = self.project(3, "Someone else's", fixed_hours=99)
        self.member(2, 2)
        for pid, tasks in ((1, led), (2, staffed), (3, other)):
            self.track(pid, tasks["work"], pid * HOUR)
        summary = self.summary()
        [recipient] = resolve_recipients(self.db, [leader])
        payload = summary_payload(summary, recipient)
        names = [p["name"] for p in payload["fixed_projects"] + payload["flexible_projects"]]
        self.assertEqual(sorted(names), ["Led", "Staffed"])
        self.assertNotIn("Someone else's", json.dumps(payload))
        # Totals are recomputed over the leader's projects only.
        self.assertEqual(payload["totals"]["total_seconds"], 3 * HOUR)
        self.assertEqual(payload["totals"]["fixed_allocated_seconds"], 20 * HOUR)
        self.assertFalse(payload["company_wide"])

    def test_admins_and_owners_receive_the_complete_dataset(self):
        owner = self.user(3, "employee", owner=True)
        for pid in (1, 2, 3):
            tasks = self.project(pid, fixed_hours=10 if pid == 1 else None)
            self.track(pid, tasks["work"], HOUR)
        summary = self.summary()
        for user in (self.db.get(User, ADMIN), owner):
            [recipient] = resolve_recipients(self.db, [user])
            payload = summary_payload(summary, recipient)
            self.assertEqual(len(payload["fixed_projects"]) + len(payload["flexible_projects"]), 3)
            self.assertTrue(payload["company_wide"])

    def test_the_run_queues_one_email_per_recipient_at_their_own_scope(self):
        self.user(2, "leader")
        self.user(3, "employee", owner=True)
        self.user(4, "employee")
        led = self.project(1, fixed_hours=20, leader_id=2)
        other = self.project(2)
        self.track(1, led["work"], HOUR)
        self.track(2, other["work"], HOUR)
        queued = {}

        def _queue(db, *, user, period, payload):
            queued[user.id] = payload
            return user.id, True

        with report_settings(), patch(f"{SERVICE}.previous_month", return_value=SEP), \
                patch(f"{WORKFLOWS}.queue_monthly_project_summary", side_effect=_queue):
            tally = MonthlyProjectSummaryService.run(self.db)
        self.assertEqual(set(queued), {ADMIN, 2, 3})
        self.assertEqual(tally["queued"], 3)
        self.assertEqual(tally["projects"], 2)
        self.assertEqual(len(queued[2]["fixed_projects"]) + len(queued[2]["flexible_projects"]), 1)
        self.assertEqual(len(queued[ADMIN]["fixed_projects"]) + len(queued[ADMIN]["flexible_projects"]), 2)

    def test_the_preview_refuses_a_non_recipient(self):
        self.user(4, "employee")
        with report_settings():
            self.assertIsNone(MonthlyProjectSummaryService.preview_payload(self.db, user_id=4, month_start=SEP.start_date))
            self.assertIsNotNone(MonthlyProjectSummaryService.preview_payload(self.db, user_id=ADMIN, month_start=SEP.start_date))


# ======================================================================
# H, P, Q, R — rendering
# ======================================================================

def _payload(**overrides):
    fixed = [{"project_id": 1, "name": "Alpha", "type": FIXED, "type_label": "Fixed Hours",
              "internal_seconds": 3 * HOUR, "billable_seconds": 6 * HOUR, "total_seconds": 9 * HOUR,
              "allocation_seconds": 100 * HOUR, "used_to_date_seconds": 36 * HOUR,
              "remaining_seconds": 64 * HOUR},
             {"project_id": 2, "name": "Overrun", "type": FIXED, "type_label": "Fixed Hours",
              "internal_seconds": 0, "billable_seconds": 8 * HOUR, "total_seconds": 8 * HOUR,
              "allocation_seconds": 100 * HOUR, "used_to_date_seconds": 108 * HOUR,
              "remaining_seconds": -8 * HOUR}]
    flexible = [{"project_id": 3, "name": "Beta", "type": FLEXIBLE, "type_label": "Flexible Time",
                 "internal_seconds": HOUR, "billable_seconds": 5 * HOUR, "total_seconds": 6 * HOUR,
                 "allocation_seconds": None, "used_to_date_seconds": None, "remaining_seconds": None}]
    payload = {
        "user_id": 1, "name": "Asha Admin", "audience": "admin", "company_wide": True,
        "month_start": "2026-09-01", "month_end": "2026-09-30", "month_label": "September 2026",
        "month_short": "Sep 2026", "period_label": "1 September 2026 – 30 September 2026",
        "timezone": "Asia/Kolkata", "can_view_all_time": True,
        "fixed_projects": fixed, "flexible_projects": flexible,
        "totals": {
            "projects_worked": 3, "internal_seconds": 4 * HOUR, "billable_seconds": 19 * HOUR,
            "total_seconds": 23 * HOUR, "fixed_projects": 2, "flexible_projects": 1,
            "fixed_allocated_seconds": 200 * HOUR, "fixed_used_to_date_seconds": 144 * HOUR,
            "fixed_remaining_seconds": 56 * HOUR, "fixed_used_this_month_seconds": 14 * HOUR,
            "over_allocation_projects": 1, "contributors": 4,
            "average_seconds_per_project": int(23 * HOUR / 3),
            "highest_project": {"name": "Alpha", "total_seconds": 9 * HOUR},
            "highest_billable_project": {"name": "Overrun", "billable_seconds": 8 * HOUR},
        },
    }
    payload.update(overrides)
    return payload


class TestRendering(unittest.TestCase):

    def render(self, payload=None, **settings_overrides):
        with report_settings(**settings_overrides):
            return messages.build_monthly_project_summary_email(payload or _payload(), ["a@example.com"])

    def test_header_period_cards_and_sections(self):
        message = self.render()
        for expected in ("Monthly Project Summary", "Project performance summary for September 2026",
                         "1 September 2026 – 30 September 2026", "Total Hours Used", "Internal Hours",
                         "Billable Hours", "23h 0m", "19h 0m", "4h 0m",
                         "Fixed Hours Projects (2)", "Flexible Time Projects (1)",
                         "64h 0m left", "Over by 8h 0m", "100h 0m allocated", "36h 0m used to date",
                         "No fixed allocation", "every project in your organisation"):
            self.assertIn(expected, message.html, f"missing {expected!r}")
        self.assertEqual(message.subject, "Monitra Monthly Project Summary — Sep 2026")
        self.assertIn("Alpha: internal 3h 0m, billable 6h 0m, total 9h 0m", message.text)
        self.assertIn("remaining Over by 8h 0m", message.text)

    def test_the_highlights_are_exactly_the_agreed_rows(self):
        totals = {**_payload()["totals"], "flexible_total_seconds": 6 * HOUR}
        rows = [label for label, _value in messages._highlight_rows(totals)]
        self.assertEqual(rows, ["Fixed hours allocated", "Flexible hours used", "Contributors",
                                "Average hours per project", "Most hours", "Most billable hours"])
        message = self.render(_payload(totals=totals))
        self.assertIn("Flexible hours used", message.html)
        self.assertIn("6h 0m", message.html)
        self.assertIn("Flexible hours used", message.text)
        for removed in ("Fixed-Hours projects", "Flexible-Time projects", "Fixed hours used to date",
                        "Fixed hours remaining", "Projects over allocation"):
            self.assertNotIn(removed, message.html, removed)
            self.assertNotIn(removed, message.text, removed)

    def test_the_flexible_table_has_no_remaining_column(self):
        flexible_table = str(messages._project_table(_payload()["flexible_projects"], fixed=False))
        self.assertNotIn("Remaining", flexible_table)
        self.assertIn("Remaining", str(messages._project_table(_payload()["fixed_projects"], fixed=True)))

    def test_a_leader_is_told_the_summary_is_scoped(self):
        message = self.render(_payload(company_wide=False, audience="leader"))
        self.assertIn("only the projects you lead or work on", message.html)

    def test_an_empty_month_still_renders_with_zeroes_and_the_button(self):
        empty = _payload(fixed_projects=[], flexible_projects=[], totals={
            "projects_worked": 0, "internal_seconds": 0, "billable_seconds": 0, "total_seconds": 0,
            "fixed_projects": 0, "flexible_projects": 0, "contributors": 0,
        })
        message = self.render(empty)
        self.assertIn("No project activity was recorded during this reporting period.", message.html)
        self.assertIn("No project activity was recorded during this reporting period.", message.text)
        self.assertIn("0h 0m", message.html)
        self.assertIn("View Detailed Project Report", message.html)
        self.assertNotIn("Fixed Hours Projects", message.html)

    def test_project_names_are_escaped(self):
        hostile = _payload()
        hostile["fixed_projects"][0]["name"] = "<b>\"A&B's\"</b>"
        hostile["totals"]["highest_project"]["name"] = "<script>x</script>"
        message = self.render(hostile)
        self.assertNotIn("<b>\"A&B", message.html)
        self.assertNotIn("<script>x", message.html)
        self.assertIn("&lt;b&gt;", message.html)
        self.assertIn("&amp;B", message.html)

    def test_the_cta_opens_the_reports_page_for_the_month(self):
        with report_settings():
            self.assertEqual(
                messages.monthly_project_summary_url(_payload()),
                "https://staff.peakworkos.com/dashboard/reports/projects?start=2026-09-01&end=2026-09-30",
            )
            self.assertEqual(
                messages.monthly_project_summary_url(_payload(can_view_all_time=False)),
                "https://staff.peakworkos.com/member/reports/projects?start=2026-09-01&end=2026-09-30",
            )
        message = self.render()
        self.assertEqual(message.html.count("View Detailed Project Report"), 1, "one button, at the end")
        self.assertGreater(message.html.index("View Detailed Project Report"),
                           message.html.index("Flexible Time Projects"), "after the tables")

    def test_the_cta_route_exists_in_the_frontend_and_reads_the_range(self):
        root = Path(__file__).resolve().parents[2] / "frontend" / "src"
        app = (root / "App.tsx").read_text(encoding="utf-8")
        self.assertIn('path="/dashboard/reports/:reportId"', app)
        self.assertIn('path="/member/reports', app)
        report_page = (root / "features" / "dashboard" / "v2" / "ReportPage.tsx").read_text(encoding="utf-8")
        self.assertIn('searchParams.get("start")', report_page)
        self.assertIn('searchParams.get("end")', report_page)

    def test_no_button_without_a_public_https_app_url(self):
        message = self.render(MONITRA_APP_URL="http://localhost:5173")
        self.assertNotIn("View Detailed Project Report", message.html)

    def test_both_logos_and_no_local_references(self):
        message = self.render()
        self.assertIn("cid:monitra-logo", message.html)
        self.assertIn("cid:store-transform-logo", message.html)
        for forbidden in ("file://", "localhost", "127.0.0.1"):
            self.assertNotIn(forbidden, message.html)

    def test_the_outbox_renders_it_from_stored_json(self):
        builder = messages.BUILDERS[TYPE_MONTHLY_PROJECT_SUMMARY]
        with report_settings():
            message = builder(json.loads(json.dumps(_payload())), ["a@example.com"])
        self.assertIn("Alpha", message.html)

    def test_the_other_emails_still_render(self):
        with report_settings():
            self.assertIn("Welcome to Monitra", messages.build_welcome_email({"name": "A"}, ["a@example.com"]).html)
            self.assertIn("MONITRA FEEDBACK RECEIVED", messages.build_feedback_email({
                "user_name": "A", "user_email": "a@example.com", "category": "report_a_problem",
                "message": "x", "submitted_at": "2026-09-10T12:00:00+00:00"}, ["b@example.com"]).text)
            from tests.test_weekly_report import _payload as weekly_payload
            self.assertIn("Weekly Productivity Report",
                          messages.build_weekly_report_email(weekly_payload(), ["a@example.com"]).html)


# ======================================================================
# N, O — idempotency and retry
# ======================================================================

def _user(user_id=1, email="a@example.com"):
    user = MagicMock()
    user.id = user_id
    user.email = email
    user.organization_id = ORG
    return user


class TestIdempotencyAndRetry(unittest.TestCase):

    def test_running_twice_queues_one_notification_per_recipient_per_month(self):
        db = MagicMock()
        existing = MagicMock(id=9, status="pending")
        with report_settings(), patch(f"{OUTBOX}.EmailNotificationRepository") as repository, \
                patch(f"{WORKFLOWS}.EmailNotificationRepository") as lookup:
            lookup.get_by_event.side_effect = [None, existing]
            repository.enqueue.side_effect = [(existing, True), (existing, False)]
            first = queue_monthly_project_summary(db, user=_user(), period=SEP, payload=_payload())
            second = queue_monthly_project_summary(db, user=_user(), period=SEP, payload=_payload())
        self.assertEqual(first, (9, True))
        self.assertEqual(second, (9, False))
        keys = {(c.kwargs["notification_type"], c.kwargs["dedupe_key"]) for c in repository.enqueue.call_args_list}
        self.assertEqual(keys, {("monthly_project_summary", "month:2026-09-01:user:1")})

    def test_a_payload_built_for_someone_else_is_refused(self):
        with self.assertRaises(ValueError):
            queue_monthly_project_summary(MagicMock(), user=_user(user_id=2), period=SEP, payload=_payload())

    def test_one_recipients_failure_does_not_stop_the_others(self):
        users = [MagicMock(id=i, organization_id=ORG, role_name="administrator", can_own_projects=False,
                           permissions={}) for i in (1, 2, 3)]

        def _queue(db, *, user, period, payload):
            if user.id == 2:
                raise RuntimeError("row refused")
            return user.id, True

        with report_settings(), patch(f"{SERVICE}.previous_month", return_value=SEP), \
                patch(f"{SERVICE}.MonthlyProjectSummaryRepository.recipient_candidates", return_value=users), \
                patch(f"{SERVICE}.build_organization_summary",
                      return_value=__import__("app.services.monthly_project_summary", fromlist=["x"]).ProjectSummary(period=SEP)), \
                patch(f"{WORKFLOWS}.queue_monthly_project_summary", side_effect=_queue):
            tally = MonthlyProjectSummaryService.run(MagicMock())
        self.assertEqual((tally["queued"], tally["failed"]), (2, 1))

    def test_a_failed_delivery_stays_retryable_and_a_sent_one_is_never_resent(self):
        row = MagicMock(id=9, notification_type=TYPE_MONTHLY_PROJECT_SUMMARY, attempt_count=1, max_attempts=6,
                        recipients=json.dumps(["b@example.com"]), payload=json.dumps(_payload()), status="pending")
        with report_settings(), patch(f"{OUTBOX}.EmailNotificationRepository") as repository, \
                patch(f"{OUTBOX}.get_email_provider") as provider:
            repository.get_by_id.return_value = row
            repository.claim.return_value = row
            provider.return_value.send.side_effect = EmailDeliveryError("mailbox unavailable")
            self.assertEqual(EmailOutboxService.deliver_one(MagicMock(), 9), "retrying")
        with report_settings(), patch(f"{OUTBOX}.EmailNotificationRepository") as repository:
            repository.get_by_id.return_value = MagicMock(status=STATUS_SENT)
            self.assertEqual(EmailOutboxService.deliver_one(MagicMock(), 9), "skipped")
            repository.claim.assert_not_called()

    def test_the_kill_switch_queues_nothing(self):
        with report_settings(MONTHLY_PROJECT_SUMMARY_ENABLED=False), \
                patch(f"{WORKFLOWS}.queue_monthly_project_summary") as queue:
            tally = MonthlyProjectSummaryService.run(MagicMock())
        queue.assert_not_called()
        self.assertTrue(tally["disabled"])

    def test_the_run_never_raises_into_the_scheduler(self):
        with report_settings(), patch(f"{SERVICE}.MonthlyProjectSummaryRepository.recipient_candidates",
                                      side_effect=RuntimeError("database down")):
            tally = MonthlyProjectSummaryService.run(MagicMock())
        self.assertEqual(tally["failed"], 1)
        self.assertIn("execution_id", tally)
        self.assertIn("duration_ms", tally)


class TestEndpointSecurity(unittest.TestCase):

    def test_run_and_preview_refuse_without_the_dispatch_token(self):
        from fastapi.testclient import TestClient

        from app.main import app

        client = TestClient(app)
        with patch.object(settings, "EMAIL_DISPATCH_TOKEN", "secret-token"):
            for path in ("/internal/reports/monthly-projects/run?dry_run=true",
                         "/internal/reports/monthly-projects/preview?user_id=1"):
                self.assertEqual(client.get(path).status_code, 401, path)
                self.assertEqual(client.get(path, headers={"Authorization": "Bearer wrong"}).status_code, 401)
        with patch.object(settings, "EMAIL_DISPATCH_TOKEN", ""):
            self.assertEqual(client.get("/internal/reports/monthly-projects/run").status_code, 503)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

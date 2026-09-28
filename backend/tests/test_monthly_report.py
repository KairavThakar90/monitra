"""The monthly productivity report — the properties that have to hold in production.

1. **Period.** The previous *completed* calendar month, cut on the reporting
   calendar, never containing the 1st the email is sent on.
2. **Schedule.** The deployed cron entry and the configured send time agree.
3. **Figures.** The six rows the email always shows — total tracked time,
   average activity, projects, total working days, average per working day,
   most productive day — computed from the same entry-grain rows the weekly
   report and the dashboard use.
4. **Privacy / escaping.** One report per person, own figures, name escaped.
5. **Idempotency and retry.** One email per user per month; failures retry.
6. **Run.** Eligibility, containment, kill switch, dry run.

No test here sends a real message: the provider is always mocked.
"""
import json
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.core.config import settings
from app.models.email_notification import STATUS_SENT, TYPE_MONTHLY_REPORT
from app.services.email import messages
from app.services.email.outbox import EmailOutboxService
from app.services.email.provider import EmailDeliveryError
from app.services.email.workflows import (
    monthly_report_dedupe_key, queue_monthly_report,
)
from app.services.monthly_report import (
    MonthlyMetrics, MonthlyReportService, build_month_metrics,
    month_containing, monthly_cron_expression, previous_month,
)

OUTBOX = "app.services.email.outbox"
WORKFLOWS = "app.services.email.workflows"
SERVICE = "app.services.monthly_report"
REPOSITORY = f"{SERVICE}.WeeklyReportRepository"

#: The month every fixture below describes: August 2026.
MONTH = month_containing(date(2026, 8, 15))


def _user(user_id=42, **overrides):
    user = MagicMock()
    user.id = user_id
    user.organization_id = overrides.get("organization_id", 7)
    user.email = overrides.get("email", "priya@example.com")
    user.name = overrides.get("name", "Priya Raman")
    user.permissions = overrides.get("permissions", {})
    return user


def _notification(**overrides):
    row = MagicMock()
    row.id = overrides.get("id", 1)
    row.notification_type = overrides.get("notification_type", TYPE_MONTHLY_REPORT)
    row.dedupe_key = overrides.get("dedupe_key", "month:2026-08-01:user:42")
    row.status = overrides.get("status", "pending")
    row.attempt_count = overrides.get("attempt_count", 1)
    row.max_attempts = overrides.get("max_attempts", 6)
    row.user_id = overrides.get("user_id", 42)
    row.recipients = overrides.get("recipients", json.dumps(["priya@example.com"]))
    row.payload = overrides.get("payload", json.dumps(_payload()))
    return row


def report_settings(**overrides):
    defaults = {
        "EMAIL_PROVIDER": "smtp",
        "EMAIL_FROM_ADDRESS": "monitra@example.com",
        "EMAIL_FROM_NAME": "Monitra",
        "EMAIL_REPLY_TO": "",
        "SMTP_HOST": "smtp.example.com",
        "SMTP_USERNAME": "",
        "SMTP_PASSWORD": "",
        "MONITRA_APP_URL": "https://staff.peakworkos.com",
        "MONITRA_SUPPORT_EMAIL": "",
        "EMAIL_ASSET_BASE_URL": "",
        "EMAIL_MAX_ATTEMPTS": 6,
        "WEEKLY_REPORT_TIMEZONE": "Asia/Kolkata",
        "MONTHLY_REPORT_ENABLED": True,
        "MONTHLY_REPORT_HOUR": 9,
        "MONTHLY_REPORT_MINUTE": 0,
    }
    defaults.update(overrides)
    return patch.multiple(settings, **defaults)


def _days():
    from app.services.monthly_report import MonthDayFigure
    return [
        MonthDayFigure(day=date(2026, 8, 3), total_seconds=27000, activity=80.0),   # 7h 30m
        MonthDayFigure(day=date(2026, 8, 4), total_seconds=33000, activity=70.0),   # 9h 10m
        MonthDayFigure(day=date(2026, 8, 5), total_seconds=30000, activity=75.0),
        MonthDayFigure(day=date(2026, 8, 6), total_seconds=0, activity=None),       # not a working day
    ]


def _metrics(user_id=42, **overrides) -> MonthlyMetrics:
    figures = MonthlyMetrics(user_id=user_id)
    figures.total_seconds = overrides.get("total_seconds", 90000)  # 25h 0m
    figures.average_activity = overrides.get("average_activity", 75.0)
    figures.project_count = overrides.get("project_count", 3)
    figures.entry_count = overrides.get("entry_count", 12)
    figures.days = overrides.get("days", _days())
    return figures


def _payload(**overrides):
    from app.services.email.workflows import _monthly_report_payload

    user_id = overrides.pop("user_id", 42)
    user = _user(user_id=user_id, **{
        key: overrides.pop(key) for key in ("name", "email", "permissions") if key in overrides
    })
    return _monthly_report_payload(
        user=user, period=MONTH, metrics=_metrics(user_id=user_id, **overrides),
    )


def _aggregates(**overrides):
    defaults = {"totals_by_user": {}, "daily_by_user": {}}
    defaults.update(overrides)
    return patch.multiple(
        REPOSITORY, **{name: MagicMock(return_value=value) for name, value in defaults.items()},
    )


def _totals(user_id=42, **overrides):
    row = {"total_seconds": 90000, "act_sum": 750.0, "act_count": 10,
           "entry_count": 12, "project_count": 3}
    row.update(overrides)
    return {user_id: row}


# ======================================================================
# 1 — the period
# ======================================================================

class TestReportPeriod(unittest.TestCase):

    def test_the_period_is_the_previous_completed_month(self):
        with report_settings():
            period = previous_month(reference=date(2026, 9, 1))
        self.assertEqual(period.start_date, date(2026, 8, 1))
        self.assertEqual(period.end_date, date(2026, 8, 31))
        self.assertEqual(period.calendar_days, 31)

    def test_january_reports_december_of_the_previous_year(self):
        with report_settings():
            period = previous_month(reference=date(2027, 1, 1))
        self.assertEqual((period.start_date, period.end_date), (date(2026, 12, 1), date(2026, 12, 31)))

    def test_february_length_is_taken_from_the_calendar(self):
        with report_settings():
            self.assertEqual(previous_month(reference=date(2028, 3, 1)).end_date, date(2028, 2, 29))
            self.assertEqual(previous_month(reference=date(2027, 3, 1)).end_date, date(2027, 2, 28))

    def test_the_period_never_contains_any_part_of_the_day_it_is_sent(self):
        with report_settings():
            period = previous_month(reference=date(2026, 9, 1))
        # 00:00 IST on 1 Sep is 18:30 UTC on 31 Aug, and it is excluded.
        self.assertEqual(period.end_time.isoformat(), "2026-08-31T18:30:00+00:00")
        self.assertEqual(period.start_time.isoformat(), "2026-07-31T18:30:00+00:00")

    def test_a_run_on_any_day_of_the_month_reports_the_same_previous_month(self):
        with report_settings():
            for day in (1, 2, 15, 30):
                self.assertEqual(previous_month(reference=date(2026, 9, day)).start_date, date(2026, 8, 1))

    def test_a_mid_month_override_is_normalised_to_the_whole_month(self):
        self.assertEqual((MONTH.start_date, MONTH.end_date), (date(2026, 8, 1), date(2026, 8, 31)))
        self.assertEqual(MONTH.label, "01 Aug 2026 – 31 Aug 2026")
        self.assertEqual(MONTH.short_label, "Aug 2026")


class TestSchedule(unittest.TestCase):

    def test_first_of_month_nine_am_ist_is_three_thirty_utc_on_the_first(self):
        with report_settings():
            self.assertEqual(monthly_cron_expression(), "30 3 1 * *")

    def test_a_local_time_that_is_still_the_previous_utc_day_is_refused(self):
        """A five-field cron cannot say "last day of month", so this must not
        be silently scheduled for the wrong day."""
        with report_settings(MONTHLY_REPORT_HOUR=0, MONTHLY_REPORT_MINUTE=30):
            with self.assertRaises(ValueError):
                monthly_cron_expression()

    def test_the_deployed_cron_entry_matches_the_configured_send_time(self):
        config = json.loads(
            (Path(__file__).resolve().parents[2] / "vercel.json").read_text(encoding="utf-8")
        )
        monthly = [
            cron for cron in config.get("crons", [])
            if cron.get("path", "").startswith("/internal/reports/monthly/run")
        ]
        self.assertEqual(len(monthly), 1, "exactly one monthly-report cron entry")
        with report_settings():
            self.assertEqual(monthly[0]["schedule"], monthly_cron_expression())
        self.assertEqual(monthly[0]["schedule"].split()[2], "1", "fires on the 1st of the month")


# ======================================================================
# 2 — the figures
# ======================================================================

class TestFigures(unittest.TestCase):

    def _build(self, **aggregates):
        with report_settings(), _aggregates(**aggregates):
            return build_month_metrics(MagicMock(), organization_id=7, user_ids=[42], period=MONTH)[42]

    def test_a_normal_month_reports_every_metric(self):
        figures = self._build(
            totals_by_user=_totals(),
            daily_by_user={42: [
                {"day": date(2026, 8, 3), "total_seconds": 27000, "act_sum": 160.0, "act_count": 2},
                {"day": date(2026, 8, 4), "total_seconds": 33000, "act_sum": 140.0, "act_count": 2},
                {"day": date(2026, 8, 5), "total_seconds": 30000, "act_sum": 150.0, "act_count": 2},
            ]},
        )
        self.assertEqual(figures.total_seconds, 90000)
        self.assertEqual(figures.average_activity, 75.0)
        self.assertEqual(figures.project_count, 3)
        self.assertEqual(figures.working_days, 3)
        self.assertEqual(figures.average_seconds_per_working_day, 30000)
        self.assertEqual(figures.most_productive_day.day, date(2026, 8, 4))
        self.assertEqual(figures.most_productive_day.label, "Tue, 04 Aug")
        self.assertTrue(figures.has_activity)

    def test_working_days_count_only_days_with_tracked_time(self):
        figures = MonthlyMetrics(user_id=42, total_seconds=60000, days=_days())
        self.assertEqual(figures.working_days, 3)

    def test_average_per_working_day_divides_by_working_days_not_calendar_days(self):
        figures = MonthlyMetrics(user_id=42, total_seconds=90000, days=_days())
        self.assertEqual(figures.average_seconds_per_working_day, 30000)
        self.assertEqual(messages.format_duration(figures.average_seconds_per_working_day), "8h 20m")

    def test_most_productive_day_is_the_day_with_the_most_tracked_time(self):
        figures = MonthlyMetrics(user_id=42, days=_days())
        self.assertEqual(figures.most_productive_day.day, date(2026, 8, 4))

    def test_an_empty_month_computes_without_dividing_by_zero(self):
        figures = MonthlyMetrics(user_id=42)
        self.assertEqual(figures.working_days, 0)
        self.assertEqual(figures.average_seconds_per_working_day, 0)
        self.assertIsNone(figures.most_productive_day)
        self.assertFalse(figures.has_activity)

    def test_activity_is_none_rather_than_zero_when_nothing_was_sampled(self):
        figures = self._build(totals_by_user=_totals(act_sum=0.0, act_count=0))
        self.assertIsNone(figures.average_activity)

    def test_the_filters_handed_to_the_aggregation_are_the_month_and_the_user(self):
        captured = {}

        def _capture(db, filters, *args, **kwargs):
            captured["filters"] = filters
            return {}

        with report_settings(), patch.multiple(
            REPOSITORY,
            totals_by_user=MagicMock(side_effect=_capture),
            daily_by_user=MagicMock(return_value={}),
        ):
            build_month_metrics(MagicMock(), organization_id=7, user_ids=[42], period=MONTH)

        filters = captured["filters"]
        self.assertEqual(filters.organization_id, 7)
        self.assertEqual(filters.member_ids, (42,))
        self.assertEqual((filters.start_date, filters.end_date), (MONTH.start_date, MONTH.end_date))
        self.assertEqual((filters.start_time, filters.end_time), (MONTH.start_time, MONTH.end_time))

    def test_the_month_reads_the_same_repository_the_weekly_report_and_dashboard_use(self):
        """No second aggregation: the monthly figures are the dashboard's
        entry-grain rows grouped over a wider period."""
        import app.services.monthly_report as service
        from app.repositories.weekly_report import WeeklyReportRepository

        self.assertIs(service.WeeklyReportRepository, WeeklyReportRepository)


# ======================================================================
# 3 — privacy, escaping, rendering
# ======================================================================

class TestPrivacyAndRendering(unittest.TestCase):

    def test_metrics_belonging_to_another_user_are_refused(self):
        from app.services.email.workflows import _monthly_report_payload

        with self.assertRaises(ValueError):
            _monthly_report_payload(user=_user(user_id=42), period=MONTH, metrics=_metrics(user_id=43))

    def test_the_payload_carries_no_internal_identifiers_or_permission_map(self):
        payload = _payload(permissions={"time_entries:view_all": True})
        self.assertNotIn("permissions", payload)
        self.assertNotIn("email", payload)
        self.assertTrue(payload["can_view_all_time"])

    def test_a_user_name_cannot_become_markup(self):
        with report_settings():
            message = messages.build_monthly_report_email(
                _payload(name="<script>alert(1)</script> Raman"), ["priya@example.com"],
            )
        self.assertNotIn("<script>", message.html)
        self.assertIn("&lt;script&gt;", message.html)

    def test_the_email_shows_all_six_rows_in_both_renderings(self):
        with report_settings():
            message = messages.build_monthly_report_email(_payload(), ["priya@example.com"])
        for label, value in (
            ("Total tracked time", "25h 0m"),
            ("Average activity", "75%"),
            ("Projects", "3"),
            ("Total working days", "3"),
            ("Average per working day", "8h 20m"),
            ("Most productive day", "Tue, 04 Aug (9h 10m)"),
        ):
            self.assertIn(label, message.html, f"missing {label!r} in html")
            self.assertIn(value, message.html, f"missing {value!r} in html")
            self.assertIn(label, message.text, f"missing {label!r} in text")
            self.assertIn(value, message.text, f"missing {value!r} in text")
        self.assertIn("01 Aug 2026", message.html)
        self.assertIn("31 Aug 2026", message.html)
        self.assertIn("Monthly Productivity Report", message.html)
        self.assertIn("Month of", message.html)
        self.assertEqual(str(messages._monthly_summary_table(_payload())).count("<table"), 1)

    def test_an_empty_month_still_renders_every_row_honestly(self):
        with report_settings():
            message = messages.build_monthly_report_email(
                _payload(total_seconds=0, project_count=0, entry_count=0,
                         average_activity=None, days=[]),
                ["priya@example.com"],
            )
        self.assertIn("no tracked activity", message.html)
        self.assertIn("0h 0m", message.html)
        self.assertIn("No activity", message.html)
        self.assertIn("Total working days", message.html)
        self.assertIn("No tracked days", message.html)
        self.assertIn("start a timer", message.text.lower())

    def test_the_subject_names_the_month_and_no_figure(self):
        with report_settings():
            subject = messages.monthly_report_subject(_payload())
        self.assertIn("Monthly Report", subject)
        self.assertIn("Aug 2026", subject)
        self.assertNotIn("25h", subject)

    def test_both_logos_are_carried_by_the_message(self):
        with report_settings():
            message = messages.build_monthly_report_email(_payload(), ["priya@example.com"])
        self.assertIn("cid:monitra-logo", message.html)
        self.assertIn("cid:store-transform-logo", message.html)

    def test_the_dashboard_button_opens_the_reported_month(self):
        with report_settings():
            self.assertEqual(
                messages.monthly_report_dashboard_url(_payload()),
                "https://staff.peakworkos.com/member/reports/projects?start=2026-08-01&end=2026-08-31",
            )
            admin = messages.monthly_report_dashboard_url(_payload(permissions={"time_entries:view_all": True}))
        self.assertTrue(admin.startswith("https://staff.peakworkos.com/dashboard/reports/projects"))

    def test_no_button_is_rendered_for_a_localhost_or_empty_app_url(self):
        for value in ("", "http://localhost:5173"):
            with self.subTest(app_url=value), report_settings(MONITRA_APP_URL=value):
                self.assertIsNone(messages.monthly_report_dashboard_url(_payload()))
                html = messages.build_monthly_report_email(_payload(), ["priya@example.com"]).html
                self.assertNotIn("View Detailed Report", html)

    def test_the_weekly_button_still_opens_the_reported_week(self):
        """The URL builder was shared, not moved: the weekly email is unchanged."""
        from tests.test_weekly_report import _payload as weekly_payload, report_settings as weekly_settings

        with weekly_settings():
            self.assertEqual(
                messages.weekly_report_dashboard_url(weekly_payload()),
                "https://staff.peakworkos.com/member/reports/projects?start=2026-09-07&end=2026-09-13",
            )


# ======================================================================
# 4 — idempotency and retry
# ======================================================================

class TestIdempotencyAndRetry(unittest.TestCase):

    def test_the_dedupe_key_is_the_month_and_the_user(self):
        self.assertEqual(monthly_report_dedupe_key(date(2026, 8, 1), 42), "month:2026-08-01:user:42")
        self.assertNotEqual(
            monthly_report_dedupe_key(date(2026, 8, 1), 42), monthly_report_dedupe_key(date(2026, 9, 1), 42),
        )

    def test_running_the_scheduler_twice_queues_one_notification(self):
        db = MagicMock()
        existing = _notification(id=9)
        with report_settings(), patch(f"{OUTBOX}.EmailNotificationRepository") as repository, \
                patch(f"{WORKFLOWS}.EmailNotificationRepository") as lookup:
            lookup.get_by_event.side_effect = [None, existing]
            repository.enqueue.side_effect = [(existing, True), (existing, False)]
            first_id, first_created = queue_monthly_report(db, user=_user(), period=MONTH, metrics=_metrics())
            second_id, second_created = queue_monthly_report(db, user=_user(), period=MONTH, metrics=_metrics())

        self.assertEqual(first_id, second_id)
        self.assertTrue(first_created)
        self.assertFalse(second_created)
        keys = {call.kwargs["dedupe_key"] for call in repository.enqueue.call_args_list}
        self.assertEqual(keys, {"month:2026-08-01:user:42"})
        types = {call.kwargs["notification_type"] for call in repository.enqueue.call_args_list}
        self.assertEqual(types, {"monthly_report"})

    def test_each_user_is_queued_separately_with_only_their_own_address(self):
        db = MagicMock()
        with report_settings(), patch(f"{WORKFLOWS}.EmailOutboxService") as outbox, \
                patch(f"{WORKFLOWS}.EmailNotificationRepository") as lookup:
            lookup.get_by_event.return_value = None
            outbox.enqueue.return_value = _notification()
            queue_monthly_report(db, user=_user(user_id=1, email="a@example.com"), period=MONTH,
                                 metrics=_metrics(user_id=1))
            queue_monthly_report(db, user=_user(user_id=2, email="b@example.com"), period=MONTH,
                                 metrics=_metrics(user_id=2))
        recipients = [call.kwargs["recipients"] for call in outbox.enqueue.call_args_list]
        self.assertEqual(recipients, [["a@example.com"], ["b@example.com"]])

    def test_an_already_sent_report_is_never_resent(self):
        db = MagicMock()
        with report_settings(), patch(f"{OUTBOX}.EmailNotificationRepository") as repository:
            repository.get_by_id.return_value = _notification(id=9, status=STATUS_SENT)
            self.assertEqual(EmailOutboxService.deliver_one(db, 9), "skipped")
        repository.claim.assert_not_called()

    def test_a_delivery_failure_leaves_the_report_pending_for_retry(self):
        db = MagicMock()
        row = _notification(id=9, attempt_count=1, max_attempts=6)
        with report_settings(), patch(f"{OUTBOX}.EmailNotificationRepository") as repository, \
                patch(f"{OUTBOX}.get_email_provider") as provider:
            repository.get_by_id.return_value = row
            repository.claim.return_value = row
            provider.return_value.send.side_effect = EmailDeliveryError("mail server refused")
            self.assertEqual(EmailOutboxService.deliver_one(db, 9), "retrying")
        self.assertIsNone(repository.mark_attempt_failed.call_args.kwargs["terminal_status"])

    def test_a_queued_monthly_report_renders_from_its_stored_payload(self):
        """The dispatcher's own path: builder looked up by type, payload as JSON."""
        from app.services.email.messages import BUILDERS

        builder = BUILDERS.get(TYPE_MONTHLY_REPORT)
        self.assertIsNotNone(builder)
        with report_settings():
            message = builder(json.loads(json.dumps(_payload())), ["priya@example.com"])
        self.assertIn("25h 0m", message.html)
        self.assertIn("Total working days", message.html)


# ======================================================================
# 5 — the run
# ======================================================================

class TestRun(unittest.TestCase):

    def test_only_active_accounts_with_an_address_are_reported_on(self):
        db = MagicMock()
        with patch(f"{SERVICE}.UserRepository.list_announcement_recipients", return_value=[]) as recipients:
            MonthlyReportService.eligible_users(db)
        recipients.assert_called_once_with(db)

    def test_a_named_user_is_still_subject_to_the_eligibility_rule(self):
        active = _user(user_id=42)
        with patch(f"{SERVICE}.UserRepository.list_announcement_recipients", return_value=[active]):
            self.assertEqual(MonthlyReportService.eligible_users(MagicMock(), user_id=42), [active])
            self.assertEqual(MonthlyReportService.eligible_users(MagicMock(), user_id=99), [])

    def test_one_users_failure_does_not_stop_the_others(self):
        users = [_user(user_id=i, email=f"user{i}@example.com") for i in range(1, 11)]

        def _queue(db_, *, user, period, metrics):
            if user.id == 3:
                raise RuntimeError("this one row is broken")
            return user.id, True

        with report_settings(), \
                patch(f"{SERVICE}.UserRepository.list_announcement_recipients", return_value=users), \
                _aggregates(), patch(f"{WORKFLOWS}.queue_monthly_report", side_effect=_queue):
            tally = MonthlyReportService.run(MagicMock())
        self.assertEqual((tally["eligible_users"], tally["queued"], tally["failed"]), (10, 9, 1))

    def test_the_kill_switch_queues_nothing(self):
        with report_settings(MONTHLY_REPORT_ENABLED=False), \
                patch(f"{WORKFLOWS}.queue_monthly_report") as queue:
            tally = MonthlyReportService.run(MagicMock())
        queue.assert_not_called()
        self.assertTrue(tally["disabled"])

    def test_a_dry_run_computes_without_queueing(self):
        with report_settings(), \
                patch(f"{SERVICE}.UserRepository.list_announcement_recipients", return_value=[_user()]), \
                _aggregates(totals_by_user=_totals()), \
                patch(f"{WORKFLOWS}.queue_monthly_report") as queue:
            tally = MonthlyReportService.run(MagicMock(), dry_run=True)
        queue.assert_not_called()
        self.assertTrue(tally["dry_run"])
        self.assertEqual(tally["queued"], 1)

    def test_the_run_reports_an_existing_month_as_already_queued_not_as_new(self):
        with report_settings(), \
                patch(f"{SERVICE}.UserRepository.list_announcement_recipients", return_value=[_user()]), \
                _aggregates(totals_by_user=_totals()), \
                patch(f"{WORKFLOWS}.queue_monthly_report", return_value=(9, False)):
            tally = MonthlyReportService.run(MagicMock())
        self.assertEqual((tally["queued"], tally["already_queued"]), (0, 1))

    def test_each_organisation_is_aggregated_once_however_many_people_it_has(self):
        users = [_user(user_id=i, organization_id=7) for i in range(1, 51)]
        with report_settings(), \
                patch(f"{SERVICE}.UserRepository.list_announcement_recipients", return_value=users), \
                patch(f"{SERVICE}.build_month_metrics", return_value={}) as aggregate, \
                patch(f"{WORKFLOWS}.queue_monthly_report", return_value=(1, True)):
            MonthlyReportService.run(MagicMock())
        self.assertEqual(aggregate.call_count, 1)
        self.assertEqual(len(aggregate.call_args.kwargs["user_ids"]), 50)

    def test_the_run_never_raises_into_its_scheduler(self):
        with report_settings(), \
                patch(f"{SERVICE}.UserRepository.list_announcement_recipients",
                      side_effect=RuntimeError("database is on fire")):
            tally = MonthlyReportService.run(MagicMock())
        self.assertEqual(tally["failed"], 1)

    def test_the_run_defaults_to_the_previous_month_and_reports_it(self):
        with report_settings(), \
                patch(f"{SERVICE}.UserRepository.list_announcement_recipients", return_value=[]):
            tally = MonthlyReportService.run(MagicMock(), month_start=date(2026, 8, 20))
        self.assertEqual((tally["month_start"], tally["month_end"]), ("2026-08-01", "2026-08-31"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

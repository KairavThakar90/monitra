"""The weekly productivity report — the properties that have to hold in production.

Grouped by the thing that would actually go wrong if it broke:

1. **Period.** The report covers the previous *completed* week, cut on the
   configured calendar. A boundary that drifts by one day either reports the
   wrong week or leaks part of the Monday the email is sent on.
2. **Schedule.** The configured send time and the deployed cron entry are
   derived from one source and asserted to still agree, so changing one and
   forgetting the other is a failing test rather than mail at the wrong hour.
3. **Figures.** Tracked time, active and idle time, average activity, the
   activity extremes and the project count are computed from the same
   entry-grain rows the dashboard groups over — asserted structurally, so a
   future "quick fix" that writes a second aggregation is caught rather than
   merely discouraged.
4. **Privacy.** One report per person, addressed to that person, containing
   only their own figures. The pairing of address to data is checked, not
   assumed.
5. **Escaping.** The reader's own name is the one piece of user-written text
   this email interpolates. It renders as characters.
6. **Idempotency and retry.** One email per user per week across scheduler
   retries, replays and re-runs; failures stay retryable; a sent report is
   never sent again.
7. **No regressions.** The welcome and feedback workflows still work.

No test here sends a real message: the provider is always mocked.
"""
import json
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.core.config import settings
from app.models.email_notification import (
    STATUS_FAILED, STATUS_PENDING, STATUS_SENT, TYPE_WEEKLY_REPORT,
)
from app.services.email import messages
from app.services.email.outbox import EmailOutboxService
from app.services.email.provider import EmailDeliveryError
from app.services.email.workflows import (
    queue_feedback_notification, queue_welcome_email, queue_weekly_report,
    weekly_report_dedupe_key,
)
from app.services.weekly_report import (
    WeeklyMetrics, WeeklyReportService, build_week_metrics, previous_week,
    week_containing, weekly_cron_expression,
)

OUTBOX = "app.services.email.outbox"
WORKFLOWS = "app.services.email.workflows"
SERVICE = "app.services.weekly_report"
REPOSITORY = f"{SERVICE}.WeeklyReportRepository"

#: The week every fixture below describes: Monday 8th to Sunday 14th.
WEEK = week_containing(date(2026, 9, 8))


def _user(user_id=42, **overrides):
    user = MagicMock()
    user.id = user_id
    user.organization_id = overrides.get("organization_id", 7)
    user.email = overrides.get("email", "priya@example.com")
    user.name = overrides.get("name", "Priya Raman")
    # An explicit dict: a bare MagicMock attribute is truthy, which would make
    # every fixture look like an administrator.
    user.permissions = overrides.get("permissions", {})
    return user


def _notification(**overrides):
    row = MagicMock()
    row.id = overrides.get("id", 1)
    row.notification_type = overrides.get("notification_type", TYPE_WEEKLY_REPORT)
    row.dedupe_key = overrides.get("dedupe_key", "week:2026-09-08:user:42")
    row.status = overrides.get("status", STATUS_PENDING)
    row.attempt_count = overrides.get("attempt_count", 1)
    row.max_attempts = overrides.get("max_attempts", 6)
    row.user_id = overrides.get("user_id", 42)
    row.recipients = overrides.get("recipients", json.dumps(["priya@example.com"]))
    row.payload = overrides.get("payload", json.dumps(_payload()))
    return row


def report_settings(**overrides):
    """Patch configuration for the duration of a `with` block."""
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
        "WEEKLY_REPORT_ENABLED": True,
        "WEEKLY_REPORT_TIMEZONE": "Asia/Kolkata",
        "WEEKLY_REPORT_DAY": "monday",
        "WEEKLY_REPORT_HOUR": 9,
        "WEEKLY_REPORT_MINUTE": 0,
    }
    defaults.update(overrides)
    return patch.multiple(settings, **defaults)


def _metrics(user_id=42, **overrides) -> WeeklyMetrics:
    figures = WeeklyMetrics(user_id=user_id)
    figures.total_seconds = overrides.get("total_seconds", 139320)  # 38h 42m
    figures.average_activity = overrides.get("average_activity", 78.0)
    figures.project_count = overrides.get("project_count", 5)
    figures.entry_count = overrides.get("entry_count", 23)
    figures.idle_seconds = overrides.get("idle_seconds", 16020)  # 4h 27m
    return figures


def _payload(**overrides):
    """The payload `queue_weekly_report` would freeze onto the outbox row."""
    from app.services.email.workflows import _weekly_report_payload

    user_id = overrides.pop("user_id", 42)
    user = _user(user_id=user_id, **{
        key: overrides.pop(key) for key in ("name", "email", "permissions")
        if key in overrides
    })
    payload = _weekly_report_payload(
        user=user, period=WEEK, metrics=_metrics(user_id=user_id, **overrides),
    )
    return payload


# ======================================================================
# 1 — the report period
# ======================================================================

class TestReportPeriod(unittest.TestCase):
    """TEST 1 and TEST 2: previous-week calculation, and its timezone boundary."""

    def test_the_period_is_the_previous_completed_week(self):
        # Run on Monday 15 September: the week reported is the Monday-to-Sunday
        # before it, never the one the run is standing in.
        with report_settings():
            period = previous_week(date(2026, 9, 14))  # a Monday
        self.assertEqual(period.start_date, date(2026, 9, 7))
        self.assertEqual(period.end_date, date(2026, 9, 13))
        self.assertEqual(period.start_date.strftime("%A"), "Monday")
        self.assertEqual(period.end_date.strftime("%A"), "Sunday")

    def test_the_period_never_contains_any_part_of_the_day_it_is_sent(self):
        """The failure this prevents: Monday's first hour leaking into Sunday's week."""
        with report_settings():
            period = previous_week(date(2026, 9, 14))
        # 00:00 on the Monday the report is sent, expressed in UTC. The upper
        # bound is exclusive, so that instant is outside the window.
        monday_midnight_ist = datetime(2026, 9, 13, 18, 30, tzinfo=timezone.utc)
        self.assertEqual(period.end_time, monday_midnight_ist)
        self.assertLess(period.start_time, period.end_time)

    def test_the_boundaries_are_local_midnights_not_utc_midnights(self):
        """TEST 2. Asia/Kolkata is +05:30, so a week begins at 18:30 UTC the
        evening before. Cutting on UTC midnight instead would move five and a
        half hours of Sunday evening into the wrong week."""
        with report_settings():
            period = previous_week(date(2026, 9, 14))
        self.assertEqual(
            period.start_time, datetime(2026, 9, 6, 18, 30, tzinfo=timezone.utc),
        )
        self.assertEqual(
            period.end_time, datetime(2026, 9, 13, 18, 30, tzinfo=timezone.utc),
        )

    def test_a_run_on_any_weekday_reports_the_same_completed_week(self):
        """A manual re-run on Thursday must not report a different period from
        the Monday run it is repeating, or it would be a second, different email."""
        with report_settings():
            periods = {
                previous_week(date(2026, 9, day)).start_date
                for day in range(14, 21)  # Monday 14th through Sunday 20th
            }
        self.assertEqual(periods, {date(2026, 9, 7)})

    def test_a_mid_week_override_is_normalised_to_the_whole_week(self):
        with report_settings():
            period = week_containing(date(2026, 9, 10))  # a Thursday
        self.assertEqual((period.start_date, period.end_date),
                         (date(2026, 9, 7), date(2026, 9, 13)))

    def test_an_unknown_timezone_falls_back_rather_than_stopping_the_run(self):
        with report_settings(WEEKLY_REPORT_TIMEZONE="Mars/Olympus_Mons"):
            period = previous_week(date(2026, 9, 14))
        self.assertEqual(period.timezone_name, "Asia/Kolkata")
        self.assertEqual(period.start_date, date(2026, 9, 7))


# ======================================================================
# 2 — the schedule
# ======================================================================

class TestSchedule(unittest.TestCase):
    """TEST 3: Monday-morning scheduling, and the deployed cron that fires it."""

    def test_monday_nine_am_ist_is_monday_three_thirty_utc(self):
        with report_settings():
            self.assertEqual(weekly_cron_expression(), "30 3 * * 1")

    def test_the_deployed_cron_entry_matches_the_configured_send_time(self):
        """The drift this catches: someone changes WEEKLY_REPORT_HOUR and the
        deployment keeps firing at the old time, silently, forever."""
        config = json.loads(
            (Path(__file__).resolve().parents[2] / "vercel.json").read_text(encoding="utf-8")
        )
        weekly = [
            cron for cron in config.get("crons", [])
            if cron.get("path", "").startswith("/internal/reports/weekly/run")
        ]
        self.assertEqual(len(weekly), 1, "exactly one weekly-report cron entry")
        with report_settings():
            self.assertEqual(weekly[0]["schedule"], weekly_cron_expression())

    def test_the_outbox_is_swept_often_enough_to_clear_a_monday_backlog(self):
        """The weekly run queues every user at once. A sweep that only ran
        hourly would deliver EMAIL_DISPATCH_BATCH_SIZE an hour, and "Monday
        morning" would become Monday afternoon for a large organisation."""
        config = json.loads(
            (Path(__file__).resolve().parents[2] / "vercel.json").read_text(encoding="utf-8")
        )
        dispatch = [
            cron for cron in config.get("crons", [])
            if cron.get("path", "").startswith("/internal/email/dispatch")
        ]
        self.assertTrue(dispatch, "the outbox must still have a dispatch cron")
        minute_field = dispatch[0]["schedule"].split()[0]
        self.assertTrue(
            minute_field.startswith("*/"),
            f"expected a sub-hourly sweep, found {dispatch[0]['schedule']!r}",
        )
        self.assertLessEqual(int(minute_field[2:]), 15)


# ======================================================================
# 3 — the figures
# ======================================================================

def _aggregates(**overrides):
    """Patch the five repository reads `build_week_metrics` performs."""
    defaults = {
        "totals_by_user": {},
        "daily_by_user": {},
        "counted_idle_seconds_by_user": {},
    }
    defaults.update(overrides)
    return patch.multiple(
        REPOSITORY,
        **{name: MagicMock(return_value=value) for name, value in defaults.items()},
    )


def _totals(user_id=42, **overrides):
    row = {
        "total_seconds": 139320,
        "act_sum": 780.0,
        "act_count": 10,
        "entry_count": 23,
        "project_count": 5,
    }
    row.update(overrides)
    return {user_id: row}


class TestFigures(unittest.TestCase):
    """TEST 4, 6, 7, 8, 9, 10, 11, 12 — every headline metric."""

    def _build(self, **aggregates):
        with report_settings(), _aggregates(**aggregates):
            return build_week_metrics(
                MagicMock(), organization_id=7, user_ids=[42], period=WEEK,
            )[42]

    def test_a_normal_week_reports_every_metric(self):
        """TEST 4."""
        figures = self._build(
            totals_by_user=_totals(),
            daily_by_user={42: [
                {"day": date(2026, 9, 8), "total_seconds": 27000, "act_sum": 160.0, "act_count": 2},
                {"day": date(2026, 9, 9), "total_seconds": 30000, "act_sum": 178.0, "act_count": 2},
                {"day": date(2026, 9, 10), "total_seconds": 28000, "act_sum": 140.0, "act_count": 2},
            ]},
            counted_idle_seconds_by_user={42: 16020},
        )
        self.assertEqual(figures.total_seconds, 139320)      # TEST 6
        self.assertEqual(figures.average_activity, 78.0)     # TEST 7: 780/10
        self.assertEqual(figures.project_count, 5)           # TEST 8
        self.assertEqual(figures.idle_seconds, 16020)
        self.assertEqual(figures.active_seconds, 123300)
        self.assertEqual(len(figures.days), 3)
        self.assertTrue(figures.has_activity)

    def test_total_hours_are_the_sum_the_repository_reported(self):
        """TEST 6. Nothing is re-derived from hours, rounded twice, or rebuilt
        from a two-decimal figure — the seconds are carried through."""
        figures = self._build(totals_by_user=_totals(total_seconds=139321))
        self.assertEqual(figures.total_seconds, 139321)
        self.assertEqual(messages.format_duration(figures.total_seconds), "38h 42m")

    def test_average_activity_is_weighted_by_sample_count(self):
        """TEST 7. A true mean over the samples, not a mean of daily means: a
        session with one sample must not weigh as much as one with five hundred."""
        figures = self._build(totals_by_user=_totals(act_sum=4400.0, act_count=100))
        self.assertEqual(figures.average_activity, 44.0)

    def test_activity_is_none_rather_than_zero_when_nothing_was_sampled(self):
        """A week of approved manual entries carries no activity samples.
        Reporting 0% would tell someone who worked that they were idle."""
        figures = self._build(totals_by_user=_totals(act_sum=0.0, act_count=0))
        self.assertIsNone(figures.average_activity)
        self.assertEqual(messages.format_activity(figures.average_activity), "No activity")

    def test_the_project_count_is_the_distinct_projects_worked_on(self):
        """The email reports how many projects the week touched, not which —
        the detail belongs to the dashboard the button opens."""
        figures = self._build(totals_by_user=_totals(project_count=5))
        self.assertEqual(figures.project_count, 5)

    def test_the_activity_extremes_come_from_real_days(self):
        figures = self._build(
            totals_by_user=_totals(),
            daily_by_user={42: [
                {"day": date(2026, 9, 8), "total_seconds": 20000, "act_sum": 60.0, "act_count": 1},
                {"day": date(2026, 9, 9), "total_seconds": 44000, "act_sum": 91.0, "act_count": 1},
                {"day": date(2026, 9, 11), "total_seconds": 30000, "act_sum": 41.0, "act_count": 1},
            ]},
        )
        self.assertEqual(figures.highest_activity_day.weekday_name, "Wednesday")
        self.assertEqual(figures.lowest_activity_day.weekday_name, "Friday")

    def test_activity_extremes_are_withheld_when_only_one_day_was_sampled(self):
        """"Highest" and "lowest" naming the same day is not a comparison."""
        figures = self._build(
            totals_by_user=_totals(),
            daily_by_user={42: [
                {"day": date(2026, 9, 8), "total_seconds": 20000, "act_sum": 60.0, "act_count": 1},
            ]},
        )
        self.assertIsNone(figures.highest_activity_day)
        self.assertIsNone(figures.lowest_activity_day)

    def test_idle_time_can_never_exceed_the_tracked_time_it_sits_inside(self):
        figures = self._build(
            totals_by_user=_totals(total_seconds=3600),
            counted_idle_seconds_by_user={42: 999999},
        )
        self.assertEqual(figures.idle_seconds, 3600)
        self.assertEqual(figures.active_seconds, 0)

    def test_active_time_is_tracked_time_less_recorded_idle(self):
        figures = self._build(
            totals_by_user=_totals(total_seconds=139320),
            counted_idle_seconds_by_user={42: 16020},
        )
        self.assertEqual(figures.active_seconds, 123300)
        self.assertEqual(messages.format_duration(figures.active_seconds), "34h 15m")


class TestZeroActivityUser(unittest.TestCase):
    """TEST 5: a week with nothing in it is a report, not an error."""

    def test_a_user_with_no_tracked_work_gets_real_zeroes(self):
        with report_settings(), _aggregates():
            figures = build_week_metrics(
                MagicMock(), organization_id=7, user_ids=[42], period=WEEK,
            )[42]

        self.assertEqual(figures.total_seconds, 0)
        self.assertEqual(figures.project_count, 0)
        self.assertEqual(figures.idle_seconds, 0)
        self.assertIsNone(figures.average_activity)
        self.assertFalse(figures.has_activity)

    def test_an_empty_week_computes_without_dividing_by_zero(self):
        """No metric divides by a day count any more, so there is nothing to
        raise — but the empty case is pinned so that stays true."""
        figures = WeeklyMetrics(user_id=42)
        self.assertEqual(figures.active_seconds, 0)
        self.assertIsNone(figures.highest_activity_day)
        self.assertIsNone(figures.lowest_activity_day)
        self.assertFalse(figures.has_activity)

    def test_the_email_still_renders_and_says_so_plainly(self):
        with report_settings():
            message = messages.build_weekly_report_email(
                _payload(total_seconds=0, project_count=0, entry_count=0,
                         average_activity=None, idle_seconds=0),
                ["priya@example.com"],
            )
        self.assertIn("no tracked activity", message.html)
        self.assertIn("0h 0m", message.html)
        self.assertIn("No activity", message.html)
        # An honest empty state, not a hidden email and not invented figures.
        # The plain-text alternative carries the same prompt as the HTML.
        self.assertIn("start a timer", message.text.lower())
        self.assertIn("start a timer", message.html.lower())


class TestDashboardConsistency(unittest.TestCase):
    """TEST: the email and the dashboard cannot disagree about the same week.

    Asserted structurally rather than by comparing two numbers, because a
    number can agree by coincidence on the day it is written. What has to stay
    true is that both read the *same* entry-grain definition — so a future
    change that adds a second, parallel aggregation fails here.
    """

    def test_every_weekly_aggregate_groups_the_dashboards_entry_grain_rows(self):
        from app.repositories import weekly_report as repository

        with patch(
            "app.react_apis.reports_page.repository.ReportsPageRepository.entry_grain_subquery"
        ) as entry_grain:
            db = MagicMock()
            for method in (
                repository.WeeklyReportRepository.totals_by_user,
                repository.WeeklyReportRepository.daily_by_user,
            ):
                entry_grain.reset_mock()
                try:
                    method(db, MagicMock())
                except Exception:  # noqa: BLE001 - the mock cannot be executed
                    pass
                self.assertTrue(
                    entry_grain.called,
                    f"{method.__name__} must group the shared entry-grain subquery",
                )

    def test_the_filters_handed_to_the_aggregation_are_the_period_and_the_user(self):
        captured = {}

        def _capture(db, filters, *args, **kwargs):
            captured["filters"] = filters
            return {}

        with report_settings(), patch.multiple(
            REPOSITORY,
            totals_by_user=MagicMock(side_effect=_capture),
            daily_by_user=MagicMock(return_value={}),
            counted_idle_seconds_by_user=MagicMock(return_value={}),
        ):
            build_week_metrics(MagicMock(), organization_id=7, user_ids=[42], period=WEEK)

        filters = captured["filters"]
        self.assertEqual(filters.organization_id, 7)
        self.assertEqual(filters.member_ids, (42,))
        self.assertEqual(filters.start_date, WEEK.start_date)
        self.assertEqual(filters.end_date, WEEK.end_date)
        self.assertEqual(filters.start_time, WEEK.start_time)
        self.assertEqual(filters.end_time, WEEK.end_time)


# ======================================================================
# 4 — privacy
# ======================================================================

class TestPrivacy(unittest.TestCase):
    """TEST 13 and TEST 21: each person receives their own report, and only it."""

    def test_metrics_belonging_to_another_user_are_refused(self):
        """TEST 13. The one way to mail A's week to B would be to pair them;
        the pairing is checked rather than trusted."""
        from app.services.email.workflows import _weekly_report_payload

        with self.assertRaises(ValueError):
            _weekly_report_payload(
                user=_user(user_id=42), period=WEEK, metrics=_metrics(user_id=99),
            )

    def test_a_mismatched_pair_never_reaches_the_outbox(self):
        db = MagicMock()
        with report_settings(), patch(f"{WORKFLOWS}.EmailOutboxService") as outbox:
            with self.assertRaises(ValueError):
                queue_weekly_report(
                    db, user=_user(user_id=42), period=WEEK, metrics=_metrics(user_id=99),
                )
        outbox.enqueue.assert_not_called()

    def test_each_user_is_queued_separately_with_only_their_own_address(self):
        """TEST 21. One notification per person — never one message with
        everybody in the recipient list, which would hand each reader the
        roster and cost all of them their report on a single failure."""
        db = MagicMock()
        users = [
            _user(user_id=42, email="priya@example.com", name="Priya Raman"),
            _user(user_id=43, email="sam@example.com", name="Sam Okonkwo"),
        ]
        with report_settings(), patch(f"{WORKFLOWS}.EmailOutboxService") as outbox, \
                patch(f"{WORKFLOWS}.EmailNotificationRepository") as repository:
            repository.get_by_event.return_value = None
            outbox.enqueue.side_effect = lambda *a, **k: _notification(id=k["user_id"])
            for user in users:
                queue_weekly_report(
                    db, user=user, period=WEEK,
                    metrics=_metrics(user_id=user.id, total_seconds=1000 * user.id),
                )

        calls = outbox.enqueue.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0].kwargs["recipients"], ["priya@example.com"])
        self.assertEqual(calls[1].kwargs["recipients"], ["sam@example.com"])
        # Each payload describes its own recipient and nobody else.
        self.assertEqual(calls[0].kwargs["payload"]["user_id"], 42)
        self.assertEqual(calls[1].kwargs["payload"]["user_id"], 43)
        self.assertEqual(calls[0].kwargs["payload"]["total_seconds"], 42000)
        self.assertEqual(calls[1].kwargs["payload"]["total_seconds"], 43000)
        self.assertNotIn("sam@example.com", json.dumps(calls[0].kwargs["payload"]))

    def test_the_rendered_email_contains_no_other_recipient(self):
        with report_settings():
            message = messages.build_weekly_report_email(
                _payload(user_id=42, name="Priya Raman"), ["priya@example.com"],
            )
        self.assertEqual(message.to, ["priya@example.com"])
        self.assertNotIn("sam@example.com", message.html)

    def test_the_payload_carries_no_internal_identifiers_or_permission_map(self):
        payload = _payload(permissions={"time_entries:view_all": True})
        self.assertNotIn("project_id", json.dumps(payload))
        self.assertNotIn("task_id", json.dumps(payload))
        self.assertNotIn("permissions", payload)
        # The one boolean the button needs, not the map it was derived from.
        self.assertIs(payload["can_view_all_time"], True)


# ======================================================================
# 5 — escaping
# ======================================================================

class TestEscaping(unittest.TestCase):
    """TEST 14: names are text somebody typed. They render as characters."""

    def test_a_user_name_cannot_become_markup(self):
        payload = _payload(name="<img src=x onerror=alert(1)>")
        with report_settings():
            html = messages.build_weekly_report_email(payload, ["priya@example.com"]).html
        self.assertNotIn("<img src=x", html)
        self.assertIn("&lt;img", html)

    def test_a_name_containing_a_newline_cannot_append_a_header(self):
        subject = messages.weekly_report_subject(
            {"period_short": "08 Sep–14 Sep\nBcc: attacker@example.com"}
        )
        self.assertNotIn("\n", subject)
        self.assertNotIn("\r", subject)


# ======================================================================
# 6 — the rendered email
# ======================================================================

class TestRenderedEmail(unittest.TestCase):
    """TEST 15 and TEST 16: the email says the right things and links correctly."""

    def test_the_email_contains_the_weeks_figures(self):
        """TEST 15."""
        with report_settings():
            message = messages.build_weekly_report_email(_payload(), ["priya@example.com"])

        for expected in (
            "38h 42m",            # total tracked
            "78%",                # average activity
            "07 Sep 2026",        # the period, stated
            "13 Sep 2026",
            "34h 15m",            # active = tracked - idle
            "4h 27m",             # idle
        ):
            self.assertIn(expected, message.html, f"missing {expected!r}")

        # The plain-text alternative must not tell a different story.
        self.assertIn("38h 42m", message.text)
        self.assertIn("78%", message.text)
        self.assertIn("Projects", message.text)

    def test_the_removed_sections_are_gone_from_both_renderings(self):
        """These were dropped deliberately. A future edit that reinstates one
        of them should have to change this test to do it."""
        with report_settings():
            message = messages.build_weekly_report_email(_payload(), ["priya@example.com"])
        for gone in ("Working days", "Avg per working day", "Sessions tracked",
                     "Most productive day", "Tasks", "Project Alpha"):
            self.assertNotIn(gone, message.html, f"{gone!r} should no longer be rendered")
            self.assertNotIn(gone, message.text, f"{gone!r} should no longer be in the text")

    def test_every_figure_lives_in_one_table(self):
        """One table, not a card grid plus four headed sections."""
        with report_settings():
            table = messages._weekly_summary_table(_payload())
        self.assertEqual(str(table).count("<table"), 1, "the summary is a single table")
        # And that one table carries the whole report.
        for expected in ("Total tracked time", "Active time", "Idle time",
                         "Average activity", "Projects"):
            self.assertIn(expected, str(table), f"missing {expected!r}")

    def test_the_subject_names_the_period(self):
        with report_settings():
            subject = messages.weekly_report_subject(_payload())
        self.assertIn("Weekly Report", subject)
        self.assertIn("07 Sep", subject)
        self.assertIn("13 Sep", subject)
        # No figure: a lock screen is not the place for somebody's hours.
        self.assertNotIn("38h", subject)

    def test_both_logos_are_carried_by_the_message(self):
        with report_settings():
            message = messages.build_weekly_report_email(_payload(), ["priya@example.com"])
        cids = {image.cid for image in message.inline_images}
        self.assertIn("monitra-logo", cids)
        self.assertIn("store-transform-logo", cids)
        self.assertIn("cid:monitra-logo", message.html)
        self.assertIn("cid:store-transform-logo", message.html)

    def test_the_dashboard_button_opens_the_reported_week(self):
        """TEST 16. An existing route with its existing query parameters —
        the Reports page already reads ?start=/?end=."""
        with report_settings(MONITRA_APP_URL="https://staff.peakworkos.com"):
            url = messages.weekly_report_dashboard_url(_payload())
        self.assertEqual(
            url,
            "https://staff.peakworkos.com/member/reports/projects"
            "?start=2026-09-07&end=2026-09-13",
        )

    def test_an_administrator_is_sent_to_the_screen_they_are_allowed_to_open(self):
        """The member guard redirects an admin to /dashboard, losing the date
        range on the way — so the button has to be pointed correctly up front."""
        with report_settings(MONITRA_APP_URL="https://staff.peakworkos.com"):
            url = messages.weekly_report_dashboard_url(
                _payload(permissions={"time_entries:view_all": True})
            )
        self.assertTrue(url.startswith("https://staff.peakworkos.com/dashboard/reports/projects"))
        self.assertIn("start=2026-09-07", url)

    def test_no_button_is_rendered_for_a_localhost_or_empty_app_url(self):
        """A development URL resolves on the *reader's* machine. No button is
        better than a broken one, and better than a link to something of theirs."""
        for value in ("", "http://localhost:5173", "http://staff.peakworkos.com"):
            with self.subTest(app_url=value), report_settings(MONITRA_APP_URL=value):
                self.assertIsNone(messages.weekly_report_dashboard_url(_payload()))
                html = messages.build_weekly_report_email(
                    _payload(), ["priya@example.com"]
                ).html
                self.assertNotIn("View Detailed Report", html)

    def test_no_production_email_references_a_local_file_or_host(self):
        with report_settings():
            message = messages.build_weekly_report_email(_payload(), ["priya@example.com"])
        for forbidden in ("file://", "localhost", "127.0.0.1", "C:\\"):
            self.assertNotIn(forbidden, message.html)


# ======================================================================
# 7 — idempotency and retry
# ======================================================================

class TestIdempotency(unittest.TestCase):
    """TEST 17 and TEST 20: one email per user per week, whatever happens."""

    def test_the_dedupe_key_is_the_week_and_the_user(self):
        self.assertEqual(
            weekly_report_dedupe_key(date(2026, 9, 7), 42), "week:2026-09-07:user:42",
        )
        # Stable across calls: no timestamp, no run id, nothing that changes.
        self.assertEqual(
            weekly_report_dedupe_key(date(2026, 9, 7), 42),
            weekly_report_dedupe_key(date(2026, 9, 7), 42),
        )

    def test_the_next_week_is_a_different_event(self):
        self.assertNotEqual(
            weekly_report_dedupe_key(date(2026, 9, 7), 42),
            weekly_report_dedupe_key(date(2026, 9, 14), 42),
        )

    def test_running_the_scheduler_twice_queues_one_notification(self):
        """TEST 17. The second run finds the row the first one wrote."""
        db = MagicMock()
        existing = _notification(id=9)
        with report_settings(), patch(f"{OUTBOX}.EmailNotificationRepository") as repository, \
                patch(f"{WORKFLOWS}.EmailNotificationRepository") as lookup:
            # First run: nothing there yet. Second: the row exists.
            lookup.get_by_event.side_effect = [None, existing]
            repository.enqueue.side_effect = [(existing, True), (existing, False)]
            first_id, first_created = queue_weekly_report(
                db, user=_user(), period=WEEK, metrics=_metrics(),
            )
            second_id, second_created = queue_weekly_report(
                db, user=_user(), period=WEEK, metrics=_metrics(),
            )

        self.assertEqual(first_id, second_id, "both runs resolve to one notification")
        self.assertTrue(first_created)
        self.assertFalse(second_created, "the second run queued nothing new")
        keys = {call.kwargs["dedupe_key"] for call in repository.enqueue.call_args_list}
        self.assertEqual(keys, {"week:2026-09-07:user:42"})

    def test_an_already_sent_report_is_never_resent(self):
        """TEST 20. A delivered row is terminal: the sweeper will not claim it
        again, and a re-run collapses onto it rather than creating a second."""
        db = MagicMock()
        sent = _notification(id=9, status=STATUS_SENT)
        with report_settings(), patch(f"{OUTBOX}.EmailNotificationRepository") as repository:
            repository.get_by_id.return_value = sent
            outcome = EmailOutboxService.deliver_one(db, 9)

        self.assertEqual(outcome, "skipped")
        repository.claim.assert_not_called()

    def test_the_run_reports_an_existing_week_as_already_queued_not_as_new(self):
        db = MagicMock()
        user = _user()
        with report_settings(), \
                patch(f"{SERVICE}.UserRepository.list_announcement_recipients", return_value=[user]), \
                _aggregates(totals_by_user=_totals()), \
                patch(f"{WORKFLOWS}.queue_weekly_report", return_value=(9, False)):
            tally = WeeklyReportService.run(db)

        self.assertEqual(tally["queued"], 0)
        self.assertEqual(tally["already_queued"], 1)


class TestRetry(unittest.TestCase):
    """TEST 18 and TEST 19: a failure stays retryable, and a retry completes."""

    def test_a_delivery_failure_leaves_the_report_pending_and_counted(self):
        """TEST 18. The report is not lost, and no second row is created for it."""
        db = MagicMock()
        row = _notification(id=9, attempt_count=1, max_attempts=6)
        with report_settings(), patch(f"{OUTBOX}.EmailNotificationRepository") as repository, \
                patch(f"{OUTBOX}.get_email_provider") as provider:
            repository.get_by_id.return_value = row
            repository.claim.return_value = row
            provider.return_value.send.side_effect = EmailDeliveryError("mail server refused")
            outcome = EmailOutboxService.deliver_one(db, 9)

        self.assertEqual(outcome, "retrying")
        failure = repository.mark_attempt_failed.call_args.kwargs
        self.assertIsNone(failure["terminal_status"], "still within its attempt budget")
        self.assertIn("mail server refused", failure["error"])
        repository.mark_sent.assert_not_called()

    def test_a_successful_retry_marks_the_report_sent(self):
        """TEST 19."""
        db = MagicMock()
        row = _notification(id=9, attempt_count=2)
        with report_settings(), patch(f"{OUTBOX}.EmailNotificationRepository") as repository, \
                patch(f"{OUTBOX}.get_email_provider") as provider:
            repository.get_by_id.return_value = row
            repository.claim.return_value = row
            outcome = EmailOutboxService.deliver_one(db, 9)

        self.assertEqual(outcome, "sent")
        self.assertEqual(provider.return_value.send.call_count, 1)
        self.assertEqual(repository.mark_sent.call_args.kwargs["notification_id"], 9)

    def test_the_last_attempt_parks_the_report_rather_than_losing_it(self):
        db = MagicMock()
        row = _notification(id=9, attempt_count=6, max_attempts=6)
        with report_settings(), patch(f"{OUTBOX}.EmailNotificationRepository") as repository, \
                patch(f"{OUTBOX}.get_email_provider") as provider:
            repository.get_by_id.return_value = row
            repository.claim.return_value = row
            provider.return_value.send.side_effect = EmailDeliveryError("still down")
            outcome = EmailOutboxService.deliver_one(db, 9)

        self.assertEqual(outcome, "failed")
        self.assertEqual(
            repository.mark_attempt_failed.call_args.kwargs["terminal_status"], STATUS_FAILED,
        )

    def test_a_queued_weekly_report_renders_from_its_stored_payload(self):
        """The dispatcher's own path: builder looked up by type, payload as JSON."""
        from app.services.email.messages import BUILDERS

        builder = BUILDERS.get(TYPE_WEEKLY_REPORT)
        self.assertIsNotNone(builder, "the outbox must know how to render this type")
        with report_settings():
            message = builder(json.loads(json.dumps(_payload())), ["priya@example.com"])
        self.assertIn("38h 42m", message.html)


# ======================================================================
# 8 — the run
# ======================================================================

class TestRun(unittest.TestCase):
    """Eligibility, containment, and the kill switch."""

    def test_only_active_accounts_with_an_address_are_reported_on(self):
        db = MagicMock()
        with report_settings(), \
                patch(f"{SERVICE}.UserRepository.list_announcement_recipients") as recipients:
            recipients.return_value = []
            WeeklyReportService.eligible_users(db)
        # Reuses the one eligibility rule rather than inventing a second answer
        # to "is this person a current employee".
        recipients.assert_called_once_with(db)

    def test_a_named_user_is_still_subject_to_the_eligibility_rule(self):
        db = MagicMock()
        active = _user(user_id=42)
        with report_settings(), \
                patch(f"{SERVICE}.UserRepository.list_announcement_recipients",
                      return_value=[active]):
            self.assertEqual(WeeklyReportService.eligible_users(db, user_id=42), [active])
            # A disabled account is not in the eligible list, so naming it
            # cannot produce a report for it.
            self.assertEqual(WeeklyReportService.eligible_users(db, user_id=99), [])

    def test_one_users_failure_does_not_stop_the_others(self):
        """TEST: 100 users, user 3 fails, users 4..100 still receive theirs."""
        db = MagicMock()
        users = [_user(user_id=index, email=f"user{index}@example.com") for index in range(1, 11)]

        def _queue(db_, *, user, period, metrics):
            if user.id == 3:
                raise RuntimeError("this one row is broken")
            return user.id, True

        with report_settings(), \
                patch(f"{SERVICE}.UserRepository.list_announcement_recipients", return_value=users), \
                _aggregates(), patch(f"{WORKFLOWS}.queue_weekly_report", side_effect=_queue):
            tally = WeeklyReportService.run(db)

        self.assertEqual(tally["eligible_users"], 10)
        self.assertEqual(tally["queued"], 9)
        self.assertEqual(tally["failed"], 1)

    def test_the_kill_switch_queues_nothing(self):
        db = MagicMock()
        with report_settings(WEEKLY_REPORT_ENABLED=False), \
                patch(f"{WORKFLOWS}.queue_weekly_report") as queue:
            tally = WeeklyReportService.run(db)
        queue.assert_not_called()
        self.assertTrue(tally["disabled"])

    def test_a_dry_run_computes_without_queueing(self):
        db = MagicMock()
        with report_settings(), \
                patch(f"{SERVICE}.UserRepository.list_announcement_recipients",
                      return_value=[_user()]), \
                _aggregates(totals_by_user=_totals()), \
                patch(f"{WORKFLOWS}.queue_weekly_report") as queue:
            tally = WeeklyReportService.run(db, dry_run=True)

        queue.assert_not_called()
        self.assertTrue(tally["dry_run"])
        self.assertEqual(tally["queued"], 1)

    def test_each_organisation_is_aggregated_once_however_many_people_it_has(self):
        """The N+1 this prevents: one aggregation pass per *user* would turn a
        500-person Monday into 2,500 queries."""
        db = MagicMock()
        users = [_user(user_id=index, organization_id=7) for index in range(1, 51)]
        with report_settings(), \
                patch(f"{SERVICE}.UserRepository.list_announcement_recipients", return_value=users), \
                patch(f"{SERVICE}.build_week_metrics", return_value={}) as aggregate, \
                patch(f"{WORKFLOWS}.queue_weekly_report", return_value=(1, True)):
            WeeklyReportService.run(db)

        self.assertEqual(aggregate.call_count, 1, "one pass for the whole organisation")
        self.assertEqual(len(aggregate.call_args.kwargs["user_ids"]), 50)

    def test_the_run_never_raises_into_its_scheduler(self):
        db = MagicMock()
        with report_settings(), \
                patch(f"{SERVICE}.UserRepository.list_announcement_recipients",
                      side_effect=RuntimeError("database is on fire")):
            tally = WeeklyReportService.run(db)
        self.assertEqual(tally["failed"], 1)


# ======================================================================
# 9 — no regressions
# ======================================================================

class TestExistingWorkflowsStillWork(unittest.TestCase):
    """TEST 22 and TEST 23: the welcome and feedback emails are untouched."""

    def test_the_welcome_email_still_queues_and_renders(self):
        """TEST 22."""
        db = MagicMock()
        with report_settings(WELCOME_EMAIL_ENABLED=True), \
                patch(f"{WORKFLOWS}.EmailOutboxService") as outbox:
            outbox.enqueue.return_value = _notification(id=9)
            queue_welcome_email(db, _user(user_id=42))

        kwargs = outbox.enqueue.call_args.kwargs
        self.assertEqual(kwargs["notification_type"], "welcome")
        self.assertEqual(kwargs["dedupe_key"], "user:42")

        with report_settings():
            message = messages.build_welcome_email({"name": "Priya Raman"}, ["priya@example.com"])
        self.assertIn("Welcome to Monitra", message.html)
        self.assertIn("Download Monitra", message.html)

    def test_the_feedback_email_still_queues_and_renders(self):
        """TEST 23."""
        db = MagicMock()
        feedback = MagicMock()
        feedback.id = 17
        feedback.organization_id = 7
        feedback.category = "report_a_problem"
        feedback.message = "The timer resets when I resume from sleep."
        feedback.created_at = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)

        with report_settings(FEEDBACK_ADMIN_EMAIL="admin@example.com",
                             FEEDBACK_HR_EMAIL="hr@example.com",
                             FEEDBACK_NOTIFICATION_EMAILS=""), \
                patch(f"{WORKFLOWS}.EmailOutboxService") as outbox:
            outbox.enqueue.return_value = _notification(id=11)
            queue_feedback_notification(db, feedback, _user(user_id=42))

        kwargs = outbox.enqueue.call_args.kwargs
        self.assertEqual(kwargs["notification_type"], "feedback")
        self.assertEqual(kwargs["dedupe_key"], "feedback:17")

        with report_settings():
            message = messages.build_feedback_email(
                {
                    "user_name": "Priya Raman", "user_email": "priya@example.com",
                    "category": "report_a_problem", "message": "The timer resets.",
                    "submitted_at": "2026-09-10T12:00:00+00:00",
                },
                ["admin@example.com"],
            )
        self.assertIn("MONITRA FEEDBACK RECEIVED", message.text)

    def test_every_registered_notification_type_still_has_a_builder(self):
        """Adding the fifth workflow must not have displaced any of the four."""
        from app.services.email.messages import BUILDERS

        self.assertEqual(
            set(BUILDERS),
            {"welcome", "feedback", "feedback_status", "release", "weekly_report"},
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

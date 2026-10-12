"""
Abandoned timers: the server's policy for a running entry whose desktop has
gone quiet (docs/TIMING_MODEL.md section 9).

The production symptom: a user started a task on Friday at 17:23 IST and never
stopped it. On Monday morning Active Users showed 65:32:33 and the database
held `status = running, end_time = NULL, total_seconds = 0` for them. Nothing
on the server could tell a desktop that is working from one that is switched
off, so the row stayed open and every consumer measured `now() - start_time`.

What these tests defend, and why each rule exists:

* **Liveness is evidence, and the figure follows the evidence.** A running
  entry is measured to `now` while its desktop reports in, and stops where the
  evidence stops (last signal + 15 minutes) once it has been silent past the
  stale window -- in Python and in SQL, so no report can show a number the
  entry's own response disagrees with.
* **The server closes only what it can justify.** Silent for hours, or an idle
  prompt nobody answered for hours; never an entry that is reporting in, never
  one that has never reported (silence from a client that never speaks proves
  nothing), never one it cannot lock.
* **Every closure is a recorded decision** -- what, why, on what evidence,
  under which thresholds -- and goes through the one stop path, so duration,
  idle sweep, rollup and WFPM behave exactly as for a user's own Stop.
* **A report-only run writes nothing.**
* **A stop is honoured however old it is.** A stop queued on a machine that
  stayed off for eight days used to be recorded at arrival time.
"""
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

from app.core.timer_policy import (
    LIVE, REASON_IDLE_UNANSWERED, REASON_NO_SIGNAL, REASON_OPERATOR, REASON_REPLACED_BY_START,
    STALE, UNKNOWN, TimerPolicy, effective_end, liveness,
)
from app.models.time_entry import TimeEntry
from app.models.user import User
from app.repositories.time_entry_liveness import effective_now
from app.services.time_entry import MAX_CLIENT_BACKDATE_SECONDS, TimeEntryService
from app.services.time_entry_liveness import TimeEntryLivenessService
from app.services.time_entry_reconciliation import (
    TimeEntryReconciliationService, decide,
)

UTC = timezone.utc
POLICY = TimerPolicy(
    stale_after=timedelta(minutes=10),
    no_signal_grace=timedelta(minutes=15),
    no_signal_stop_after=timedelta(hours=2),
    idle_unanswered_stop_after=timedelta(hours=4),
)
#: Friday 2026-10-09 17:23:21 IST -- the entry in the production report.
FRIDAY_START = datetime(2026, 10, 9, 11, 53, 21, tzinfo=UTC)
#: Monday 2026-10-12 10:55 IST, when Active Users read "65:32:33".
MONDAY = datetime(2026, 10, 12, 5, 25, 54, tzinfo=UTC)


def _entry(**overrides) -> TimeEntry:
    fields = dict(
        id=5958, organization_id=1, user_id=260, project_id=3268, task_id=5298,
        start_time=FRIDAY_START, end_time=None, total_seconds=0, status="running",
        is_manual=False, is_billable=False, description=None, client_op="timer:5298:x",
        last_seen_at=None, created_at=FRIDAY_START, updated_at=FRIDAY_START,
    )
    fields.update(overrides)
    return TimeEntry(**fields)


def _idle(detected, started=None, id=77):
    return SimpleNamespace(
        id=id, idle_detected_at=detected, idle_started_at=started or detected - timedelta(minutes=10),
    )


# ── the policy ───────────────────────────────────────────────────────────────

class PolicyTests(unittest.TestCase):
    def test_the_thresholds_are_made_consistent(self):
        """stale <= grace <= stop-after, whatever the settings say. The first
        keeps a running figure continuous when an entry turns stale (never a
        step backwards); the second means the reconciler never closes before
        the instant the entry was already measured to."""
        with patch.multiple(
            "app.core.timer_policy.settings",
            TIMER_STALE_AFTER_SECONDS=1800, TIMER_NO_SIGNAL_GRACE_SECONDS=60,
            TIMER_NO_SIGNAL_STOP_AFTER_SECONDS=10, TIMER_IDLE_UNANSWERED_STOP_AFTER_SECONDS=5,
        ):
            policy = TimerPolicy.current()
        self.assertEqual(policy.stale_after, timedelta(minutes=30))
        self.assertGreaterEqual(policy.no_signal_grace, policy.stale_after)
        self.assertGreaterEqual(policy.no_signal_stop_after, policy.no_signal_grace)
        self.assertGreaterEqual(policy.idle_unanswered_stop_after, timedelta(seconds=60))

    def test_the_defaults_are_the_documented_ones(self):
        policy = TimerPolicy.current()
        self.assertEqual(policy.stale_after, timedelta(minutes=10))
        self.assertEqual(policy.no_signal_grace, timedelta(minutes=15))
        self.assertEqual(policy.no_signal_stop_after, timedelta(hours=2))
        self.assertEqual(policy.idle_unanswered_stop_after, timedelta(hours=4))

    def test_liveness_states(self):
        now = MONDAY
        self.assertEqual(liveness(None, now - timedelta(minutes=2), now, POLICY), LIVE)
        self.assertEqual(liveness(None, now - timedelta(minutes=10), now, POLICY), LIVE)
        self.assertEqual(liveness(None, now - timedelta(minutes=11), now, POLICY), STALE)
        self.assertEqual(liveness(None, None, now, POLICY), UNKNOWN)
        self.assertIsNone(liveness(now, now - timedelta(days=3), now, POLICY), "an ended entry has no liveness")

    def test_a_running_entry_is_measured_to_now_while_it_reports_in(self):
        seen = MONDAY - timedelta(minutes=4)
        self.assertEqual(effective_end(FRIDAY_START, None, seen, MONDAY, POLICY), MONDAY)

    def test_a_silent_entry_stops_growing_where_the_evidence_stops(self):
        seen = FRIDAY_START + timedelta(minutes=27)          # laptop lid closed Friday evening
        self.assertEqual(
            effective_end(FRIDAY_START, None, seen, MONDAY, POLICY), seen + timedelta(minutes=15),
        )

    def test_the_figure_is_continuous_as_an_entry_turns_stale(self):
        """No step backwards at the stale boundary: from 10 to 15 minutes of
        silence the entry is still measured to now."""
        seen = MONDAY - timedelta(minutes=12)
        self.assertEqual(effective_end(FRIDAY_START, None, seen, MONDAY, POLICY), MONDAY)

    def test_an_entry_that_never_reported_is_measured_as_it_always_was(self):
        self.assertEqual(effective_end(FRIDAY_START, None, None, MONDAY, POLICY), MONDAY)

    def test_the_effective_end_is_never_before_the_start(self):
        seen = FRIDAY_START - timedelta(hours=5)
        self.assertEqual(effective_end(FRIDAY_START, None, seen, MONDAY, POLICY), FRIDAY_START)

    def test_an_ended_entry_is_measured_to_its_own_end(self):
        end = FRIDAY_START + timedelta(hours=1)
        self.assertEqual(effective_end(FRIDAY_START, end, None, MONDAY, POLICY), end)

    def test_the_sql_twin_has_the_same_shape(self):
        """`effective_now` is the SQL form of `effective_end`: now() unless
        the entry reported and has been silent past the stale window, then
        the last evidence plus the grace, floored at the start."""
        sql = str(effective_now(TimeEntry.last_seen_at, TimeEntry.start_time, POLICY)
                  .compile(dialect=postgresql.dialect()))
        self.assertIn("time_entries.last_seen_at IS NOT NULL", sql)
        self.assertIn("now() - time_entries.last_seen_at >", sql)
        self.assertIn("time_entries.last_seen_at + ", sql)
        self.assertNotIn("least(", sql, "portable CASE, so the SQLite-backed report suites can run it")
        self.assertNotIn("greatest(", sql)
        self.assertTrue(sql.rstrip().endswith("ELSE now() END"), sql)


# ── the decision ─────────────────────────────────────────────────────────────

class DecideTests(unittest.TestCase):
    def test_the_production_row_friday_start_monday_morning(self):
        """user 260: started Friday 17:23 IST, last heard from 27 minutes
        later, still `running` on Monday. Closed at last signal + 15 minutes:
        42 minutes of Friday, not 65 hours."""
        seen = FRIDAY_START + timedelta(minutes=27)
        decision = decide(_entry(last_seen_at=seen), None, MONDAY, POLICY)
        self.assertEqual(decision.reason, REASON_NO_SIGNAL)
        self.assertEqual(decision.end_time, seen + timedelta(minutes=15))
        self.assertEqual(decision.total_seconds(_entry()), 42 * 60)
        self.assertEqual(decision.evidence_at, seen)

    def test_a_weekend_is_not_counted(self):
        seen = FRIDAY_START + timedelta(hours=6)
        decision = decide(_entry(last_seen_at=seen), None, FRIDAY_START + timedelta(days=3), POLICY)
        self.assertEqual(decision.end_time, seen + timedelta(minutes=15))

    def test_a_reporting_entry_is_never_closed_however_long_it_runs(self):
        """A legitimate overnight shift: the desktop keeps reporting, so the
        entry runs for as long as its owner works. There is no maximum."""
        seen = MONDAY - timedelta(seconds=90)
        self.assertIsNone(decide(_entry(last_seen_at=seen), None, MONDAY, POLICY))

    def test_a_short_silence_is_not_abandonment(self):
        """A lunch break or a laptop asleep through a meeting: its owner can
        still answer the idle prompt when the machine wakes."""
        for minutes in (11, 45, 119):
            with self.subTest(minutes=minutes):
                seen = MONDAY - timedelta(minutes=minutes)
                self.assertIsNone(decide(_entry(last_seen_at=seen), None, MONDAY, POLICY))

    def test_two_hours_of_silence_is_the_threshold(self):
        seen = MONDAY - timedelta(hours=2, seconds=1)
        decision = decide(_entry(last_seen_at=seen), None, MONDAY, POLICY)
        self.assertEqual(decision.reason, REASON_NO_SIGNAL)
        self.assertEqual(decision.end_time, seen + timedelta(minutes=15))

    def test_an_entry_that_never_reported_is_left_alone(self):
        """No evidence either way. The scheduled run cannot reach it."""
        self.assertIsNone(decide(_entry(last_seen_at=None), None, MONDAY, POLICY))

    def test_an_operator_may_close_an_unreported_entry_on_the_newest_evidence(self):
        newest_upload = FRIDAY_START + timedelta(hours=1)
        decision = decide(
            _entry(last_seen_at=None), None, MONDAY, POLICY,
            include_unreported=True, evidence_at=newest_upload,
        )
        self.assertEqual(decision.reason, REASON_OPERATOR)
        self.assertEqual(decision.end_time, newest_upload + timedelta(minutes=15))

    def test_an_operator_with_no_evidence_at_all_uses_the_start(self):
        decision = decide(_entry(last_seen_at=None), None, MONDAY, POLICY, include_unreported=True)
        self.assertEqual(decision.end_time, FRIDAY_START + timedelta(minutes=15))

    def test_an_ended_entry_is_never_decided_on(self):
        self.assertIsNone(decide(_entry(end_time=MONDAY, last_seen_at=FRIDAY_START), None, MONDAY, POLICY))

    def test_an_unanswered_idle_prompt_on_a_live_desktop_closes_at_the_prompt(self):
        """Monitra is still running, the person has gone home: heartbeats
        arrive, the prompt is never answered. Closed at the instant the prompt
        was raised -- what Stop on the unanswered prompt produces."""
        detected = MONDAY - timedelta(hours=5)
        decision = decide(
            _entry(last_seen_at=MONDAY - timedelta(minutes=1)), _idle(detected), MONDAY, POLICY,
        )
        self.assertEqual(decision.reason, REASON_IDLE_UNANSWERED)
        self.assertEqual(decision.end_time, detected)

    def test_a_recent_idle_prompt_is_the_users_to_answer(self):
        detected = MONDAY - timedelta(hours=3, minutes=59)
        self.assertIsNone(decide(
            _entry(last_seen_at=MONDAY - timedelta(minutes=1)), _idle(detected), MONDAY, POLICY,
        ))

    def test_silence_wins_over_an_unanswered_prompt(self):
        seen = MONDAY - timedelta(hours=9)
        decision = decide(
            _entry(last_seen_at=seen), _idle(MONDAY - timedelta(hours=9, minutes=5)), MONDAY, POLICY,
        )
        self.assertEqual(decision.reason, REASON_NO_SIGNAL)

    def test_the_closing_instant_never_precedes_a_pending_prompts_detection(self):
        """Resolving a prompt cannot move backwards past its own detection;
        the stop path deducts from the prompt, so the entry must not end before
        it or the deduction would exceed the entry."""
        seen = MONDAY - timedelta(hours=3)
        detected = seen + timedelta(minutes=40)          # after seen + grace
        decision = decide(_entry(last_seen_at=seen), _idle(detected), MONDAY, POLICY)
        self.assertEqual(decision.end_time, detected)


# ── closing one entry ────────────────────────────────────────────────────────

class CloseTests(unittest.TestCase):
    def setUp(self):
        self.db = MagicMock()
        self.owner = User()
        self.owner.id = 260
        self.owner.organization_id = 1
        self.db.get.return_value = self.owner
        self.patches = [
            patch("app.services.time_entry_reconciliation.TimeEntryLivenessRepository"),
            patch("app.services.time_entry_reconciliation.TimeEntryIdlePeriodRepository"),
            patch("app.services.time_entry_reconciliation.TimeEntryReconciliationRepository"),
            patch("app.services.time_entry.TimeEntryService.finalize_entry"),
        ]
        (self.liveness_repo, self.idle_repo, self.audit_repo, self.finalize) = [p.start() for p in self.patches]
        for p in self.patches:
            self.addCleanup(p.stop)
        self.idle_repo.get_pending_for_entry.return_value = None
        self.liveness_repo.last_evidence_at.return_value = None

        def finalize(db, entry, end_time, description, user, **kwargs):
            entry.end_time = end_time
            entry.total_seconds = int((end_time - entry.start_time).total_seconds())
            return entry, True

        self.finalize.side_effect = finalize

    def _close(self, entry, **kwargs):
        self.liveness_repo.lock_running.return_value = entry
        kwargs.setdefault("reason", REASON_NO_SIGNAL)
        kwargs.setdefault("decided_by", "system:reconciler")
        return TimeEntryReconciliationService.close(
            self.db, entry.id, now=MONDAY, policy=POLICY, **kwargs,
        )

    def test_a_silent_entry_is_closed_at_its_last_evidence_plus_grace(self):
        seen = FRIDAY_START + timedelta(minutes=27)
        entry = _entry(last_seen_at=seen)
        closed = self._close(entry)

        self.assertIs(closed, entry)
        self.assertEqual(closed.end_time, seen + timedelta(minutes=15))
        self.assertEqual(closed.total_seconds, 42 * 60)
        self.assertEqual(closed.start_time, FRIDAY_START, "the original start is preserved")

    def test_the_decision_is_recorded_with_its_evidence_and_policy(self):
        seen = FRIDAY_START + timedelta(minutes=27)
        self._close(_entry(last_seen_at=seen))

        fields = self.audit_repo.create.call_args.kwargs
        self.assertEqual(fields["time_entry_id"], 5958)
        self.assertEqual(fields["user_id"], 260)
        self.assertEqual(fields["reason"], REASON_NO_SIGNAL)
        self.assertEqual(fields["decided_by"], "system:reconciler")
        self.assertEqual(fields["last_seen_at"], seen)
        self.assertEqual(fields["ended_at"], seen + timedelta(minutes=15))
        self.assertEqual(fields["total_seconds"], 42 * 60)
        self.assertEqual(fields["entry_start_time"], FRIDAY_START)
        self.assertEqual(fields["silent_for_seconds"], int((MONDAY - seen).total_seconds()))
        self.assertEqual(fields["policy"], POLICY.as_dict())

    def test_it_ends_the_entry_through_the_one_stop_path(self):
        self._close(_entry(last_seen_at=FRIDAY_START + timedelta(minutes=27)))
        self.finalize.assert_called_once()
        kwargs = self.finalize.call_args.kwargs
        self.assertIn("reconciled reason=no_signal", kwargs["note"])
        self.assertIn("Timer stopped automatically", kwargs["activity_description"])
        self.assertEqual(kwargs["activity_at"], MONDAY, "the trail records when it was decided")

    def test_an_entry_somebody_else_holds_is_not_ours_this_pass(self):
        self.liveness_repo.lock_running.return_value = None
        self.assertIsNone(TimeEntryReconciliationService.close(
            self.db, 5958, reason=REASON_NO_SIGNAL, decided_by="system:reconciler",
            now=MONDAY, policy=POLICY,
        ))
        self.finalize.assert_not_called()
        self.audit_repo.create.assert_not_called()

    def test_a_heartbeat_that_landed_first_wins(self):
        """The locked re-read shows the entry alive again: nothing is closed,
        whatever the caller saw a moment earlier."""
        entry = _entry(last_seen_at=MONDAY - timedelta(seconds=30))
        self.assertIsNone(self._close(entry))
        self.finalize.assert_not_called()
        self.audit_repo.create.assert_not_called()
        self.db.rollback.assert_called()

    def test_an_unanswered_prompt_is_recorded_and_resolved_by_the_stop_path(self):
        detected = MONDAY - timedelta(hours=6)
        self.idle_repo.get_pending_for_entry.return_value = _idle(detected, id=91)
        entry = _entry(last_seen_at=MONDAY - timedelta(minutes=1))
        closed = self._close(entry, reason=REASON_IDLE_UNANSWERED)

        self.assertEqual(closed.end_time, detected)
        self.assertEqual(self.audit_repo.create.call_args.kwargs["idle_period_id"], 91)
        self.assertEqual(self.audit_repo.create.call_args.kwargs["reason"], REASON_IDLE_UNANSWERED)

    def test_a_lost_compare_and_set_returns_nothing(self):
        self.finalize.side_effect = lambda db, entry, end, desc, user, **kw: (entry, False)
        self.assertIsNone(self._close(_entry(last_seen_at=FRIDAY_START + timedelta(minutes=27))))

    def test_a_start_that_replaces_a_silent_entry_cannot_overlap_it(self):
        """The new session's own start caps the old entry's end."""
        seen = MONDAY - timedelta(minutes=12)            # stale, but now is still the effective end
        entry = _entry(last_seen_at=seen)
        new_start = MONDAY - timedelta(seconds=3)
        closed = self._close(entry, reason=REASON_REPLACED_BY_START, decided_by="system:start",
                             not_after=new_start)
        self.assertEqual(closed.end_time, new_start)

    def test_replacement_requires_the_entry_to_be_stale(self):
        for last_seen in (MONDAY - timedelta(minutes=3), None):
            with self.subTest(last_seen=last_seen):
                self.finalize.reset_mock()
                entry = _entry(last_seen_at=last_seen)
                self.assertIsNone(self._close(
                    entry, reason=REASON_REPLACED_BY_START, decided_by="system:start",
                ))
                self.finalize.assert_not_called()


class ReplaceOnStartTests(unittest.TestCase):
    def test_only_a_stale_entry_is_replaced(self):
        for last_seen, expected in (
            (MONDAY - timedelta(minutes=3), False),        # live
            (None, False),                                 # never reported
            (MONDAY - timedelta(minutes=30), True),        # stale
        ):
            with self.subTest(last_seen=last_seen), patch.object(
                TimeEntryReconciliationService, "close", return_value=_entry() if expected else None,
            ) as close, patch("app.services.time_entry_reconciliation.TimerPolicy.current", return_value=POLICY):
                replaced = TimeEntryReconciliationService.close_if_replaced_by_start(
                    MagicMock(), _entry(last_seen_at=last_seen), MONDAY, now=MONDAY,
                )
                self.assertEqual(replaced, expected)
                self.assertEqual(close.called, expected)


class StartTimerReplacementTests(unittest.TestCase):
    """A new start must not be answered with Friday's timer."""

    def setUp(self):
        self.db = MagicMock()
        self.user = User()
        self.user.id, self.user.organization_id, self.user.role_name = 260, 1, "employee"
        self.user.permissions = {}
        self.task = patch("app.services.time_entry.TaskService.get_task")
        self.task.start()
        self.addCleanup(self.task.stop)

    def _start(self, active, replaced, **kwargs):
        created = _entry(id=6001, start_time=MONDAY, last_seen_at=None)
        with patch("app.services.time_entry.TimeEntryRepository.get_by_client_op", return_value=None), \
             patch("app.services.time_entry.TimeEntryRepository.get_active_for_user",
                   side_effect=[active, None if replaced else active]), \
             patch("app.services.time_entry_reconciliation.TimeEntryReconciliationService.close_if_replaced_by_start",
                   return_value=replaced) as replace, \
             patch("app.services.time_entry.TimeEntryRepository.create", return_value=created) as create, \
             patch("app.services.time_entry.ActivityLogService.capture"):
            result = TimeEntryService.start_timer(
                self.db, 3268, 5298, None, None, self.user, client_op="timer:5298:new", **kwargs,
            )
        return result, replace, create

    def test_a_silent_entry_is_closed_and_the_new_session_starts(self):
        (entry, created), replace, create = self._start(_entry(last_seen_at=FRIDAY_START), replaced=True)
        self.assertTrue(created)
        self.assertEqual(entry.id, 6001)
        replace.assert_called_once()
        create.assert_called_once()

    def test_a_live_entry_is_still_refused_with_409_and_itself(self):
        live = _entry(last_seen_at=datetime.now(UTC))
        with self.assertRaises(HTTPException) as error:
            self._start(live, replaced=False)
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(error.exception.detail["active_entry"]["id"], 5958)

    def test_a_client_that_reports_liveness_starts_the_entry_alive(self):
        _, _, create = self._start(None, replaced=False, reports_liveness=True)
        self.assertIsNotNone(create.call_args.kwargs["last_seen_at"])

    def test_an_older_client_starts_the_entry_unreported(self):
        _, _, create = self._start(None, replaced=False)
        self.assertIsNone(create.call_args.kwargs["last_seen_at"])


# ── the run ──────────────────────────────────────────────────────────────────

class RunTests(unittest.TestCase):
    def setUp(self):
        self.db = MagicMock()
        patcher = patch("app.services.time_entry_reconciliation.TimeEntryLivenessRepository")
        self.repo = patcher.start()
        self.addCleanup(patcher.stop)
        idle = patch("app.services.time_entry_reconciliation.TimeEntryIdlePeriodRepository")
        self.idle = idle.start()
        self.addCleanup(idle.stop)
        self.idle.get_pending_for_entry.return_value = None
        self.silent = _entry(id=1, user_id=11, last_seen_at=FRIDAY_START + timedelta(minutes=27))
        self.live = _entry(id=2, user_id=12, last_seen_at=MONDAY - timedelta(minutes=1))
        self.unreported = _entry(id=3, user_id=13, last_seen_at=None)
        self.repo.list_running.return_value = [self.silent, self.live, self.unreported]

    def _run(self, **kwargs):
        with patch("app.services.time_entry_reconciliation.TimerPolicy.current", return_value=POLICY):
            return TimeEntryReconciliationService.run(self.db, now=MONDAY, **kwargs)

    def test_a_report_only_run_writes_nothing(self):
        with patch.object(TimeEntryReconciliationService, "close") as close:
            report = self._run(apply=False)
        close.assert_not_called()
        self.assertTrue(report["dry_run"])
        self.assertEqual((report["examined"], report["candidates"], report["closed"]), (3, 1, 0))
        item = report["items"][0]
        self.assertEqual(item["time_entry_id"], 1)
        self.assertEqual(item["reason"], REASON_NO_SIGNAL)
        self.assertEqual(item["would_total_seconds"], 42 * 60)
        self.assertGreater(item["currently_measured_seconds"], 60 * 3600, "the 65-hour figure it would correct")
        self.assertFalse(item["applied"])

    def test_it_counts_the_entries_that_never_reported_but_does_not_touch_them(self):
        with patch.object(TimeEntryReconciliationService, "close", return_value=self.silent) as close:
            report = self._run(apply=True)
        self.assertEqual(report["unreported_running"], 1)
        close.assert_called_once()
        self.assertEqual(close.call_args.args[1], 1)

    def test_an_applied_run_closes_the_candidates_and_reports_it(self):
        with patch.object(TimeEntryReconciliationService, "close", return_value=self.silent):
            report = self._run(apply=True)
        self.assertFalse(report["dry_run"])
        self.assertEqual(report["closed"], 1)
        self.assertTrue(report["items"][0]["applied"])

    def test_one_failing_entry_does_not_stop_the_pass(self):
        second = _entry(id=4, user_id=14, last_seen_at=FRIDAY_START + timedelta(hours=1))
        self.repo.list_running.return_value = [self.silent, second]
        results = [RuntimeError("database went away"), second]
        with patch.object(TimeEntryReconciliationService, "close", side_effect=results):
            report = self._run(apply=True)
        self.assertEqual((report["failed"], report["closed"]), (1, 1))
        self.db.rollback.assert_called()

    def test_a_row_held_elsewhere_is_skipped(self):
        with patch.object(TimeEntryReconciliationService, "close", return_value=None):
            report = self._run(apply=True)
        self.assertEqual((report["skipped"], report["closed"]), (1, 0))

    def test_the_batch_size_bounds_what_one_run_closes(self):
        many = [_entry(id=10 + i, user_id=100 + i, last_seen_at=FRIDAY_START) for i in range(5)]
        self.repo.list_running.return_value = many
        with patch.object(TimeEntryReconciliationService, "close", side_effect=many):
            report = self._run(apply=True, limit=2)
        self.assertEqual(report["closed"], 2)

    def test_unreported_entries_are_reachable_only_when_named(self):
        with self.assertRaises(ValueError):
            self._run(apply=True, include_unreported=True)
        self.repo.last_evidence_at.return_value = FRIDAY_START + timedelta(hours=2)
        with patch.object(TimeEntryReconciliationService, "close", return_value=self.unreported) as close:
            report = self._run(apply=True, include_unreported=True, entry_ids=[3])
        self.assertEqual([i["time_entry_id"] for i in report["items"]], [3])
        self.assertEqual(report["items"][0]["reason"], REASON_OPERATOR)
        close.assert_called_once()

    def test_the_report_carries_no_names_or_titles(self):
        report = self._run(apply=False)
        for item in report["items"]:
            self.assertEqual(
                set(item),
                {"time_entry_id", "user_id", "reason", "start_time", "last_seen_at", "evidence_at",
                 "silent_for_seconds", "would_end_at", "would_total_seconds",
                 "currently_measured_seconds", "pending_idle_period_id", "applied"},
            )


# ── the heartbeat ────────────────────────────────────────────────────────────

class HeartbeatTests(unittest.TestCase):
    def setUp(self):
        self.db = MagicMock()
        self.user = User()
        self.user.id = 260

    def test_somebody_elses_entry_is_a_404(self):
        with patch("app.services.time_entry_liveness.TimeEntryRepository.get_by_id",
                   return_value=_entry(user_id=999)):
            with self.assertRaises(HTTPException) as error:
                TimeEntryLivenessService.heartbeat(self.db, 5958, self.user)
        self.assertEqual(error.exception.status_code, 404)

    def test_a_missing_entry_is_a_404(self):
        with patch("app.services.time_entry_liveness.TimeEntryRepository.get_by_id", return_value=None):
            with self.assertRaises(HTTPException) as error:
                TimeEntryLivenessService.heartbeat(self.db, 5958, self.user)
        self.assertEqual(error.exception.status_code, 404)

    def test_a_running_entry_is_touched_and_answered(self):
        entry = _entry(last_seen_at=datetime.now(UTC))
        with patch("app.services.time_entry_liveness.TimeEntryRepository.get_by_id", return_value=entry), \
             patch("app.services.time_entry_liveness.TimeEntryLivenessRepository.touch", return_value=True) as touch:
            result = TimeEntryLivenessService.heartbeat(self.db, 5958, self.user)
        touch.assert_called_once_with(self.db, 5958, 260)
        self.db.commit.assert_called()
        self.assertIs(result["entry"], entry)
        self.assertFalse(result["closed_by_server"])

    def test_an_entry_the_server_closed_says_so_and_why(self):
        closed = _entry(end_time=FRIDAY_START + timedelta(minutes=42), total_seconds=2520, status="stopped")
        with patch("app.services.time_entry_liveness.TimeEntryRepository.get_by_id", return_value=closed), \
             patch("app.services.time_entry_liveness.TimeEntryLivenessRepository.touch") as touch, \
             patch("app.services.time_entry_liveness.TimeEntryReconciliationRepository.get_for_entry",
                   return_value=SimpleNamespace(reason=REASON_NO_SIGNAL)):
            result = TimeEntryLivenessService.heartbeat(self.db, 5958, self.user)
        touch.assert_not_called()
        self.assertTrue(result["closed_by_server"])
        self.assertEqual(result["closed_reason"], REASON_NO_SIGNAL)

    def test_an_entry_the_user_stopped_is_not_reported_as_closed_by_the_server(self):
        stopped = _entry(end_time=FRIDAY_START + timedelta(hours=1), status="stopped")
        with patch("app.services.time_entry_liveness.TimeEntryRepository.get_by_id", return_value=stopped), \
             patch("app.services.time_entry_liveness.TimeEntryReconciliationRepository.get_for_entry",
                   return_value=None):
            result = TimeEntryLivenessService.heartbeat(self.db, 5958, self.user)
        self.assertFalse(result["closed_by_server"])
        self.assertIsNone(result["closed_reason"])

    def test_a_heartbeat_that_loses_to_a_closure_answers_with_what_is_there(self):
        running = _entry(last_seen_at=datetime.now(UTC) - timedelta(hours=3))
        closed = _entry(end_time=datetime.now(UTC), status="stopped")
        with patch("app.services.time_entry_liveness.TimeEntryRepository.get_by_id",
                   side_effect=[running, closed]), \
             patch("app.services.time_entry_liveness.TimeEntryLivenessRepository.touch", return_value=False), \
             patch("app.services.time_entry_liveness.TimeEntryReconciliationRepository.get_for_entry",
                   return_value=SimpleNamespace(reason=REASON_NO_SIGNAL)):
            result = TimeEntryLivenessService.heartbeat(self.db, 5958, self.user)
        self.assertIs(result["entry"], closed)
        self.assertTrue(result["closed_by_server"])

    def test_the_repository_only_touches_a_running_entry_of_the_caller(self):
        from app.repositories.time_entry_liveness import TimeEntryLivenessRepository

        db = MagicMock()
        TimeEntryLivenessRepository.touch(db, 5958, 260)
        sql = str(db.execute.call_args.args[0].compile(dialect=postgresql.dialect()))
        self.assertIn("time_entries.id =", sql)
        self.assertIn("time_entries.user_id =", sql)
        self.assertIn("time_entries.end_time IS NULL", sql)
        squeezed = sql.replace(" ", "").replace("\n", "")
        self.assertIn("last_seen_at=now()", squeezed)
        self.assertIn("updated_at=time_entries.updated_at", squeezed,
                      "a heartbeat is not a change to the entry")


class TouchTests(unittest.TestCase):
    def test_a_screenshot_upload_counts_as_proof_of_life_only_after_it_is_stored(self):
        """The one upload that cannot run against a database without Google Drive: the route
        function is called directly with the storage layer patched, and the order is the point
        -- a touch before the upload would credit a desktop for a request that then failed."""
        import io

        from app.api import time_entry_screenshot as route

        order = MagicMock()
        user = SimpleNamespace(id=260, organization_id=1)
        db = object()
        upload = SimpleNamespace(file=io.BytesIO(b"webp"), content_type="image/webp")
        with patch.object(route.TimeEntryScreenshotService, "upload_screenshot", order.upload), \
             patch.object(route.TimeEntryLivenessService, "touch", order.touch), \
             patch.object(route, "TimeEntryScreenshotRead", MagicMock()):
            order.upload.return_value = (SimpleNamespace(), False)
            route.upload_screenshot(
                time_entry_id=5958, file=upload, client_screenshot_id="shot-1", captured_at=None,
                monitor_number=1, display_count=1, current_user=user, db=db,
            )
        self.assertEqual([c[0] for c in order.mock_calls], ["upload", "touch"])
        self.assertEqual(order.touch.call_args.args, (db, 5958, 260))

    def test_a_stand_in_database_is_not_asked_anything(self):
        db = MagicMock()
        TimeEntryLivenessService.touch(db, 5958, 260)
        db.execute.assert_not_called()

    def test_a_failure_never_fails_the_upload_it_rides_on(self):
        from sqlalchemy.orm import Session

        db = MagicMock(spec=Session)
        db.execute.side_effect = RuntimeError("connection reset")
        TimeEntryLivenessService.touch(db, 5958, 260)          # must not raise
        db.rollback.assert_called()

    def test_no_entry_means_nothing_to_touch(self):
        from sqlalchemy.orm import Session

        db = MagicMock(spec=Session)
        TimeEntryLivenessService.touch(db, None, 260)
        db.execute.assert_not_called()


# ── a stop is honoured however old it is ─────────────────────────────────────

class OldStopTests(unittest.TestCase):
    def test_a_stop_queued_for_eight_days_is_recorded_when_it_was_pressed(self):
        """Offline for eight days, then delivered. Recorded at arrival time it
        ended the entry eight days after the user stopped it."""
        now = datetime(2026, 10, 20, 9, 0, 0, tzinfo=UTC)
        start = now - timedelta(days=9)
        pressed = now - timedelta(days=8)
        end = TimeEntryService._event_time(
            pressed, label="stopped_at", not_before=start, client_time=now, now=now, trust_age=True,
        )
        self.assertEqual(end, pressed)

    def test_the_ceiling_still_applies_to_a_start(self):
        now = datetime(2026, 10, 20, 9, 0, 0, tzinfo=UTC)
        pressed = now - timedelta(seconds=MAX_CLIENT_BACKDATE_SECONDS + 1)
        self.assertEqual(
            TimeEntryService._event_time(pressed, label="started_at", client_time=now, now=now), now,
        )

    def test_the_legacy_path_without_client_time_honours_an_old_stop_too(self):
        now = datetime(2026, 10, 20, 9, 0, 0, tzinfo=UTC)
        pressed = now - timedelta(days=8)
        self.assertEqual(
            TimeEntryService._event_time(pressed, label="stopped_at", now=now, trust_age=True), pressed,
        )

    def test_a_stop_still_cannot_precede_its_start(self):
        now = datetime(2026, 10, 20, 9, 0, 0, tzinfo=UTC)
        start = now - timedelta(hours=1)
        end = TimeEntryService._event_time(
            now - timedelta(days=8), label="stopped_at", not_before=start, client_time=now,
            now=now, trust_age=True,
        )
        self.assertGreaterEqual(end, start)


# ── Time Tracking's drill-down ───────────────────────────────────────────────

class DrillDownTests(unittest.TestCase):
    """The member drill-down shows a silent entry ending where its figure ends."""

    def _detail(self, entry, duration, day):
        from app.schemas.time_tracking import TimeTrackingDetailResponse
        from app.services.time_tracking import TimeTrackingService

        user = SimpleNamespace(
            id=10, organization_id=1, role_name="administrator", permissions={"time_entries:view_all": True},
            name="Kunal Vaghela", email="kunal@example.invalid", designation="Developer",
        )
        project = SimpleNamespace(id=1, project_name="Coach Companion")
        task = SimpleNamespace(id=2, task_name="Mobile Application Development")
        rows = [(entry, project, task, None, None, duration)]
        with patch("app.services.time_tracking.TimeTrackingRepository.get_employee", return_value=user),              patch("app.services.time_tracking.TimeTrackingRepository.detail_entries", return_value=rows):
            return TimeTrackingDetailResponse.model_validate(
                TimeTrackingService.detail(None, user, 10, None, day, None, None)
            )

    def test_a_silent_running_entry_ends_at_its_last_evidence_on_the_day_it_started(self):
        from datetime import date

        seen = FRIDAY_START + timedelta(minutes=27)
        entry = SimpleNamespace(
            id=5958, start_time=FRIDAY_START, end_time=None, last_seen_at=seen, is_manual=False,
        )
        shown = self._detail(entry, 42 * 60, date(2026, 10, 9)).projects[0].tasks[0].entries[0]
        self.assertEqual(shown.end_time, seen + timedelta(minutes=15), "Clock Out is where the figure ends")
        self.assertEqual(shown.duration_seconds, 42 * 60)
        self.assertTrue(shown.is_running, "it is still open on the server; the page says where it stops")

    def test_a_silent_entry_adds_nothing_to_the_days_after_its_evidence(self):
        from datetime import date

        seen = FRIDAY_START + timedelta(minutes=27)
        entry = SimpleNamespace(
            id=5958, start_time=FRIDAY_START, end_time=None, last_seen_at=seen, is_manual=False,
        )
        saturday = self._detail(entry, 0, date(2026, 10, 10))
        self.assertEqual(saturday.summary.total_seconds, 0)

    def test_a_running_entry_that_reports_in_is_open_as_before(self):
        from datetime import date

        today = datetime.now(UTC)
        entry = SimpleNamespace(
            id=1, start_time=today - timedelta(hours=1), end_time=None,
            last_seen_at=today - timedelta(seconds=30), is_manual=False,
        )
        from app.core.time_format import ist_today

        shown = self._detail(entry, 3600, ist_today()).projects[0].tasks[0].entries[0]
        self.assertIsNone(shown.end_time)
        self.assertTrue(shown.is_running)


# ── the routes ───────────────────────────────────────────────────────────────

class RouteTests(unittest.TestCase):
    TOKEN = "reconcile-test-token"

    def setUp(self):
        from fastapi.testclient import TestClient

        from app.core.database import get_db
        from app.core.security import get_current_user
        from app.main import app

        self.user = User()
        self.user.id, self.user.organization_id, self.user.role_name = 260, 1, "employee"
        self.user.permissions = {}
        app.dependency_overrides[get_current_user] = lambda: self.user
        app.dependency_overrides[get_db] = lambda: None
        self.client = TestClient(app)
        self.addCleanup(app.dependency_overrides.clear)
        net = patch("app.repositories.time_entry_adjustment.TimeEntryAdjustmentRepository.net_for_entries",
                    return_value={})
        net.start()
        self.addCleanup(net.stop)
        token = patch("app.api.email_notifications.settings.EMAIL_DISPATCH_TOKEN", self.TOKEN)
        token.start()
        self.addCleanup(token.stop)

    def _headers(self):
        return {"X-Email-Dispatch-Token": self.TOKEN}

    def test_the_heartbeat_answers_with_the_entry_and_the_server_clock(self):
        entry = _entry(last_seen_at=datetime.now(UTC))
        with patch("app.api.time_entry.TimeEntryLivenessService.heartbeat", return_value={
            "entry": entry, "closed_by_server": False, "closed_reason": None,
            "server_time": datetime.now(UTC),
        }):
            response = self.client.post("/time-entries/5958/heartbeat")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["entry"]["id"], 5958)
        self.assertEqual(body["entry"]["liveness"], "live")
        self.assertFalse(body["closed_by_server"])
        self.assertIn("server_time", body)

    def test_the_heartbeat_tells_the_desktop_the_server_closed_its_entry(self):
        closed = _entry(end_time=FRIDAY_START + timedelta(minutes=42), total_seconds=2520, status="stopped")
        with patch("app.api.time_entry.TimeEntryLivenessService.heartbeat", return_value={
            "entry": closed, "closed_by_server": True, "closed_reason": "no_signal",
            "server_time": datetime.now(UTC),
        }):
            response = self.client.post("/api/v1/time-entries/5958/heartbeat")
        body = response.json()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(body["entry"]["is_running"])
        self.assertTrue(body["closed_by_server"])
        self.assertEqual(body["closed_reason"], "no_signal")

    def test_a_running_entry_reports_its_liveness_and_a_frozen_figure_when_silent(self):
        """The read every client sees: a silent entry's elapsed time stops at
        the evidence, and says why."""
        entry = _entry(last_seen_at=FRIDAY_START + timedelta(minutes=27))
        with patch("app.api.time_entry.TimeEntryService.get_active_entry", return_value=entry):
            body = self.client.get("/time-entries/active").json()["entry"]
        self.assertEqual(body["liveness"], "stale")
        self.assertEqual(body["elapsed_seconds"], 42 * 60)
        self.assertEqual(body["elapsed_time"], "00:42:00")

    def test_an_entry_that_never_reported_is_unknown_and_measured_as_before(self):
        entry = _entry(start_time=datetime.now(UTC) - timedelta(hours=3), last_seen_at=None)
        with patch("app.api.time_entry.TimeEntryService.get_active_entry", return_value=entry):
            body = self.client.get("/time-entries/active").json()["entry"]
        self.assertEqual(body["liveness"], "unknown")
        self.assertAlmostEqual(body["elapsed_seconds"], 3 * 3600, delta=5)

    def test_a_start_may_promise_liveness(self):
        entry = _entry(id=61)
        with patch("app.api.time_entry.TimeEntryService.start_timer", return_value=(entry, True)) as start:
            self.client.post("/time-entries/start", json={
                "project_id": 2, "task_id": 3, "client_op": "timer:3:k", "reports_liveness": True,
            })
        self.assertIs(start.call_args.kwargs["reports_liveness"], True)

    def test_the_reconcile_endpoint_refuses_a_missing_or_wrong_token(self):
        self.assertEqual(self.client.post("/internal/time-entries/reconcile").status_code, 401)
        self.assertEqual(
            self.client.post("/internal/time-entries/reconcile",
                             headers={"X-Email-Dispatch-Token": "wrong"}).status_code, 401,
        )

    def _run(self, query="", **settings):
        from app.core.config import settings as app_settings

        report = {"dry_run": True, "policy": POLICY.as_dict()}
        with patch.object(app_settings, "TIMER_RECONCILER_ENABLED", settings.get("enabled", False)),              patch("app.api.time_entry_reconcile.TimeEntryReconciliationService.run",
                   return_value=report) as run:
            response = self.client.post(f"/internal/time-entries/reconcile{query}", headers=self._headers())
        return response, run

    def test_the_scheduled_run_only_reports_until_the_deployment_turns_it_on(self):
        response, run = self._run(enabled=False)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(run.call_args.kwargs["apply"])

    def test_the_scheduled_run_applies_once_enabled(self):
        _, run = self._run(enabled=True)
        self.assertTrue(run.call_args.kwargs["apply"])
        self.assertEqual(run.call_args.kwargs["decided_by"], "system:reconciler")

    def test_an_explicit_dry_run_reports_even_when_enabled(self):
        _, run = self._run("?dry_run=true", enabled=True)
        self.assertFalse(run.call_args.kwargs["apply"])

    def test_an_explicit_apply_is_the_operators_decision_even_when_disabled(self):
        _, run = self._run("?dry_run=false&entry_id=5958", enabled=False)
        self.assertTrue(run.call_args.kwargs["apply"])
        self.assertEqual(run.call_args.kwargs["decided_by"], "operator")
        self.assertEqual(run.call_args.kwargs["entry_ids"], [5958])

    def test_closing_an_unreported_entry_must_name_it(self):
        response, run = self._run("?dry_run=false&include_unreported=true")
        self.assertEqual(response.status_code, 400, response.text)
        run.assert_not_called()

    def test_an_operator_request_must_say_whether_it_is_a_look_or_a_closure(self):
        """With the job enabled an omitted `dry_run` means "apply", so a request that was
        only meant to look at a legacy row would have closed it."""
        response, run = self._run("?include_unreported=true&entry_id=5958", enabled=True)
        self.assertEqual(response.status_code, 400, response.text)
        run.assert_not_called()

    def test_an_operator_look_at_a_named_unreported_entry_is_report_only(self):
        response, run = self._run("?include_unreported=true&entry_id=5958&dry_run=true", enabled=True)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(run.call_args.kwargs["apply"])
        self.assertTrue(run.call_args.kwargs["include_unreported"])

    def test_the_get_alias_only_ever_reports(self):
        """A GET is a safe method: whatever a crawler, a prefetch or a cron-style scheduler that
        can only issue one does with it, it reads. Applying -- the scheduled pass or an operator
        closure -- is a POST, even with the job enabled."""
        with patch("app.api.time_entry_reconcile.settings.TIMER_RECONCILER_ENABLED", True), \
             patch("app.api.time_entry_reconcile.TimeEntryReconciliationService.run",
                   return_value={"dry_run": True, "policy": POLICY.as_dict()}) as run:
            plain = self.client.get("/internal/time-entries/reconcile", headers=self._headers())
            forced = self.client.get("/internal/time-entries/reconcile?dry_run=false", headers=self._headers())
            named = self.client.get(
                "/internal/time-entries/reconcile?include_unreported=true&entry_id=5958&dry_run=true",
                headers=self._headers(),
            )
        self.assertEqual(plain.status_code, 200, plain.text)
        self.assertEqual((forced.status_code, named.status_code), (400, 400))
        self.assertEqual(run.call_count, 1, "only the plain report ran")
        self.assertFalse(run.call_args.kwargs["apply"], "and it applied nothing, though the job is enabled")

    def test_the_post_still_applies_when_the_job_is_enabled(self):
        _, run = self._run(enabled=True)
        self.assertTrue(run.call_args.kwargs["apply"])

    def test_the_last_run_is_remembered_for_health_to_show(self):
        from app.api import time_entry_reconcile as route

        route._LAST_RUN.clear()
        self.assertIsNone(route.last_run_snapshot(), "nothing has run in this process yet")
        report = {"dry_run": True, "examined": 7, "candidates": 2, "closed": 0, "skipped": 0,
                  "failed": 0, "unreported_running": 1, "policy": POLICY.as_dict()}
        with patch("app.api.time_entry_reconcile.TimeEntryReconciliationService.run", return_value=report):
            self.client.post("/internal/time-entries/reconcile", headers=self._headers())
        seen = route.last_run_snapshot()
        self.assertEqual(
            {k: seen[k] for k in ("dry_run", "examined", "candidates", "closed", "failed")},
            {"dry_run": True, "examined": 7, "candidates": 2, "closed": 0, "failed": 0},
        )
        self.assertIn("at", seen)
        self.assertNotIn("items", seen, "counts only: no entry, user or project leaves through /health")
        route._LAST_RUN.clear()


if __name__ == "__main__":
    unittest.main()

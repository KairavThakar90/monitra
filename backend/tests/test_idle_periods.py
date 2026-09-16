"""Idle time + reassign time.

These are unit tests in the style of the rest of this suite: the session and
the repositories are mocked, so the business rules are exercised without a
database. What they pin down is exactly the part a client must not be trusted
with -- whether idle time counts, how long it was, and where reassigned time
lands.
"""

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.models.project import Project
from app.models.task import Task
from app.models.time_entry import TimeEntry
from app.models.time_entry_idle_period import (
    IdlePeriodAction, IdlePeriodStatus, TimeEntryIdlePeriod,
)
from app.models.user import User
from app.schemas.time_entry_idle_period import (
    IdlePeriodCreate, IdlePeriodReassign, IdlePeriodResolve,
)
from app.schemas.user import UserUpdate
from app.services.time_entry_idle_period import (
    TimeEntryIdlePeriodService, counts_idle_time,
)

SVC = "app.services.time_entry_idle_period"

# Anchored a day in the past so every instant in this module -- including the
# multi-idle-period session, which runs hours past the anchor -- stays behind
# the real clock. The service clamps a resolution instant to "now", so
# timestamps fixed to a calendar date would start clamping the moment the
# suite ran before that time of day.
_ANCHOR = (datetime.now(timezone.utc) - timedelta(days=1)).replace(
    hour=10, minute=0, second=0, microsecond=0
)
T0 = _ANCHOR                                    # timer started
IDLE_START = _ANCHOR + timedelta(minutes=10)    # last keyboard/mouse activity
DETECTED = _ANCHOR + timedelta(minutes=15)      # threshold hit, popup raised
RESOLVED = _ANCHOR + timedelta(minutes=20)      # user answered the popup
# The example from the specification: the popup sat open for five minutes
# beyond the threshold, so the real idle period is ten minutes, not five.
FULL_IDLE_SECONDS = 600


def _user(**overrides) -> User:
    defaults = dict(
        id=1, organization_id=10, permissions={}, role_name="employee",
        idle_enabled=True, idle_minutes=5,
    )
    defaults.update(overrides)
    return User(**defaults)


def _entry(**overrides) -> TimeEntry:
    defaults = dict(
        id=100, organization_id=10, user_id=1, project_id=5, task_id=7,
        start_time=T0, end_time=None, total_seconds=0, status="running",
        is_manual=False, is_billable=False,
    )
    defaults.update(overrides)
    return TimeEntry(**defaults)


def _idle(**overrides) -> TimeEntryIdlePeriod:
    defaults = dict(
        id=456, organization_id=10, user_id=1, time_entry_id=100,
        original_project_id=5, original_task_id=7,
        idle_started_at=IDLE_START, idle_detected_at=DETECTED,
        status=IdlePeriodStatus.PENDING, reassigned=False,
    )
    defaults.update(overrides)
    return TimeEntryIdlePeriod(**defaults)


# ----------------------------------------------------------------------
# 1-3. User-specific idle configuration
# ----------------------------------------------------------------------

class TestIdleConfiguration(unittest.TestCase):
    def test_default_five_minute_configuration(self):
        config = TimeEntryIdlePeriodService.get_idle_config(_user())
        self.assertEqual(config, {"idle_enabled": True, "idle_minutes": 5})

    def test_custom_threshold_is_read_from_the_user_row(self):
        config = TimeEntryIdlePeriodService.get_idle_config(_user(idle_minutes=10))
        self.assertEqual(config["idle_minutes"], 10)

    def test_idle_disabled_is_reported(self):
        config = TimeEntryIdlePeriodService.get_idle_config(_user(idle_enabled=False))
        self.assertFalse(config["idle_enabled"])

    def test_disabled_user_cannot_open_an_idle_period(self):
        with pytest.raises(HTTPException) as exc:
            TimeEntryIdlePeriodService.report_idle_period(
                MagicMock(),
                IdlePeriodCreate(time_entry_id=100, idle_started_at=IDLE_START),
                _user(idle_enabled=False),
            )
        self.assertEqual(exc.value.status_code, 409)

    def test_non_positive_threshold_is_rejected_on_write(self):
        with pytest.raises(ValidationError):
            UserUpdate(idle_minutes=0)
        self.assertEqual(UserUpdate(idle_minutes=15).idle_minutes, 15)

    def test_custom_threshold_governs_the_report_not_a_hardcoded_five(self):
        """A ten-minute user reporting a five-minute gap is rejected; the same
        report from a five-minute user is accepted."""
        user = _user(idle_minutes=10)
        db = MagicMock()
        with patch(f"{SVC}.TimeEntryIdlePeriodService._owned_entry", return_value=_entry()):
            with pytest.raises(HTTPException) as exc:
                TimeEntryIdlePeriodService.report_idle_period(
                    db,
                    IdlePeriodCreate(
                        time_entry_id=100, idle_started_at=IDLE_START,
                        idle_detected_at=DETECTED,
                    ),
                    user,
                )
        self.assertEqual(exc.value.status_code, 400)
        self.assertIn("10 minute", exc.value.detail)


# ----------------------------------------------------------------------
# 4-7. The four core combinations
# ----------------------------------------------------------------------

class TestCountingRule(unittest.TestCase):
    def test_the_four_combinations(self):
        self.assertFalse(counts_idle_time(False, IdlePeriodAction.STOP))
        self.assertFalse(counts_idle_time(False, IdlePeriodAction.RESUME))
        # "Yes, keep idle time" is overridden by Stop -- condition 3.
        self.assertFalse(counts_idle_time(True, IdlePeriodAction.STOP))
        self.assertTrue(counts_idle_time(True, IdlePeriodAction.RESUME))


class TestResolution(unittest.TestCase):
    def setUp(self):
        self.db = MagicMock()

    def _resolve(self, keep, action, idle_period=None, entry=None):
        idle_period = idle_period or _idle()
        entry = entry or _entry()
        with patch(f"{SVC}.TimeEntryIdlePeriodService._owned_idle_period", return_value=idle_period), \
             patch(f"{SVC}.TimeEntryIdlePeriodService._owned_entry", return_value=entry), \
             patch(f"{SVC}.TimeEntryAdjustmentRepository.create_pending") as adjust, \
             patch("app.services.time_entry.TimeEntryService.stop_timer") as stop:
            result = TimeEntryIdlePeriodService.resolve(
                self.db, 456,
                IdlePeriodResolve(keep_idle_time=keep, action=action, resolved_at=RESOLVED),
                _user(),
            )
        return result, adjust, stop

    def test_discard_and_stop_does_not_count_idle_time(self):
        result, adjust, stop = self._resolve(False, "stop")
        self.assertFalse(result.counted)
        self.assertEqual(result.idle_duration_seconds, FULL_IDLE_SECONDS)
        self.assertEqual(adjust.call_args.kwargs["adjustment_seconds"], -FULL_IDLE_SECONDS)
        stop.assert_called_once()

    def test_discard_and_resume_does_not_count_idle_time(self):
        result, adjust, stop = self._resolve(False, "resume")
        self.assertFalse(result.counted)
        self.assertEqual(adjust.call_args.kwargs["adjustment_seconds"], -FULL_IDLE_SECONDS)
        stop.assert_not_called()

    def test_keep_and_stop_still_discards_idle_time(self):
        result, adjust, stop = self._resolve(True, "stop")
        self.assertFalse(result.counted)
        self.assertEqual(adjust.call_args.kwargs["adjustment_seconds"], -FULL_IDLE_SECONDS)
        stop.assert_called_once()

    def test_keep_and_resume_counts_idle_time(self):
        result, adjust, stop = self._resolve(True, "resume")
        self.assertTrue(result.counted)
        adjust.assert_not_called()          # nothing is deducted
        stop.assert_not_called()            # the timer keeps running
        self.assertEqual(result.idle_duration_seconds, FULL_IDLE_SECONDS)

    def test_actual_duration_not_the_threshold(self):
        """The popup sat open past the threshold; all ten minutes are the
        idle period, not the configured five."""
        result, adjust, _ = self._resolve(False, "resume")
        self.assertEqual(result.idle_duration_seconds, 600)
        self.assertNotEqual(result.idle_duration_seconds, 5 * 60)

    def test_stop_is_delegated_to_the_existing_stop_path(self):
        _, _, stop = self._resolve(False, "stop")
        self.assertEqual(stop.call_args.kwargs["entry_id"], 100)
        self.assertEqual(stop.call_args.kwargs["stopped_at"], RESOLVED)

    def test_resume_on_an_already_stopped_entry_is_rejected(self):
        with pytest.raises(HTTPException) as exc:
            self._resolve(True, "resume", entry=_entry(end_time=RESOLVED, status="stopped"))
        self.assertEqual(exc.value.status_code, 409)

    def test_invalid_action_is_rejected_by_the_schema(self):
        with pytest.raises(ValidationError):
            IdlePeriodResolve(keep_idle_time=True, action="pause")


# ----------------------------------------------------------------------
# 8-18. Reassignment
# ----------------------------------------------------------------------

def _project(**overrides) -> Project:
    defaults = dict(id=2, organization_id=10, project_name="Development",
                    created_by=1, is_billable=True)
    defaults.update(overrides)
    return Project(**defaults)


def _task(**overrides) -> Task:
    defaults = dict(id=11, organization_id=10, project_id=2,
                    task_name="Frontend Development", created_by=1)
    defaults.update(overrides)
    return Task(**defaults)


class TestEntryAdjustmentOnTheResponse(unittest.TestCase):
    """Every idle-period response names the entry's net adjustment, so the
    desktop can drop its running clock by exactly what the server deducted
    -- without computing idle time itself."""

    def _resolve(self, keep, action, net):
        with patch(f"{SVC}.TimeEntryIdlePeriodService._owned_idle_period", return_value=_idle()), \
             patch(f"{SVC}.TimeEntryIdlePeriodService._owned_entry", return_value=_entry()), \
             patch(f"{SVC}.TimeEntryAdjustmentRepository.create_pending"), \
             patch(f"{SVC}.TimeEntryAdjustmentRepository.sum_for_entry", return_value=net) as summed, \
             patch("app.services.time_entry.TimeEntryService.stop_timer"):
            result = TimeEntryIdlePeriodService.resolve(
                MagicMock(), 456,
                IdlePeriodResolve(keep_idle_time=keep, action=action, resolved_at=RESOLVED),
                _user(),
            )
        return result, summed

    def test_a_discarded_period_reports_the_entrys_deduction(self):
        result, summed = self._resolve(False, "resume", net=-FULL_IDLE_SECONDS)
        self.assertEqual(result.time_entry_adjustment_seconds, -FULL_IDLE_SECONDS)
        # Summed for the period's own entry, after the resolution was written.
        self.assertEqual(summed.call_args[0][1], 100)

    def test_a_kept_period_reports_an_unchanged_entry(self):
        result, _ = self._resolve(True, "resume", net=0)
        self.assertEqual(result.time_entry_adjustment_seconds, 0)

    def test_the_figure_is_part_of_the_response_schema(self):
        from app.schemas.time_entry_idle_period import IdlePeriodResponse

        result, _ = self._resolve(False, "stop", net=-FULL_IDLE_SECONDS)
        result.created_at = result.updated_at = RESOLVED
        read = IdlePeriodResponse.model_validate(result)
        self.assertEqual(read.time_entry_adjustment_seconds, -FULL_IDLE_SECONDS)

    def test_a_pending_lookup_carries_it_too(self):
        with patch(f"{SVC}.TimeEntryIdlePeriodService._owned_entry", return_value=_entry()), \
             patch(f"{SVC}.TimeEntryIdlePeriodRepository.get_pending_for_entry", return_value=_idle()), \
             patch(f"{SVC}.TimeEntryAdjustmentRepository.sum_for_entry", return_value=-300):
            pending = TimeEntryIdlePeriodService.get_pending_for_entry(MagicMock(), 100, _user())
        self.assertEqual(pending.time_entry_adjustment_seconds, -300)


class TestReassignment(unittest.TestCase):
    def setUp(self):
        self.db = MagicMock()
        self.added = []
        self.db.add.side_effect = self.added.append

    def _reassign(self, idle_period=None, entry=None, project=None, task=None,
                  project_exc=None, task_exc=None):
        idle_period = idle_period or _idle()
        entry = entry or _entry()
        with patch(f"{SVC}.TimeEntryIdlePeriodService._owned_idle_period", return_value=idle_period), \
             patch(f"{SVC}.TimeEntryIdlePeriodService._owned_entry", return_value=entry), \
             patch(f"{SVC}.ProjectService.get_project",
                   side_effect=project_exc, return_value=project or _project()) as get_project, \
             patch(f"{SVC}.TaskService.get_task",
                   side_effect=task_exc, return_value=task or _task()) as get_task, \
             patch(f"{SVC}.TimeEntryAdjustmentRepository.create_pending") as adjust, \
             patch(f"{SVC}.TimeEntryIdlePeriodService._refresh_task_rollup"):
            result = TimeEntryIdlePeriodService.reassign(
                self.db, 456, IdlePeriodReassign(project_id=2, task_id=11), _user()
            )
        return result, adjust, get_project, get_task

    def test_authorization_is_delegated_to_the_project_and_task_services(self):
        """The dropdowns and the reassignment enforce the same rules: the
        project/task the client names is re-checked through the services that
        decide what the user may see at all."""
        _, _, get_project, get_task = self._reassign()
        get_project.assert_called_once()
        self.assertEqual(get_project.call_args[0][1], 2)
        # The task is looked up *within* the selected project, so a task from
        # another project cannot be accepted.
        self.assertEqual(get_task.call_args[0][1:3], (2, 11))

    def test_unauthorized_or_missing_project_is_rejected(self):
        with pytest.raises(HTTPException) as exc:
            self._reassign(project_exc=HTTPException(404, "Project not found"))
        self.assertEqual(exc.value.status_code, 404)
        self.db.rollback.assert_called_once()

    def test_task_from_another_project_is_rejected(self):
        with pytest.raises(HTTPException) as exc:
            self._reassign(task_exc=HTTPException(404, "Task not found"))
        self.assertEqual(exc.value.status_code, 404)
        self.db.rollback.assert_called_once()

    def test_successful_reassignment_records_the_destination(self):
        (idle_period, project, task), adjust, _, _ = self._reassign()
        self.assertTrue(idle_period.reassigned)
        self.assertEqual(idle_period.reassigned_project_id, 2)
        self.assertEqual(idle_period.reassigned_task_id, 11)
        self.assertGreater(idle_period.reassigned_seconds, 0)
        self.assertEqual(project.id, 2)
        self.assertEqual(task.id, 11)
        self.db.commit.assert_called_once()

    def test_a_destination_time_entry_carries_the_idle_seconds(self):
        (idle_period, _, _), _, _, _ = self._reassign()
        destination = next(o for o in self.added if isinstance(o, TimeEntry))
        self.assertEqual(destination.project_id, 2)
        self.assertEqual(destination.task_id, 11)
        self.assertEqual(destination.user_id, 1)
        self.assertEqual(destination.organization_id, 10)
        # Already stopped, so it cannot collide with the one-active-timer index.
        self.assertIsNotNone(destination.end_time)
        self.assertEqual(destination.status, "stopped")
        self.assertEqual(destination.start_time, IDLE_START)
        self.assertEqual(destination.total_seconds, idle_period.reassigned_seconds)

    def test_the_same_seconds_are_deducted_from_the_original_entry(self):
        """Counted exactly once: added at the destination, removed from the
        original by a matching negative adjustment."""
        (idle_period, _, _), adjust, _, _ = self._reassign()
        kwargs = adjust.call_args.kwargs
        self.assertEqual(kwargs["time_entry_id"], 100)
        self.assertEqual(kwargs["project_id"], 5)   # the ORIGINAL project
        self.assertEqual(kwargs["task_id"], 7)
        self.assertEqual(kwargs["adjustment_seconds"], -idle_period.reassigned_seconds)

    def test_the_original_entry_itself_is_never_re_pointed(self):
        """Moving project_id/task_id on the original entry would drag the
        legitimate work done before the idle period with it."""
        entry = _entry()
        self._reassign(entry=entry)
        self.assertEqual(entry.project_id, 5)
        self.assertEqual(entry.task_id, 7)

    def test_duplicate_reassignment_is_rejected(self):
        already = _idle(reassigned=True, reassigned_project_id=2,
                        reassigned_task_id=11, reassigned_seconds=300)
        with pytest.raises(HTTPException) as exc:
            self._reassign(idle_period=already)
        self.assertEqual(exc.value.status_code, 409)

    def test_a_resolved_period_cannot_be_reassigned(self):
        resolved = _idle(status=IdlePeriodStatus.RESOLVED)
        with pytest.raises(HTTPException) as exc:
            self._reassign(idle_period=resolved)
        self.assertEqual(exc.value.status_code, 409)

    def test_reassignment_leaves_the_period_pending_for_the_main_popup(self):
        (idle_period, _, _), _, _, _ = self._reassign()
        self.assertEqual(idle_period.status, IdlePeriodStatus.PENDING)


class TestReassignmentThenResolution(unittest.TestCase):
    """After a reassignment the keep/discard rule applies only to the residual
    idle time -- the reassigned seconds are neither counted again nor deducted
    a second time."""

    def setUp(self):
        self.db = MagicMock()

    def _resolve(self, keep, action, reassigned_seconds):
        idle_period = _idle(
            reassigned=True, reassigned_at=DETECTED, reassigned_project_id=2,
            reassigned_task_id=11, reassigned_time_entry_id=900,
            reassigned_seconds=reassigned_seconds,
        )
        with patch(f"{SVC}.TimeEntryIdlePeriodService._owned_idle_period", return_value=idle_period), \
             patch(f"{SVC}.TimeEntryIdlePeriodService._owned_entry", return_value=_entry()), \
             patch(f"{SVC}.TimeEntryAdjustmentRepository.create_pending") as adjust, \
             patch("app.services.time_entry.TimeEntryService.stop_timer"):
            result = TimeEntryIdlePeriodService.resolve(
                self.db, 456,
                IdlePeriodResolve(keep_idle_time=keep, action=action, resolved_at=RESOLVED),
                _user(),
            )
        return result, adjust

    def test_only_the_residual_is_deducted_on_discard(self):
        # 600s idle, 300s already moved to the destination project.
        result, adjust = self._resolve(False, "resume", 300)
        self.assertEqual(result.idle_duration_seconds, 600)
        self.assertEqual(adjust.call_args.kwargs["adjustment_seconds"], -300)

    def test_no_second_deduction_when_the_whole_period_was_reassigned(self):
        result, adjust = self._resolve(False, "resume", 600)
        adjust.assert_not_called()
        self.assertEqual(result.idle_duration_seconds, 600)

    def test_keep_and_resume_after_reassignment_deducts_nothing_more(self):
        result, adjust = self._resolve(True, "resume", 300)
        self.assertTrue(result.counted)
        adjust.assert_not_called()


# ----------------------------------------------------------------------
# 19-27. Data integrity, idempotency, concurrency
# ----------------------------------------------------------------------

class TestReporting(unittest.TestCase):
    def setUp(self):
        self.db = MagicMock()

    def _report(self, payload, user=None, entry=None, pending=None, by_key=None):
        with patch(f"{SVC}.TimeEntryIdlePeriodService._owned_entry",
                   return_value=entry or _entry()), \
             patch(f"{SVC}.TimeEntryIdlePeriodRepository.get_pending_for_entry",
                   return_value=pending), \
             patch(f"{SVC}.TimeEntryIdlePeriodRepository.get_by_client_event_id",
                   return_value=by_key), \
             patch(f"{SVC}.TimeEntryIdlePeriodRepository.create",
                   side_effect=lambda **kw: _idle(**{
                       k: v for k, v in kw.items() if k != "db"
                   })) as create:
            result = TimeEntryIdlePeriodService.report_idle_period(
                self.db, payload, user or _user()
            )
        return result, create

    def test_a_valid_report_opens_a_pending_period(self):
        result, create = self._report(IdlePeriodCreate(
            time_entry_id=100, idle_started_at=IDLE_START, idle_detected_at=DETECTED,
        ))
        self.assertEqual(result.status, IdlePeriodStatus.PENDING)
        create.assert_called_once()
        # Identity comes from the authenticated user and the entry, never the body.
        self.assertEqual(create.call_args.kwargs["organization_id"], 10)
        self.assertEqual(create.call_args.kwargs["user_id"], 1)
        self.assertEqual(create.call_args.kwargs["original_project_id"], 5)

    def test_duplicate_report_returns_the_existing_pending_period(self):
        existing = _idle()
        result, create = self._report(
            IdlePeriodCreate(time_entry_id=100, idle_started_at=IDLE_START,
                             idle_detected_at=DETECTED),
            pending=existing,
        )
        self.assertIs(result, existing)
        create.assert_not_called()

    def test_network_retry_with_the_same_client_event_id_is_idempotent(self):
        existing = _idle(client_event_id="evt-idle-1")
        result, create = self._report(
            IdlePeriodCreate(time_entry_id=100, idle_started_at=IDLE_START,
                             idle_detected_at=DETECTED, client_event_id="evt-idle-1"),
            by_key=existing,
        )
        self.assertIs(result, existing)
        create.assert_not_called()

    def test_another_users_event_key_cannot_be_claimed(self):
        with pytest.raises(HTTPException) as exc:
            self._report(
                IdlePeriodCreate(time_entry_id=100, idle_started_at=IDLE_START,
                                 idle_detected_at=DETECTED, client_event_id="evt-x"),
                by_key=_idle(user_id=999),
            )
        self.assertEqual(exc.value.status_code, 409)

    def test_report_against_a_stopped_entry_is_rejected(self):
        with pytest.raises(HTTPException) as exc:
            self._report(
                IdlePeriodCreate(time_entry_id=100, idle_started_at=IDLE_START,
                                 idle_detected_at=DETECTED),
                entry=_entry(end_time=RESOLVED, status="stopped"),
            )
        self.assertEqual(exc.value.status_code, 409)

    def test_idle_cannot_start_before_the_entry_it_belongs_to(self):
        early = T0 - timedelta(minutes=30)
        with pytest.raises(HTTPException) as exc:
            self._report(IdlePeriodCreate(
                time_entry_id=100, idle_started_at=early,
                idle_detected_at=early + timedelta(minutes=6),
            ))
        self.assertEqual(exc.value.status_code, 400)

    def test_future_timestamps_are_rejected(self):
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        with pytest.raises(HTTPException) as exc:
            self._report(IdlePeriodCreate(
                time_entry_id=100, idle_started_at=future,
                idle_detected_at=future + timedelta(minutes=6),
            ))
        self.assertEqual(exc.value.status_code, 400)

    def test_out_of_order_timestamps_are_rejected(self):
        with pytest.raises(HTTPException) as exc:
            self._report(IdlePeriodCreate(
                time_entry_id=100, idle_started_at=DETECTED, idle_detected_at=IDLE_START,
            ))
        self.assertEqual(exc.value.status_code, 400)


class TestRepeatedResolution(unittest.TestCase):
    def setUp(self):
        self.db = MagicMock()

    def _resolve(self, idle_period, keep, action):
        with patch(f"{SVC}.TimeEntryIdlePeriodService._owned_idle_period", return_value=idle_period), \
             patch(f"{SVC}.TimeEntryIdlePeriodService._owned_entry", return_value=_entry()), \
             patch(f"{SVC}.TimeEntryAdjustmentRepository.create_pending") as adjust, \
             patch("app.services.time_entry.TimeEntryService.stop_timer"):
            result = TimeEntryIdlePeriodService.resolve(
                self.db, 456,
                IdlePeriodResolve(keep_idle_time=keep, action=action, resolved_at=RESOLVED),
                _user(),
            )
        return result, adjust

    def test_repeating_the_same_decision_never_deducts_twice(self):
        """A double-clicked Resume must not remove the idle seconds twice."""
        resolved = _idle(
            status=IdlePeriodStatus.RESOLVED, resolved_at=RESOLVED,
            idle_duration_seconds=FULL_IDLE_SECONDS, keep_idle_time=False,
            action=IdlePeriodAction.RESUME, counted=False,
        )
        result, adjust = self._resolve(resolved, False, "resume")
        self.assertIs(result, resolved)
        adjust.assert_not_called()

    def test_a_different_decision_on_a_resolved_period_is_rejected(self):
        resolved = _idle(
            status=IdlePeriodStatus.RESOLVED, resolved_at=RESOLVED,
            idle_duration_seconds=FULL_IDLE_SECONDS, keep_idle_time=False,
            action=IdlePeriodAction.RESUME, counted=False,
        )
        with pytest.raises(HTTPException) as exc:
            self._resolve(resolved, True, "resume")
        self.assertEqual(exc.value.status_code, 409)

    def test_concurrent_resolution_takes_a_row_lock(self):
        """The second request must block on the first rather than race it."""
        db = MagicMock()
        with patch(f"{SVC}.TimeEntryIdlePeriodRepository.get_for_update",
                   return_value=_idle()) as locked:
            TimeEntryIdlePeriodService._owned_idle_period(db, 456, _user())
        locked.assert_called_once_with(db, 456)


class TestMultipleIdlePeriods(unittest.TestCase):
    """Each idle period in one timer session is handled independently, with no
    overlap and no double counting."""

    def setUp(self):
        self.db = MagicMock()

    def test_three_periods_resolve_independently(self):
        entry = _entry()
        outcomes = []
        for offset, keep, action in (
            (0, False, "resume"),   # discarded
            (1, True, "resume"),    # counted
            (2, True, "stop"),      # discarded despite "keep"
        ):
            started = IDLE_START + timedelta(hours=offset)
            period = _idle(
                id=456 + offset, idle_started_at=started,
                idle_detected_at=started + timedelta(minutes=5),
            )
            with patch(f"{SVC}.TimeEntryIdlePeriodService._owned_idle_period", return_value=period), \
                 patch(f"{SVC}.TimeEntryIdlePeriodService._owned_entry", return_value=entry), \
                 patch(f"{SVC}.TimeEntryAdjustmentRepository.create_pending") as adjust, \
                 patch("app.services.time_entry.TimeEntryService.stop_timer"):
                result = TimeEntryIdlePeriodService.resolve(
                    self.db, period.id,
                    IdlePeriodResolve(
                        keep_idle_time=keep, action=action,
                        resolved_at=started + timedelta(minutes=10),
                    ),
                    _user(),
                )
            outcomes.append((result.counted, adjust.call_count,
                             adjust.call_args.kwargs["adjustment_seconds"] if adjust.call_count else 0))

        self.assertEqual(outcomes[0], (False, 1, -FULL_IDLE_SECONDS))
        self.assertEqual(outcomes[1], (True, 0, 0))
        self.assertEqual(outcomes[2], (False, 1, -FULL_IDLE_SECONDS))


class TestStopWhileIdlePending(unittest.TestCase):
    """A direct Stop while an idle period is still pending must never bank the
    unresolved idle time."""

    def test_pending_periods_are_resolved_as_discarded(self):
        db = MagicMock()
        entry = _entry()
        pending = _idle()
        with patch(f"{SVC}.TimeEntryIdlePeriodRepository.list_pending_for_entry",
                   return_value=[pending]), \
             patch(f"{SVC}.TimeEntryAdjustmentRepository.create_pending") as adjust:
            resolved = TimeEntryIdlePeriodService.resolve_pending_for_stop(db, entry, RESOLVED)

        self.assertEqual(len(resolved), 1)
        self.assertEqual(pending.status, IdlePeriodStatus.RESOLVED)
        self.assertFalse(pending.counted)
        self.assertFalse(pending.keep_idle_time)
        self.assertEqual(pending.action, IdlePeriodAction.STOP)
        self.assertEqual(pending.idle_duration_seconds, FULL_IDLE_SECONDS)
        self.assertEqual(adjust.call_args.kwargs["adjustment_seconds"], -FULL_IDLE_SECONDS)

    def test_nothing_pending_means_nothing_is_written(self):
        db = MagicMock()
        with patch(f"{SVC}.TimeEntryIdlePeriodRepository.list_pending_for_entry",
                   return_value=[]), \
             patch(f"{SVC}.TimeEntryAdjustmentRepository.create_pending") as adjust:
            resolved = TimeEntryIdlePeriodService.resolve_pending_for_stop(db, _entry(), RESOLVED)
        self.assertEqual(resolved, [])
        adjust.assert_not_called()

    def test_the_stop_path_sweeps_pending_idle_before_ending_the_entry(self):
        """Regression guard for the ordering: if the sweep ran after the end
        time was written, unresolved idle seconds would already be banked."""
        from app.services.time_entry import TimeEntryService

        db = MagicMock()
        entry = _entry()
        calls = []
        with patch("app.repositories.time_entry.TimeEntryRepository.get_by_id", return_value=entry), \
             patch("app.repositories.time_entry.TimeEntryRepository.stop",
                   side_effect=lambda **kw: calls.append("stop") or entry), \
             patch(f"{SVC}.TimeEntryIdlePeriodService.resolve_pending_for_stop",
                   side_effect=lambda *a, **kw: calls.append("sweep") or []):
            db.scalar.return_value = 0
            TimeEntryService.stop_timer(
                db=db, entry_id=100, description=None,
                current_user=_user(), stopped_at=RESOLVED,
            )
        self.assertEqual(calls[:2], ["sweep", "stop"])


# ----------------------------------------------------------------------
# 30. Ownership / cross-user isolation
# ----------------------------------------------------------------------

class TestAuthorization(unittest.TestCase):
    def test_another_organizations_idle_period_is_not_found(self):
        db = MagicMock()
        with patch(f"{SVC}.TimeEntryIdlePeriodRepository.get_for_update",
                   return_value=_idle(organization_id=99)):
            with pytest.raises(HTTPException) as exc:
                TimeEntryIdlePeriodService._owned_idle_period(db, 456, _user())
        self.assertEqual(exc.value.status_code, 404)

    def test_another_users_idle_period_in_the_same_org_is_forbidden(self):
        db = MagicMock()
        with patch(f"{SVC}.TimeEntryIdlePeriodRepository.get_for_update",
                   return_value=_idle(user_id=2)):
            with pytest.raises(HTTPException) as exc:
                TimeEntryIdlePeriodService._owned_idle_period(db, 456, _user())
        self.assertEqual(exc.value.status_code, 403)

    def test_another_users_time_entry_is_forbidden(self):
        db = MagicMock()
        with patch(f"{SVC}.TimeEntryRepository.get_by_id", return_value=_entry(user_id=2)):
            with pytest.raises(HTTPException) as exc:
                TimeEntryIdlePeriodService._owned_entry(db, 100, _user())
        self.assertEqual(exc.value.status_code, 403)

    def test_another_organizations_time_entry_is_not_found(self):
        db = MagicMock()
        with patch(f"{SVC}.TimeEntryRepository.get_by_id",
                   return_value=_entry(organization_id=99)):
            with pytest.raises(HTTPException) as exc:
                TimeEntryIdlePeriodService._owned_entry(db, 100, _user())
        self.assertEqual(exc.value.status_code, 404)

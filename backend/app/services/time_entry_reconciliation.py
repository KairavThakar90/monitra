"""
Closing a running entry whose desktop has stopped reporting in.

The policy is `app.core.timer_policy` and docs/TIMING_MODEL.md section 9; this
module is the mechanism. It does three things and nothing else:

* **Decide** (`decide`) -- a pure function of an entry, its pending idle
  period, `now` and the thresholds. No database, no clock, so every rule is
  testable with a plain object and the dry run and the live run cannot
  disagree about what they would do.
* **Close** (`close`) -- one entry, in its own transaction, under a row lock
  that is *skipped* rather than waited for, re-deciding on the locked row so a
  heartbeat that landed a millisecond earlier wins. It writes the audit row and
  ends the entry through `TimeEntryService.finalize_entry`: the same duration
  rule, idle sweep, rollup, activity trail and WFPM stop as the user's own Stop.
* **Run** (`run`) -- every running entry, one by one; report-only unless told
  to apply, bounded in batch size, safe to repeat and safe to run from two
  places at once (a row somebody else holds is not ours this pass).

What it never does: delete or edit a stopped entry, close an entry that has
never reported liveness (silence from a client that never speaks proves
nothing -- `include_unreported` is an explicit, per-entry operator decision),
or reopen anything.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.timer_policy import (
    REASON_IDLE_UNANSWERED, REASON_NO_SIGNAL, REASON_OPERATOR, REASON_REPLACED_BY_START,
    STALE, TimerPolicy, effective_end, liveness,
)
from app.models.time_entry import TimeEntry
from app.models.time_entry_idle_period import TimeEntryIdlePeriod
from app.models.user import User
from app.core.time_format import elapsed_seconds
from app.repositories.time_entry_idle_period import TimeEntryIdlePeriodRepository
from app.repositories.time_entry_liveness import (
    TimeEntryLivenessRepository, TimeEntryReconciliationRepository,
)

log = logging.getLogger(__name__)
#: The same logger the start/stop lifecycle uses, so one grep follows an entry
#: from its start to the server closing it.
timing_log = logging.getLogger("app.timing")


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


#: How long a closure waits for a row somebody else holds (a user's Stop, an idle answer).
LOCK_WAIT_MILLISECONDS = 5000


def _bound_lock_wait(db: Session) -> None:
    """Fail a closure that cannot take its locks in a few seconds, rather than queue
    behind a stuck transaction (the scheduler runs every five minutes and tries again).
    PostgreSQL only -- `SET LOCAL` ends with the transaction, so nothing leaks into the
    session's next use."""
    bind = db.get_bind() if hasattr(db, "get_bind") else None
    if bind is not None and getattr(bind.dialect, "name", None) == "postgresql":
        db.execute(text(f"SET LOCAL lock_timeout = {int(LOCK_WAIT_MILLISECONDS)}"))


@dataclass(frozen=True)
class Decision:
    """What would be done to one entry, and on what evidence."""

    reason: str
    end_time: datetime
    #: The instant the evidence ran out (last signal, or the idle prompt).
    evidence_at: Optional[datetime]
    silent_for_seconds: Optional[int]

    def total_seconds(self, entry: TimeEntry) -> int:
        return max(0, elapsed_seconds(entry.start_time, self.end_time))


def decide(
    entry: TimeEntry,
    pending_idle: Optional[TimeEntryIdlePeriod],
    now: datetime,
    policy: TimerPolicy,
    *,
    include_unreported: bool = False,
    evidence_at: Optional[datetime] = None,
) -> Optional[Decision]:
    """Whether `entry` should be closed now, and where.

    1. Never an entry that has ended.
    2. **No signal.** The entry's desktop has been silent for longer than
       `no_signal_stop_after`: close at `last signal + grace` -- the last
       instant the evidence supports plus the same quarter hour the desktop
       itself allows after a crash.
    3. **Unanswered idle prompt.** The desktop is alive but its person is not:
       a prompt unanswered for longer than `idle_unanswered_stop_after` closes
       the entry at the instant the prompt was raised, which is what the owner
       pressing Stop on the unanswered prompt would produce (the idle stretch
       is discarded by the stop path's own idle sweep).
    4. An entry that has never reported (`last_seen_at IS NULL`) has no
       evidence either way and is left alone -- unless an operator names it
       (`include_unreported`), in which case `evidence_at` (the newest upload
       that mentions it, or its start) stands in for the last signal.

    The closing instant is never before the entry's start, never in the future,
    and never before the idle prompt's detection instant when one is pending
    (resolving a prompt cannot move backwards past its own detection).
    """
    if entry.end_time is not None:
        return None
    start = _aware(entry.start_time)
    seen = _aware(entry.last_seen_at) if entry.last_seen_at is not None else None

    if seen is None:
        if not include_unreported:
            return None
        seen = _aware(evidence_at) if evidence_at is not None else start
        silent = now - seen
        if silent <= policy.no_signal_stop_after:
            return None
        end = max(start, min(now, seen + policy.no_signal_grace))
        return Decision(REASON_OPERATOR, _floor_to_prompt(end, pending_idle, now),
                        seen, int(silent.total_seconds()))

    silent = now - seen
    if silent > policy.no_signal_stop_after:
        end = effective_end(start, None, seen, now, policy)
        return Decision(REASON_NO_SIGNAL, _floor_to_prompt(end, pending_idle, now),
                        seen, int(silent.total_seconds()))

    if pending_idle is not None:
        detected = _aware(pending_idle.idle_detected_at)
        if now - detected > policy.idle_unanswered_stop_after:
            return Decision(REASON_IDLE_UNANSWERED, max(start, min(now, detected)),
                            detected, int(silent.total_seconds()))
    return None


def _floor_to_prompt(end: datetime, pending_idle: Optional[TimeEntryIdlePeriod], now: datetime) -> datetime:
    if pending_idle is None:
        return end
    return max(end, min(now, _aware(pending_idle.idle_detected_at)))


class TimeEntryReconciliationService:
    """See the module docstring."""

    @staticmethod
    def close(
        db: Session,
        entry_id: int,
        *,
        reason: str,
        decided_by: str,
        now: Optional[datetime] = None,
        policy: Optional[TimerPolicy] = None,
        include_unreported: bool = False,
        not_after: Optional[datetime] = None,
        request_id: Optional[str] = None,
    ) -> Optional[TimeEntry]:
        """Close one entry if, and only if, the rules still say to.

        Returns the closed entry, or None when there was nothing to do: the
        entry has ended, is held by someone else, or turned out on the locked
        read not to qualify (a heartbeat or an answer arrived first).

        `not_after` caps the closing instant -- a start that replaces a silent
        entry passes its own start, so the two never overlap.
        """
        from app.services.time_entry import TimeEntryService

        now = now or TimeEntryLivenessRepository.db_now(db)
        policy = policy or TimerPolicy.current()

        # Lock order: idle-period rows, then the entry. The user's own Stop takes them
        # in that order (`resolve_pending_for_stop`, then the compare-and-set on the
        # entry) and so does answering an idle prompt. Taking the entry first made a
        # late Stop racing the reconcile of an entry with a pending prompt a deadlock:
        # each held what the other wanted. The wait is bounded either way, so a stuck
        # transaction costs one failed (and retried) closure, not a stuck run.
        _bound_lock_wait(db)
        TimeEntryIdlePeriodRepository.list_pending_for_entry(db, entry_id)
        entry = TimeEntryLivenessRepository.lock_running(db, entry_id)
        if entry is None:
            db.rollback()
            return None

        # Read again now that both are held: a prompt may have opened or been
        # answered between the two locks.
        pending = TimeEntryIdlePeriodRepository.get_pending_for_entry(db, entry.id)
        evidence = None
        if include_unreported and entry.last_seen_at is None:
            evidence = TimeEntryLivenessRepository.last_evidence_at(db, entry.id)

        if reason == REASON_REPLACED_BY_START:
            # The new session's own start is what proves the old one is over,
            # but only for an entry that has demonstrably gone quiet. Judged on
            # the locked row, never on what the caller saw a moment ago.
            if liveness(entry.end_time, entry.last_seen_at, now, policy) != STALE:
                db.rollback()
                return None
            seen = _aware(entry.last_seen_at)
            decision = Decision(
                REASON_REPLACED_BY_START,
                _floor_to_prompt(effective_end(entry.start_time, None, seen, now, policy), pending, now),
                seen, int((now - seen).total_seconds()),
            )
        else:
            decision = decide(entry, pending, now, policy,
                              include_unreported=include_unreported, evidence_at=evidence)
            if decision is None:
                db.rollback()
                return None
            if reason == REASON_OPERATOR:
                decision = Decision(REASON_OPERATOR, decision.end_time,
                                    decision.evidence_at, decision.silent_for_seconds)
            elif decision.reason != reason:
                # The caller expected one rule and the locked row says another;
                # the decision on the row is the true one.
                reason = decision.reason

        end_time = decision.end_time
        if not_after is not None:
            end_time = max(_aware(entry.start_time), min(end_time, _aware(not_after)))

        owner = db.get(User, entry.user_id)
        total = max(0, elapsed_seconds(entry.start_time, end_time))
        TimeEntryReconciliationRepository.create(
            db,
            organization_id=entry.organization_id,
            time_entry_id=entry.id,
            user_id=entry.user_id,
            reason=decision.reason,
            decided_by=decided_by,
            entry_start_time=entry.start_time,
            last_seen_at=entry.last_seen_at,
            evidence_at=decision.evidence_at if entry.last_seen_at is None else None,
            silent_for_seconds=decision.silent_for_seconds,
            ended_at=end_time,
            total_seconds=total,
            idle_period_id=pending.id if pending is not None else None,
            policy=policy.as_dict(),
        )
        closed, finalized = TimeEntryService.finalize_entry(
            db, entry, end_time, None, owner or _OwnerStub(entry),
            now=now, request_id=request_id,
            note=f"reconciled reason={decision.reason} decided_by={decided_by}"
                 f" silent_for={decision.silent_for_seconds}",
            activity_description=_activity_text(decision, entry, end_time),
            activity_at=now,
        )
        if not finalized:
            # Another stop won between the lock and the compare-and-set. Its
            # instants stand; the audit row was rolled back with the attempt.
            return None
        timing_log.info(
            "event=reconcile.closed entry=%s user=%s reason=%s decided_by=%s "
            "last_seen_at=%s silent_for=%s start_time=%s end_time=%s total_seconds=%s idle_period=%s",
            closed.id, closed.user_id, decision.reason, decided_by,
            entry.last_seen_at.isoformat() if entry.last_seen_at else None,
            decision.silent_for_seconds, closed.start_time.isoformat(),
            closed.end_time.isoformat() if closed.end_time else None,
            closed.total_seconds, pending.id if pending is not None else None,
        )
        return closed

    @staticmethod
    def close_if_replaced_by_start(
        db: Session, active: TimeEntry, new_start: datetime, *,
        now: Optional[datetime] = None, request_id: Optional[str] = None,
    ) -> bool:
        """A start arrived while `active` is running. If `active` has gone
        quiet it is over -- close it, so the person who just pressed Start on
        a working machine is not handed a Friday timer as their session.

        Returns whether it was closed. A live entry (or one that never
        reported) is never touched: the start is refused with the 409 it
        always got.
        """
        policy = TimerPolicy.current()
        now = now or datetime.now(timezone.utc)
        if liveness(active.end_time, active.last_seen_at, now, policy) != STALE:
            return False
        closed = TimeEntryReconciliationService.close(
            db, active.id, reason=REASON_REPLACED_BY_START, decided_by="system:start",
            now=now, policy=policy, not_after=new_start, request_id=request_id,
        )
        return closed is not None

    @staticmethod
    def run(
        db: Session,
        *,
        apply: bool,
        now: Optional[datetime] = None,
        limit: Optional[int] = None,
        user_id: Optional[int] = None,
        entry_ids: Optional[Sequence[int]] = None,
        include_unreported: bool = False,
        decided_by: str = "system:reconciler",
    ) -> Dict[str, Any]:
        """Look at every running entry and, if `apply`, close those that
        qualify. The report is the same either way.

        `entry_ids` restricts the pass to those entries -- the shape of an
        operator-approved correction. `include_unreported` is honoured only
        together with `entry_ids`: the scheduled run can never reach an entry
        that has no evidence of life, however it is configured.
        """
        policy = TimerPolicy.current()
        now = now or TimeEntryLivenessRepository.db_now(db)
        limit = limit or settings.TIMER_RECONCILER_BATCH_SIZE
        if include_unreported and not entry_ids:
            raise ValueError("include_unreported requires explicit entry_ids")

        entries = TimeEntryLivenessRepository.list_running(db, 10_000, user_id=user_id)
        if entry_ids:
            wanted = {int(i) for i in entry_ids}
            entries = [e for e in entries if e.id in wanted]

        report: Dict[str, Any] = {
            "dry_run": not apply,
            "examined": len(entries),
            "candidates": 0,
            "closed": 0,
            "skipped": 0,
            "failed": 0,
            "unreported_running": 0,
            "policy": policy.as_dict(),
            "items": [],
        }
        for entry in entries:
            if entry.last_seen_at is None:
                report["unreported_running"] += 1
            pending = TimeEntryIdlePeriodRepository.get_pending_for_entry(db, entry.id)
            evidence = None
            if include_unreported and entry.last_seen_at is None:
                evidence = TimeEntryLivenessRepository.last_evidence_at(db, entry.id)
            decision = decide(entry, pending, now, policy,
                              include_unreported=include_unreported, evidence_at=evidence)
            if decision is None:
                continue
            report["candidates"] += 1
            item = {
                "time_entry_id": entry.id,
                "user_id": entry.user_id,
                "reason": decision.reason,
                "start_time": _aware(entry.start_time).isoformat(),
                "last_seen_at": _aware(entry.last_seen_at).isoformat() if entry.last_seen_at else None,
                "evidence_at": decision.evidence_at.isoformat() if decision.evidence_at else None,
                "silent_for_seconds": decision.silent_for_seconds,
                "would_end_at": decision.end_time.isoformat(),
                "would_total_seconds": decision.total_seconds(entry),
                "currently_measured_seconds": max(0, elapsed_seconds(entry.start_time, now)),
                "pending_idle_period_id": pending.id if pending is not None else None,
                "applied": False,
            }
            if apply and report["closed"] < limit:
                try:
                    closed = TimeEntryReconciliationService.close(
                        db, entry.id, reason=decision.reason, decided_by=decided_by,
                        now=now, policy=policy, include_unreported=include_unreported,
                    )
                except Exception:  # noqa: BLE001 -- one entry must not stop the pass
                    db.rollback()
                    log.exception("RECONCILE_FAILED entry=%s user=%s", entry.id, entry.user_id)
                    report["failed"] += 1
                else:
                    if closed is None:
                        report["skipped"] += 1
                    else:
                        report["closed"] += 1
                        item["applied"] = True
            if len(report["items"]) < 200:
                report["items"].append(item)
        log.info(
            "RECONCILE_RUN dry_run=%s examined=%d candidates=%d closed=%d skipped=%d failed=%d "
            "unreported_running=%d",
            report["dry_run"], report["examined"], report["candidates"], report["closed"],
            report["skipped"], report["failed"], report["unreported_running"],
        )
        return report


class _OwnerStub:
    """Stands in for an owner row that has been deleted out from under an
    entry (the cascade would have taken the entry too; this is belt and braces
    so a closure never fails on the trail's actor)."""

    def __init__(self, entry: TimeEntry) -> None:
        self.id = entry.user_id
        self.organization_id = entry.organization_id
        self.name = None


_REASON_TEXT = {
    REASON_NO_SIGNAL: "Monitra stopped reporting",
    REASON_IDLE_UNANSWERED: "the idle prompt was never answered",
    REASON_REPLACED_BY_START: "a new timer was started",
    REASON_OPERATOR: "an administrator corrected a timer that was never closed",
}


def _activity_text(decision: Decision, entry: TimeEntry, end_time: datetime) -> str:
    from app.services.activity_log import ActivityLogService

    return (
        "Timer stopped automatically ("
        + _REASON_TEXT.get(decision.reason, decision.reason)
        + ") at " + _aware(end_time).isoformat(timespec="seconds")
        + f" after {ActivityLogService.format_duration(max(0, elapsed_seconds(entry.start_time, end_time)))}"
    )

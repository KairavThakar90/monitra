"""
The desktop telling the server it is alive.

Two ways in, both ending in one conditional UPDATE of
`time_entries.last_seen_at` (`TimeEntryLivenessRepository.touch`):

* **The heartbeat** (`POST /time-entries/{id}/heartbeat`) -- the desktop's
  explicit "still here", sent every couple of minutes while a timer runs. It
  is also how a desktop learns the server has closed its entry: the answer
  carries the entry and, when the server ended it, the reason.
* **Any upload that names the entry** -- an activity window, an
  application-usage batch, a screenshot, an idle report. They prove the same
  thing, so they count, and that is what makes the policy work for desktops
  that predate the heartbeat: they upload activity every minute they run.

`touch` never fails the request it rides on.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.core.timer_policy import LIVE, STALE, TimerPolicy, liveness
from app.models.time_entry import TimeEntry
from app.models.user import User
from app.repositories.time_entry import TimeEntryRepository
from app.repositories.time_entry_liveness import (
    TimeEntryLivenessRepository, TimeEntryReconciliationRepository,
)

log = logging.getLogger(__name__)
timing_log = logging.getLogger("app.timing")


class TimeEntryLivenessService:
    @staticmethod
    def heartbeat(
        db: Session, entry_id: int, current_user: User, request_id: Optional[str] = None,
    ) -> dict:
        """Record a heartbeat for the caller's entry; describe where it stands.

        Returns `{"entry", "closed_by_server", "closed_reason", "server_time"}`.
        An entry that has already ended answers with itself (and, when the
        server ended it, why) rather than an error: the desktop's reply to
        that is to end its own session, and a 404 would read as "retry".
        """
        entry = TimeEntryRepository.get_by_id(db, entry_id)
        # 404, not 403, for somebody else's entry -- the same rule stop and
        # transfer follow: whether it exists is not this caller's to learn.
        if not entry or entry.user_id != current_user.id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Active timer not found")

        if entry.end_time is None:
            policy = TimerPolicy.current()
            before = liveness(entry.end_time, entry.last_seen_at, datetime.now(timezone.utc), policy)
            touched = TimeEntryLivenessRepository.touch(db, entry.id, current_user.id)
            db.commit()
            if touched:
                db.refresh(entry)
                if before != LIVE:
                    # Only the edges are logged: the first heartbeat of an
                    # entry, and one that ends a silence. A line per beat
                    # would be a log of nothing.
                    timing_log.info(
                        "event=heartbeat.%s user=%s entry=%s request_id=%s last_seen_at=%s",
                        "resumed" if before == STALE else "first", current_user.id, entry.id,
                        request_id, entry.last_seen_at.isoformat() if entry.last_seen_at else None,
                    )
            else:
                # Ended between the read and the update (the reconciler, or a
                # stop from another client). Answer with what is there now.
                db.expire_all()
                entry = TimeEntryRepository.get_by_id(db, entry_id)

        closed_by_server = False
        closed_reason = None
        if entry.end_time is not None:
            row = TimeEntryReconciliationRepository.get_for_entry(db, entry.id)
            if row is not None:
                closed_by_server, closed_reason = True, row.reason
        return {
            "entry": entry,
            "closed_by_server": closed_by_server,
            "closed_reason": closed_reason,
            "server_time": datetime.now(timezone.utc),
        }

    @staticmethod
    def touch(db: Session, entry_id: Optional[int], user_id: int) -> None:
        """Count an upload as proof of life. Best effort, never raises.

        Called after the upload has succeeded and committed: a failure here
        must not fail (and so trigger the retry of) a request whose real work
        is already done. Silent about stand-in databases, like the activity
        trail's `capture`, so code that runs against one is not made to
        answer extra queries.
        """
        if entry_id is None or not isinstance(db, Session):
            return
        try:
            if TimeEntryLivenessRepository.touch(db, int(entry_id), user_id):
                db.commit()
            else:
                db.rollback()
        except Exception:  # noqa: BLE001
            db.rollback()
            log.warning("LIVENESS_TOUCH_FAILED entry=%s user=%s", entry_id, user_id, exc_info=True)

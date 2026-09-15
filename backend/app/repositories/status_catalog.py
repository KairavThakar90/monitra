"""The `project_statuses` and `task_statuses` reference tables, read once.

These two tables are seeded by migration `b4f7c2d9e1a6` and are not written by
any route: nothing in the application creates, renames or deletes a status row.
Every project-shaped response nevertheless re-read both of them, and against a
managed Postgres each of those reads is a network round trip -- measured at
~85 ms apiece on the development database, for eight rows that had not changed
since deployment. `GET /api/v1/projects` spent roughly a quarter of its time
fetching them, and `/project-management/metadata` was nothing else.

So they are cached in the process, for `TTL_SECONDS`, behind this one accessor.
The TTL rather than a permanent cache is deliberate: the rows *can* change --
a migration or a hand-run seed script -- and a process that would serve a stale
palette until it is restarted is worse than one that re-reads twice a minute.

Rows are copied into `StatusRow`, a plain frozen value, rather than cached as
ORM instances. A cached `ProjectStatus` would stay bound to the `Session` that
loaded it; once that session closed, the first attribute read from another
request would raise `DetachedInstanceError`. `StatusRow` carries `id`, `name`
and `color`, which is every column the table has and everything `StatusRead`
serialises, so callers cannot tell the difference.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.project_status import ProjectStatus, TaskStatus

#: How long a snapshot is served before the table is read again.
TTL_SECONDS = 300.0


@dataclass(frozen=True)
class StatusRow:
    """One status, detached from any session.

    Field-for-field what `StatusRead` needs, so it validates through
    `from_attributes` exactly as the ORM row it replaces did.
    """

    id: int
    name: str
    color: str


class _Snapshot:
    __slots__ = ("rows", "loaded_at")

    def __init__(self, rows: dict[int, StatusRow], loaded_at: float) -> None:
        self.rows = rows
        self.loaded_at = loaded_at


class StatusCatalog:
    """Cached access to the two status reference tables.

    Every method returns `{id: StatusRow}` in table order. The cache is shared
    by the whole process and guarded by one lock; a race between two requests
    can only cause the table to be read twice, never to be read inconsistently.
    """

    _lock = threading.Lock()
    _snapshots: dict[str, _Snapshot] = {}

    @classmethod
    def project_statuses(cls, db: Session) -> dict[int, StatusRow]:
        return cls._get(db, "project", ProjectStatus)

    @classmethod
    def task_statuses(cls, db: Session) -> dict[int, StatusRow]:
        return cls._get(db, "task", TaskStatus)

    @classmethod
    def project_status(cls, db: Session, status_id: int) -> Optional[StatusRow]:
        return cls.project_statuses(db).get(status_id)

    @classmethod
    def task_status(cls, db: Session, status_id: int) -> Optional[StatusRow]:
        return cls.task_statuses(db).get(status_id)

    @classmethod
    def invalidate(cls) -> None:
        """Drop the cache. For tests, and for a script that reseeds the tables."""
        with cls._lock:
            cls._snapshots.clear()

    @classmethod
    def _get(cls, db: Session, key: str, model) -> dict[int, StatusRow]:
        now = time.monotonic()
        with cls._lock:
            snapshot = cls._snapshots.get(key)
            if snapshot is not None and now - snapshot.loaded_at < TTL_SECONDS:
                return snapshot.rows

        rows = {
            item.id: StatusRow(id=item.id, name=item.name, color=item.color)
            for item in db.scalars(select(model).order_by(model.id)).all()
        }

        # An empty result means the table has not been seeded -- a deployment
        # problem, not a cacheable answer. Caching it would make every request
        # for the next five minutes fail in the same confusing way (project
        # creation answers "Todo task status is not configured") long after the
        # seed had been applied. Re-read instead.
        if not rows:
            return rows

        with cls._lock:
            cls._snapshots[key] = _Snapshot(rows, now)
        return rows

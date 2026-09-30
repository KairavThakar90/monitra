"""The activity trail: one row in ``activity_logs`` per user action.

Every service that performs something a person did -- signing in, starting
the timer, creating a task, approving a request, switching a member's login
off -- records it through ``ActivityLogService.capture`` (or ``record``). The rules that keep
the trail trustworthy:

* **A log failure never fails the action.** The action is the product; the
  row is a record of it. ``capture`` therefore never raises: a row it could
  not write is logged with a traceback and the caller's response is
  unaffected.
* **Recorded after the change, in a session of its own.** Every hook runs
  once the change it describes has been committed, and the row is written and
  committed through a separate short-lived session on the same database. The
  caller's session is never added to, committed or rolled back on the trail's
  account, so a failed audit write cannot disturb the work it was recording.
* **The client is recorded, never trusted.** Which client acted (desktop or
  web) and from which address come from the request context the middleware
  captured, not from anything in a payload.
* **The actor is the row's owner.** ``user_id`` is who did it; the person it
  was done *to* is named in the description. That is what the employee-wise
  reading of the trail groups on.

Reads are scoped exactly as every other people-reading surface: a leader sees
their own team, everyone else with ``view_employees`` sees the organization
(``member_scope.visible_member_ids``).
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.request_context import current_request_context
from app.core.time_format import ist_day_start_utc, ist_today
from app.models.activity_log import ActivityLog, ActivityLogAction, ActivityLogModule
from app.models.project import Project
from app.models.task import Task
from app.models.user import User
from app.repositories.activity_log import ActivityLogRepository
from app.services.member_scope import visible_member_ids

logger = logging.getLogger(__name__)

#: The widest window one read may ask for. The trail is dense -- every timer
#: start is a row -- so a year is already a very large answer.
MAX_RANGE_DAYS = 366
#: The most rows one read returns. Past this the response says it was cut,
#: rather than growing without bound.
MAX_ROWS = 5000
#: A client-reported instant further ahead of the server than this is a
#: wrong clock, not a future event; it is recorded as "now" instead.
MAX_CLIENT_CLOCK_LEAD = timedelta(minutes=5)

#: The description suffix that records which client acted. Kept as one
#: convention, written and read only here, so a reader never parses it.
_SOURCE_SEPARATOR = " · via "


def _as_utc(value: Optional[datetime]) -> Optional[datetime]:
    """An aware UTC instant. The column is `timestamptz`, but a naive value --
    which a browser would read as its own local time -- must never leave here."""
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


def _format_duration(seconds: Optional[int]) -> str:
    total = max(0, int(seconds or 0))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


class ActivityLogService:

    # ── Writes ────────────────────────────────────────────────────────────

    @staticmethod
    def capture(db: Session, build: Callable[[], Optional[Dict[str, Any]]]) -> Optional[int]:
        """Record the action ``build`` describes. Never raises.

        ``build`` returns the fields of one row -- ``actor``, ``module``,
        ``action``, ``description`` and optionally ``project_id``, ``task_id``,
        ``entity_id``, ``created_at`` -- or None for "nothing to record". It is
        called *inside* the protection, because describing an action usually
        reads a name or an id off the thing that was just changed, and a
        failure there must be swallowed exactly like a failure to write.

        :return: the new row's id, or None when nothing was recorded.
        """
        # The trail is written through a real database session and nothing
        # else; a caller holding anything other than one has no database to
        # record into.
        if not isinstance(db, Session):
            return None
        try:
            fields = build()
            if not fields or fields.get("actor") is None:
                return None
            return ActivityLogService._write(db, **fields)
        except Exception:  # noqa: BLE001 - the action stands; only its record is lost
            logger.exception("ACTIVITY_LOG_WRITE_FAILED")
            return None

    @staticmethod
    def record(
        db: Session,
        actor: User,
        *,
        module: str,
        action: str,
        description: str,
        project_id: Optional[int] = None,
        task_id: Optional[int] = None,
        entity_id: Optional[int] = None,
        created_at: Optional[datetime] = None,
    ) -> Optional[int]:
        """Record one action by ``actor`` from values already in hand. Never raises."""
        return ActivityLogService.capture(db, lambda: {
            "actor": actor, "module": module, "action": action, "description": description,
            "project_id": project_id, "task_id": task_id, "entity_id": entity_id,
            "created_at": created_at,
        })

    @staticmethod
    def _write(
        db: Session,
        *,
        actor: User,
        module: str,
        action: str,
        description: str,
        project_id: Optional[int] = None,
        task_id: Optional[int] = None,
        entity_id: Optional[int] = None,
        created_at: Optional[datetime] = None,
    ) -> int:
        context = current_request_context()
        source = ActivityLogService._source_label(
            context.client if context else None,
            context.client_version if context else None,
        )
        # A session of its own, on the caller's database. The caller's session
        # has just committed the change being recorded and is about to be read
        # for the response; committing the audit row through it would expire
        # everything it holds and make the row's fate part of the caller's
        # transaction. Here the two are independent in both directions.
        with Session(bind=db.get_bind()) as log_db:
            row = ActivityLog(
                organization_id=actor.organization_id,
                user_id=actor.id,
                project_id=project_id,
                task_id=task_id,
                module=module,
                action=action,
                entity_id=entity_id,
                description=f"{description}{_SOURCE_SEPARATOR}{source}" if source else description,
                ip_address=context.ip_address if context else None,
                created_at=created_at or datetime.now(timezone.utc),
            )
            ActivityLogRepository.add(log_db, row)
            log_db.flush()
            row_id = row.id
            log_db.commit()
            return row_id

    @staticmethod
    def record_client_event(
        db: Session, actor: User, *, event: str, occurred_at: datetime
    ) -> Dict[str, Any]:
        """Record an event the desktop reports about itself (opened, closed).

        Idempotent on (actor, event, instant): the desktop delivers these
        through its durable queue, which retries until the backend answers,
        so the same report may arrive more than once.
        """
        if event not in ActivityLogAction.CLIENT_EVENTS:
            raise HTTPException(422, f"Unknown client event '{event}'.")
        if occurred_at.tzinfo is None:
            occurred_at = occurred_at.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        instant = occurred_at.astimezone(timezone.utc)
        if instant > now + MAX_CLIENT_CLOCK_LEAD:
            instant = now

        existing = ActivityLogRepository.find_event(
            db,
            organization_id=actor.organization_id,
            user_id=actor.id,
            module=ActivityLogModule.DESKTOP,
            action=event,
            created_at=instant,
        )
        if existing is not None:
            return {"recorded": False, "id": existing.id}

        description = {
            ActivityLogAction.APP_OPENED: "Opened the Monitra desktop application",
            ActivityLogAction.APP_CLOSED: "Closed the Monitra desktop application",
        }[event]
        row_id = ActivityLogService.record(
            db, actor,
            module=ActivityLogModule.DESKTOP, action=event,
            description=description, created_at=instant,
        )
        if row_id is None:
            # Unlike every other caller, recording *is* this request's whole
            # job, so a failure is reported: the desktop's queue retries it.
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "The event could not be recorded.")
        return {"recorded": True, "id": row_id}

    # ── Description helpers ───────────────────────────────────────────────

    @staticmethod
    def describe_project_task(db: Session, project_id: Optional[int], task_id: Optional[int]) -> str:
        """``Project › Task`` by name, for a description. Falls back to ids."""
        parts: List[str] = []
        if project_id is not None:
            project = db.get(Project, project_id)
            parts.append(project.project_name if project else f"project #{project_id}")
        if task_id is not None:
            task = db.get(Task, task_id)
            parts.append(task.task_name if task else f"task #{task_id}")
        return " › ".join(parts)

    @staticmethod
    def format_duration(seconds: Optional[int]) -> str:
        return _format_duration(seconds)

    @staticmethod
    def _source_label(client: Optional[str], version: Optional[str]) -> Optional[str]:
        if not client:
            return None
        return f"{client} {version}" if version else client

    @staticmethod
    def split_source(description: Optional[str]) -> tuple[Optional[str], Optional[str], Optional[str]]:
        """``(text, client, client_version)`` from a stored description."""
        if not description:
            return description, None, None
        text, separator, source = description.rpartition(_SOURCE_SEPARATOR)
        if not separator:
            return description, None, None
        client, _, version = source.strip().partition(" ")
        return text, client or None, version or None

    # ── Reads ─────────────────────────────────────────────────────────────

    @staticmethod
    def list_grouped(
        db: Session,
        current_user: User,
        *,
        start_day: Optional[date] = None,
        end_day: Optional[date] = None,
        member_id: Optional[int] = None,
        module: Optional[str] = None,
        search: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Every row the caller may read in the window, grouped by employee."""
        today = ist_today()
        end_day = end_day or today
        start_day = start_day or (end_day - timedelta(days=6))
        if start_day > end_day:
            raise HTTPException(422, "`start` must not be after `end`.")
        if (end_day - start_day).days + 1 > MAX_RANGE_DAYS:
            raise HTTPException(
                422, f"The range may cover at most {MAX_RANGE_DAYS} days."
            )
        if module and module not in ActivityLogModule.ALL:
            raise HTTPException(422, f"Unknown module '{module}'.")

        scope = visible_member_ids(db, current_user)
        if member_id is not None and scope is not None and member_id not in scope:
            # Outside the caller's team: answered as an empty trail, the same
            # way the member directory answers with 404 -- nothing is learned.
            return ActivityLogService._payload(start_day, end_day, [], {}, {}, {}, truncated=False)

        rows = ActivityLogRepository.list_in_range(
            db,
            organization_id=current_user.organization_id,
            start=ist_day_start_utc(start_day),
            end=ist_day_start_utc(end_day + timedelta(days=1)),
            user_ids=scope,
            user_id=member_id,
            module=module,
            search=search,
            limit=MAX_ROWS + 1,
        )
        truncated = len(rows) > MAX_ROWS
        rows = rows[:MAX_ROWS]

        users = ActivityLogService._users_by_id(db, current_user.organization_id, {row.user_id for row in rows})
        projects = ActivityLogService._names(db, Project, Project.project_name, {row.project_id for row in rows if row.project_id})
        tasks = ActivityLogService._names(db, Task, Task.task_name, {row.task_id for row in rows if row.task_id})
        return ActivityLogService._payload(start_day, end_day, rows, users, projects, tasks, truncated=truncated)

    @staticmethod
    def _users_by_id(db: Session, organization_id: int, ids: Iterable[int]) -> Dict[int, User]:
        ids = list(ids)
        if not ids:
            return {}
        rows = db.scalars(select(User).where(User.id.in_(ids), User.organization_id == organization_id)).all()
        return {user.id: user for user in rows}

    @staticmethod
    def _names(db: Session, model, column, ids: Iterable[int]) -> Dict[int, str]:
        ids = list(ids)
        if not ids:
            return {}
        return {row[0]: row[1] for row in db.execute(select(model.id, column).where(model.id.in_(ids))).all()}

    @staticmethod
    def _payload(
        start_day: date, end_day: date, rows: List[ActivityLog],
        users: Dict[int, User], projects: Dict[int, str], tasks: Dict[int, str], *, truncated: bool,
    ) -> Dict[str, Any]:
        groups: Dict[int, Dict[str, Any]] = {}
        for row in rows:
            text, client, version = ActivityLogService.split_source(row.description)
            entry = {
                "id": row.id,
                "module": row.module,
                "action": row.action,
                "description": text,
                "source": client,
                "client_version": version,
                "project_id": row.project_id,
                "project_name": projects.get(row.project_id) if row.project_id else None,
                "task_id": row.task_id,
                "task_name": tasks.get(row.task_id) if row.task_id else None,
                "entity_id": row.entity_id,
                "ip_address": row.ip_address,
                "created_at": _as_utc(row.created_at),
            }
            group = groups.get(row.user_id)
            if group is None:
                user = users.get(row.user_id)
                group = groups[row.user_id] = {
                    "user_id": row.user_id,
                    # A row outlives its account by design; say so rather
                    # than invent a name.
                    "name": user.name if user else f"Former member #{row.user_id}",
                    "email": user.email if user else None,
                    "designation": user.designation if user else None,
                    "role_name": user.role_name if user else None,
                    "entry_count": 0,
                    "last_activity_at": _as_utc(row.created_at),
                    "entries": [],
                }
            group["entries"].append(entry)
            group["entry_count"] += 1
        members = sorted(groups.values(), key=lambda group: (group["name"] or "").lower())
        return {
            "start_date": start_day,
            "end_date": end_day,
            "total": len(rows),
            "truncated": truncated,
            "modules": list(ActivityLogModule.ALL),
            "members": members,
        }

"""Reads and atomic claims behind fixed-hours budget alerts."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable, Optional

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models.project import Project
from app.models.project_budget_alert import OUTCOME_NOTIFIED, ProjectBudgetAlert

#: Lifecycle states whose projects are monitored. Completed, cancelled and
#: archived projects are not; their alert history is kept.
MONITORED_STATUSES = ("planning", "active", "todo", "pending")


class ProjectBudgetAlertRepository:

    @staticmethod
    def monitored_fixed_projects(db: Session, project_ids: Optional[Iterable[int]] = None) -> list[Project]:
        """Every fixed-hours project with an allocation, in a monitored state.

        Flexible projects are excluded here, in SQL, so no later step can
        alert on one. A fixed project with no (or a zero) allocation is
        excluded too -- it has nothing to be remaining from.
        """
        query = select(Project).where(
            Project.billing_type == "fixed",
            Project.fixed_hours.isnot(None),
            Project.fixed_hours > 0,
            Project.status.in_(MONITORED_STATUSES),
        )
        if project_ids is not None:
            query = query.where(Project.id.in_(list(project_ids)))
        return list(db.scalars(query.order_by(Project.organization_id, Project.id)).all())

    @staticmethod
    def events_for(db: Session, keys: Iterable[tuple[int, int]]) -> dict[tuple[int, int], set[str]]:
        """``{(project_id, budget_version): {event, ...}}`` already recorded."""
        keys = list(keys)
        if not keys:
            return {}
        project_ids = {project_id for project_id, _version in keys}
        wanted = set(keys)
        result: dict[tuple[int, int], set[str]] = {}
        rows = db.execute(
            select(ProjectBudgetAlert.project_id, ProjectBudgetAlert.budget_version, ProjectBudgetAlert.event)
            .where(ProjectBudgetAlert.project_id.in_(project_ids))
        ).all()
        for project_id, version, event in rows:
            if (project_id, version) in wanted:
                result.setdefault((project_id, version), set()).add(event)
        return result

    @staticmethod
    def claim(
        db: Session,
        *,
        organization_id: int,
        project_id: int,
        budget_version: int,
        event: str,
        outcome: str,
        allocation_seconds: int,
        used_seconds: int,
    ) -> Optional[int]:
        """Insert the event row, or do nothing if it already exists.

        Returns the new row's id when *this* call created it -- the caller is
        then the only one allowed to act on the event -- and None when someone
        else already had. A single ``INSERT ... ON CONFLICT DO NOTHING``, so
        two concurrent evaluations cannot both win.
        """
        dialect = db.get_bind().dialect.name
        if dialect == "postgresql":
            from sqlalchemy.dialects.postgresql import insert
        elif dialect == "sqlite":
            from sqlalchemy.dialects.sqlite import insert
        else:  # pragma: no cover - only the two dialects this project runs on
            raise RuntimeError(f"Unsupported dialect for budget alert claims: {dialect}")
        statement = (
            insert(ProjectBudgetAlert)
            .values(
                organization_id=organization_id, project_id=project_id,
                budget_version=budget_version, event=event, outcome=outcome,
                allocation_seconds=allocation_seconds, used_seconds=used_seconds,
            )
            .on_conflict_do_nothing(index_elements=["project_id", "budget_version", "event"])
            .returning(ProjectBudgetAlert.id)
        )
        return db.execute(statement).scalar_one_or_none()

    @staticmethod
    def unqueued_notifications(db: Session, limit: int = 100) -> list[ProjectBudgetAlert]:
        """Notified events whose emails were never fully queued (a crash
        between claim and queue). Re-queueing them is safe: every recipient's
        outbox row has a deterministic dedupe key."""
        return list(
            db.scalars(
                select(ProjectBudgetAlert)
                .where(ProjectBudgetAlert.outcome == OUTCOME_NOTIFIED, ProjectBudgetAlert.emails_queued_at.is_(None))
                .order_by(ProjectBudgetAlert.id)
                .limit(limit)
            ).all()
        )

    @staticmethod
    def mark_queued(db: Session, alert_id: int) -> None:
        db.execute(
            update(ProjectBudgetAlert)
            .where(ProjectBudgetAlert.id == alert_id)
            .values(emails_queued_at=datetime.now(timezone.utc))
        )

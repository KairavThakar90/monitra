"""The audit trail this database already has.

``activity_logs`` predates Alembic in this project, like ``users`` and
``organizations``: it is part of the base schema every environment was created
from (see ``docs/Database_Documentation.md``, "Audit trail (login, logout,
task/project/employee changes, etc.)"). It had no ORM model because nothing
wrote to it. The maintenance-mode switch was the first administrator action
asked to leave a durable, queryable record of *who did what, when*; the user
actions in ``ActivityLogAction`` followed, all written through
``app.services.activity_log.ActivityLogService`` and all into this one table --
not a second, parallel one.

The mapping below is the table as it stands. The column set is not extended
here; ``docs/steering/rules.md`` names ``activity_logs`` among the tables that
are never modified. The constants below are the vocabulary written into its
``module`` and ``action`` columns.
"""
from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, Identity, String, TIMESTAMP, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class ActivityLogModule:
    """``module`` values this codebase writes."""

    #: Deployment-wide switches: maintenance mode.
    SYSTEM = "system"
    #: Signing in and out, on any client.
    AUTH = "auth"
    #: Creating, editing and archiving projects.
    PROJECT = "project"
    #: Creating, editing and archiving tasks.
    TASK = "task"
    #: Starting and stopping the timer, and moving a recorded entry.
    TIMER = "timer"
    #: Manual time requests and the decisions on them.
    MANUAL_TIME = "manual_time"
    #: The member directory: accounts and their switches.
    MEMBER = "member"
    #: The Feedback & Help workflow.
    FEEDBACK = "feedback"
    #: Events the desktop application reports about itself.
    DESKTOP = "desktop"
    #: A notice about somebody's screenshot.
    SCREENSHOT = "screenshot"

    #: Every module a reader may filter on, in display order.
    ALL = (AUTH, DESKTOP, TIMER, MANUAL_TIME, PROJECT, TASK, MEMBER, FEEDBACK, SCREENSHOT, SYSTEM)


class ActivityLogAction:
    """``action`` values this codebase writes. Kept distinct rather than a
    generic "update" so the trail can be read without decoding descriptions."""

    LOGIN = "login"
    LOGOUT = "logout"

    PROJECT_CREATED = "project_created"
    PROJECT_UPDATED = "project_updated"
    PROJECT_ARCHIVED = "project_archived"

    TASK_CREATED = "task_created"
    TASK_UPDATED = "task_updated"
    TASK_ARCHIVED = "task_archived"

    TIMER_STARTED = "timer_started"
    TIMER_STOPPED = "timer_stopped"
    ENTRY_TRANSFERRED = "entry_transferred"

    MANUAL_TIME_REQUESTED = "manual_time_requested"
    MANUAL_TIME_APPROVED = "manual_time_approved"
    MANUAL_TIME_REJECTED = "manual_time_rejected"
    MANUAL_TIME_WITHDRAWN = "manual_time_withdrawn"

    MEMBER_CREATED = "member_created"
    MEMBER_UPDATED = "member_updated"
    MEMBER_DEACTIVATED = "member_deactivated"
    MEMBER_DELETED = "member_deleted"
    LOGIN_EXCLUDED = "login_excluded"
    LOGIN_ALLOWED = "login_allowed"
    ADD_TASKS_EXCLUDED = "add_tasks_excluded"
    ADD_TASKS_ALLOWED = "add_tasks_allowed"

    FEEDBACK_STATUS_CHANGED = "feedback_status_changed"

    SCREENSHOT_NOTICE_SENT = "screenshot_notice_sent"

    DESKTOP_NOTIFICATION_CREATED = "desktop_notification_created"
    DESKTOP_NOTIFICATION_UPDATED = "desktop_notification_updated"
    DESKTOP_NOTIFICATION_DELETED = "desktop_notification_deleted"

    APP_OPENED = "app_opened"
    APP_CLOSED = "app_closed"

    #: The events the desktop may report about itself.
    CLIENT_EVENTS = (APP_OPENED, APP_CLOSED)


class ActivityLog(Base):
    """One audited action."""

    __tablename__ = "activity_logs"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    organization_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    project_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    task_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    #: The actor. Not a foreign key in the base schema, so the row survives
    #: the account.
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    module: Mapped[str] = mapped_column(String(50), nullable=False)
    action: Mapped[str] = mapped_column(String(50), nullable=False)
    entity_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    #: ``inet`` in Postgres. Mapped as text: written from the request's
    #: address when there is one, and a read only ever renders it.
    ip_address: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now(),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<ActivityLog {self.module}.{self.action} by {self.user_id}>"

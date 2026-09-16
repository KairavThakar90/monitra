"""Product-wide settings an administrator flips at runtime.

Why a table, not an environment variable
----------------------------------------
``Settings`` in ``app/core/config.py`` is read once, at process start. The
backend runs as a serverless function, so a value that lives there reaches a
running deployment only when every instance is cold-started again — and there
is no way for a person in the admin screen to change it at all. A setting that
an administrator turns on and off during the day has to be a row, read on
every request that needs it.

Why one generic key/value table
-------------------------------
The first setting is ``maintenance_mode``, a single boolean. A table with one
boolean column would be correct today and a second table tomorrow. A key/value
row costs nothing extra now and means the *next* administrator-controlled
switch is a new key, not a new migration. The value column is JSONB so a
setting is not limited to a flag, and the key is the primary key so there can
only ever be one row per setting — which is what makes "the" maintenance state
unambiguous under concurrent writes (the service locks that one row).

Scope
-----
Settings here are product-wide, not per organisation. Like ``desktop_releases``
there is deliberately no ``organization_id``: maintenance is a state of the
deployment, and a per-tenant flag would let one tenant be told the system is
under maintenance while another is not, with no way to tell which was meant.

What is recorded about a change
-------------------------------
The row carries who last changed it and when — the same two facts
``feedback_requests`` keeps for its status workflow — and the username as
well as the id, so the trail still names a person after the account is gone.
The *history* of changes is not kept here: each change is also written to
``activity_logs``, the audit-trail table this database already has, and the
service logs an ALL-CAPS event line beside every write.
"""
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import BigInteger, ForeignKey, String, TIMESTAMP, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class SystemSettingKey:
    """The setting keys this codebase knows. Nothing else is ever written."""

    #: ``{"enabled": bool}``. True while administrators want every client to
    #: show the maintenance notice. Purely informational: nothing in the
    #: backend reads it to refuse, delay or alter a request.
    MAINTENANCE_MODE = "maintenance_mode"

    ALL = (MAINTENANCE_MODE,)


class SystemSetting(Base):
    """One product-wide setting, by key."""

    __tablename__ = "system_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now(),
    )
    #: ``users.id`` of the administrator who last wrote this row. ON DELETE
    #: SET NULL: removing an administrator must not remove a system setting.
    updated_by_user_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
    )
    #: Their username at the time, so the trail survives the account.
    updated_by_username: Mapped[Optional[str]] = mapped_column(
        String(150), nullable=True,
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<SystemSetting {self.key}={self.value!r}>"

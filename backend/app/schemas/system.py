"""Request and response bodies for ``/system``."""
from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field


class MaintenanceStatusRead(BaseModel):
    """What every signed-in client polls.

    Deliberately small: a boolean and when it last changed. The clients keep
    their own "what did I last see" and react only to a transition, so
    nothing here is an instruction to show or hide anything.
    """

    maintenance_mode: bool
    updated_at: Optional[datetime] = None
    #: The server's clock at the time of the answer, so a client that wants
    #: to say "since 10:42" can place `updated_at` against a clock it can
    #: compare with -- the same reason every timer response carries one.
    server_time: datetime


class MaintenanceModeRead(MaintenanceStatusRead):
    """The administrator's view: the state plus who set it."""

    updated_by_user_id: Optional[int] = None
    updated_by_username: Optional[str] = None


class MaintenanceModeUpdate(BaseModel):
    """Enable or disable. Idempotent: asking for the state that already holds
    changes nothing and records nothing."""

    enabled: bool


class MaintenanceAuditEntry(BaseModel):
    """One change, as recorded in ``activity_logs``."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    #: ``maintenance_enabled`` or ``maintenance_disabled``.
    action: str
    user_id: int
    description: Optional[str] = None
    created_at: datetime


class MaintenanceAuditList(BaseModel):
    items: List[MaintenanceAuditEntry] = Field(default_factory=list)

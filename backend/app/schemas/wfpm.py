"""Request shapes for the /WFPM API (app/api/wfpm.py).

These are deliberately thin subsets of `app.schemas.project_management`'s
`ProjectCreate`/`TaskCreate` -- a WFPM caller does not know Monitra's internal
`status_id`, so the route resolves a default status and builds a full
`ProjectCreate`/`TaskCreate` from it. That means these schemas carry no
business-rule validators of their own: the field values still pass through
`ProjectCreate`/`TaskCreate`'s own `field_validator`/`model_validator`
functions once the full payload is constructed, so a rule (empty name,
deadline in the past, fixed-hours billing) is defined once and enforced
identically on both APIs.
"""
from datetime import date
from typing import Optional

from pydantic import BaseModel, Field

from app.core.validation import OptionalIdempotencyKey
from app.schemas.project_management import BillingType


class WfpmProjectCreate(BaseModel):
    #: `leader_id` and `fixed_hours` are deliberately absent -- a WFPM caller
    #: never chooses them. The route pins `leader_id` to
    #: `app.api.wfpm.DEFAULT_PROJECT_LEADER_ID` and `fixed_hours` to `None`
    #: for every project it creates. See app/api/wfpm.py.
    project_name: str = Field(..., max_length=150)
    description: Optional[str] = Field(None, max_length=5000)
    employee_ids: list[int] = Field(default_factory=list)
    deadline: date
    billing_type: BillingType


class WfpmTaskCreate(BaseModel):
    #: `assignee_id` is deliberately absent -- a WFPM caller never names one.
    #: The route pins it to the authenticated caller's own id for every task
    #: it creates, so a request can never assign another user's task. See
    #: app/api/wfpm.py.
    name: str = Field(..., max_length=150)
    client_op: OptionalIdempotencyKey = None

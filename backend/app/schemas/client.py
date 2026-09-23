from datetime import datetime

from pydantic import BaseModel, Field

from app.core.validation import Email, IdentifierList


class ClientPermissions(BaseModel):
    """What a client may see, beyond the shared projects' own name and
    description (those are never gated). Every flag defaults to true except
    screenshots, matching `Client`'s own column defaults -- see that model
    for why."""

    share_member_details: bool = True
    share_screenshots: bool = False
    share_tasks: bool = True
    share_timing: bool = True


class ClientInvitationCreate(BaseModel):
    email: Email
    project_ids: IdentifierList = Field(default=None)
    permissions: ClientPermissions = Field(default_factory=ClientPermissions)


class ClientAccessUpdate(BaseModel):
    """Everything an admin can change about an existing client's access in
    one call: which projects are shared, and what they may see within them."""

    project_ids: IdentifierList = Field(default=None)
    permissions: ClientPermissions = Field(default_factory=ClientPermissions)


class ClientProjectRef(BaseModel):
    id: int
    project_name: str


class ClientListItem(BaseModel):
    id: int
    name: str
    email: str
    status: str
    projects: list[ClientProjectRef]
    permissions: ClientPermissions
    created_at: datetime


class ClientListResponse(BaseModel):
    items: list[ClientListItem]
    pagination: dict


class ClientLoginLinkRequest(BaseModel):
    email: Email

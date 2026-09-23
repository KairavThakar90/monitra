from datetime import datetime

from pydantic import BaseModel, Field

from app.core.validation import Email, IdentifierList


class ClientInvitationCreate(BaseModel):
    email: Email
    project_ids: IdentifierList = Field(default=None)


class ClientProjectsUpdate(BaseModel):
    project_ids: IdentifierList = Field(default=None)


class ClientProjectRef(BaseModel):
    id: int
    project_name: str


class ClientListItem(BaseModel):
    id: int
    name: str
    email: str
    status: str
    projects: list[ClientProjectRef]
    created_at: datetime


class ClientListResponse(BaseModel):
    items: list[ClientListItem]
    pagination: dict


class ClientLoginLinkRequest(BaseModel):
    email: Email

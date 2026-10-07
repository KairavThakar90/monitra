from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field

from app.core.validation import Credential, Email, IdentifierList, Password


class ClientPermissions(BaseModel):
    """What a client may see, beyond the shared projects' own name and
    description (those are never gated). Every flag defaults to true except
    screenshots, matching `Client`'s own column defaults -- see that model
    for why."""

    share_member_details: bool = True
    share_screenshots: bool = False
    share_tasks: bool = True
    share_timing: bool = True
    share_billing: bool = False


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


class ClientLoginRequest(BaseModel):
    """A client's sign-in. ``password`` is absent for the older email-only
    sign-in and present for a client who chose one.

    ``Credential``, not ``Password``: it is being *presented*, so only the upper
    bound applies -- enforcing the minimum here would lock out an account made
    under an older policy -- and it is compared exactly as typed.
    """

    email: Email
    password: Optional[Credential] = None


class ClientSignInMethod(BaseModel):
    """Which way a client signs in: with a password they set, or not."""

    password_required: bool


class ClientInvitationRead(BaseModel):
    """What the set-password page may know about the invitation its link names:
    which address the account is for, so the page can say so. Nothing else --
    the link is a bearer secret, so what it reveals is kept to the one fact the
    person holding it needs."""

    email: str


class ClientSetPassword(BaseModel):
    """The password a client chooses from their invitation link.

    ``Password``, not ``Credential``: it is being *chosen*, so the full length
    policy applies. It is stored as a hash and otherwise untouched -- never
    trimmed, normalised or content-checked (docs/VALIDATION.md, "Passwords are
    special")."""

    password: Password


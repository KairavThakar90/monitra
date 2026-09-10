from pydantic import BaseModel, ConfigDict, Field
from typing import Literal, Optional, Dict, Any
from datetime import datetime
from app.core.validation import (
    Credential,
    Email,
    Name,
    OptionalName,
    PlainText,
)


class UserBase(BaseModel):
    """Shared user fields.

    Left unvalidated on purpose: ``UserRead`` inherits from this, so tightening
    it would apply input rules to every user record on the way *out* and turn a
    legacy row into a 500. ``UserCreate`` below overrides the user-supplied
    fields with validated types; that is where a client can still influence the
    value.
    """

    username: str
    email: str
    name: str
    designation: Optional[str] = None
    role_name: str
    permissions: Dict[str, Any] = Field(default_factory=dict)
    wp_capabilities: Optional[Dict[str, Any]] = None
    idle_enabled: bool = True
    idle_minutes: int = 5
    capture_frequency: int
    status: str = "active"
    is_active: bool = True

class UserCreate(UserBase):
    #: Re-declared with validated types. These are the fields an administrator
    #: types into a form, so they get the shared name/email rules; the rest of
    #: ``UserBase`` is server-derived.
    username: PlainText
    email: Email
    name: Name
    designation: OptionalName = None
    organization_id: int
    hubstaff_user_id: Optional[str] = None

class UserRead(UserBase):
    id: int
    organization_id: int
    hubstaff_user_id: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)

class UserUpdate(BaseModel):
    name: OptionalName = None
    designation: OptionalName = None
    role_name: Optional[str] = None
    status: Optional[str] = None
    permissions: Optional[Dict[str, Any]] = None
    wp_capabilities: Optional[Dict[str, Any]] = None
    idle_enabled: Optional[bool] = None
    #: Minutes of inactivity before the idle popup. Must be positive: a zero
    #: or negative threshold would make every poll look like an idle period.
    idle_minutes: Optional[int] = Field(None, gt=0)
    capture_frequency: Optional[int] = None

    model_config = ConfigDict(from_attributes=True)

#: Which client a sign-in is being performed for. The value is forwarded to the
#: provider verbatim, so the set is fixed here rather than accepting free text:
#: the provider is an external system that interprets this field, and letting a
#: caller put arbitrary text in a payload we send upstream is exactly the kind of
#: unvalidated pass-through the validation rules exist to prevent.
LoginFor = Literal["Desktop", "Web"]

#: What a request that omits ``login_for`` means. Desktop, because that is where
#: this endpoint's traffic comes from, and because an existing client that has
#: not been updated must keep getting exactly the behaviour it has today.
DEFAULT_LOGIN_FOR: LoginFor = "Desktop"


class LoginRequest(BaseModel):
    """Credentials presented at sign-in.

    ``username`` accepts either a username or an email address — this
    deployment supports both — so it is held to plain-text rules rather than
    email rules.

    ``login_for`` names the client the session is for and is passed on to the
    provider. It is optional and defaults to ``"Desktop"`` so that clients
    written against the earlier contract are unaffected.

    ``password`` uses ``Credential``, not ``Password``: it is being *presented*,
    not chosen. Enforcing the minimum-length policy here would lock out any
    account created before that policy, and would let an attacker probe the
    policy from the login form. The value is passed to the verifier exactly as
    typed — never trimmed, normalised, or content-checked, because every one of
    those would silently alter a secret.
    """

    username: PlainText
    password: Credential
    login_for: LoginFor = DEFAULT_LOGIN_FOR

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "username": "provider-user@example.com",
                    "password": "provider-password",
                    "login_for": "Desktop",
                }
            ]
        }
    )

class PermissionSchema(BaseModel):
    name: str
    permissions: Dict[str, Any] = Field(default_factory=dict)


class HubstaffLoginPayload(BaseModel):
    user_id: int
    username: PlainText
    email: Email
    name: Name
    hubstaff_user_id: str
    hubstaff_designation: OptionalName = None
    organization_id: int         
    idle_enabled: bool = True
    idle_minutes: int = Field(5, gt=0)
    capture_frequency: int
    permission_schema: PermissionSchema

class SsoTokenRequest(BaseModel):
    """The provider-issued JWT handed to the browser as ?token=... ."""
    token: str = Field(..., min_length=1)


class DevLoginRequest(BaseModel):
    email: Email
    #: Presented, not chosen — see ``LoginRequest``.
    password: Credential

class EmployeeListItem(BaseModel):
    id: int
    name: str
    email: str
    role_name: str
    is_active: bool
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)

class EmployeeDetail(EmployeeListItem):
    organization_id: int

    model_config = ConfigDict(from_attributes=True)

class EmployeeStatusUpdate(BaseModel):
    is_active: bool

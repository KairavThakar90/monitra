import re
from datetime import date, datetime
from enum import Enum
from typing import Annotated, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.validation import optional_integer_field

#: Screenshot capture interval, in minutes (`users.capture_frequency`).
#:
#: The column has no unit of its own. `AuthService`'s SSO sync path
#: (app/services/auth.py) used to default new accounts to a literal `300`
#: meaning "seconds", disagreeing with every account created any other way --
#: 61 of 63 real accounts on the live database at the time this was added
#: held a plain minute count (`10`, matching the desktop's actual 10-minute
#: screenshot window). That default is now `10` everywhere a new account is
#: created, matching the convention the data actually uses. Existing rows
#: still holding the old `300` default are not migrated automatically -- an
#: admin can correct them per member from the User Management page. Do not
#: reintroduce a seconds conversion here.
#:
#: No upper bound: the admin sets this per member based on their own
#: monitoring requirements, and the desktop's screenshot scheduler has no
#: fixed ceiling of its own to enforce here. A positive whole number is the
#: only real constraint -- zero or negative minutes is not an interval.
CaptureFrequencyMinutes = Annotated[
    Optional[int], optional_integer_field(label="Screenshot capture frequency", minimum=1)
]

#: Idle detection threshold in minutes (`users.idle_minutes`).
IdleMinutes = Annotated[
    Optional[int], optional_integer_field(label="Idle time threshold", minimum=1, maximum=120)
]


class MemberRole(str, Enum):
    administrator = "administrator"
    hr = "hr"
    leader = "leader"
    employee = "employee"


class MemberRoleFilter(str, Enum):
    """What the Members directory's Role filter accepts.

    Every `MemberRole` plus `client`. A client is an external account, not
    someone an administrator creates or re-roles from the Members form -- that
    is why it is not in `MemberRole` -- but the directory lists clients, so it
    has to be filterable.
    """

    administrator = "administrator"
    hr = "hr"
    leader = "leader"
    employee = "employee"
    client = "client"


class MemberStatus(str, Enum):
    active = "active"
    inactive = "inactive"


def _clean_required(value: str, field_name: str, max_length: int) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{field_name} cannot be empty")
    if len(cleaned) > max_length:
        raise ValueError(f"{field_name} must be {max_length} characters or fewer")
    return cleaned


class MemberCreate(BaseModel):
    name: str = Field(..., max_length=150)
    email: str = Field(..., max_length=254)
    role: MemberRole
    status: MemberStatus = MemberStatus.active
    date_of_joining: date
    date_of_birth: date
    designation: str = Field(..., max_length=150)

    @field_validator("name", "designation")
    @classmethod
    def clean_text(cls, value: str, info):
        return _clean_required(value, info.field_name.replace("_", " ").title(), 150)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str):
        email = value.strip().lower()
        if len(email) > 254 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
            raise ValueError("email must be a valid email address")
        return email

    @model_validator(mode="after")
    def validate_dates(self):
        today = date.today()
        if self.date_of_birth > today:
            raise ValueError("Date of birth cannot be in the future")
        if self.date_of_joining > today:
            raise ValueError("Date of joining cannot be in the future")
        return self


class MemberUpdate(BaseModel):
    name: Optional[str] = Field(None, max_length=150)
    email: Optional[str] = Field(None, max_length=254)
    role: Optional[MemberRole] = None
    status: Optional[MemberStatus] = None
    date_of_joining: Optional[date] = None
    date_of_birth: Optional[date] = None
    designation: Optional[str] = Field(None, max_length=150)
    idle_enabled: Optional[bool] = None
    idle_minutes: IdleMinutes = None
    capture_frequency: CaptureFrequencyMinutes = None
    #: The Members directory's Allow / Not allow switch for task creation.
    can_add_tasks: Optional[bool] = None
    #: The Members directory's Allow / Exclude switch for signing in.
    can_login: Optional[bool] = None
    #: Whether this member may be chosen as a project's Owner. Withdrawing it
    #: does not unassign the projects they already own; it only stops them
    #: being chosen again. See app/services/project_ownership.py.
    can_own_projects: Optional[bool] = None

    @field_validator("can_own_projects")
    @classmethod
    def owner_switch_is_a_boolean(cls, value: Optional[bool]):
        # Runs only when the key was sent. Omitting it leaves the switch as it
        # is; an explicit null would reach a NOT NULL column as a 500.
        if value is None:
            raise ValueError("can_own_projects must be true or false")
        return value

    @field_validator("name", "designation")
    @classmethod
    def clean_optional_text(cls, value: Optional[str], info):
        if value is None:
            return value
        return _clean_required(value, info.field_name.replace("_", " ").title(), 150)

    @field_validator("email")
    @classmethod
    def normalize_optional_email(cls, value: Optional[str]):
        if value is None:
            return value
        email = value.strip().lower()
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
            raise ValueError("email must be a valid email address")
        return email


class MemberResponse(BaseModel):
    id: int
    name: str
    email: str
    role: str = Field(validation_alias="role_name")
    status: str
    date_of_joining: Optional[date]
    date_of_birth: Optional[date]
    designation: Optional[str]
    idle_enabled: bool
    idle_minutes: int
    capture_frequency: int
    can_add_tasks: bool = True
    can_login: bool = True
    can_own_projects: bool = False
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)

    @field_validator("can_own_projects", mode="before")
    @classmethod
    def unset_means_not_eligible(cls, value):
        # The opposite default to `can_add_tasks`: eligibility is granted,
        # never assumed.
        return False if value is None else value

    @field_validator("can_add_tasks", "can_login", mode="before")
    @classmethod
    def unset_means_allowed(cls, value):
        # Only an explicit False withdraws; an unset value is the default.
        return True if value is None else value


class MemberAccessUpdate(BaseModel):
    """Turn the two Members-directory switches on or off for several members.

    Only the switches that are sent change; at least one must be. Nothing else
    about a member can be written through this body, which is what lets HR --
    who may not edit members -- use it.
    """

    member_ids: list[int] = Field(..., min_length=1, max_length=1000)
    can_login: Optional[bool] = None
    can_add_tasks: Optional[bool] = None

    @field_validator("member_ids")
    @classmethod
    def distinct_ids(cls, value: list[int]):
        return list(dict.fromkeys(value))

    @model_validator(mode="after")
    def at_least_one_switch(self):
        if self.can_login is None and self.can_add_tasks is None:
            raise ValueError("Send can_login, can_add_tasks, or both")
        return self


class MemberAccessFailure(BaseModel):
    id: int
    detail: str


class MemberAccessResponse(BaseModel):
    updated: list[MemberResponse]
    failed: list[MemberAccessFailure]


class MemberListResponse(BaseModel):
    items: list[MemberResponse]
    page: int
    limit: int
    total: int
    pages: int
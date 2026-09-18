import re
from datetime import date, datetime
from enum import Enum
from typing import Annotated, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.validation import optional_integer_field

#: Screenshot capture interval, in minutes (`users.capture_frequency`).
#:
#: The column has no unit of its own, and production data is not consistent:
#: `AuthService`'s SSO sync path (app/services/auth.py) writes a literal `300`
#: as a "seconds" default, but every account created any other way -- 61 of
#: 63 real accounts on the live database at the time this was added -- holds
#: a plain minute count (`10`, matching the desktop's actual 10-minute
#: screenshot window). This type follows the convention the data actually
#: uses. Do not reintroduce a seconds conversion here without first fixing
#: the SSO path that disagrees with it.
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
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class MemberListResponse(BaseModel):
    items: list[MemberResponse]
    page: int
    limit: int
    total: int
    pages: int
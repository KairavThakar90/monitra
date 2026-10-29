from pydantic import BaseModel, model_validator
from typing import List, Literal, Optional
from datetime import datetime

from app.core.validation import (
    Identifier, IdentifierList, Name, OptionalName, OptionalPlainText, PlainText,
)


class ScreenshotRuleScope(BaseModel):
    """Who a new rule is applied to, in the same request that creates it.

    ``all`` is every active member of the caller's organization *at that
    moment*; ``members`` is exactly the listed people. Either way the rule is
    switched on for each of them as a per-member exclusion -- the same rows the
    per-member toggle writes -- so it can still be turned off for any one of
    them afterwards. A member who joins later is not covered by ``all``: there
    is no standing "everyone" flag, only the rows that exist.
    """

    scope: Literal["all", "members"]
    #: The members, for ``scope == "members"``; refused with ``all``.
    user_ids: IdentifierList = None

    @model_validator(mode="after")
    def _members_match_scope(self):
        if self.scope == "members" and not self.user_ids:
            raise ValueError("Choose at least one member, or apply the rule to all members.")
        if self.scope == "all" and self.user_ids:
            raise ValueError("user_ids is only for scope 'members'.")
        return self


class ScreenshotApplicationBase(BaseModel):
    name: str
    process_name: str
    category: str
    description: Optional[str] = None
    is_active: bool = True

class ScreenshotApplicationCreate(ScreenshotApplicationBase):
    # Re-declared here, not on the Base: the Response models inherit the Base, and a row stored
    # before these rules must still be returned as it is. These are what an administrator TYPES.
    name: Name
    process_name: PlainText
    category: Name
    description: OptionalPlainText = None
    #: Optional. Omitted, the rule is only added to the catalogue (nobody is
    #: excluded until the per-member toggle is used), exactly as before.
    apply_to: Optional[ScreenshotRuleScope] = None

class ScreenshotApplicationUpdate(BaseModel):
    name: OptionalName = None
    process_name: OptionalPlainText = None
    category: OptionalName = None
    description: OptionalPlainText = None
    is_active: Optional[bool] = None

class ScreenshotApplicationResponse(ScreenshotApplicationBase):
    id: int
    created_at: datetime
    updated_at: datetime
    class Config:
        orm_mode = True


class ScreenshotApplicationCreatedResponse(ScreenshotApplicationResponse):
    #: How many members the rule was switched on for (0 when `apply_to` was omitted).
    applied_to_count: int = 0

class ScreenshotUrlBase(BaseModel):
    name: str
    domain: str
    url_pattern: str
    category: str
    is_active: bool = True

class ScreenshotUrlCreate(ScreenshotUrlBase):
    name: Name
    #: A hostname or a pattern such as `*.example.com` -- so plain text, not the strict DOMAIN rule.
    domain: PlainText
    url_pattern: PlainText
    category: Name
    #: See `ScreenshotApplicationCreate.apply_to`.
    apply_to: Optional[ScreenshotRuleScope] = None

class ScreenshotUrlUpdate(BaseModel):
    name: OptionalName = None
    domain: OptionalPlainText = None
    url_pattern: OptionalPlainText = None
    category: OptionalName = None
    is_active: Optional[bool] = None

class ScreenshotUrlResponse(ScreenshotUrlBase):
    id: int
    created_at: datetime
    updated_at: datetime
    class Config:
        orm_mode = True


class ScreenshotUrlCreatedResponse(ScreenshotUrlResponse):
    #: How many members the rule was switched on for (0 when `apply_to` was omitted).
    applied_to_count: int = 0

class ScreenshotExclusionBase(BaseModel):
    user_id: int
    application_id: Optional[int] = None
    url_id: Optional[int] = None
    exclusion_type: str
    is_excluded: bool = True

class ScreenshotExclusionCreate(ScreenshotExclusionBase):
    user_id: Identifier
    application_id: Optional[Identifier] = None
    url_id: Optional[Identifier] = None
    #: The two the table's CHECK constraint allows; anything else was a 500 from the database.
    exclusion_type: Literal["application", "url"]

class ScreenshotExclusionUpdate(BaseModel):
    is_excluded: bool

class ScreenshotExclusionResponse(ScreenshotExclusionBase):
    id: int
    created_at: datetime
    updated_at: datetime
    class Config:
        orm_mode = True

class PrivacyConfigResponse(BaseModel):
    applications: List[ScreenshotApplicationResponse]
    urls: List[ScreenshotUrlResponse]
    excluded_applications: List[ScreenshotExclusionResponse]
    excluded_urls: List[ScreenshotExclusionResponse]
    #: Screen numbers (1-based, the operating system's order) not to capture for this member.
    excluded_displays: List[int] = []

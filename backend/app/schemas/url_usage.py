from pydantic import BaseModel, Field, ConfigDict, field_validator
from typing import List, Optional
from datetime import datetime

from app.core.activity_identity import canonical_application_name, canonical_domain
from app.core.validation import Identifier, OptionalIdempotencyKey, OptionalUrl

#: Bounds on captured page titles. Like window titles, these come from other
#: applications and are length-limited but not content-checked.
PAGE_TITLE_MAX_LENGTH = 1024

#: Largest batch the desktop may upload at once. It buffers while offline and
#: flushes on reconnect, so batches are legitimately large but not unbounded.
MAX_BATCH_RECORDS = 500


class URLUsageCreate(BaseModel):
    time_entry_id: Identifier = Field(..., description="Time entry ID associated with the URL usage")
    browser_name: str = Field(..., min_length=1, max_length=100, description="Browser name e.g. Google Chrome")
    domain: str = Field(..., min_length=1, max_length=255, description="Domain e.g. github.com")
    #: The address bar's contents, read via UI Automation.
    #:
    #: Held to URL rules, which is what makes the scheme allowlist apply --
    #: this value is stored and later rendered as a link, so a `javascript:`
    #: URL here is the one case that turns captured telemetry into script
    #: execution. Plain-text rules would be wrong: `?`, `&`, `=`, `%` and `#`
    #: are ordinary URL punctuation.
    #:
    #: Stays optional. Where no URL can be read -- Firefox without
    #: accessibility, macOS, Linux -- the desktop sends nothing and the time is
    #: still recorded against the browser as application usage. Never
    #: substitute a placeholder domain here.
    url: OptionalUrl = Field(None, description="Full URL")
    #: Captured from the page, so bounded but not content-checked: a real page
    #: title can legitimately contain angle brackets.
    page_title: Optional[str] = Field(
        None, max_length=PAGE_TITLE_MAX_LENGTH, description="Page title"
    )
    duration_seconds: int = Field(..., ge=0, description="Duration spent in seconds")
    recorded_at: Optional[datetime] = Field(None, description="Time event was recorded by desktop")
    client_event_id: OptionalIdempotencyKey = Field(None, description="Client idempotency key")

    @field_validator("browser_name", mode="after")
    @classmethod
    def _canonicalize_browser(cls, value: str) -> str:
        """Name the browser the same way application usage names it.

        The two tables describe the same session from two angles, so a
        browser that appears as "Brave" in application usage must not appear
        as something else here. Both go through one catalogue.
        """
        canonical = canonical_application_name(value)
        if not canonical:
            raise ValueError("browser_name must identify a browser")
        return canonical

    @field_validator("domain", mode="after")
    @classmethod
    def _canonicalize_domain(cls, value: str) -> str:
        """Group a site under one hostname.

        ``www.`` is a routing prefix, not a different site; left on, the
        same site arrives as two report rows each holding part of the time.
        No other label is stripped -- ``docs.google.com`` is genuinely not
        ``mail.google.com``.
        """
        canonical = canonical_domain(value)
        if not canonical:
            raise ValueError("domain must identify a site")
        return canonical


class URLUsageBatchCreate(BaseModel):
    records: List[URLUsageCreate] = Field(
        ...,
        min_length=1,
        max_length=MAX_BATCH_RECORDS,
        description="List of URL usage records to sync",
    )

class URLUsageRecord(BaseModel):
    id: int
    organization_id: int
    time_entry_id: int
    browser_name: str
    domain: str
    url: Optional[str] = None
    page_title: Optional[str] = None
    duration_seconds: int
    recorded_at: datetime
    created_at: datetime
    client_event_id: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)

class URLUsageResponse(BaseModel):
    success: bool = True
    message: str = "URL usage recorded successfully"
    data: URLUsageRecord

class URLUsageBatchSummaryData(BaseModel):
    accepted: int
    failed: int

class URLUsageBatchResponse(BaseModel):
    success: bool = True
    message: str = "URL usage batch synced successfully"
    data: URLUsageBatchSummaryData

class URLUsageListResponseData(BaseModel):
    items: List[URLUsageRecord]
    total: int
    skip: int
    limit: int

class URLUsageListResponse(BaseModel):
    success: bool = True
    data: URLUsageListResponseData

class URLUsageDomainSummary(BaseModel):
    domain: str
    duration_seconds: int

class URLUsageBrowserSummary(BaseModel):
    browser_name: str
    duration_seconds: int

class URLUsageSummaryData(BaseModel):
    time_entry_id: Optional[int] = None
    total_duration_seconds: int
    domains: List[URLUsageDomainSummary]
    browsers: List[URLUsageBrowserSummary]

class URLUsageSummaryResponse(BaseModel):
    success: bool = True
    data: URLUsageSummaryData

class URLUsagePageSummary(BaseModel):
    """One visited page and the total time spent on it in the queried window."""
    domain: str
    url: Optional[str] = None
    page_title: Optional[str] = None
    duration_seconds: int

class URLUsageGlobalSummaryData(BaseModel):
    total_duration_seconds: int
    pages: List[URLUsagePageSummary]

class URLUsageGlobalSummaryResponse(BaseModel):
    success: bool = True
    data: URLUsageGlobalSummaryData

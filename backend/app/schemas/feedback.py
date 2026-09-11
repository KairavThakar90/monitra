from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: Upper bound on a feedback message. Long enough for a detailed bug report,
#: short enough that a single row cannot be used to store arbitrary payloads.
#: The desktop dialog enforces the same number client-side.
MESSAGE_MAX_LENGTH = 5000


class FeedbackCategory(str, Enum):
    """The six categories the Feedback & Help form offers. Nothing else is accepted."""

    suggestion = "suggestion"
    report_a_problem = "report_a_problem"
    general_feedback = "general_feedback"
    need_help = "need_help"
    account_login_issue = "account_login_issue"
    other = "other"


class FeedbackStatus(str, Enum):
    """Support workflow states. A submission always starts at ``new``."""

    new = "new"
    reviewing = "reviewing"
    in_progress = "in_progress"
    resolved = "resolved"
    closed = "closed"


class FeedbackStatusAction(str, Enum):
    """The only two states an administrator may move feedback *into*.

    Deliberately narrower than `FeedbackStatus`. The dashboard offers two
    buttons — Working and Resolved — and this is the wire vocabulary for
    exactly those, so anything else is a 422 from the schema before a route
    function runs. `new` is absent because a submission cannot be un-submitted;
    `reviewing` and `closed` are absent because no part of the product sets
    them, and a status nothing can produce is not a status a client may request.

    ``in_progress`` rather than a new ``working`` value: the column already has
    a state meaning "someone is working on this", and adding a synonym beside
    it would leave two spellings of one state for every future reader to
    reconcile. "Working" is the label the button carries; `in_progress` is what
    it means.
    """

    in_progress = "in_progress"
    resolved = "resolved"


class FeedbackCreate(BaseModel):
    """The only fields a client may send.

    `user_id`, `organization_id` and `status` are deliberately absent: they are
    derived server-side from the access token so a client cannot file feedback
    as another user, against another tenant, or in a non-initial state.
    """

    category: FeedbackCategory = Field(
        ...,
        description="One of the six supported feedback categories.",
        examples=["report_a_problem"],
    )
    message: str = Field(
        ...,
        max_length=MESSAGE_MAX_LENGTH,
        description="The user's message. Surrounding whitespace is trimmed; the rest is stored verbatim.",
        examples=["The timer keeps showing 00:00 after I resume from sleep."],
    )

    @field_validator("message")
    @classmethod
    def message_must_not_be_blank(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("Message must not be empty.")
        if len(trimmed) > MESSAGE_MAX_LENGTH:
            raise ValueError(f"Message must be at most {MESSAGE_MAX_LENGTH} characters.")
        return trimmed


class FeedbackRead(BaseModel):
    """What the client gets back — enough to confirm the submission, no more."""

    id: int
    category: FeedbackCategory
    message: str
    status: FeedbackStatus
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class FeedbackItem(BaseModel):
    """One row of the dashboard's feedback list.

    The submitter is identified by the existing user record — `employee_id` is
    `users.id` and `employee_name` is `users.name`; the feedback table stores
    neither, so nothing about a person is duplicated here.

    `status` is the workflow state the Admin buttons drive. It is what lets the
    dashboard show the current state rather than guessing from the last action
    the browser happens to have performed — the server is the source of truth,
    and this is how it says so.

    What is deliberately *not* here: `status_changed_by`. It is an internal
    user id, it is of no use to the React client, and this same model answers
    `/feedback/my`, where it would tell an employee which administrator handled
    their complaint. The audit trail is a database and log concern.
    """

    id: int
    employee_id: int
    employee_name: str
    category: FeedbackCategory
    message: str
    status: FeedbackStatus = FeedbackStatus.new
    created_at: datetime
    updated_at: Optional[datetime] = None


class FeedbackStatusUpdate(BaseModel):
    """The Working / Resolved request body — one field, and it is not a person.

    There is no `employee_id`, no `recipient_email` and no `notify` flag, and
    their absence is the security property: the recipient of the notification
    is resolved from the feedback row's own `user_id` server-side. A client
    that sends an address is sending a field this model does not define, and
    it is discarded rather than honoured.
    """

    status: FeedbackStatusAction = Field(
        ...,
        description="The state to move this feedback into: in_progress (Working) or resolved.",
        examples=["in_progress"],
    )


class FeedbackStatusUpdateResponse(FeedbackItem):
    """The updated row, plus what the request actually caused.

    `notification_queued` is false when the row was already in the requested
    state and nothing was sent — which is what a double-click, a browser
    refresh or a retried request produces. The client uses it to word the
    toast honestly instead of claiming an email went out.
    """

    notification_queued: bool = False


class FeedbackListResponse(BaseModel):
    """Same envelope the other React-facing list endpoints return."""

    items: list[FeedbackItem]
    page: int
    limit: int
    total: int
    pages: int

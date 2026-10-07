from datetime import datetime
from enum import Enum
from typing import Annotated, Optional

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator

from app.core.validation import description_field

#: Upper bound on a feedback message. Long enough for a detailed bug report,
#: short enough that a single row cannot be used to store arbitrary payloads.
#: The desktop dialog enforces the same number client-side.
MESSAGE_MAX_LENGTH = 5000

#: Upper bound on the note an administrator may add when they move a feedback on
#: (it is printed in the email the employee receives, so it is a paragraph, not
#: a document). A `max_length` override of the catalogue's description rule; the
#: dashboard's dialog enforces the same number
#: (`STATUS_MESSAGE_MAX_LENGTH` in `features/feedback/feedbackActions.ts`).
STATUS_MESSAGE_MAX_LENGTH = 1000


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


class FeedbackAttachmentRead(BaseModel):
    """Safe metadata for one attachment — never a storage id, path or URL.

    The file itself is fetched from `GET /feedback/attachments/{id}/content`,
    which authorises the caller first. Nothing here lets a client reach the
    object directly, and nothing here names where it is stored.
    """

    id: int
    original_filename: str
    content_type: str
    file_size: int
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_image(self) -> bool:
        return self.content_type.startswith("image/")


class FeedbackSubmissionRead(FeedbackRead):
    """The reply to a submission that may carry attachments.

    `duplicate` is true when this submission's `client_op` had already been
    stored — the desktop's retry after a lost reply. Nothing was created a
    second time and this is the original record, which is what lets the client
    treat it as success and stop retrying.
    """

    attachments: list[FeedbackAttachmentRead] = Field(default_factory=list)
    duplicate: bool = False


class FeedbackReplyRead(BaseModel):
    """A note an administrator wrote to the employee when moving their feedback on.

    It is what the employee was sent in the status email: the text, which move
    it came with, and when. There is deliberately no author -- the update is
    from Monitra, not from a named person, and this model also answers
    `/feedback/my`.
    """

    message: str
    #: The move the note went out with: `in_progress` (Working) or `resolved`.
    status: FeedbackStatusAction
    created_at: datetime


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
    #: Attachment metadata only — a list page never carries file bytes, and a
    #: feedback with none (every row that predates attachments) reads `0` / `[]`.
    attachment_count: int = 0
    attachments: list[FeedbackAttachmentRead] = Field(default_factory=list)
    #: What an administrator wrote to the employee, oldest first -- empty when
    #: nobody added a note (the usual case: Working carries none, and a Resolved
    #: note is optional).
    replies: list[FeedbackReplyRead] = Field(default_factory=list)


class FeedbackStatusUpdate(BaseModel):
    """The Working / Resolved request body -- a status, an optional note, and no person.

    There is no `employee_id`, no `recipient_email` and no `notify` flag, and
    their absence is the security property: the recipient of the notification
    is resolved from the feedback row's own `user_id` server-side. A client
    that sends an address is sending a field this model does not define, and
    it is discarded rather than honoured.

    `message` is the one addition: free text the administrator may write when
    they resolve a feedback, printed in the email the employee receives. It is
    not a recipient, an address or a template -- it is validated as plain text
    by the catalogue's description rule (blank means "no note", markup and
    control characters are refused) and escaped when the email is rendered.
    """

    status: FeedbackStatusAction = Field(
        ...,
        description="The state to move this feedback into: in_progress (Working) or resolved.",
        examples=["in_progress"],
    )
    message: Annotated[
        Optional[str],
        description_field(label="Message", max_length=STATUS_MESSAGE_MAX_LENGTH),
    ] = Field(
        None,
        description=(
            "Optional note to the employee, included in the email about this status "
            f"change. Up to {STATUS_MESSAGE_MAX_LENGTH} characters; blank or omitted "
            "sends the standard update. It is not stored on the feedback itself."
        ),
        examples=["Fixed in the next release. Thanks for flagging it."],
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

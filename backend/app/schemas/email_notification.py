from typing import Optional

from pydantic import BaseModel, Field


class DispatchResult(BaseModel):
    """What one sweep of the email outbox did.

    Counts only — never a recipient, a subject or any part of a message body.
    This endpoint is triggered by a scheduler whose logs are not a place for
    the contents of somebody's feedback.
    """

    attempted: int = Field(0, description="Notifications this sweep claimed and tried.")
    sent: int = Field(0, description="Delivered on this sweep.")
    retrying: int = Field(0, description="Failed this time; still within their attempt budget.")
    failed: int = Field(0, description="Out of attempts. Parked with the reason recorded.")
    skipped: int = Field(0, description="Not due, or already claimed by another sweep.")
    unconfigured: int = Field(
        0, description="Not attempted because this deployment cannot send email."
    )
    error: int = Field(0, description="Raised unexpectedly; logged and left queued.")
    reason: Optional[str] = Field(
        None, description="Why the sweep did nothing, when it did nothing."
    )

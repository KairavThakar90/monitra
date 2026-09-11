from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.user import User
from app.schemas.feedback import (
    FeedbackCategory,
    FeedbackCreate,
    FeedbackItem,
    FeedbackListResponse,
    FeedbackRead,
    FeedbackStatusUpdate,
    FeedbackStatusUpdateResponse,
)
from app.services.feedback import FeedbackService

router = APIRouter(prefix="/feedback", tags=["Feedback"])


@router.post(
    "",
    response_model=FeedbackRead,
    summary="Submit a Feedback & Help message.",
    description=(
        "Records feedback from the authenticated user. The submitting user and "
        "their organization are taken from the access token, and the status is "
        "always set to 'new' — none of the three can be supplied by the client.\n\n"
        "Submitting also queues a notification email to the configured Admin and "
        "HR recipients. That notification is queued, not sent inline: this "
        "response reports whether the feedback was **saved**, and email delivery "
        "cannot change it. A submission is never rejected because mail could not "
        "go out."
    ),
    responses={422: {"description": "Unsupported category, or an empty/too-long message."}},
)
def submit_feedback(
    feedback_in: FeedbackCreate,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return FeedbackService.submit_feedback(
        db, feedback_in, current_user, background_tasks=background_tasks
    )


# The two `/my` routes are declared before `/{feedback_id}`: FastAPI matches in
# declaration order, so the literal path must come first or "my" would be parsed
# as a feedback id.


@router.get(
    "/my",
    response_model=FeedbackListResponse,
    summary="List the feedback the current user submitted.",
    description=(
        "Returns only feedback whose `user_id` is the authenticated user's, newest "
        "first. The owner comes from the access token — there is no user_id "
        "parameter, so a caller cannot read anyone else's feedback. View-only."
    ),
)
def list_my_feedback(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return FeedbackService.list_my_feedback(db, current_user, page, limit)


@router.get(
    "/my/{feedback_id}",
    response_model=FeedbackItem,
    summary="Get one feedback the current user submitted.",
    description=(
        "Returns the feedback only when the authenticated user submitted it. "
        "Another user's id is answered with 404, identically to an id that does "
        "not exist. View-only."
    ),
    responses={404: {"description": "No such feedback submitted by this user."}},
)
def get_my_feedback(
    feedback_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return FeedbackService.get_my_feedback(db, current_user, feedback_id)


@router.get(
    "",
    response_model=FeedbackListResponse,
    summary="List all feedback in the organization (Admin / HR / Leader only).",
    description=(
        "Returns every feedback submitted inside the caller's organization, "
        "newest first — including feedback the caller submitted themselves. "
        "Restricted to the Admin, HR and Leader roles; any other role gets 403. "
        "Feedback from another organization is never returned. View-only."
    ),
    responses={403: {"description": "The caller is not an Admin, HR or Leader."}},
)
def list_all_feedback(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    category: Optional[FeedbackCategory] = Query(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return FeedbackService.list_all_feedback(
        db, current_user, page, limit, category.value if category else None
    )


@router.get(
    "/{feedback_id}",
    response_model=FeedbackItem,
    summary="Get one feedback in the organization (Admin / HR / Leader only).",
    description=(
        "Restricted to the Admin, HR and Leader roles. Feedback belonging to "
        "another organization is answered with 404. View-only."
    ),
    responses={
        403: {"description": "The caller is not an Admin, HR or Leader."},
        404: {"description": "No such feedback in this organization."},
    },
)
def get_feedback(
    feedback_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return FeedbackService.get_feedback(db, current_user, feedback_id)


@router.patch(
    "/{feedback_id}/status",
    response_model=FeedbackStatusUpdateResponse,
    summary="Mark feedback Working or Resolved, and notify the submitter (Admin only).",
    description=(
        "Moves one feedback through the support workflow and emails the person "
        "who submitted it.\n\n"
        "**Admin only.** HR and Leader may read every submission through `GET "
        "/feedback` and are refused here with 403, as is any other role — this "
        "gate is enforced server-side and does not depend on the dashboard "
        "hiding the buttons.\n\n"
        "**The recipient is not a parameter.** The request body carries a "
        "status and nothing else; the address is resolved from the feedback "
        "row's own submitter, server-side. There is no field a caller could "
        "add to redirect the notification.\n\n"
        "Allowed transitions are `new → in_progress`, `new → resolved` and "
        "`in_progress → resolved`. Requesting the status the feedback is "
        "already in succeeds, changes nothing and sends nothing — which is "
        "what a double-click or a replayed request produces — and is reported "
        "by `notification_queued: false`. Reopening a resolved feedback is "
        "answered with 409.\n\n"
        "The notification is queued, not sent inline: this response reports "
        "whether the **status changed**, and mail delivery cannot alter it."
    ),
    responses={
        403: {"description": "The caller is not an Admin."},
        404: {"description": "No such feedback in this organization."},
        409: {"description": "That status transition is not allowed."},
        422: {"description": "`status` is not one of in_progress, resolved."},
    },
)
def update_feedback_status(
    feedback_id: int,
    update: FeedbackStatusUpdate,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return FeedbackService.update_status(
        db, current_user, feedback_id, update.status, background_tasks=background_tasks
    )

from typing import List, Optional
from urllib.parse import quote

from fastapi import (
    APIRouter, BackgroundTasks, Depends, File, Form, Path, Query, Response, UploadFile,
    status,
)
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
    FeedbackSubmissionRead,
)
from app.services.feedback import FeedbackService
from app.services.feedback_attachments import (
    FeedbackAttachmentService, max_count, max_total_bytes, read_upload_bounded,
    too_large_message, too_many_message,
)

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
    db: Session = Depends(get_db, scope="function"),
):
    return FeedbackService.submit_feedback(
        db, feedback_in, current_user, background_tasks=background_tasks
    )


@router.post(
    "/with-attachments",
    response_model=FeedbackSubmissionRead,
    status_code=status.HTTP_201_CREATED,
    summary="Submit a Feedback & Help message with optional attachments.",
    description=(
        "The same submission as `POST /feedback`, as `multipart/form-data`, with "
        "up to three image attachments (PNG, JPEG or WebP; 10 MB in total). "
        "`POST /feedback` is unchanged and remains the route for a message with "
        "no attachment.\n\n"
        "**Idempotent on `client_op`.** The desktop generates one per submission "
        "attempt and re-sends it on a retry; a repeat is answered with the "
        "original record and `duplicate: true`, creates nothing and uploads "
        "nothing. Everything is validated before anything is stored, and the "
        "feedback and its attachment rows commit together — a failure leaves "
        "no feedback, no rows and (best effort) no stored objects.\n\n"
        "As on `POST /feedback`, the submitter, organization and status come "
        "from the access token, and the Admin/HR notification is queued after "
        "the commit and cannot change this response."
    ),
    responses={
        413: {"description": "The attachments exceed the total size limit."},
        422: {"description": "Bad category/message/submission id, too many files, an empty file, a type that is not allowed, or content that is not the image its name says."},
        502: {"description": "Attachment storage is temporarily unavailable."},
        503: {"description": "Attachment storage is not configured on this server."},
    },
)
def submit_feedback_with_attachments(
    background_tasks: BackgroundTasks,
    category: str = Form(...),
    message: str = Form(...),
    client_op: str = Form(..., description="Client-generated key, 8-64 chars of A-Z a-z 0-9 _ -"),
    files: Optional[List[UploadFile]] = File(
        None, description="Zero to three image files, as repeated `files` parts."
    ),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db, scope="function"),
):
    # A plain `def`, so FastAPI runs it on the thread pool: the Drive upload
    # below is seconds of blocking I/O and must not stall the event loop. The
    # multipart body is already parsed; `file.file` is its spooled temp file.
    from fastapi import HTTPException

    uploads = []
    parts = files or []
    if len(parts) > max_count():
        raise HTTPException(status_code=422, detail=too_many_message())
    remaining = max_total_bytes()
    for part in parts:
        # Bounded read: a file over the budget is noticed after budget+1 bytes
        # instead of being loaded whole, and never reaches storage.
        content = read_upload_bounded(part.file, remaining)
        remaining -= len(content)
        if remaining < 0:
            raise HTTPException(status_code=413, detail=too_large_message())
        uploads.append((part.filename, part.content_type, content))

    return FeedbackAttachmentService.submit(
        db,
        category=category,
        message=message,
        client_op=client_op,
        uploads=uploads,
        current_user=current_user,
        background_tasks=background_tasks,
    )


@router.get(
    "/attachments/{attachment_id}/content",
    summary="Download or display one feedback attachment.",
    response_class=Response,
    description=(
        "Streams the file to a caller who may read the feedback it belongs to: "
        "Admin, HR and Leader inside the same organization, and the person who "
        "submitted it. Anyone else — another employee, another organization — "
        "gets 404, identical to an id that does not exist.\n\n"
        "The bytes are proxied, never linked: the storage object is private and "
        "no storage URL or credential is ever returned. Served with the type "
        "that was validated at upload, `nosniff`, and a locked-down CSP. With "
        "`download=true` the response is an attachment rather than inline."
    ),
    responses={
        404: {"description": "No such attachment, not yours to read, or its file is gone."},
        502: {"description": "Attachment storage is temporarily unavailable."},
    },
)
def get_feedback_attachment_content(
    attachment_id: int = Path(..., gt=0),
    download: bool = Query(False),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db, scope="function"),
):
    content, content_type, filename = FeedbackAttachmentService.get_content(
        db, attachment_id, current_user
    )
    ascii_name = "".join(
        ch if ch.isascii() and ch.isprintable() and ch not in '";\\' else "_" for ch in filename
    )
    disposition = "attachment" if download else "inline"
    return Response(
        content=content,
        media_type=content_type,
        headers={
            "Content-Disposition": (
                f"{disposition}; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"
            ),
            # The file is only ever what was validated; never let a browser
            # decide otherwise, and never let it run anything if it tried.
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; img-src 'self' data:; sandbox",
            # An attachment is private to its audience: cacheable by that
            # person's browser for a short while (a thumbnail grid re-opens
            # often) and by no shared cache.
            "Cache-Control": "private, max-age=300",
        },
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
    db: Session = Depends(get_db, scope="function"),
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
    db: Session = Depends(get_db, scope="function"),
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
    db: Session = Depends(get_db, scope="function"),
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
    db: Session = Depends(get_db, scope="function"),
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
    db: Session = Depends(get_db, scope="function"),
):
    return FeedbackService.update_status(
        db, current_user, feedback_id, update.status, background_tasks=background_tasks
    )

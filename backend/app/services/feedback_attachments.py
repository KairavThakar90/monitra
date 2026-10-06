"""feedback_attachments — optional files on a Feedback & Help submission.

Four responsibilities, in the order a file lives them:

1. **Validate.** An upload is untrusted input. The extension, the declared
   content type *and* the file's own bytes must all agree on one of the
   allowed image types, and the bytes must decode as that image. Nothing the
   client says — the file name least of all — decides where or how the file is
   stored.
2. **Store.** The bytes go to the private Google Drive the screenshots already
   use, under a **server-generated** name, before any database row exists.
3. **Record.** The feedback row and every attachment row are written in one
   transaction (`FeedbackRepository.create_with_attachments`). If that fails,
   the objects this call created are deleted; if even that fails, the ids are
   logged and the orphan sweep (`scripts/feedback_attachment_sweep.py`)
   reclaims them.
4. **Serve.** One authorised read endpoint. Bytes are proxied through the
   backend, never linked: a Drive URL would outlive the permission check and
   would need the folder made public.

Why this order. Validation is complete for *every* file before the first byte
is stored, so a request with one bad file among three stores nothing. Storage
comes before the database write because a row pointing at a missing object is
worse than an object with no row: the first is a broken page, the second is
reclaimable.

Supported types are images only (PNG, JPEG, WebP). PDF was evaluated and left
out: this feature exists so someone can *show* what went wrong, an image does
that, and a PDF is an active-content container whose safety cannot be proven by
a signature check the way an image's can be decoded. Adding it later is one
entry in `ALLOWED_ATTACHMENT_TYPES` plus a validator for it.
"""
from __future__ import annotations

import io
import logging
import re
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Dict, List, Optional, Sequence, Tuple

from fastapi import HTTPException, status
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import end_transaction
from app.core.permissions import resolve_role_alias
from app.core.validation import InputValidationError
from app.core.validation.validators import validate_idempotency_key
from app.models.feedback_attachment import FeedbackAttachment
from app.models.feedback_request import FeedbackRequest
from app.models.user import User
from app.repositories.feedback import FeedbackRepository
from app.repositories.feedback_attachment import FeedbackAttachmentRepository
from app.schemas.feedback import FeedbackCreate, FeedbackStatus
from app.services.google_drive_service import (
    GoogleDriveError, GoogleDriveFileNotFound, GoogleDriveNotAccessible, drive_service,
)

logger = logging.getLogger("uvicorn.error")

# ── The allow-list ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class AttachmentType:
    content_type: str
    #: Extensions (lower case, no dot) that may name this type. The first is the
    #: canonical one used in the stored object's name.
    extensions: Tuple[str, ...]
    #: What Pillow must report after decoding the bytes.
    pil_format: str


#: The one table every rule below reads. Adding a type means adding a row here
#: and a signature in `sniff_content_type`; nothing else needs to know.
ALLOWED_ATTACHMENT_TYPES: Dict[str, AttachmentType] = {
    "image/png": AttachmentType("image/png", ("png",), "PNG"),
    "image/jpeg": AttachmentType("image/jpeg", ("jpg", "jpeg"), "JPEG"),
    "image/webp": AttachmentType("image/webp", ("webp",), "WEBP"),
}

_TYPE_BY_EXTENSION: Dict[str, AttachmentType] = {
    ext: kind for kind in ALLOWED_ATTACHMENT_TYPES.values() for ext in kind.extensions
}

#: Types a client may *declare* without being wrong about it. A generic
#: octet-stream is tolerated because some clients cannot guess; the sniffed
#: type is what is stored either way.
_NEUTRAL_DECLARED_TYPES = frozenset({"application/octet-stream", "binary/octet-stream"})

#: A decompression-bomb ceiling: a tiny PNG can declare a gigapixel canvas.
#: 50 megapixels is a 8000x6000 photograph, far beyond any screenshot.
MAX_IMAGE_PIXELS = 50_000_000

MAX_DISPLAY_FILENAME_LENGTH = 120

UNSUPPORTED_TYPE_MESSAGE = "Unsupported attachment type. Please attach a PNG, JPG or WEBP image."
CONTENT_MISMATCH_MESSAGE = "The file's content does not match its type. Please attach a valid image."
TOO_LARGE_MESSAGE = "Attachment is too large. Maximum allowed size is 10 MB."
STORAGE_UNAVAILABLE_MESSAGE = "Unable to upload the attachment. Please try again."
SAVE_FAILED_MESSAGE = "Your feedback could not be saved. Please try again."

_CLIENT_OP_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


def max_total_bytes() -> int:
    return int(settings.FEEDBACK_ATTACHMENT_MAX_TOTAL_BYTES)


def max_count() -> int:
    return int(settings.FEEDBACK_ATTACHMENT_MAX_COUNT)


def too_many_message() -> str:
    return f"Maximum {max_count()} attachments allowed."


def too_large_message() -> str:
    # Rendered from the setting, so a deployment that raises the limit does not
    # keep telling people "10 MB". The default reads exactly like the constant.
    megabytes = max_total_bytes() / (1024 * 1024)
    shown = f"{megabytes:g}"
    return f"Attachment is too large. Maximum allowed size is {shown} MB."


# ── Content checks ────────────────────────────────────────────────────────────


def sniff_content_type(content: bytes) -> Optional[str]:
    """The allowed type the bytes actually are, from their signature, or None."""
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    return None


_FORBIDDEN_NAME_CHARACTERS = re.compile(r'[\\/:*?"<>|]')


def sanitize_display_filename(raw: Optional[str]) -> str:
    """Reduce an uploader's file name to something safe to *display*.

    Display only. It is never used for a storage path or an object name — those
    are generated server-side — so this is not what stands between a hostile
    name and the filesystem. It exists so the dashboard and the response
    headers never carry a path, a control character or an unbounded string.
    """
    text = unicodedata.normalize("NFC", str(raw or ""))
    # Whatever directory the client's OS prefixed is not part of the name.
    text = text.replace("\\", "/").rsplit("/", 1)[-1]
    text = "".join(ch for ch in text if ch.isprintable())
    text = _FORBIDDEN_NAME_CHARACTERS.sub("", text)
    text = " ".join(text.split()).lstrip(". ").strip()
    if not text:
        return "attachment"
    if len(text) > MAX_DISPLAY_FILENAME_LENGTH:
        suffix = PurePosixPath(text).suffix[:12]
        stem = text[: MAX_DISPLAY_FILENAME_LENGTH - len(suffix)].rstrip(". ")
        text = f"{stem}{suffix}"
    return text


@dataclass(frozen=True)
class ValidatedAttachment:
    display_name: str
    content_type: str
    extension: str
    size: int
    content: bytes


def _reject(code: int, detail: str, reason: str, **context) -> HTTPException:
    """Log a rejection (never the content) and build the response."""
    logger.warning(
        "FEEDBACK_ATTACHMENT_REJECTED reason=%s status=%s %s",
        reason, code, " ".join(f"{k}={v}" for k, v in context.items()),
    )
    return HTTPException(status_code=code, detail=detail)


def _decode_check(content: bytes, expected: AttachmentType) -> None:
    """Prove the bytes decode as the image they claim to be, within bounds."""
    try:
        from PIL import Image  # type: ignore
    except ImportError:
        # The signature has already been checked. Refusing every upload because
        # an optional dependency is missing would be a worse outcome; Pillow is
        # in requirements.txt, so this is a mis-built deployment, and it says so.
        logger.warning("Pillow is not installed; attachment decode check skipped")
        return
    try:
        with Image.open(io.BytesIO(content)) as image:
            if image.format != expected.pil_format:
                raise ValueError("format mismatch")
            width, height = image.size
            if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
                raise ValueError("unreasonable dimensions")
            # Walks the file's structure and checksums without decoding every
            # pixel, so it is cheap and still rejects a truncated or forged one.
            image.verify()
    except Exception as exc:  # noqa: BLE001 - any decode failure is the same refusal
        raise _reject(
            status.HTTP_422_UNPROCESSABLE_ENTITY, CONTENT_MISMATCH_MESSAGE,
            "decode_failed", error=type(exc).__name__,
        )


def validate_upload(
    filename: Optional[str], declared_content_type: Optional[str], content: bytes,
) -> ValidatedAttachment:
    """Validate one uploaded file. Raises `HTTPException` (422) on any refusal.

    Three independent signals must agree: the extension, the declared content
    type, and the bytes themselves. The bytes are the authority — a renamed
    executable keeps its signature — and the other two exist to refuse a client
    that is confused or lying before the bytes are even trusted.
    """
    display = sanitize_display_filename(filename)
    if not content:
        raise _reject(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "The attachment is empty. Please choose a different file.", "empty",
        )

    extension = PurePosixPath(display).suffix.lstrip(".").lower()
    by_extension = _TYPE_BY_EXTENSION.get(extension)
    if by_extension is None:
        raise _reject(
            status.HTTP_422_UNPROCESSABLE_ENTITY, UNSUPPORTED_TYPE_MESSAGE,
            "extension_not_allowed", extension=extension[:12] or "none",
        )

    declared = (declared_content_type or "").split(";")[0].strip().lower()
    if declared and declared not in _NEUTRAL_DECLARED_TYPES:
        if declared not in ALLOWED_ATTACHMENT_TYPES:
            raise _reject(
                status.HTTP_422_UNPROCESSABLE_ENTITY, UNSUPPORTED_TYPE_MESSAGE,
                "declared_type_not_allowed", declared=declared[:60],
            )
        if declared != by_extension.content_type:
            raise _reject(
                status.HTTP_422_UNPROCESSABLE_ENTITY, CONTENT_MISMATCH_MESSAGE,
                "declared_type_vs_extension", declared=declared, extension=extension,
            )

    sniffed = sniff_content_type(content)
    if sniffed is None or sniffed != by_extension.content_type:
        raise _reject(
            status.HTTP_422_UNPROCESSABLE_ENTITY, CONTENT_MISMATCH_MESSAGE,
            "signature_mismatch", extension=extension, sniffed=sniffed or "unknown",
        )

    _decode_check(content, by_extension)
    return ValidatedAttachment(
        display_name=display,
        content_type=by_extension.content_type,
        extension=by_extension.extensions[0],
        size=len(content),
        content=content,
    )


def validate_client_op(value: Optional[str]) -> str:
    """The submission's idempotency key. It becomes part of an object name, so
    it is held to a narrower alphabet than the shared key rule allows."""
    try:
        key = validate_idempotency_key(value, field_label="Submission id", required=True)
    except InputValidationError as exc:
        raise _reject(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc), "bad_client_op")
    if not key or not _CLIENT_OP_PATTERN.match(key):
        raise _reject(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Submission id has an unsupported format.", "bad_client_op",
        )
    return key


def read_upload_bounded(file_obj, budget: int) -> bytes:
    """Read at most `budget + 1` bytes, so an oversized file is noticed without
    being loaded whole. The caller treats `len > budget` as "too large"."""
    return file_obj.read(budget + 1)


# ── Service ───────────────────────────────────────────────────────────────────


class FeedbackAttachmentService:
    @staticmethod
    def _to_read(row: FeedbackAttachment) -> dict:
        return {
            "id": row.id,
            "original_filename": row.original_filename,
            "content_type": row.content_type,
            "file_size": row.file_size,
            "created_at": row.created_at,
        }

    @staticmethod
    def _submission(
        feedback: FeedbackRequest, rows: Sequence[FeedbackAttachment], duplicate: bool
    ) -> dict:
        return {
            "id": feedback.id,
            "category": feedback.category,
            "message": feedback.message,
            "status": feedback.status,
            "created_at": feedback.created_at,
            "attachments": [FeedbackAttachmentService._to_read(r) for r in rows],
            "duplicate": duplicate,
        }

    # ── Submit ────────────────────────────────────────────────────────────────

    @staticmethod
    def submit(
        db: Session,
        *,
        category: str,
        message: str,
        client_op: str,
        uploads: Sequence[Tuple[Optional[str], Optional[str], bytes]],
        current_user: User,
        background_tasks=None,
    ) -> dict:
        """Store one feedback together with its attachments, exactly once.

        `uploads` is ``[(filename, declared_content_type, bytes), ...]``, already
        read by the route with the size budget applied.
        """
        # The message and category go through the same schema the JSON route
        # uses, so the two routes cannot disagree about what is acceptable.
        try:
            payload = FeedbackCreate(category=category, message=message)
        except ValidationError as exc:
            first = exc.errors()[0] if exc.errors() else {}
            detail = str(first.get("msg", "")).removeprefix("Value error, ") or "Invalid feedback."
            if first.get("loc") == ("category",):
                detail = "Please select a valid category."
            raise _reject(status.HTTP_422_UNPROCESSABLE_ENTITY, detail, "invalid_form")

        if not current_user.organization_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Your account is not associated with an organization.",
            )

        key = validate_client_op(client_op)
        user_id = current_user.id
        organization_id = current_user.organization_id

        # Idempotency first, before any validation work or storage: a retry of a
        # submission that already landed costs one indexed lookup.
        existing = FeedbackRepository.get_by_client_op(db, user_id=user_id, client_op=key)
        if existing is not None:
            return FeedbackAttachmentService._existing(db, existing)

        if len(uploads) > max_count():
            raise _reject(
                status.HTTP_422_UNPROCESSABLE_ENTITY, too_many_message(),
                "too_many", count=len(uploads), user=user_id,
            )
        total = sum(len(content) for _, _, content in uploads)
        if total > max_total_bytes():
            raise _reject(
                status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, too_large_message(),
                "too_large", bytes_read=total, user=user_id,
            )

        # Everything is validated before anything is stored.
        validated: List[ValidatedAttachment] = [
            validate_upload(name, declared, content) for name, declared, content in uploads
        ]

        if validated and not drive_service.configured:
            logger.error(
                "FEEDBACK_ATTACHMENT_STORAGE_FAILED stage=config user=%s reason=%s",
                user_id, drive_service.unconfigured_reason(),
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=STORAGE_UNAVAILABLE_MESSAGE,
            )

        # The Drive calls below can take seconds each; the connection goes back
        # to the pool first instead of sitting idle in a transaction.
        end_transaction(db)

        stored = FeedbackAttachmentService._store(validated, user_id, key)
        fresh_ids = [item["file_id"] for item in stored if not item["reused"]]

        try:
            feedback, rows = FeedbackRepository.create_with_attachments(
                db,
                organization_id=organization_id,
                user_id=user_id,
                category=payload.category.value,
                message=payload.message,
                status=FeedbackStatus.new.value,
                client_op=key,
                attachments=[
                    {
                        "position": index,
                        "original_filename": item["validated"].display_name,
                        "content_type": item["validated"].content_type,
                        "file_size": item["validated"].size,
                        "file_name": item["file_name"],
                        "file_path": item["path"],
                        "google_drive_file_id": item["file_id"],
                        "google_drive_folder_id": item["folder_id"],
                    }
                    for index, item in enumerate(stored, start=1)
                ],
            )
        except IntegrityError:
            # Two attempts at the same submission raced past the lookup above.
            # The unique index let exactly one commit; this one returns it.
            db.rollback()
            winner = FeedbackRepository.get_by_client_op(db, user_id=user_id, client_op=key)
            if winner is None:
                FeedbackAttachmentService._discard(fresh_ids, user_id, key, "integrity_error")
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=SAVE_FAILED_MESSAGE,
                )
            kept = {
                a.google_drive_file_id
                for a in FeedbackAttachmentRepository.list_for_feedback_ids(db, [winner.id]).get(winner.id, [])
            }
            FeedbackAttachmentService._discard(
                [f for f in fresh_ids if f not in kept], user_id, key, "concurrent_duplicate",
            )
            logger.info(
                "FEEDBACK_DUPLICATE user=%s feedback=%s reason=concurrent_submission", user_id, winner.id,
            )
            return FeedbackAttachmentService._existing(db, winner)
        except Exception:  # noqa: BLE001
            db.rollback()
            logger.exception(
                "FEEDBACK_ATTACHMENT_DB_WRITE_FAILED user=%s client_op=%s objects=%d",
                user_id, key, len(stored),
            )
            FeedbackAttachmentService._discard(fresh_ids, user_id, key, "db_write_failed")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=SAVE_FAILED_MESSAGE,
            )

        logger.info(
            "FEEDBACK_ATTACHMENTS_STORED feedback=%s user=%s count=%d bytes=%d",
            feedback.id, user_id, len(rows), sum(r.file_size for r in rows),
        )

        # The notification is queued after the commit and cannot affect it,
        # exactly as on the JSON route.
        from app.services.email import deliver_in_background, queue_feedback_notification

        notification_id = queue_feedback_notification(
            db, feedback, current_user, attachment_count=len(rows),
        )
        if notification_id is not None and background_tasks is not None:
            background_tasks.add_task(deliver_in_background, notification_id)

        return FeedbackAttachmentService._submission(feedback, rows, duplicate=False)

    @staticmethod
    def _existing(db: Session, feedback: FeedbackRequest) -> dict:
        rows = FeedbackAttachmentRepository.list_for_feedback_ids(db, [feedback.id]).get(feedback.id, [])
        logger.info("FEEDBACK_DUPLICATE user=%s feedback=%s reason=already_stored", feedback.user_id, feedback.id)
        return FeedbackAttachmentService._submission(feedback, rows, duplicate=True)

    @staticmethod
    def _store(validated: Sequence[ValidatedAttachment], user_id: int, client_op: str) -> List[dict]:
        """Put every file in Drive; on any failure remove what this call added."""
        stored: List[dict] = []
        if not validated:
            return stored
        started = time.monotonic()
        try:
            folder_id, logical = drive_service.ensure_feedback_folder(datetime.now(timezone.utc).date())
            for index, item in enumerate(validated, start=1):
                # Server-generated and deterministic: built from validated
                # identifiers only, so a retry finds (and reuses) the object a
                # failed attempt left behind instead of storing it twice.
                file_name = f"u{user_id}_{client_op}_{index}.{item.extension}"
                file_id, reused = drive_service.upload_file_idempotent(
                    folder_id=folder_id, file_name=file_name,
                    content=item.content, mime_type=item.content_type,
                )
                stored.append({
                    "validated": item, "file_name": file_name, "file_id": file_id,
                    "reused": reused, "folder_id": folder_id, "path": f"{logical}/{file_name}",
                })
        except Exception as exc:  # noqa: BLE001
            drive_service.invalidate_folder_cache()
            FeedbackAttachmentService._discard(
                [s["file_id"] for s in stored if not s["reused"]], user_id, client_op, "storage_failed",
            )
            misconfigured = isinstance(exc, GoogleDriveNotAccessible)
            logger.error(
                "FEEDBACK_ATTACHMENT_STORAGE_FAILED stage=drive user=%s client_op=%s reason=%s detail=%s",
                user_id, client_op, "misconfigured" if misconfigured else type(exc).__name__, exc,
                exc_info=not isinstance(exc, GoogleDriveError),
            )
            raise HTTPException(
                status_code=(
                    status.HTTP_503_SERVICE_UNAVAILABLE if misconfigured
                    else status.HTTP_502_BAD_GATEWAY
                ),
                detail=STORAGE_UNAVAILABLE_MESSAGE,
            )
        logger.info(
            "FEEDBACK_ATTACHMENT_UPLOADED user=%s client_op=%s files=%d reused=%d elapsed_ms=%d",
            user_id, client_op, len(stored), sum(1 for s in stored if s["reused"]),
            int((time.monotonic() - started) * 1000),
        )
        return stored

    @staticmethod
    def _discard(file_ids: Sequence[str], user_id: int, client_op: str, why: str) -> None:
        """Best-effort removal of objects that no row will ever point at.

        Uses the strict delete so a removal that could not be confirmed is
        *known*: its id is logged under a stable tag so it is findable, and the
        sweep reclaims anything older than its threshold regardless. An object
        that is already gone counts as removed.
        """
        for file_id in file_ids:
            try:
                drive_service.delete_file_strict(file_id)
            except GoogleDriveFileNotFound:
                continue
            except Exception:  # noqa: BLE001 - already unwinding a failure
                logger.error(
                    "FEEDBACK_ATTACHMENT_ORPHAN user=%s client_op=%s drive_file=%s why=%s",
                    user_id, client_op, file_id, why,
                )

    # ── Read ──────────────────────────────────────────────────────────────────

    @staticmethod
    def _may_read(feedback: FeedbackRequest, current_user: User) -> bool:
        """The same audience that may read the feedback itself, no wider.

        Admin, HR and Leader (`FEEDBACK_VIEW_ALL_ROLES`) within the feedback's
        own organisation, and the person who submitted it. A manager or another
        employee is neither. An attachment is never more visible than the
        message it illustrates.
        """
        from app.services.feedback import FEEDBACK_VIEW_ALL_ROLES

        if not current_user.organization_id or feedback.organization_id != current_user.organization_id:
            return False
        if feedback.user_id == current_user.id:
            return True
        role = resolve_role_alias((current_user.role_name or "").strip().lower())
        return role in FEEDBACK_VIEW_ALL_ROLES

    @staticmethod
    def get_content(
        db: Session, attachment_id: int, current_user: User
    ) -> Tuple[bytes, str, str]:
        """Read one attachment back for an authorised caller.

        :return: `(content, content_type, display_filename)`.
        """
        found = FeedbackAttachmentRepository.get_with_feedback(db, attachment_id)
        if found is None or not FeedbackAttachmentService._may_read(found[1], current_user):
            # Another user's attachment and a non-existent one answer
            # identically: a 403 on a guessed id would confirm it exists.
            logger.warning(
                "FEEDBACK_ATTACHMENT_ACCESS_DENIED attachment=%s user=%s role=%s found=%s",
                attachment_id, current_user.id, current_user.role_name, found is not None,
            )
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Attachment not found.")
        attachment = found[0]
        file_id = attachment.google_drive_file_id
        content_type = attachment.content_type
        filename = attachment.original_filename
        end_transaction(db)

        try:
            content = drive_service.download_file(file_id)
        except Exception as exc:  # noqa: BLE001
            code = getattr(getattr(exc, "resp", None), "status", None) or getattr(exc, "status_code", None)
            if code in (404, 410):
                logger.error("FEEDBACK_ATTACHMENT_MISSING attachment=%s drive_file=%s", attachment_id, file_id)
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Attachment unavailable.")
            logger.error(
                "FEEDBACK_ATTACHMENT_READ_FAILED attachment=%s drive_file=%s detail=%s",
                attachment_id, file_id, type(exc).__name__,
                exc_info=not isinstance(exc, GoogleDriveError),
            )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Attachment storage is temporarily unavailable.",
            )

        # Defence in depth: what is served must still be what was validated. An
        # object swapped out of band is refused rather than relayed.
        if sniff_content_type(content) != content_type:
            logger.error(
                "FEEDBACK_ATTACHMENT_CONTENT_CHANGED attachment=%s drive_file=%s", attachment_id, file_id,
            )
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Attachment unavailable.")
        return content, content_type, filename

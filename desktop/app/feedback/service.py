"""
Backend call for Feedback & Help.

One method, one endpoint, in the same shape as the other services in `app/`.
It carries the request and hands the answer back; it decides nothing about who
the feedback belongs to. The submitting user, their organisation and the
initial status are all determined server-side from the access token, so this
client sends only what the user actually typed.

Every failure is translated into an `ApiError` carrying a sentence that can be
shown to a person. Raw httpx errors, tracebacks and backend internals never
reach the dialog.
"""
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.api.client import ApiClient, TIMEOUT_NORMAL, TIMEOUT_SLOW
from app.api.exceptions import (
    ApiConnectionError, ApiError, ApiHttpError, ApiTimeoutError, error_detail,
)
from core.logging_setup import get_logger

log = get_logger("feedback.api")

#: Mirrors the backend's MESSAGE_MAX_LENGTH. Validated here as well so an
#: over-long message is caught before a request is made, not after.
MESSAGE_MAX_LENGTH = 5000

#: The six categories the backend accepts, as wire values.
FEEDBACK_CATEGORIES = (
    "suggestion",
    "report_a_problem",
    "general_feedback",
    "need_help",
    "account_login_issue",
    "other",
)


# ── Attachments ──────────────────────────────────────────────────────────────
#
# One definition, shared by the dialog (which checks a file when it is chosen)
# and the service (which checks the bytes it is about to send). The backend is
# authoritative and applies the same rules; these exist so an obviously
# unacceptable file is refused before anything leaves the machine.

#: At most this many files per submission.
MAX_ATTACHMENTS = 3

#: The cap is on the *total* of all files in one submission.
MAX_TOTAL_ATTACHMENT_BYTES = 10 * 1024 * 1024

#: extension (lower case, no dot) -> (kind, MIME type, label shown to the user).
#: Anything not listed here -- .exe, .bat, .cmd, .msi, .dll, .ps1, .js,
#: archives, documents -- is refused by the same allow-list rule.
ALLOWED_ATTACHMENT_TYPES: Dict[str, Tuple[str, str, str]] = {
    "png": ("png", "image/png", "PNG image"),
    "jpg": ("jpeg", "image/jpeg", "JPEG image"),
    "jpeg": ("jpeg", "image/jpeg", "JPEG image"),
    "webp": ("webp", "image/webp", "WEBP image"),
}

#: How many leading bytes identify a file's real type.
HEADER_BYTES = 12

#: The idempotency key the backend accepts.
_CLIENT_OP_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,64}$")

MSG_ATTACHMENT_TYPE = (
    "That file type isn't supported. Please attach a PNG, JPG or WEBP image."
)
MSG_ATTACHMENT_TOO_LARGE = "Attachment is too large. Maximum allowed size is 10 MB."
MSG_ATTACHMENT_COUNT = "Maximum 3 attachments allowed."
MSG_ATTACHMENT_UNREADABLE = "That file couldn't be read. Please choose another."
MSG_ATTACHMENT_DUPLICATE = "That file is already attached."
MSG_ATTACHMENT_VANISHED = (
    "An attachment could no longer be read. Please remove it and try again."
)
MSG_GENERIC_FAILURE = (
    "Something went wrong while submitting your feedback. Please try again."
)


class FeedbackAttachmentError(ApiError):
    """An attachment was refused. `str(exc)` is a sentence for the user."""


@dataclass(frozen=True)
class AttachmentInfo:
    """What the dialog knows about one chosen file. No bytes are held."""

    path: str
    name: str
    size: int
    mtime_ns: int
    extension: str
    kind: str
    content_type: str
    type_label: str


def format_file_size(size: int) -> str:
    """'512 B', '3.4 KB', '1.2 MB' -- one decimal above a kilobyte."""
    size = max(0, int(size))
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def detect_image_type(header: bytes) -> Optional[str]:
    """'png' / 'jpeg' / 'webp' from the leading bytes, else None."""
    if header[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if header[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "webp"
    return None


def attachment_extension(path: str) -> str:
    """The lower-case extension without its dot ('' when there is none)."""
    return os.path.splitext(path)[1].lstrip(".").lower()


def validate_attachment_file(path: str) -> AttachmentInfo:
    """Check one file the way the dialog does when it is chosen.

    Cheap on purpose -- it runs on the GUI thread: one `stat`, the extension,
    and the first twelve bytes. It never reads the whole file.

    Raises `FeedbackAttachmentError` (with a sentence for the user) when the
    file is not a readable, non-empty, regular PNG/JPEG/WEBP of at most 10 MB.
    """
    try:
        if not os.path.isfile(path):  # a directory, or a link to nowhere
            raise FeedbackAttachmentError(MSG_ATTACHMENT_UNREADABLE)
        stat = os.stat(path)
    except OSError:
        raise FeedbackAttachmentError(MSG_ATTACHMENT_UNREADABLE)

    extension = attachment_extension(path)
    allowed = ALLOWED_ATTACHMENT_TYPES.get(extension)
    if allowed is None:
        raise FeedbackAttachmentError(MSG_ATTACHMENT_TYPE)
    if stat.st_size <= 0:
        raise FeedbackAttachmentError(MSG_ATTACHMENT_UNREADABLE)
    if stat.st_size > MAX_TOTAL_ATTACHMENT_BYTES:
        raise FeedbackAttachmentError(MSG_ATTACHMENT_TOO_LARGE)

    try:
        with open(path, "rb") as handle:
            header = handle.read(HEADER_BYTES)
    except OSError:
        raise FeedbackAttachmentError(MSG_ATTACHMENT_UNREADABLE)
    kind, content_type, label = allowed
    if detect_image_type(header) != kind:
        raise FeedbackAttachmentError(MSG_ATTACHMENT_TYPE)

    return AttachmentInfo(
        path=path,
        name=os.path.basename(path),
        size=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
        extension=extension,
        kind=kind,
        content_type=content_type,
        type_label=label,
    )


class FeedbackApiService:
    """Client for the backend's feedback endpoints."""

    def __init__(self, api_client: ApiClient) -> None:
        self.api_client = api_client

    def submit_feedback(self, category: str, message: str) -> Dict[str, Any]:
        """Submit one feedback message and return the created record.

        Returns the backend's payload::

            {"id": int, "category": str, "message": str,
             "status": "new", "created_at": str}

        Raises `ApiError` with a user-presentable message on every failure.
        """
        text = (message or "").strip()
        if not text:
            raise ApiError("Please enter a message before submitting.")
        if len(text) > MESSAGE_MAX_LENGTH:
            raise ApiError(
                f"Your message is too long. Please keep it under "
                f"{MESSAGE_MAX_LENGTH} characters."
            )
        if category not in FEEDBACK_CATEGORIES:
            raise ApiError("Please select a category.")

        try:
            response = self.api_client.post(
                "/feedback",
                json_data={"category": category, "message": text},
                timeout=TIMEOUT_NORMAL,
            )
            return response.json()
        except ApiHttpError as exc:
            if exc.status_code in (401, 403):
                raise ApiError(
                    "Your session has expired. Please sign in again and retry.",
                    status_code=exc.status_code,
                )
            if exc.status_code == 422:
                raise ApiError(
                    error_detail(
                        exc.response_body,
                        "Please check your message and try again.",
                    ),
                    status_code=422,
                )
            log.warning("feedback submission failed: HTTP %s", exc.status_code)
            raise ApiError(
                "Something went wrong while submitting your feedback. Please try again.",
                status_code=exc.status_code,
            )
        except ApiTimeoutError:
            raise ApiError(
                "Submitting your feedback timed out. Please try again."
            )
        except ApiConnectionError:
            raise ApiError(
                "Unable to submit feedback. Please check your internet "
                "connection and try again."
            )
        except ApiError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("feedback submission failed unexpectedly", exc_info=True)
            raise ApiError(
                "Something went wrong while submitting your feedback. Please try again."
            )

    def submit_feedback_with_attachments(
        self,
        category: str,
        message: str,
        attachment_paths: Sequence[str],
        client_op: str,
    ) -> Dict[str, Any]:
        """Submit feedback with up to three image attachments.

        Runs on the task pool (never the GUI thread): it reads the files here,
        re-checks their real type and the total size from the bytes actually
        read, and sends one multipart request. `client_op` is the idempotency
        key -- the backend answers a repeat with the record it already stored
        and `duplicate: true`, which is a success.

        Raises `ApiError` with a user-presentable message on every failure.
        """
        text = (message or "").strip()
        if not text:
            raise ApiError("Please enter a message before submitting.")
        if len(text) > MESSAGE_MAX_LENGTH:
            raise ApiError(
                f"Your message is too long. Please keep it under "
                f"{MESSAGE_MAX_LENGTH} characters."
            )
        if category not in FEEDBACK_CATEGORIES:
            raise ApiError("Please select a category.")
        if not isinstance(client_op, str) or not _CLIENT_OP_PATTERN.match(client_op):
            raise ApiError(MSG_GENERIC_FAILURE)
        paths = list(attachment_paths or [])
        if len(paths) > MAX_ATTACHMENTS:
            raise FeedbackAttachmentError(MSG_ATTACHMENT_COUNT)

        parts = self._read_attachments(paths)

        try:
            response = self.api_client.post_multipart(
                "/feedback/with-attachments",
                files=[("files", part) for part in parts],
                data={"category": category, "message": text, "client_op": client_op},
                timeout=TIMEOUT_SLOW,
            )
            # `duplicate: true` means this client_op was already stored (a
            # retry after a lost reply). Either way the feedback is in.
            return response.json()
        except ApiHttpError as exc:
            status = exc.status_code
            if status == 413:
                raise ApiError(MSG_ATTACHMENT_TOO_LARGE, status_code=413)
            if status == 422:
                raise ApiError(
                    error_detail(
                        exc.response_body,
                        "Please check your message and attachment and try again.",
                    ),
                    status_code=422,
                )
            if status in (401, 403):
                raise ApiError(
                    "Your session has expired. Please sign in again and retry.",
                    status_code=status,
                )
            log.warning("feedback attachment submission failed: HTTP %s", status)
            if status in (502, 503):
                raise ApiError(
                    "Unable to upload the attachment. Please try again.",
                    status_code=status,
                )
            raise ApiError(MSG_GENERIC_FAILURE, status_code=status)
        except ApiTimeoutError:
            log.warning("feedback attachment submission timed out")
            raise ApiError("Uploading your attachment timed out. Please try again.")
        except ApiConnectionError:
            log.warning("feedback attachment submission: connection error")
            raise ApiError(
                "Unable to submit feedback. Please check your internet "
                "connection and try again."
            )
        except ApiError:
            raise
        except Exception:  # noqa: BLE001
            log.warning("feedback attachment submission failed unexpectedly", exc_info=True)
            raise ApiError(MSG_GENERIC_FAILURE)

    @staticmethod
    def _read_attachments(paths: List[str]) -> List[Tuple[str, bytes, str]]:
        """Read each file and return `(filename, bytes, mime)` parts.

        The rules are re-applied to what was actually read: the file may have
        changed, or vanished, since the user chose it. Reading is bounded by
        the budget that is left, so a file that grew to gigabytes is never
        pulled into memory.
        """
        parts: List[Tuple[str, bytes, str]] = []
        total = 0
        for path in paths:
            allowed = ALLOWED_ATTACHMENT_TYPES.get(attachment_extension(path))
            if allowed is None:
                raise FeedbackAttachmentError(MSG_ATTACHMENT_TYPE)
            kind, content_type, _label = allowed
            budget = MAX_TOTAL_ATTACHMENT_BYTES - total
            try:
                with open(path, "rb") as handle:
                    data = handle.read(budget + 1)
            except OSError:
                log.warning("feedback attachment could not be read: %s", os.path.basename(path))
                raise FeedbackAttachmentError(MSG_ATTACHMENT_VANISHED)
            if len(data) > budget:
                raise FeedbackAttachmentError(MSG_ATTACHMENT_TOO_LARGE)
            if not data:
                raise FeedbackAttachmentError(MSG_ATTACHMENT_UNREADABLE)
            if detect_image_type(data[:HEADER_BYTES]) != kind:
                raise FeedbackAttachmentError(MSG_ATTACHMENT_TYPE)
            total += len(data)
            parts.append((os.path.basename(path), data, content_type))
        return parts

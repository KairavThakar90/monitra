"""time_entry_screenshot — Storing, serving and grouping desktop screenshots.

Three responsibilities, in the order the product uses them:

1. **Upload.** Validate the image, authorise it against the *authenticated*
   session rather than against anything the request body claims, put the bytes
   in Google Drive under Year/Month/User/Date, and write the metadata row —
   idempotently on the client's own `client_screenshot_id`.
2. **View.** Stream one screenshot back to a caller who is allowed to see it.
   The bytes are proxied, never linked: a Drive URL would outlive the
   permission check and would require the folder to be public.
3. **Timeline.** Group screenshots and activity into the same fixed windows the
   desktop captures against, so a window's activity percentage describes that
   window and nothing else.
4. **Delete.** Destroy one capture — the Drive object first, then the metadata
   row — for an administrator or HR. This is the only destructive operation in
   the module, and the only one that is not available to the person whose day
   the screenshot describes.
"""
from __future__ import annotations

import logging
from datetime import date as date_type, datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from fastapi import HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.time_format import ist_day_end_utc, ist_day_start_utc, ist_today, to_ist
from app.models.time_entry import TimeEntry
from app.models.time_entry_screenshot import TimeEntryScreenshot
from app.models.user import User
from app.repositories.time_entry import TimeEntryRepository
from app.repositories.time_entry_screenshot import TimeEntryScreenshotRepository
from app.schemas.time_entry_screenshot import TimeEntryScreenshotCreate
from app.services.google_drive_service import (
    GoogleDriveError, GoogleDriveFileNotFound, GoogleDriveNotAccessible,
    drive_service,
)
from app.services.member_scope import visible_member_ids

logger = logging.getLogger(__name__)

#: The only image format the desktop produces and the only one accepted.
ALLOWED_MIME_TYPES = {"image/webp"}

#: WebP's container signature: "RIFF" .... "WEBP". Checked against the actual
#: bytes rather than trusting the multipart content type, which the client
#: chooses and can be wrong about — by mistake or otherwise.
_RIFF = b"RIFF"
_WEBP = b"WEBP"


def _expected_dimensions() -> int:
    """The edge length of a single-display capture's square canvas."""
    return 1000


#: Hard ceiling on either edge of an accepted screenshot. A merged
#: multi-display image is wider than the single-display square by design, and
#: the desktop caps its own long edge at 4000 (see the client's
#: `MAX_CANVAS_LONG_EDGE`); this is the server's independent bound, because the
#: backend validates everything even when the client already did.
MAX_IMAGE_EDGE = 4000

#: Widest aspect ratio accepted, long edge over short. Eight 16:9 monitors in a
#: row reach about 14:1; beyond that the image is not a desk, and the rule is
#: here so a malformed or hostile upload cannot claim to be one.
MAX_ASPECT_RATIO = 20.0


def _validate_geometry(width: int, height: int) -> Optional[str]:
    """
    Why this geometry is unacceptable, or None if it is fine.

    The rule used to be "exactly 1000x1000", which was right while every
    capture was one letterboxed display. A merged multi-display capture keeps
    the desk's real aspect ratio — two 1080p monitors produce 2000x562 — so an
    exact-square test would reject every multi-monitor screenshot with a 422,
    and the desktop would park each one as permanently failed.

    What actually needs enforcing is that the image is a plausible screenshot
    and is bounded: the client's geometry is not trusted, it is checked. The
    single-display square stays explicitly valid so nothing about existing
    clients changes.
    """
    edge = _expected_dimensions()
    if width == edge and height == edge:
        return None
    if width <= 0 or height <= 0:
        return f"Screenshot has a non-positive dimension ({width}x{height})"
    if width > MAX_IMAGE_EDGE or height > MAX_IMAGE_EDGE:
        return (
            f"Screenshot exceeds the {MAX_IMAGE_EDGE}px limit on either edge; "
            f"received {width}x{height}"
        )
    if min(width, height) < edge // 4:
        return (
            f"Screenshot is too small to be readable; received {width}x{height}"
        )
    longest, shortest = max(width, height), min(width, height)
    if longest / shortest > MAX_ASPECT_RATIO:
        return (
            f"Screenshot aspect ratio {longest / shortest:.1f}:1 exceeds the "
            f"{MAX_ASPECT_RATIO:.0f}:1 limit; received {width}x{height}"
        )
    return None


def _as_utc(value: datetime) -> datetime:
    """A timestamp as an aware UTC one. Naive rows are stored in UTC."""
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _overlap_seconds(
    intervals: List[Tuple[datetime, datetime]], start: datetime, end: datetime
) -> int:
    """Seconds of the given tracked spans that fall inside ``[start, end)``.

    Entries are clipped to the span rather than counted whole, so a session
    that runs across midnight -- or across the edge of a ten-minute window --
    contributes only the part that actually belongs to it.
    """
    total = 0.0
    for began, ended in intervals:
        overlap = (min(_as_utc(ended), end) - max(_as_utc(began), start)).total_seconds()
        if overlap > 0:
            total += overlap
    return int(round(total))


def _build_windows(
    window_seconds: int,
    screenshots: List[TimeEntryScreenshot],
    activity: List[Tuple[datetime, int, int]],
    intervals: Optional[List[Tuple[datetime, datetime]]] = None,
) -> List[dict]:
    """Bucket one member's captures and activity into fixed windows.

    Shared by the single-member timeline and the all-members grid so the two
    cannot drift: a window's activity figure must mean the same thing on the
    member's own page as it does on the admin's.

    Windows are derived from the timestamps themselves -- ``epoch // length`` --
    rather than stored on the rows, which is what lets the window length be a
    configuration change instead of a migration. Only windows that contain a
    screenshot or measured activity are produced; an untracked hour makes no
    empty blocks to scroll past.

    ``intervals`` are that member's tracked spans; each window reports how many
    of its seconds they cover as ``tracked_seconds``. That is the window's
    *worked* time, which is a different fact from ``activity_measured_seconds``
    -- the part of it activity was actually sampled for.
    """
    buckets: Dict[int, dict] = {}

    def bucket(when: datetime) -> dict:
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        index = int(when.timestamp() // window_seconds)
        return buckets.setdefault(index, {
            "index": index,
            "screenshots": [],
            "weighted": 0.0,
            "measured": 0,
        })

    for shot in screenshots:
        bucket(shot.captured_at)["screenshots"].append(shot)

    for recorded_at, percentage, seconds in activity:
        if seconds <= 0:
            continue
        slot = bucket(recorded_at)
        slot["weighted"] += percentage * seconds
        slot["measured"] += seconds

    windows: List[dict] = []
    for index in sorted(buckets):
        slot = buckets[index]
        window_start = datetime.fromtimestamp(index * window_seconds, tz=timezone.utc)
        window_end = window_start + timedelta(seconds=window_seconds)
        measured = slot["measured"]
        # Weighted by duration, so a 12-second tail window cannot count as much
        # as a full one. Zero measured seconds means "not measured", which is
        # reported as 0% alongside the measured count so a caller can tell the
        # two apart.
        percentage = int(round(slot["weighted"] / measured)) if measured else 0
        shots = sorted(slot["screenshots"], key=lambda s: s.captured_at)
        windows.append({
            "window_start": window_start,
            "window_end": window_end,
            "activity_percentage": max(0, min(100, percentage)),
            "activity_measured_seconds": measured,
            "tracked_seconds": _overlap_seconds(intervals or [], window_start, window_end),
            "screenshots": [
                {
                    "id": s.id,
                    "captured_at": s.captured_at,
                    "monitor_number": s.monitor_number,
                    # Rows predating merged capture have no value; they are
                    # single-display captures, so 1 is the fact, not a guess.
                    "display_count": s.display_count or 1,
                    "width": s.width,
                    "height": s.height,
                    "file_size_bytes": s.file_size_bytes,
                    "view_url": f"/time-entry-screenshots/{s.id}/view",
                }
                for s in shots
            ],
            "screenshot_count": len(shots),
        })
    return windows


class TimeEntryScreenshotService:
    # ── Authorisation ─────────────────────────────────────────────────────────

    @staticmethod
    def _entry_for_upload(db: Session, time_entry_id: int, current_user: User) -> TimeEntry:
        """The entry a screenshot may be written against, or an error.

        Organization and user are derived here from the authenticated session
        and the entry — never read from the request. A client that could name
        its own `organization_id` could write into another organization's data.
        """
        entry = TimeEntryRepository.get_by_id(db, time_entry_id)
        if not entry or entry.organization_id != current_user.organization_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Time entry not found",
            )
        if entry.user_id != current_user.id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Cannot upload screenshots for another user's time entry",
            )
        return entry

    @staticmethod
    def _may_view(
        db: Session,
        screenshot: TimeEntryScreenshot,
        current_user: User,
        entry: Optional[TimeEntry] = None,
    ) -> bool:
        """Whether this caller may see this screenshot.

        The same scope every other read surface uses: your own always; your
        team's if you are a leader; your organization's if your role sees past
        itself. There is no separate screenshot permission model, deliberately
        — a second one would drift out of step with the first.

        `entry` may be supplied by a caller that has already loaded it, to save
        a round trip; it is only ever an optimisation, and the checks applied
        are identical either way.
        """
        if screenshot.organization_id != current_user.organization_id:
            return False
        if entry is None:
            entry = TimeEntryRepository.get_by_id(db, screenshot.time_entry_id)
        if not entry:
            return False
        if entry.user_id == current_user.id:
            return True
        if current_user.role_name == "employee":
            return False
        allowed = visible_member_ids(db, current_user)
        return allowed is None or entry.user_id in allowed

    # ── Validation ────────────────────────────────────────────────────────────

    @staticmethod
    def _validate_image(content: bytes, content_type: Optional[str]) -> Tuple[int, int]:
        """Check the upload really is a WebP of the expected geometry.

        :return: `(width, height)` read from the file itself.
        """
        if not content:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="The uploaded screenshot is empty",
            )
        if len(content) > settings.SCREENSHOT_MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=(
                    f"Screenshot exceeds the {settings.SCREENSHOT_MAX_UPLOAD_BYTES} "
                    f"byte limit"
                ),
            )
        if content_type and content_type.split(";")[0].strip() not in ALLOWED_MIME_TYPES:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Unsupported screenshot type '{content_type}'; expected image/webp",
            )
        if not (content[:4] == _RIFF and content[8:12] == _WEBP):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="The uploaded file is not a WebP image",
            )

        width, height = TimeEntryScreenshotService._read_dimensions(content)
        if width and height:
            problem = _validate_geometry(width, height)
            if problem:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=problem,
                )
        return width, height

    @staticmethod
    def _read_dimensions(content: bytes) -> Tuple[int, int]:
        """Read the image's real geometry with Pillow.

        Returns `(0, 0)` when Pillow is not installed, in which case the
        dimension check above is skipped rather than failing every upload — the
        format check has already established the file is a WebP, and refusing
        real screenshots because an optional dependency is missing would be a
        worse outcome than not verifying the size.
        """
        try:
            import io

            from PIL import Image  # type: ignore
        except ImportError:
            logger.warning(
                "Pillow is not installed; screenshot dimensions cannot be verified"
            )
            return 0, 0
        try:
            with Image.open(io.BytesIO(content)) as image:
                return int(image.width), int(image.height)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"The uploaded screenshot could not be decoded: {exc}",
            )

    # ── Upload ────────────────────────────────────────────────────────────────

    @staticmethod
    def upload_screenshot(
        db: Session,
        time_entry_id: int,
        content: bytes,
        content_type: Optional[str],
        client_screenshot_id: str,
        current_user: User,
        captured_at: Optional[datetime] = None,
        monitor_number: int = 1,
        display_count: int = 1,
    ) -> Tuple[TimeEntryScreenshot, bool]:
        """
        Store one screenshot.

        :return: `(row, duplicate)`. `duplicate` is True when this
            `client_screenshot_id` had already been stored — the desktop's
            retry after a lost response. Nothing is uploaded a second time and
            the original record is returned, which is exactly what the client
            needs to stop retrying and delete its local copy.
        """
        if not client_screenshot_id:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="client_screenshot_id is required for idempotent upload",
            )

        entry = TimeEntryScreenshotService._entry_for_upload(db, time_entry_id, current_user)

        # Idempotency, checked before any work is done, so the common retry
        # case costs one indexed lookup rather than a Drive upload.
        existing = TimeEntryScreenshotRepository.get_by_client_id(
            db, current_user.organization_id, client_screenshot_id
        )
        if existing is not None:
            logger.info(
                "screenshot %s already stored as id %s; returning the existing record",
                client_screenshot_id, existing.id,
            )
            return existing, True

        width, height = TimeEntryScreenshotService._validate_image(content, content_type)

        if not drive_service.configured:
            # Honest failure: nothing is written, so the desktop keeps the file
            # and retries. Silently accepting an upload we cannot store would
            # make the client delete its only copy.
            #
            # The reason goes to the log, not to the response: the client
            # cannot act on it, and naming which server-side setting is missing
            # in an HTTP body describes this deployment to whoever asked.
            logger.error(
                "refusing screenshot upload: Google Drive storage is not "
                "configured (%s)",
                drive_service.unconfigured_reason(),
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Screenshot storage is not configured on this server",
            )

        when = captured_at or datetime.now(timezone.utc)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)

        file_name = f"screenshot_{client_screenshot_id}.webp"
        try:
            # The folder is named for the entry's owner, not for the caller —
            # they are the same person on the upload path today, but naming a
            # folder after whoever happened to send the request is the kind of
            # assumption that silently misfiles data the moment it stops
            # holding.
            owner = db.get(User, entry.user_id)
            folder_id, logical_path = drive_service.ensure_screenshot_folder(
                user_id=entry.user_id,
                captured_on=when.astimezone(timezone.utc).date(),
                user_name=getattr(owner, "name", None),
            )
            file_id = drive_service.upload_file(
                folder_id=folder_id,
                file_name=file_name,
                content=content,
                mime_type="image/webp",
            )
        except GoogleDriveNotAccessible as exc:
            # A misconfiguration, not an outage. Reported as 503 so it reads as
            # "this server cannot store screenshots" rather than as a blip the
            # client should expect to clear on its own.
            logger.error("screenshot storage is misconfigured: %s", exc)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Screenshot storage is not available on this server",
            )
        except GoogleDriveError as exc:
            logger.error("Drive upload failed for %s: %s", client_screenshot_id, exc)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Screenshot storage is temporarily unavailable",
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("unexpected Drive failure for %s", client_screenshot_id)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Screenshot storage is temporarily unavailable",
            ) from exc

        try:
            record = TimeEntryScreenshotRepository.create_uploaded(
                db=db,
                organization_id=current_user.organization_id,
                time_entry_id=time_entry_id,
                captured_at=when,
                file_path=f"{logical_path}/{file_name}",
                file_name=file_name,
                google_drive_file_id=file_id,
                google_drive_folder_id=folder_id,
                file_size_bytes=len(content),
                mime_type="image/webp",
                width=width or None,
                height=height or None,
                monitor_number=monitor_number,
                display_count=display_count,
                client_screenshot_id=client_screenshot_id,
            )
        except IntegrityError:
            # Two uploads of the same capture raced past the lookup above. The
            # unique index is what makes that safe; the loser discards its own
            # Drive object and returns the winner's row, so the retry still
            # sees a success and the orphan does not accumulate.
            db.rollback()
            drive_service.delete_file(file_id)
            winner = TimeEntryScreenshotRepository.get_by_client_id(
                db, current_user.organization_id, client_screenshot_id
            )
            if winner is None:
                raise
            return winner, True

        logger.info(
            "stored screenshot %s for entry %s as Drive file %s (%d bytes)",
            client_screenshot_id, time_entry_id, file_id, len(content),
        )
        return record, False

    # ── View ──────────────────────────────────────────────────────────────────

    @staticmethod
    def get_screenshot_bytes(
        db: Session, screenshot_id: int, current_user: User
    ) -> Tuple[bytes, str, str]:
        """
        Read one screenshot back for an authorised caller.

        :return: `(content, mime_type, file_name)`.
        """
        # Both rows in one round trip; the database answers in ~80ms and a
        # grid pays this per thumbnail.
        record, entry = TimeEntryScreenshotRepository.get_with_entry(db, screenshot_id)
        if not record or not TimeEntryScreenshotService._may_view(
            db, record, current_user, entry=entry
        ):
            # A screenshot the caller may not see is reported as absent rather
            # than as forbidden: "403" on an id you guessed confirms the id
            # exists, which is itself information about another user's day.
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Screenshot not found",
            )
        if not record.google_drive_file_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="This screenshot has no stored image",
            )
        try:
            content = drive_service.download_file(record.google_drive_file_id)
        except GoogleDriveError as exc:
            logger.error("could not read Drive file %s: %s", record.google_drive_file_id, exc)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Screenshot storage is temporarily unavailable",
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("unexpected failure reading Drive file %s", record.google_drive_file_id)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Screenshot storage is temporarily unavailable",
            ) from exc
        return content, record.mime_type or "image/webp", record.file_name or f"screenshot_{record.id}.webp"

    # ── Delete ────────────────────────────────────────────────────────────────

    @staticmethod
    def delete_screenshot(db: Session, screenshot_id: int, current_user: User) -> int:
        """Permanently remove one screenshot: the image, then its metadata.

        The caller has already been checked for the `screenshots:delete`
        permission by the route dependency — that gate is what limits this to
        administrators and HR, and it deliberately lives on the route so it
        cannot be reached around by another service calling in here. What is
        left to enforce is *which* screenshot: the row must belong to the
        caller's own organization, so an administrator of one organization
        cannot delete another's data. That is the same `_may_view` scope every
        read surface uses, and a row outside it answers 404 rather than 403 for
        the same reason the view endpoint does — a 403 on a guessed id confirms
        the id exists.

        Order matters, and it is Drive first. If the row were removed first and
        Drive then refused, the image would stay readable to anyone with the
        file id while the record of it was gone — an undetectable orphan. Doing
        it the other way round leaves, at worst, a row whose image is already
        deleted, which the next attempt cleans up: the missing file is treated
        as "already done" rather than as a permanent blocker.

        :return: the id that was deleted.
        """
        record, entry = TimeEntryScreenshotRepository.get_with_entry(db, screenshot_id)
        if not record or not TimeEntryScreenshotService._may_view(
            db, record, current_user, entry=entry
        ):
            # Also the idempotency answer: a second delete of the same id finds
            # nothing and says so, rather than reporting a second success.
            logger.info(
                "delete refused: screenshot %s not found or out of scope for "
                "user %s (role %s)",
                screenshot_id, current_user.id, current_user.role_name,
            )
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Screenshot not found",
            )

        file_id = record.google_drive_file_id
        drive_status = "skipped: no stored file"
        if file_id:
            try:
                drive_service.delete_file_strict(file_id)
                drive_status = "deleted"
            except GoogleDriveFileNotFound:
                # The bytes are gone already. Blocking on this would pin the
                # metadata in the database forever, guarding nothing.
                drive_status = "already absent"
                logger.warning(
                    "Drive file %s for screenshot %s was already missing; "
                    "removing its metadata anyway",
                    file_id, screenshot_id,
                )
            except GoogleDriveNotAccessible as exc:
                logger.error(
                    "delete failed for screenshot %s: screenshot storage is "
                    "misconfigured (%s); database row kept",
                    screenshot_id, exc,
                )
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Screenshot storage is not available on this server",
                )
            except GoogleDriveError as exc:
                # The row stays. Reporting success here would leave the image
                # readable in Drive with nothing left pointing at it.
                logger.error(
                    "delete failed for screenshot %s: Drive file %s could not "
                    "be deleted (%s); database row kept",
                    screenshot_id, file_id, exc,
                )
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail="Screenshot storage is temporarily unavailable; nothing was deleted",
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "unexpected failure deleting Drive file %s for screenshot %s; "
                    "database row kept",
                    file_id, screenshot_id,
                )
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail="Screenshot storage is temporarily unavailable; nothing was deleted",
                ) from exc
        else:
            logger.info(
                "screenshot %s has no Drive file id; deleting metadata only",
                screenshot_id,
            )

        try:
            TimeEntryScreenshotRepository.delete(db, record)
        except Exception as exc:  # noqa: BLE001
            # The image is already gone, so this is not recoverable by keeping
            # the row — but it must never be reported as a success, or the row
            # would sit in the database describing an image that no longer
            # exists with nobody aware of it.
            db.rollback()
            logger.exception(
                "screenshot %s: Drive deletion %s but the metadata row could "
                "NOT be deleted; the row now describes a missing image and "
                "needs manual cleanup",
                screenshot_id, drive_status,
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="The screenshot image was removed but its record could not be deleted",
            ) from exc

        logger.info(
            "screenshot %s deleted by user %s (role %s): Drive %s, database deleted",
            screenshot_id, current_user.id, current_user.role_name, drive_status,
        )
        return screenshot_id

    # ── Timeline ──────────────────────────────────────────────────────────────

    @staticmethod
    def get_timeline(
        db: Session,
        current_user: User,
        user_id: Optional[int] = None,
        target_date: Optional[date_type] = None,
    ) -> Tuple[int, List[dict]]:
        """
        Screenshots and activity for one IST day, grouped into fixed windows.

        Windows are derived from timestamps rather than stored on the rows:
        `captured_at` and `recorded_at` are bucketed by the same epoch-aligned
        arithmetic the desktop schedules against, so nothing has to be
        backfilled and changing the window length is a configuration change
        rather than a migration.

        Only windows that contain a screenshot or measured activity are
        returned — an untracked hour produces no empty blocks to scroll past.

        :return: `(window_minutes, windows)`.
        """
        subject_id = TimeEntryScreenshotService._resolve_subject(db, current_user, user_id)
        day = target_date or ist_today()
        start = ist_day_start_utc(day)
        end = ist_day_end_utc(day)
        window_minutes = max(1, int(settings.SCREENSHOT_WINDOW_MINUTES))
        window_seconds = window_minutes * 60

        screenshots = TimeEntryScreenshotRepository.list_screenshots(
            db=db,
            organization_id=current_user.organization_id,
            user_id=subject_id,
            start=start,
            end=end,
            # A tracked day at one capture per ten minutes is ~144 rows; the
            # ceiling is generous enough for a future five-per-window setting.
            limit=5000,
        )
        activity = TimeEntryScreenshotRepository.get_activity_totals_in_range(
            db=db,
            organization_id=current_user.organization_id,
            user_id=subject_id,
            start=start,
            end=end,
        )
        intervals = TimeEntryScreenshotRepository.list_tracked_intervals(
            db=db,
            organization_id=current_user.organization_id,
            user_id=subject_id,
            start=start,
            end=end,
        )

        return window_minutes, _build_windows(
            window_seconds, screenshots, activity, intervals
        )

    #: The widest span the grid will read in one request. "Last 30 days" is the
    #: widest preset the UI offers and a hand-picked span reaches across the two
    #: months the calendar shows, so the cap sits well clear of both; what it
    #: stops is a hand-edited query string asking for a year of every employee's
    #: captures in a single round trip.
    MAX_GRID_DAYS = 92

    @staticmethod
    def get_day_grid(
        db: Session,
        current_user: User,
        date_from: Optional[date_type] = None,
        date_to: Optional[date_type] = None,
        user_id: Optional[int] = None,
    ) -> Tuple[int, List[dict]]:
        """Every member the caller may see, with their screenshots over a span.

        The default view for an admin or HR, who are looking for "what did the
        team do" rather than for one person. It answers in a single round trip:
        fanning the single-member timeline out over a hundred employees and a
        week of days would be hundreds of requests to paint one screen.

        Scope is the same ``visible_member_ids`` set every other read surface
        uses, and a caller who cannot see past themselves gets exactly their own
        row -- so this endpoint can never show more than the timeline would.

        ``user_id`` narrows the result to one member, checked through the same
        ``_resolve_subject`` the timeline uses. It only ever narrows: a caller
        who may not see that member is refused rather than quietly widened back
        to their own row.

        :return: ``(window_minutes, members)``, ordered by member name. Each
            member carries only the IST days they actually captured on, newest
            day first; members who captured nothing are left out entirely.
        """
        end_day = date_to or ist_today()
        start_day = date_from or end_day
        if start_day > end_day:
            start_day, end_day = end_day, start_day
        span = (end_day - start_day).days + 1
        if span > TimeEntryScreenshotService.MAX_GRID_DAYS:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"A screenshot range may cover at most "
                    f"{TimeEntryScreenshotService.MAX_GRID_DAYS} days"
                ),
            )

        start = ist_day_start_utc(start_day)
        end = ist_day_end_utc(end_day)
        window_minutes = max(1, int(settings.SCREENSHOT_WINDOW_MINUTES))
        window_seconds = window_minutes * 60

        # A caller with no org-wide reach sees one row: themselves. Narrowing
        # here rather than trusting the read surfaces below keeps it in one
        # place.
        allowed = visible_member_ids(db, current_user)
        if current_user.role_name == "employee":
            allowed = {current_user.id}
        if user_id is not None:
            # Authorised the same way the timeline authorises its subject, then
            # intersected rather than substituted, so a narrowing filter stays
            # narrowing.
            subject = TimeEntryScreenshotService._resolve_subject(db, current_user, user_id)
            allowed = {subject} if allowed is None else (allowed & {subject})

        tagged = TimeEntryScreenshotRepository.list_screenshots_by_user(
            db=db,
            organization_id=current_user.organization_id,
            start=start,
            end=end,
            user_ids=allowed,
        )
        if not tagged:
            return window_minutes, []

        # Grouped by member and then by the IST calendar day the capture falls
        # on, because a day is what a viewer scrolls through -- and the UTC day
        # a timestamp sits in is not the same day the person worked.
        shots: Dict[int, Dict[date_type, List[TimeEntryScreenshot]]] = {}
        for user_id, shot in tagged:
            day = TimeEntryScreenshotService._ist_day_of(shot.captured_at)
            shots.setdefault(user_id, {}).setdefault(day, []).append(shot)

        activity: Dict[int, Dict[date_type, List[Tuple[datetime, int, int]]]] = {}
        for user_id, recorded_at, percentage, seconds in (
            TimeEntryScreenshotRepository.get_activity_totals_by_user(
                db=db,
                organization_id=current_user.organization_id,
                start=start,
                end=end,
                user_ids=allowed,
            )
        ):
            if user_id not in shots:
                continue
            day = TimeEntryScreenshotService._ist_day_of(recorded_at)
            activity.setdefault(user_id, {}).setdefault(day, []).append(
                (recorded_at, percentage, seconds)
            )

        # Worked time comes from the entries themselves, so a day's total is the
        # time actually tracked rather than the part of it a screenshot landed
        # in. Kept unclipped here and intersected per day and per window below.
        intervals: Dict[int, List[Tuple[datetime, datetime]]] = {}
        for member_id, began, ended in (
            TimeEntryScreenshotRepository.list_tracked_intervals_by_user(
                db=db,
                organization_id=current_user.organization_id,
                start=start,
                end=end,
                user_ids=allowed,
            )
        ):
            if member_id not in shots:
                continue
            intervals.setdefault(member_id, []).append((began, ended))

        names = {
            user.id: user.name
            for user in db.query(User).filter(User.id.in_(shots.keys())).all()
        }

        members: List[dict] = []
        for user_id, by_day in shots.items():
            member_intervals = intervals.get(user_id, [])
            days = [
                {
                    "date": day,
                    "windows": _build_windows(
                        window_seconds,
                        day_shots,
                        activity.get(user_id, {}).get(day, []),
                        member_intervals,
                    ),
                    "screenshot_count": len(day_shots),
                    # The whole IST day, not the sum of the windows below: time
                    # tracked in a window that produced no capture is still time
                    # this person worked, and a header that hid it would be
                    # under-reporting them.
                    "tracked_seconds": _overlap_seconds(
                        member_intervals, ist_day_start_utc(day), ist_day_end_utc(day)
                    ),
                }
                for day, day_shots in sorted(by_day.items(), reverse=True)
            ]
            members.append({
                "user_id": user_id,
                # A row whose user record is missing is still shown, under its
                # id: dropping it would silently hide real captures.
                "user_name": names.get(user_id) or f"User {user_id}",
                "days": days,
                "screenshot_count": sum(d["screenshot_count"] for d in days),
                "tracked_seconds": sum(d["tracked_seconds"] for d in days),
            })

        members.sort(key=lambda m: (m["user_name"].lower(), m["user_id"]))
        return window_minutes, members

    @staticmethod
    def _ist_day_of(value: datetime) -> date_type:
        """The IST calendar day a UTC timestamp belongs to."""
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        converted = to_ist(value)
        return converted.date() if converted else value.date()

    @staticmethod
    def _resolve_subject(db: Session, current_user: User, user_id: Optional[int]) -> int:
        """Whose day is being read, after scope enforcement."""
        if user_id is None or user_id == current_user.id:
            return current_user.id
        if current_user.role_name == "employee":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You may only view your own screenshots",
            )
        allowed = visible_member_ids(db, current_user)
        if allowed is not None and user_id not in allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="This member is outside your visible team",
            )
        return user_id

    # ── Existing surface (unchanged behaviour) ────────────────────────────────

    @staticmethod
    def create_screenshot(
        db: Session,
        payload: TimeEntryScreenshotCreate,
        current_user: User
    ) -> TimeEntryScreenshot:
        # Verify time entry exists and belongs to the user's organization
        time_entry = TimeEntryRepository.get_by_id(db, payload.time_entry_id)
        if not time_entry or time_entry.organization_id != current_user.organization_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Time entry not found"
            )
        # If employee, ensure they own the time entry
        if current_user.role_name == "employee" and time_entry.user_id != current_user.id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You are not authorized to add screenshots to this time entry"
            )

        return TimeEntryScreenshotRepository.create(
            db=db,
            organization_id=current_user.organization_id,
            time_entry_id=payload.time_entry_id,
            file_path=payload.file_path,
            monitor_number=payload.monitor_number,
            display_count=payload.display_count,
            captured_at=payload.captured_at
        )

    @staticmethod
    def list_screenshots(
        db: Session,
        time_entry_id: Optional[int],
        user_id: Optional[int],
        limit: int,
        current_user: User
    ) -> List[TimeEntryScreenshot]:
        # Enforce scoping: employees can only list their own screenshots
        target_user_id = user_id
        if current_user.role_name == "employee":
            target_user_id = current_user.id

        # And a leader only their team's -- the same set `member_scope` hands
        # the member directory and the time-tracking listing. `None` is "no
        # restriction", which is every other role, and a `user_id` on the query
        # string is filtered against the set rather than trusted.
        return TimeEntryScreenshotRepository.list_screenshots(
            db=db,
            organization_id=current_user.organization_id,
            user_id=target_user_id,
            time_entry_id=time_entry_id,
            limit=limit,
            user_ids=visible_member_ids(db, current_user),
        )

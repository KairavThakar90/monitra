"""
feedback_attachment_sweep — find (and optionally delete) feedback-attachment
objects in Google Drive that no database row points at.

Why this exists. A submission stores its files in Drive first and writes its
rows second, in one transaction. When that write fails the service deletes what
it just stored; if the delete cannot be confirmed it logs
`FEEDBACK_ATTACHMENT_ORPHAN` with the Drive id. Objects can also outlive their
rows when a user or organization is deleted (the rows cascade; Drive is not
touched). This sweep is the safety net that makes "orphans do not accumulate
silently" true: anything under `<root>/Feedback/*` that is older than
`--min-age-hours` and referenced by no `feedback_attachments` row is an orphan.

The age floor is what keeps it safe against a submission that is mid-flight —
its objects exist for a few seconds before its rows do.

    # list what would be removed (the default; changes nothing)
    python scripts/feedback_attachment_sweep.py

    # remove it
    python scripts/feedback_attachment_sweep.py --apply

Run it where the backend runs, with the backend's own configuration. Prints
Drive ids and object names only — never file contents, never credentials.
Exit status is 0 unless it could not complete.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone

BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_ROOT not in sys.path:
    sys.path.insert(0, BACKEND_ROOT)


def find_orphans(objects: list, referenced: set, cutoff: datetime) -> list:
    """The objects that are old enough and referenced by nothing."""
    orphans = []
    for item in objects:
        if item["id"] in referenced:
            continue
        created = item.get("createdTime")
        try:
            when = datetime.fromisoformat(created.replace("Z", "+00:00")) if created else None
        except ValueError:
            when = None
        # An object whose age cannot be read is left alone: deleting on a guess
        # is the one mistake this tool must not make.
        if when is None or when > cutoff:
            continue
        orphans.append(item)
    return orphans


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", action="store_true", help="delete the orphans (default: list only)")
    parser.add_argument("--min-age-hours", type=float, default=24.0)
    args = parser.parse_args(argv)

    from sqlalchemy import select

    from app.core.database import get_session_local
    from app.models.feedback_attachment import FeedbackAttachment
    from app.services.google_drive_service import GoogleDriveFileNotFound, drive_service

    if not drive_service.configured:
        print(f"Drive is not configured: {drive_service.unconfigured_reason()}")
        return 1

    objects = drive_service.list_feedback_objects()
    db = get_session_local()()
    try:
        referenced = set(db.scalars(select(FeedbackAttachment.google_drive_file_id)).all())
    finally:
        db.close()

    cutoff = datetime.now(timezone.utc) - timedelta(hours=args.min_age_hours)
    orphans = find_orphans(objects, referenced, cutoff)
    print(f"{len(objects)} object(s) in Drive, {len(referenced)} referenced, {len(orphans)} orphaned")
    failures = 0
    for item in orphans:
        label = f"{item['folder']}/{item['name']}  id={item['id']}  created={item.get('createdTime')}"
        if not args.apply:
            print(f"  would delete  {label}")
            continue
        try:
            drive_service.delete_file_strict(item["id"])
            print(f"  deleted       {label}")
        except GoogleDriveFileNotFound:
            print(f"  already gone  {label}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"  FAILED        {label}  ({type(exc).__name__})")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

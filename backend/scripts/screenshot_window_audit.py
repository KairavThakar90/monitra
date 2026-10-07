"""
screenshot_window_audit -- Say, window by window, what the database knows about
a member's screenshots on one IST day, and which windows nobody can explain.

This is the tool for a report that reads "the admin page shows No capture for
these windows". It reads only; it never writes, never lists Drive, and opens its
transaction READ ONLY, so it is safe to point at any environment you are allowed
to read:

    # a development checkout (reads backend/.env; ENV decides the database)
    python scripts/screenshot_window_audit.py --user-id 281 --date 2026-10-06

    # the production VM, with the service's own environment
    sudo -u monitra bash -c 'set -a; . /etc/monitra/backend.env; set +a; \
        cd /opt/monitra/backend && /opt/monitra/venv/bin/python \
        scripts/screenshot_window_audit.py --user-id 281 --date 2026-10-06'

    # find the user id from a name or email first
    python scripts/screenshot_window_audit.py --find "neha"

For every ten-minute window the member was tracked or measured in, it prints one
line: the time, how long the timer ran, the measured activity, how many images
the database holds, and the verdict:

    captured     an image is stored (and the Drive id is present)
    NOT EXPECTED no timer was running in the window, so no screenshot was due
    ORPHAN       a row exists but carries no Drive file id -- the image cannot be shown
    pending      the desktop reported the image queued and failing to upload
    failed       the desktop reported the capture failed, or the server refused the upload
    blocked      the OS (or the privacy settings not loading) held the capture back
    excluded     a privacy rule excluded what was on screen
    unavailable  that computer cannot capture
    NO RECORD    the timer ran and nothing at all was reported -- the one case
                 the database cannot explain. Read that computer's log around
                 that time: `SCREENSHOT_CAPTURE_ATTEMPT_FAILED`,
                 `SCREENSHOT_WINDOW_UNRESOLVED`, `SCREENSHOT_CAPTURE_STUCK`,
                 `SCREENSHOT_PERMISSION_BLOCKED`, `SCREENSHOT_QUEUED`.
                 (An older desktop reports nothing, so every miss on one is
                 NO RECORD whatever the cause.)

With `--check-drive` it also asks Drive about every stored image (metadata only;
nothing is downloaded, created, renamed or deleted) and reports, per screenshot:

    OK                 the object exists and is an image
    MISSING-IN-DRIVE   the row names a Drive object Drive says does not exist --
                       the web page shows this as "Image missing from storage"
    TRASHED / FORBIDDEN / NOT-AN-IMAGE / DRIVE-ERROR
    DRIVE-ONLY         an object in that day's Drive folder with no database row
                       (the reverse inconsistency: stored, but never shown)

It also flags a capture filed within `--edge` seconds of a window boundary, the
shape the old capture timestamp produced (stamped after the encode, so a capture
read in the last second of a window was filed in the next).

Exit status is 0 whenever it could read; the verdicts are the output.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional

BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_ROOT not in sys.path:
    sys.path.insert(0, BACKEND_ROOT)


def verdict_for(window: dict) -> str:
    """The one-word verdict for a window dict from `_build_windows`.

    Pure, so the vocabulary is tested without a database.
    """
    if window["screenshots"]:
        return "captured"
    state = window.get("capture_state", "none")
    if state == "not_expected":
        return "NOT EXPECTED"
    if state == "none":
        return "NO RECORD" if window.get("tracked_seconds", 0) > 0 else "-"
    return state


#: A Drive object is an acceptable screenshot only if it is an image with bytes.
def drive_verdict(stat: dict) -> str:
    """One word for what `GoogleDriveService.stat_file` found. Pure."""
    state = stat.get("state")
    if state == "ok":
        mime = (stat.get("mime_type") or "").lower()
        if not mime.startswith("image/") or not stat.get("size"):
            return "NOT-AN-IMAGE"
        return "OK"
    return {
        "missing": "MISSING-IN-DRIVE", "trashed": "TRASHED", "forbidden": "FORBIDDEN",
    }.get(state or "", "DRIVE-ERROR")


def drive_only_files(day_files: List[dict], client_ids: set) -> List[dict]:
    """Objects in the day's Drive folder that no database row accounts for.

    Screenshot objects are named ``screenshot_<client_screenshot_id>.webp``, and
    the id is the join key. Pure.
    """
    extra = []
    for item in day_files:
        name = item.get("name") or ""
        if name.startswith("screenshot_") and name.endswith(".webp"):
            if name[len("screenshot_"):-len(".webp")] not in client_ids:
                extra.append(item)
    return extra


def edge_spills(windows: List[dict], edge_seconds: int) -> List[str]:
    """Captures filed within `edge_seconds` after a boundary whose previous
    window has activity and no image -- the signature of a capture stamped late."""
    notes: List[str] = []
    for previous, current in zip(windows, windows[1:]):
        if previous["screenshots"] or not previous["activity_measured_seconds"]:
            continue
        if previous["window_end"] != current["window_start"]:
            continue
        for shot in current["screenshots"]:
            gap = (shot["captured_at"] - current["window_start"]).total_seconds()
            if 0 <= gap <= edge_seconds:
                notes.append(
                    f"capture {shot['id']} at {shot['captured_at']:%H:%M:%S}Z is {gap:.0f}s "
                    f"into the window after an empty one"
                )
    return notes


def format_window(window: dict, ist) -> str:
    start = window["window_start"].astimezone(ist)
    end = window["window_end"].astimezone(ist)
    reason = window.get("capture_reason")
    attempts = window.get("capture_attempts") or 0
    extra = ""
    if reason:
        extra = f"  reason={reason}"
        if attempts:
            extra += f" attempts={attempts}"
    return (
        f"{start:%H:%M}-{end:%H:%M}  worked={window['tracked_seconds']:>4}s  "
        f"activity={window['activity_percentage']:>3}% of {window['activity_measured_seconds']:>3}s  "
        f"images={window['screenshot_count']}  {verdict_for(window)}{extra}"
    )


def _find(db, needle: str) -> int:
    from app.models.user import User

    rows = (
        db.query(User.id, User.name, User.email)
        .filter((User.name.ilike(f"%{needle}%")) | (User.email.ilike(f"%{needle}%")))
        .order_by(User.name).limit(25).all()
    )
    for row in rows:
        print(f"{row.id:>6}  {row.name}  <{row.email}>")
    if not rows:
        print("no such user")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--user-id", type=int)
    parser.add_argument("--date", help="IST calendar day, YYYY-MM-DD (default: today)")
    parser.add_argument("--find", help="list users whose name or email contains this")
    parser.add_argument("--check-drive", action="store_true",
                        help="also verify every stored image in Drive (read-only metadata)")
    parser.add_argument("--edge", type=int, default=5,
                        help="seconds after a boundary that count as a late-stamped capture")
    args = parser.parse_args(argv)

    from sqlalchemy import text

    from app.core.config import settings
    from app.core.database import describe_url, get_database_url, get_engine
    from app.core.time_format import IST, ist_day_end_utc, ist_day_start_utc, ist_today
    from app.models.time_entry_screenshot import TimeEntryScreenshot
    from app.models.user import User
    from app.repositories.time_entry_screenshot import TimeEntryScreenshotRepository as Repo
    from app.services.time_entry_screenshot import TimeEntryScreenshotService as Service
    from app.services.time_entry_screenshot import _build_windows
    from sqlalchemy.orm import Session

    print(f"database: {describe_url(get_database_url())}  (ENV={settings.ENV}, read only)")
    engine = get_engine()
    with Session(engine) as db:
        db.execute(text("SET TRANSACTION READ ONLY"))
        if args.find:
            return _find(db, args.find)
        if args.user_id is None:
            parser.error("--user-id is required (use --find to look one up)")

        user = db.get(User, args.user_id)
        if user is None:
            print(f"user {args.user_id} does not exist")
            return 1
        day = date.fromisoformat(args.date) if args.date else ist_today()
        start, end = ist_day_start_utc(day), ist_day_end_utc(day)
        window_minutes = Service._window_minutes_for(user.capture_frequency)

        shots = Repo.list_screenshots(
            db, user.organization_id, user_id=user.id, start=start, end=end, limit=5000
        )
        activity = Repo.get_activity_totals_in_range(
            db, user.organization_id, user.id, start, end
        )
        intervals = Repo.list_tracked_intervals(db, user.organization_id, user.id, start, end)
        events_note = ""
        try:
            # A savepoint, so a database that has not had the capture-event
            # migration yet loses only this query, not the whole audit.
            with db.begin_nested():
                events = Repo.list_events(db, user.organization_id, user.id, start, end)
        except Exception as exc:  # noqa: BLE001 -- UndefinedTable on a pre-migration database
            events = []
            events_note = (
                "capture events: NOT AVAILABLE (the time_entry_screenshot_events table is "
                f"missing -- migration a7c3e9d15b42 is not applied here): {type(exc).__name__}"
            )
        windows = _build_windows(window_minutes * 60, shots, activity, intervals, {}, events=events)

        print(f"member: {user.name} (id {user.id})  day: {day} IST  "
              f"capture_frequency: {user.capture_frequency} min "
              f"(window used: {window_minutes} min)")
        print(f"rows: {len(shots)} screenshots, {len(events)} capture events, "
              f"{len(intervals)} tracked spans")
        if events_note:
            print(events_note)
        print()

        counts: Dict[str, int] = {}
        for window in windows:
            v = verdict_for(window)
            counts[v] = counts.get(v, 0) + 1
            print(format_window(window, IST))

        orphans = [s for s in shots if not s.google_drive_file_id]
        for shot in orphans:
            counts["ORPHAN"] = counts.get("ORPHAN", 0) + 1
            print(f"ORPHAN: screenshot {shot.id} captured {shot.captured_at:%H:%M:%S}Z "
                  f"has no Drive file id")

        if args.check_drive:
            from app.services.google_drive_service import drive_service

            print("\nDrive check (metadata only):")
            for shot in shots:
                if not shot.google_drive_file_id:
                    continue
                stat = drive_service.stat_file(shot.google_drive_file_id)
                verdict = drive_verdict(stat)
                counts["drive:" + verdict] = counts.get("drive:" + verdict, 0) + 1
                if verdict != "OK":
                    print(f"  {verdict}: screenshot {shot.id} captured {shot.captured_at:%H:%M:%S}Z "
                          f"drive_file={shot.google_drive_file_id} "
                          f"entry={shot.time_entry_id} client_id={shot.client_screenshot_id} "
                          f"stat={stat}")
            try:
                day_files = drive_service.list_screenshot_day_files(user.id, day, user.name)
            except Exception as exc:  # noqa: BLE001
                day_files = None
                print(f"  could not list the Drive folder: {type(exc).__name__}")
            if day_files is None:
                print("  no Drive folder for that member and day")
            else:
                extra = drive_only_files(
                    day_files, {s.client_screenshot_id for s in shots if s.client_screenshot_id}
                )
                counts["drive:DRIVE-ONLY"] = len(extra)
                for item in extra:
                    print(f"  DRIVE-ONLY: {item.get('name')} (id {item.get('id')}) has no database row")

        print("\nsummary: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
        notes = edge_spills(windows, args.edge)
        if notes:
            print("\nlate-stamped captures (filed just after a boundary, empty window before):")
            for note in notes:
                print("  " + note)
        if counts.get("NO RECORD"):
            print(
                "\nNO RECORD windows are the ones this database cannot explain. On that "
                "computer, read the desktop log (logs/monitra.log) around those times for "
                "SCREENSHOT_* lines."
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())

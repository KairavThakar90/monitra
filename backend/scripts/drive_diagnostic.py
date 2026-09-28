"""
drive_diagnostic — Prove, from a given backend environment, that screenshots
can reach Google Drive and be read back, one step at a time.

Run it where the backend runs, with the backend's own configuration, so the
result describes *that* deployment and not the operator's laptop:

    # a development checkout (reads backend/.env)
    python scripts/drive_diagnostic.py

    # the production VM (systemd loads /etc/monitra/backend.env for the
    # service; a shell has to load it the same way)
    sudo -u monitra bash -c 'set -a; . /etc/monitra/backend.env; set +a; \
        cd /opt/monitra/backend && /opt/monitra/venv/bin/python scripts/drive_diagnostic.py'

What it checks, in order, stopping at the first failure:

    1. the two settings are present, and where the credential comes from
    2. the credential parses and names a service account
    3. Drive answers, and the configured root is visible to that account
    4. the account can create files in the root (Editor / Content manager)
    5. the year folder resolves (found or created)
    6. the month folder resolves
    7. a user folder resolves -- a clearly named diagnostic one, never a
       real person's
    8. today's (IST) date folder resolves under it
    9. a small generated WebP uploads, idempotently
   10. the object is found again by name and reads back byte for byte
   11. the Drive file id is reported
   12. the database this process would write the row to is reachable, at
       the expected schema revision, with every column the row needs

Everything it creates is named `MONITRA DIAGNOSTIC` and is removed again
unless `--keep` is given. Nothing here reads, writes or lists any real
person's screenshots.

Prints nothing secret: the service-account address and the root folder id
are operator-facing identifiers, not credentials; the key itself is never
printed.

Exit status is 0 only when every step passed.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Callable, List, Optional

BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_ROOT not in sys.path:
    sys.path.insert(0, BACKEND_ROOT)

#: The folder a diagnostic run writes under, in place of a real user folder.
DIAGNOSTIC_USER_ID = 0
DIAGNOSTIC_USER_NAME = "MONITRA DIAGNOSTIC"

#: Columns `TimeEntryScreenshotRepository.create_uploaded` writes.
REQUIRED_COLUMNS = {
    "organization_id", "time_entry_id", "captured_at", "file_path", "file_name",
    "google_drive_file_id", "google_drive_folder_id", "file_size_bytes",
    "mime_type", "width", "height", "monitor_number", "display_count",
    "client_screenshot_id", "upload_status", "uploaded_at",
}


class Step:
    def __init__(self, number: int, title: str) -> None:
        self.number = number
        self.title = title
        self.passed: Optional[bool] = None
        self.detail = ""

    def report(self) -> str:
        mark = "PASS" if self.passed else "FAIL" if self.passed is False else "SKIP"
        return f"  [{mark}] {self.number:>2}. {self.title}" + (f" -- {self.detail}" if self.detail else "")


def _tiny_webp() -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    image = Image.new("RGB", (1000, 1000), (24, 24, 27))
    # A few distinct pixels so the file is not degenerate.
    for x in range(0, 1000, 97):
        image.putpixel((x, x), (200, 60, 60))
    image.save(buffer, format="WEBP", quality=60)
    return buffer.getvalue()


def run(keep: bool) -> int:
    from app.core.time_format import ist_today
    from app.services import google_drive_service as gds

    drive = gds.drive_service
    steps: List[Step] = []
    created_files: List[str] = []
    created_folders: List[str] = []
    file_id: Optional[str] = None
    day_folder: Optional[str] = None

    def step(number: int, title: str, fn: Callable[[], str]) -> bool:
        current = Step(number, title)
        steps.append(current)
        try:
            current.detail = fn() or ""
            current.passed = True
        except Exception as exc:  # noqa: BLE001
            current.passed = False
            current.detail = f"{type(exc).__name__}: {exc}"
        print(current.report(), flush=True)
        return bool(current.passed)

    print("Monitra screenshot storage diagnostic")
    print(f"  started {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    print(f"  backend root {BACKEND_ROOT}")

    # 1. configuration
    def configuration() -> str:
        described = drive.describe_configuration()
        if not described["configured"]:
            raise RuntimeError(drive.unconfigured_reason())
        return f"root={described['root_folder_id']} credentials={described['credential_source']}"

    if not step(1, "configuration present", configuration):
        return _finish(steps)

    # 2. credential
    def credential() -> str:
        creds = drive._credentials()
        email = getattr(creds, "service_account_email", None) or drive._service_account_email()
        return f"service account {email}"

    if not step(2, "service-account credential loads", credential):
        return _finish(steps)

    # 3 + 4. root visible and writable
    def root() -> str:
        drive.verify_root_access()
        client = drive._client()
        info = client.files().get(
            fileId=drive.root_folder_id, fields="id,name,driveId", supportsAllDrives=True,
        ).execute()
        kind = "shared drive" if info.get("driveId") == info.get("id") else "folder"
        return f"{kind} '{info.get('name')}' ({info.get('id')})"

    if not step(3, "Drive reachable and root visible", root):
        return _finish(steps)
    step(4, "service account may create files in the root", lambda: "canAddChildren=true")

    # 5-8. the folder tree, through the production code path
    today = ist_today()
    resolved: dict = {}

    def tree() -> str:
        folder, logical = drive.ensure_screenshot_folder(
            user_id=DIAGNOSTIC_USER_ID, captured_on=today, user_name=DIAGNOSTIC_USER_NAME,
        )
        resolved["day"] = folder
        resolved["path"] = logical
        return f"{logical} -> {folder}"

    if not step(5, "year folder resolves", lambda: _resolve_level(drive, today, 0)):
        return _finish(steps)
    if not step(6, "month folder resolves", lambda: _resolve_level(drive, today, 1)):
        return _finish(steps)
    if not step(7, f"user folder resolves (User_{DIAGNOSTIC_USER_ID}_{DIAGNOSTIC_USER_NAME})",
                lambda: _resolve_level(drive, today, 2)):
        return _finish(steps)
    if not step(8, f"date folder resolves ({today.isoformat()}, IST)", tree):
        return _finish(steps)
    day_folder = resolved["day"]
    created_folders.append(day_folder)
    user_folder = _resolve_level(drive, today, 2, want_id=True)
    if user_folder:
        created_folders.append(user_folder)

    # 9. upload
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    file_name = f"screenshot_diagnostic_{stamp}.webp"
    payload = _tiny_webp()

    def upload() -> str:
        nonlocal file_id
        started = time.monotonic()
        file_id, reused = drive.upload_file_idempotent(day_folder, file_name, payload)
        created_files.append(file_id)
        again, reused_again = drive.upload_file_idempotent(day_folder, file_name, payload)
        if again != file_id or not reused_again:
            raise RuntimeError(f"a second upload of the same name produced {again} (reused={reused_again})")
        return (f"{len(payload)} bytes as {file_name} in "
                f"{int((time.monotonic() - started) * 1000)} ms; retry reused the object")

    if not step(9, "test screenshot uploads (and a retry reuses it)", upload):
        return _finish(steps, drive, created_files, created_folders, keep)

    # 10. read back
    def read_back() -> str:
        found = drive.find_file(day_folder, file_name)
        if found != file_id:
            raise RuntimeError(f"listing found {found}, expected {file_id}")
        drive.image_cache.discard(file_id)
        data = drive.download_file(file_id)
        if data != payload:
            raise RuntimeError(f"downloaded {len(data)} bytes, uploaded {len(payload)}")
        return f"listed and downloaded {len(data)} bytes, identical"

    step(10, "object is listed and reads back byte for byte", read_back)

    # 11. id
    step(11, "Drive file id known", lambda: f"{file_id} in folder {day_folder} ({resolved['path']})")

    # 12. database
    def database() -> str:
        from sqlalchemy import inspect, text

        from app.core.database import describe_url, get_database_url, get_engine

        engine = get_engine()
        with engine.connect() as conn:
            version = conn.execute(text("SELECT version_num FROM alembic_version")).scalars().all()
            columns = {c["name"] for c in inspect(conn).get_columns("time_entry_screenshots")}
            missing = REQUIRED_COLUMNS - columns
            if missing:
                raise RuntimeError(f"time_entry_screenshots is missing {sorted(missing)}")
            todays = conn.execute(text(
                "SELECT COUNT(*), COUNT(*) FILTER (WHERE google_drive_file_id IS NULL) "
                "FROM time_entry_screenshots WHERE captured_at > now() - interval '1 day'"
            )).one()
        return (f"{describe_url(get_database_url())} alembic={version} "
                f"rows_last_24h={todays[0]} without_drive_id={todays[1]}")

    step(12, "database reachable with the screenshot schema", database)

    return _finish(steps, drive, created_files, created_folders, keep)


def _resolve_level(drive, today, depth: int, want_id: bool = False):
    """Resolve the tree down to `depth` (0=year, 1=month, 2=user) and describe it."""
    year = f"{today.year:04d}"
    month = today.strftime("%B")
    user = drive.user_folder_name(DIAGNOSTIC_USER_ID, DIAGNOSTIC_USER_NAME)
    parent = drive.root_folder_id
    names = [year, month, user][: depth + 1]
    folder = parent
    for name in names:
        folder = drive.ensure_folder(folder, name)
    if want_id:
        return folder
    return f"{'/'.join(names)} -> {folder}"


def _finish(steps, drive=None, files=(), folders=(), keep=False) -> int:
    if drive is not None and not keep:
        _cleanup(drive, list(files), list(folders))
    elif drive is not None and (files or folders):
        print(f"  kept {len(files)} file(s) and {len(folders)} diagnostic folder(s) (--keep)")
    failed = [s for s in steps if s.passed is False]
    print()
    print("RESULT:", "PASS" if not failed else f"FAIL at step {failed[0].number} ({failed[0].title})")
    print(json.dumps({
        "passed": not failed,
        "steps": [{"n": s.number, "title": s.title, "ok": s.passed, "detail": s.detail} for s in steps],
    }))
    return 0 if not failed else 1


def _cleanup(drive, files: List[str], folders: List[str]) -> None:
    """Remove what this run created: the test object, then the diagnostic
    day and user folders. A folder that will not delete is trashed instead,
    and a failure to tidy is reported rather than hidden -- it does not fail
    the diagnostic, whose subject is the upload path."""
    client = drive._client()
    for file_id in files:
        try:
            drive.delete_file_strict(file_id)
            print(f"  cleaned up test file {file_id}")
        except Exception as exc:  # noqa: BLE001
            print(f"  WARNING: could not delete test file {file_id}: {exc}")
    for folder_id in folders:
        try:
            client.files().delete(fileId=folder_id, supportsAllDrives=True).execute()
            print(f"  cleaned up diagnostic folder {folder_id}")
        except Exception:  # noqa: BLE001
            try:
                client.files().update(
                    fileId=folder_id, body={"trashed": True}, supportsAllDrives=True,
                ).execute()
                print(f"  trashed diagnostic folder {folder_id}")
            except Exception as exc:  # noqa: BLE001
                print(f"  WARNING: could not remove diagnostic folder {folder_id}: {exc}")
    drive.invalidate_folder_cache()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--keep", action="store_true",
                        help="leave the diagnostic file and folders in Drive for inspection")
    args = parser.parse_args(argv)
    # Deliberately no dotenv loading here: `app.core.config.Settings` reads
    # backend/.env itself, and `app.core.database.get_database_url` treats a
    # DATABASE_URL found in the *process* environment as an explicit override
    # of the ENV-based choice. Loading the file into the environment would
    # therefore make step 12 describe a database the application itself would
    # not have chosen.
    return run(keep=args.keep)


if __name__ == "__main__":
    sys.exit(main())

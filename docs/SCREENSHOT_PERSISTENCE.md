# Screenshot persistence — capture to Google Drive, end to end

This document is authoritative for what happens to a screenshot after the
desktop takes it, which component owns each step, what state it is in at
every point, and how to tell from the outside whether the pipeline is
working. Read it before touching `desktop/background_services/screenshot/`,
`desktop/background_services/sync/sync_service.py`,
`backend/app/services/time_entry_screenshot.py` or
`backend/app/services/google_drive_service.py`.

## The invariant

> If Monitra reports that a screenshot was captured, the screenshot either
> reaches Google Drive **and** gets a `time_entry_screenshots` row carrying the
> Drive file id, or it stays in the desktop's local queue **with its file** and
> is retried until it does. There is no third state.

Every rule below exists to keep that true.

## The path

| Stage | Owner | Durable record |
|---|---|---|
| Capture (one image per capture event, however many displays) | `desktop/background_services/screenshot/screenshot_service.py` on the `TaskRunner` pool | — |
| Optimise (WebP, 1000px reference, adaptive quality) | `screenshot/image_processor.py` | — |
| Local persistence | `screenshot/store.py` → `<data dir>/screenshot-cache/<YYYY-MM-DD>/ss_<uuid>.webp`, written to a temp name and renamed | the file |
| Queue | `desktop/sync/local_cache.py` → `pending_screenshots` (SQLite, WAL) | the row: `status`, `retry_count`, `next_retry_at`, `last_error`, `time_entry_id`, `client_op` |
| Upload | `desktop/background_services/sync/sync_service.py::_upload_one_screenshot`, one multipart `POST /time-entries/{id}/screenshots` per capture, timeout 30 s | the row moves `pending → uploading → (deleted)` |
| API | `backend/app/api/time_entry_screenshot.py::upload_screenshot` | — |
| Validation + authorisation | `backend/app/services/time_entry_screenshot.py::upload_screenshot` (WebP signature, geometry, size; entry must belong to the caller) | — |
| Drive | `backend/app/services/google_drive_service.py`: `<root>/<year>/<month>/User_<id>_<name>/<IST date>/screenshot_<uuid>.webp` | the Drive object |
| Database | `TimeEntryScreenshotRepository.create_uploaded` → `time_entry_screenshots` with `google_drive_file_id`, `google_drive_folder_id`, `upload_status='uploaded'` | the row |
| UI | desktop Activity tab and the web client both read `/time-entry-screenshots/timeline` and `/day`; images stream through `/time-entry-screenshots/{id}/view` | — |

Two consequences of the ordering:

* **A database row exists only after Drive has returned a file id.** Nothing
  in any UI can show a screenshot that is not in Drive. If a screenshot is on
  screen and "not in Drive", it is in Drive; check the folder for the IST day
  (below) and the `google_drive_file_id` on the row.
* **The local file is deleted only after the backend's response carries the
  Drive file id.** A 2xx without one is treated as unconfirmed and retried.

## The desktop's queue states

| `status` | Meaning | Leaves the state when |
|---|---|---|
| `pending`, `time_entry_id IS NULL` | Captured before the backend issued an entry id (offline start). Withheld from upload. | the start lands and `bind_screenshots_to_client_op` adopts it (both in-process and via the durable action queue) |
| `pending`, `next_retry_at` in the future | Waiting out a jittered exponential backoff after a **transient** failure (network, timeout, 5xx, unconfirmed 2xx). Unbounded; the delay doubles from 1 s and is capped at 5 min. | the time passes, a hold ends (`make_screenshots_ready`), or the app relaunches |
| `uploading` | Claimed by the uploader for one attempt. | the attempt finishes; a crash mid-attempt is reset to `pending` at the next launch and at shutdown |
| `failed` (parked) | The backend **refused** it: 401 (session expired), 403, 404, 413, 422. The file is kept. | re-authentication (`resume_after_auth`), a hold ending, the hourly revival (`PARKED_RETRY_INTERVAL_SECONDS`), or a launch |
| row deleted | The backend confirmed the Drive file id. The local file is deleted immediately afterwards. | — |

Nothing deletes a queued file except a confirmed upload, an unreadable or
empty local file, or a logout (`clear_screenshots`).

`BackgroundApi.screenshot_queue_status()` / `count_unattributed_screenshots()`
expose these counts to diagnostics. A non-zero unattributed count that does
not fall is a start whose confirmation never arrived.

## What the backend guarantees

* **Idempotent on `client_screenshot_id`**, in two layers. The row's unique
  index makes a retry after a lost response return the existing record with
  `duplicate: true`. Before uploading, the Drive folder is searched for an
  object of the same name, so a retry after "bytes stored, row not written"
  reuses the object instead of storing a second one.
* **A database failure after the upload keeps the Drive object** and answers
  500, so the desktop retries against the same object.
* **Any Drive failure drops the process-wide folder-id cache.** A folder
  trashed or moved under a long-lived process therefore costs one failed
  upload, not a day of uploads into a folder nobody can see.
* **The day folder is the IST calendar day**, the same day the timeline, the
  admin grid and the desktop Activity tab group by. (It used to be the UTC
  date, which filed every capture between 00:00 and 05:30 IST under the
  previous day.)
* **Read-only Drive calls retry** on 5xx/429/socket errors inside the client
  library; the media upload does not, because the name lookup is the
  idempotent path.
* Status codes the desktop acts on: 503 = storage misconfigured or root not
  accessible (retried as transient, since the fix is server-side and the
  next attempt may succeed); 502 = Drive error (transient); 500 = row not
  written (transient); 401 = hold until re-auth; 403/404/413/422 = parked.

## Observability

Every stage logs one line with a fixed prefix and `key=value` fields, so a
production failure is diagnosable without opening any image.

Desktop (`~/.monitra/logs/monitra.log`, or the data dir's `logs/`):

```
SCREENSHOT_QUEUED id=<uuid> entry=<id> display_count=1 ... bytes=51394
SCREENSHOT_UPLOAD_ATTEMPT id=<uuid> entry=<id> attempt=3 bytes=51394 captured_at=...
SCREENSHOT_UPLOADED id=<uuid> entry=<id> attempt=3 backend_id=4284 drive_file=1Jjo... duplicate=False bytes=51394 elapsed_ms=812 local_file_removed=True
SCREENSHOT_UPLOAD_FAILED id=<uuid> entry=<id> attempt=3 reason=HTTP 502 action=retry next_retry_in=4s detail=...
SCREENSHOT_UPLOAD_REFUSED id=<uuid> entry=<id> attempt=1 http=422 action=parked_with_file next_retry_in=3600s
SCREENSHOT_UPLOAD_HELD id=<uuid> entry=<id> attempt=1 reason=auth_required
SCREENSHOT_REVIVED count=2 reason=hold_ended|re_authenticated|parked_interval_elapsed
SCREENSHOT_DROPPED id=<uuid> entry=<id> reason=local_file_unreadable path=...
```

Backend (`journalctl -u monitra-backend` on the VM):

```
SCREENSHOT_STORAGE_PROBE ok=true root=<id> elapsed_ms=640
SCREENSHOT_STORAGE_PROBE ok=false reason=root_folder_not_accessible root=<id> detail=<operator detail>
SCREENSHOT_FOLDER_CREATED name=2026-09-28 parent=<id>
SCREENSHOT_STORED client_id=<uuid> user=281 entry=3503 id=4290 folder=<id> drive_file=<id> path=2026/September/User_281_.../2026-09-28/screenshot_<uuid>.webp bytes=51394 geometry=1000x1000 displays=1 drive_reused=False elapsed_ms=1420
SCREENSHOT_DUPLICATE client_id=<uuid> user=281 entry=3503 stored_id=4290 drive_file=<id> reason=already_stored|concurrent_upload
SCREENSHOT_UPLOAD_FAILED stage=drive client_id=<uuid> user=281 entry=3503 outcome=502|503 reason=... detail=...
SCREENSHOT_DB_WRITE_FAILED client_id=<uuid> ... drive_file=<id>; the Drive object is kept and the client will retry against it
```

No log line ever contains the key, a token, or image bytes.

## Checking a deployment

1. `GET /health` → `screenshot_storage.configured` says the two settings are
   present; `screenshot_storage.probe.ok` says the configured credential can
   actually open the root and create files in it. `reason` is one of
   `root_folder_not_accessible`, `credentials_unreadable`,
   `credentials_rejected`, `client_library_missing`, `drive_api_timeout`,
   `drive_api_error`, or `pending` for the first seconds after boot. The
   operator-facing detail is in the backend log.
2. On the machine that runs the backend, with the backend's own environment:

   ```bash
   # development checkout
   cd backend && python scripts/drive_diagnostic.py

   # production VM (systemd loads /etc/monitra/backend.env for the service)
   sudo -u monitra bash -c 'set -a; . /etc/monitra/backend.env; set +a; \
     cd /opt/monitra/backend && /opt/monitra/venv/bin/python scripts/drive_diagnostic.py'
   ```

   Twelve steps, stopping at the first failure: configuration, credential,
   root visible, root writable, year/month/user/date folders, an upload that
   is retried and reused, read-back, the file id, and the database schema.
   Everything it creates is under `User_0_MONITRA DIAGNOSTIC` and is removed.
3. The real end-to-end suite, against a local backend, the development
   database, a real display and the real shared drive:

   ```bash
   cd desktop && MONITRA_E2E=1 python -m pytest tests/e2e/test_screenshot_lifecycle_e2e.py -q -s
   ```

   It covers the happy path, a queued (offline) start, idempotent replay,
   multi-display merging, an offline capture, a Drive outage followed by
   recovery on a different backend process, and a desktop restart with an
   upload outstanding.

## Production configuration (backend VM)

| Setting | Where | Value |
|---|---|---|
| `GOOGLE_DRIVE_ROOT_FOLDER_ID` | `/etc/monitra/backend.env` | the shared drive or folder id (a copied Drive URL is accepted) |
| `GOOGLE_SERVICE_ACCOUNT_JSON_PATH` | `/etc/monitra/backend.env` | `/etc/monitra/google-service-account.json`, readable by the `monitra` user (`root:monitra`, mode 640) |
| Drive share | Google Drive | the service account's `client_email` added to the shared drive as **Content manager** (a folder in My Drive: **Editor**) |
| Drive API | Google Cloud project that owns the service account | enabled |
| nginx | `deploy/backend/nginx-api*.conf` | `client_max_body_size 10m`, `proxy_read_timeout 60s` (an upload is a few hundred KB and one Drive round trip) |

`/health` must show `probe.ok: true` after a deploy; `deploy.sh` waits for
`/health` to answer, not for the probe, so check it by hand once.

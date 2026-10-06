# Feedback attachments

An employee may attach up to three screenshots to a Feedback & Help message.
The attachment is **optional**: category and message stay required, and a
submission with no file behaves exactly as it always did.

This document is authoritative for the feature. Code: `backend/app/services/feedback_attachments.py`
(validation, storage, reads), `backend/app/api/feedback.py` (routes),
`desktop/ui/feedback_dialog.py` + `desktop/app/feedback/service.py` (client),
`frontend/src/features/feedback/FeedbackAttachments.tsx` (admin view).

## What is accepted

| | |
|---|---|
| Types | PNG, JPEG (`.jpg`/`.jpeg`), WebP — images only |
| Count | 3 per submission (`FEEDBACK_ATTACHMENT_MAX_COUNT`) |
| Size | 10 MB **total** per submission (`FEEDBACK_ATTACHMENT_MAX_TOTAL_BYTES`) |

**PDF is deliberately not supported.** The feature exists so someone can *show*
what went wrong; an image does that. An image's safety can be established by
decoding it, a PDF's cannot (it is an active-content container). Adding one later
is one entry in `ALLOWED_ATTACHMENT_TYPES` plus a validator for it. The admin
view already renders any non-image as a file card with a Download button.

Everything else is refused, not converted: executables (`.exe .bat .cmd .msi .dll`),
scripts, archives, SVG (it can carry script), GIF, HTML.

## API

`POST /feedback` (JSON) is **unchanged** and remains the route for a message with
no attachment. A message with attachments uses:

`POST /feedback/with-attachments` — `multipart/form-data`, bearer auth

| Field | |
|---|---|
| `category`, `message` | exactly the rules of the JSON route |
| `client_op` | idempotency key, 8–64 chars of `A-Za-z0-9_-` (the desktop sends a fresh `uuid4().hex` per attempt) |
| `files` | zero to three repeated file parts |

`201` → `{id, category, message, status, created_at, attachments: [...], duplicate}`.
`duplicate: true` means this `client_op` was already stored (a retry after a lost
reply): nothing was created or uploaded again, and the original record is returned.

Errors are `{"detail": "<a sentence a person can read>"}`: `422` (bad field, type
not allowed, content not the image its name says, empty file, more than 3 files),
`413` (over the size limit), `502`/`503` (storage unavailable or not configured).

`GET /feedback`, `GET /feedback/{id}`, `GET /feedback/my` and the status-update
response carry `attachment_count` and `attachments: [{id, original_filename,
content_type, file_size, created_at, is_image}]` — **metadata only**, fetched for a
whole page in one query. Every feedback that predates this feature reads `0` / `[]`.
Storage ids, paths and URLs are never in a response.

`GET /feedback/attachments/{id}/content[?download=true]` returns the bytes.

## Storage

The existing private Google Drive (`google_drive_service`), the same store and the
same credentials as screenshots — nothing new to configure beyond
`GOOGLE_DRIVE_ROOT_FOLDER_ID` and a service-account key, which an environment that
captures screenshots already has. Layout: `<root>/Feedback/<YYYY-MM>/u<user>_<client_op>_<n>.<ext>`.

The object name is **server-generated** from validated identifiers; the uploader's
file name is kept only as display text (`original_filename`, reduced to a printable
basename) and is never used in a path. Nothing is written to the backend's local
disk, so an ephemeral or serverless deployment loses nothing.

## Security

* **Validation, before anything is stored** (`validate_upload`): the extension, the
  declared content type and the file's own signature must all agree on one allowed
  type; the bytes must decode as that image (Pillow) within 50 megapixels; an empty
  file is refused. The bytes are the authority — a renamed executable keeps its
  signature. One bad file among three stores nothing.
* **Access** (`FeedbackAttachmentService._may_read`): the audience of the feedback
  itself, no wider — Admin, HR and Leader inside the feedback's own organization
  (`FEEDBACK_VIEW_ALL_ROLES`), and the person who submitted it. Everyone else —
  another employee, a manager, another organization, a guessed id — gets `404`,
  identical to an id that does not exist.
* **Serving**: bytes are proxied through the backend, never linked; the Drive
  object is private and no URL or credential reaches a client. The type served is
  the one validated at upload, with `X-Content-Type-Options: nosniff`, a sandboxing
  CSP and `Cache-Control: private`. The bytes are re-sniffed on the way out, so an
  object swapped in Drive out of band is refused rather than relayed.
* Logs (`FEEDBACK_ATTACHMENT_*`) carry ids, counts and reasons — never file
  content, tokens or signed URLs.

## Consistency

Order: validate everything → upload to Drive → write the feedback **and** every
attachment row in one transaction → queue the email.

* A failure while uploading removes what that call had uploaded and stores no
  feedback.
* A failure writing the rows rolls back (no feedback, no rows) and removes the
  objects. A removal that cannot be confirmed is logged as
  `FEEDBACK_ATTACHMENT_ORPHAN … drive_file=<id>`; because object names are derived
  from `client_op`, the retry reuses the leftover object instead of storing a copy.
* **Orphan sweep**: `python scripts/feedback_attachment_sweep.py` lists (default) or
  with `--apply` deletes objects under `Feedback/*` that no row references and that
  are older than `--min-age-hours` (24). Run it periodically, and after deleting a
  user or organization (the rows cascade; Drive does not).
* Double submission: the desktop disables Submit, de-duplicates in flight, and
  reuses one `client_op` across retries of unchanged content; the unique
  `(user_id, client_op)` index makes two concurrent attempts collide, and the loser
  returns the winner.

## Email

The Admin/HR notification gains an attachment count and a link to the dashboard;
the files are never attached. See [Email_Notifications.md](Email_Notifications.md).

## Deployment and migration

* Migration `d4a7e1c93b58` adds `feedback_attachments` and a nullable
  `feedback_requests.client_op` with a *partial* unique index. Additive, nothing
  backfilled, reversible (`alembic downgrade -1`); every existing feedback is
  untouched. Apply with `python -m alembic upgrade head` from `backend/`.
* **Request-body limits.** The backend accepts 10 MB, but a serverless platform may
  cap a request body below that (Vercel Functions: 4.5 MB). The platform answers
  with `413` before this code runs; the desktop turns any `413` into
  "Attachment is too large. Maximum allowed size is 10 MB." Where that cap applies,
  attachments larger than it cannot be accepted until the upload goes direct to
  storage. This is a deployment constraint, not something the code can lift.
* Without Drive configured, feedback with no attachment is unaffected; one with an
  attachment is refused with `503` and nothing is saved (`/health` reports storage).

## Testing

```bash
cd backend  && python -m pytest tests/test_feedback_attachments.py -q   # validation, routes over real SQLite, access, atomicity, email
cd desktop  && python -m pytest tests/test_feedback_dialog.py tests/test_feedback_attachments.py -q
cd frontend && npx vitest run src/features/feedback
```

The backend routes are tested over a real SQLite session with only Drive faked, so
the unique index, the rollback and the single-transaction guarantee are exercised
for real. A real end-to-end run needs the local backend, the development database
and a Drive root, with `EMAIL_PROVIDER=console` so nothing is sent.

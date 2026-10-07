"""google_drive_service — The only place this application talks to Google Drive.

Credential handling
-------------------
The service account key is read from settings, which read it from the
environment. It is never logged, never returned in a response, and never sent
to the desktop client: the desktop uploads an image to this backend over its
ordinary authenticated session, and this module is what puts the bytes in
Drive. `describe_configuration()` deliberately reports only whether a
credential is present and which folder is the root — never any part of the key
itself, so it is safe in a health endpoint and in a log line.

Folder layout
-------------
    <root>/<year>/<month name>/User_<id>/<YYYY-MM-DD>/<file>

Each level is looked up by name inside its parent and created only if missing.
Two things make that safe under concurrency, which matters because a dozen
desktop clients can upload their first screenshot of a month within the same
second:

* Lookups are cached per (parent, name) for the process lifetime. Folder ids
  never change, and the alternative is a Drive query per level per upload —
  four extra round trips on every screenshot.
* A create that races another creates a *second* folder with the same name,
  because Drive permits duplicate names. So after creating, the code re-queries
  and keeps the **oldest** match, and every process converges on the same
  folder. Without that step a month boundary would quietly fan a team's
  screenshots across several identically named folders.

The Google client libraries are imported lazily so the rest of the backend —
and its test suite — runs without them installed.
"""
from __future__ import annotations

import io
import json
import logging
import threading
import time
from collections import OrderedDict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Dict, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from app.core.config import settings

logger = logging.getLogger(__name__)

#: Drive's own type for a folder.
FOLDER_MIME = "application/vnd.google-apps.folder"

#: Write + read of files this service account created. Deliberately not
#: `drive`, which would grant the whole of the user's Drive.
SCOPES = ["https://www.googleapis.com/auth/drive"]


class _ImageCache:
    """A bounded in-memory cache of screenshot bytes, keyed by Drive file id.

    A stored screenshot never changes — the id is minted once, the object is
    written once, and nothing ever rewrites it — so caching it needs no
    invalidation beyond eviction. Without this, painting a grid of twelve
    thumbnails meant twelve Drive round trips *every time the tab was opened*,
    which is what made previews take seconds to appear and what made them time
    out often enough to be noticed.

    Eviction is least-recently-used against a byte budget rather than an entry
    count, because entry count says nothing about memory when items differ by
    an order of magnitude in size. The cache is per process: a serverless
    instance simply starts cold, which is correct rather than merely tolerable.
    """

    def __init__(self, max_bytes: int = 64 * 1024 * 1024) -> None:
        self._max_bytes = max_bytes
        self._entries: "OrderedDict[str, bytes]" = OrderedDict()
        self._size = 0
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> Optional[bytes]:
        with self._lock:
            data = self._entries.get(key)
            if data is None:
                self.misses += 1
                return None
            self._entries.move_to_end(key)
            self.hits += 1
            return data

    def put(self, key: str, data: bytes) -> None:
        if not data or len(data) > self._max_bytes:
            return
        with self._lock:
            if key in self._entries:
                self._size -= len(self._entries.pop(key))
            self._entries[key] = data
            self._size += len(data)
            while self._size > self._max_bytes and self._entries:
                _, evicted = self._entries.popitem(last=False)
                self._size -= len(evicted)

    def discard(self, key: str) -> None:
        """Forget one entry, for the rare case where an object is deleted."""
        with self._lock:
            data = self._entries.pop(key, None)
            if data is not None:
                self._size -= len(data)

    def stats(self) -> dict:
        with self._lock:
            return {
                "entries": len(self._entries),
                "bytes": self._size,
                "hits": self.hits,
                "misses": self.misses,
            }


class GoogleDriveError(RuntimeError):
    """A Drive operation failed. Carries no credential material."""


class GoogleDriveNotAccessible(GoogleDriveError):
    """The configured root folder cannot be reached by the service account.

    Kept distinct from the general error because the two need opposite
    responses. A transient Drive failure is worth retrying; this one never
    resolves on its own — the folder is not shared, or the id names a shared
    drive this account is not a member of — and Drive reports it as a bare 404
    that reads exactly like a missing file. Surfacing it as "temporarily
    unavailable" would have every client in the fleet retry a permanent
    misconfiguration until their retry budgets ran out.
    """


class GoogleDriveFileNotFound(GoogleDriveError):
    """The object named by a file id is not in Drive.

    Raised by `download_file` (so a viewer is told the image is *missing*, not
    that storage is having a bad minute) and by `delete_file_strict`, where "it
    is already gone" is the one
    failure that must not block the caller: a screenshot whose bytes were
    removed out of band would otherwise be undeletable forever, its metadata
    pinned in the database by a file that no longer exists.
    """


#: Socket timeout for one Drive call. A stalled read must fail rather than
#: hold a threadpool slot indefinitely: the pool is what serves every other
#: endpoint, so one wedged Drive read otherwise takes the whole API with it.
DRIVE_TIMEOUT_SECONDS = 20

#: How many times a *read-only or idempotent* Drive call is re-sent by the
#: client library on a 5xx, a 429 or a dropped socket, with its own
#: exponential backoff. Folder lookups and folder creation are safe to repeat
#: (creation re-queries and keeps the oldest, see the module docstring). The
#: media upload is deliberately **not** retried by the library: a re-sent
#: upload after a response lost in transit would store the image twice, and
#: the desktop's own retry -- which goes through `find_file` first -- is the
#: idempotent path for that case.
DRIVE_READ_RETRIES = 3

#: How long a successful reachability probe is trusted before `/health`
#: re-runs it in the background. A failed probe is re-run sooner, so an
#: operator fixing a share sees the result without waiting.
PROBE_TTL_SECONDS = 600
PROBE_FAILURE_TTL_SECONDS = 60

#: Longest display name allowed in a folder name. Drive permits far more, but
#: the logical path built from it is stored on every screenshot row and shown
#: in support tooling, so an unbounded name would push real detail off-screen.
MAX_NAME_SEGMENT = 60


def _sanitize_folder_segment(value: Optional[str]) -> str:
    """Make a display name safe to use as one folder-name segment.

    A display name is user-editable text. Two characters matter: ``/``, which
    would read as a separator inside the logical path stored on every row, and
    control characters, which Drive accepts but which corrupt any log line the
    path lands in. Whitespace is collapsed so ``"Hardik   Raval"`` and
    ``"Hardik Raval"`` cannot become two different folders for one person.
    """
    if not value:
        return ""
    text = str(value).replace("/", "-").replace("\\", "-")
    text = "".join(ch for ch in text if ch.isprintable())
    text = " ".join(text.split())
    return text[:MAX_NAME_SEGMENT].strip()


def normalize_folder_id(value: str) -> str:
    """Accept either a Drive folder id or the URL a person actually copies.

    Nobody reads a folder id out of Drive; they open the folder and copy the
    address bar. Passing that whole URL to the API as a parent id fails with a
    404 that says nothing about the real cause, and the operator's natural next
    move — re-checking that the folder is shared — does not fix it. Since the
    id is unambiguously recoverable from every URL form Drive produces, taking
    the URL is strictly better than rejecting it.

    Handles:
        https://drive.google.com/drive/folders/<id>?usp=sharing
        https://drive.google.com/drive/u/0/folders/<id>
        https://drive.google.com/open?id=<id>
        <id>
    """
    text = (value or "").strip().strip('"').strip("'")
    if not text or "/" not in text and "?" not in text:
        return text

    parsed = urlparse(text)
    query_id = parse_qs(parsed.query).get("id")
    if query_id and query_id[0]:
        return query_id[0]

    segments = [segment for segment in parsed.path.split("/") if segment]
    if "folders" in segments:
        index = segments.index("folders")
        if index + 1 < len(segments):
            return segments[index + 1]
    return segments[-1] if segments else text


def _http_status(exc: BaseException) -> Optional[int]:
    """The HTTP status a googleapiclient `HttpError` carries, else None."""
    resp = getattr(exc, "resp", None)
    status = getattr(resp, "status", None)
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


class GoogleDriveService:
    """Uploads and reads screenshot objects. One instance per process."""

    def __init__(self) -> None:
        #: One Drive client per thread; see `_client`.
        self._local = threading.local()
        self._lock = threading.Lock()
        #: (parent_id, name) -> folder id.
        self._folder_cache: Dict[Tuple[str, str], str] = {}
        #: Drive file id -> stored bytes. See _ImageCache.
        self.image_cache = _ImageCache()
        #: Last reachability probe, see `probe()`. Guarded by `_probe_lock`;
        #: `_probe_running` stops `/health` from stacking refreshes.
        self._probe_lock = threading.Lock()
        self._probe_result: Optional[dict] = None
        self._probe_running = False

    def invalidate_folder_cache(self) -> None:
        """Forget every cached folder id.

        Folder ids never change, but a folder can stop being the right target:
        an administrator tidying Drive can trash or move a day or user folder
        while a long-lived backend process still holds its id. Drive then
        accepts uploads into the trashed parent without complaint -- the file
        exists, the row references it, the timeline serves it, and nobody
        browsing Drive can find it. The upload path calls this on any Drive
        failure so the next attempt re-resolves the tree from the root; the
        cost is four lookups, once.
        """
        with self._lock:
            self._folder_cache.clear()

    # ── Configuration ─────────────────────────────────────────────────────────

    @property
    def configured(self) -> bool:
        return settings.google_drive_configured

    @property
    def root_folder_id(self) -> str:
        """The configured root, as an id even when a URL was supplied."""
        return normalize_folder_id(settings.GOOGLE_DRIVE_ROOT_FOLDER_ID)

    def unconfigured_reason(self) -> Optional[str]:
        """Why storage cannot work, or None. Logged when an upload is refused.

        An upload that fails with a bare "not configured" tells an operator
        nothing about which of the two settings is missing, and the desktop's
        message cannot say more than that without leaking configuration to a
        client. So the detail goes to the server log instead.
        """
        if not settings.GOOGLE_DRIVE_ROOT_FOLDER_ID:
            return "GOOGLE_DRIVE_ROOT_FOLDER_ID is not set"
        if not (settings.GOOGLE_SERVICE_ACCOUNT_JSON or settings.GOOGLE_SERVICE_ACCOUNT_JSON_PATH):
            return (
                "neither GOOGLE_SERVICE_ACCOUNT_JSON nor "
                "GOOGLE_SERVICE_ACCOUNT_JSON_PATH is set"
            )
        return None

    def describe_configuration(self) -> dict:
        """Non-sensitive summary, safe to log or return in diagnostics."""
        return {
            "configured": self.configured,
            "root_folder_id": self.root_folder_id or None,
            "credential_source": (
                "inline" if settings.GOOGLE_SERVICE_ACCOUNT_JSON
                else "file" if settings.GOOGLE_SERVICE_ACCOUNT_JSON_PATH
                else None
            ),
        }

    @staticmethod
    def _key_file_path() -> str:
        """Resolve the key file relative to the backend, not to the CWD.

        `GOOGLE_SERVICE_ACCOUNT_JSON_PATH` is naturally written relative
        (`./secrets/google-service-account.json`), and a relative path is
        resolved against whatever directory the server was launched from — the
        repository root, a systemd unit's WorkingDirectory, a serverless
        function's sandbox. The same configuration then works for one operator
        and fails for the next, reporting only "file not found".
        """
        raw = settings.GOOGLE_SERVICE_ACCOUNT_JSON_PATH
        if not raw:
            return raw
        path = Path(raw).expanduser()
        if path.is_absolute():
            return str(path)
        # <backend>/ — this file is app/services/google_drive_service.py.
        backend_root = Path(__file__).resolve().parents[2]
        candidate = (backend_root / path).resolve()
        if candidate.exists():
            return str(candidate)
        return str(path)

    def _credentials(self):
        from google.oauth2 import service_account  # type: ignore

        if settings.GOOGLE_SERVICE_ACCOUNT_JSON:
            try:
                info = json.loads(settings.GOOGLE_SERVICE_ACCOUNT_JSON)
            except json.JSONDecodeError as exc:
                # The message deliberately does not include the value.
                raise GoogleDriveError(
                    "GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON"
                ) from exc
            return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)

        path = self._key_file_path()
        try:
            return service_account.Credentials.from_service_account_file(path, scopes=SCOPES)
        except FileNotFoundError as exc:
            raise GoogleDriveError(
                f"the service account key file was not found at {path}"
            ) from exc

    def _client(self):
        """This thread's Drive client.

        One client **per thread**, not one per process. `googleapiclient` is
        built on `httplib2`, which is explicitly not thread-safe, and FastAPI
        runs every `def` route in a threadpool — so a grid of thumbnails means
        several threads reaching for the same connection at once. Sharing one
        client did not corrupt anything visibly; it serialised. Measured: six
        concurrent reads that take about a second each all returned together
        after 6.4 seconds, and an unrelated `/docs` request queued behind them
        for two more. On a real grid that pushed image requests past the
        client's timeout, and pushed `/time-entries` and `/app-usage` past
        theirs, which the desktop reported as the backend being unreachable.

        Thread-local is correct here in a way it is not on the desktop side:
        these are ordinary Python threads created by anyio, not Qt threads, so
        `threading.local` keys on a thread object that actually lives as long
        as the thread. The pool is bounded, so the number of clients is too.
        """
        cached = getattr(self._local, "service", None)
        if cached is not None:
            return cached
        service = self._build_service()
        self._local.service = service
        return service

    def _build_service(self):
        """Construct one thread's client.

        Deliberately unlocked. Nothing shared is mutated here — each thread
        builds its own client — and building involves reading the key file and
        acquiring an OAuth token, which is network I/O. Holding a process-wide
        lock across that serialised the first request of every thread behind
        the others: a cold grid of six thumbnails took 4.4s, most of it spent
        queueing to build clients that do not contend in the first place.
        """
        if not self.configured:
            raise GoogleDriveError(
                "Google Drive is not configured: set GOOGLE_DRIVE_ROOT_FOLDER_ID "
                "and one of GOOGLE_SERVICE_ACCOUNT_JSON / "
                "GOOGLE_SERVICE_ACCOUNT_JSON_PATH"
            )
        try:
            from googleapiclient.discovery import build  # type: ignore
        except ImportError as exc:
            raise GoogleDriveError(
                "google-api-python-client is not installed on this backend"
            ) from exc
        try:
            # Each client gets its own socket, with an explicit timeout.
            # Without one, httplib2 blocks forever on a stalled read and the
            # threadpool slot never comes back — the failure mode that takes a
            # whole backend down rather than one request.
            import httplib2  # type: ignore
            import google_auth_httplib2  # type: ignore

            http = google_auth_httplib2.AuthorizedHttp(
                self._credentials(), http=httplib2.Http(timeout=DRIVE_TIMEOUT_SECONDS)
            )
            return build("drive", "v3", http=http, cache_discovery=False)
        except GoogleDriveError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise GoogleDriveError(f"could not initialise the Drive client: {exc}") from exc

    # ── Folders ───────────────────────────────────────────────────────────────

    @staticmethod
    def _escape(name: str) -> str:
        """Escape a name for a Drive query literal."""
        return name.replace("\\", "\\\\").replace("'", "\\'")

    def _service_account_email(self) -> str:
        """The address the root folder has to be shared with. Log-only."""
        try:
            if settings.GOOGLE_SERVICE_ACCOUNT_JSON:
                return json.loads(settings.GOOGLE_SERVICE_ACCOUNT_JSON).get("client_email", "?")
            with open(self._key_file_path(), encoding="utf-8") as handle:
                return json.load(handle).get("client_email", "?")
        except Exception:  # noqa: BLE001
            return "?"

    def verify_root_access(self) -> None:
        """Confirm the configured root exists and is writable by this account.

        :raises GoogleDriveNotAccessible: with an actionable message naming the
            address the folder must be shared with. That message is for the
            server log and for an operator running this check by hand — it is
            never returned to a client.
        """
        root = self.root_folder_id
        try:
            info = self._client().files().get(
                fileId=root,
                fields="id, name, driveId, trashed, capabilities(canAddChildren)",
                supportsAllDrives=True,
            ).execute(num_retries=DRIVE_READ_RETRIES)
        except GoogleDriveError:
            raise
        except Exception as exc:  # noqa: BLE001
            if "notFound" in str(exc) or "File not found" in str(exc):
                raise GoogleDriveNotAccessible(
                    f"the configured root folder '{root}' is not visible to the "
                    f"service account {self._service_account_email()}. Share the "
                    f"folder (or add the account to the shared drive) with "
                    f"Editor/Content manager access, and check that "
                    f"GOOGLE_DRIVE_ROOT_FOLDER_ID names that folder"
                ) from exc
            raise GoogleDriveError(f"could not read the root folder: {exc}") from exc
        # Visible is not the same as usable. A viewer-only share, or a root
        # that was trashed, both answer the lookup and then fail every upload
        # with an error that reads like a transient one.
        if info.get("trashed"):
            raise GoogleDriveNotAccessible(
                f"the configured root folder '{root}' is in the trash"
            )
        if info.get("capabilities", {}).get("canAddChildren") is False:
            raise GoogleDriveNotAccessible(
                f"the service account {self._service_account_email()} can see the "
                f"root folder '{root}' but cannot create files in it; it needs "
                f"Editor (or Content manager) access, not Viewer"
            )

    # ── Reachability probe ────────────────────────────────────────────────────

    @staticmethod
    def _classify_failure(exc: BaseException) -> str:
        """A coarse, non-sensitive reason code for a failed probe.

        Returned by `/health`, which is public, so it names the *class* of
        problem and never the message: the message can carry the key path or
        the service-account address, which describe this deployment to
        whoever asked.
        """
        if isinstance(exc, GoogleDriveNotAccessible):
            return "root_folder_not_accessible"
        text = str(exc)
        if "not valid JSON" in text or "key file was not found" in text:
            return "credentials_unreadable"
        if "invalid_grant" in text or "Invalid JWT" in text or "invalid_client" in text:
            return "credentials_rejected"
        if "not installed" in text:
            return "client_library_missing"
        if "timed out" in text or "Timeout" in text or "timeout" in text:
            return "drive_api_timeout"
        return "drive_api_error"

    def probe(self, force: bool = False) -> dict:
        """Actually talk to Drive, and say whether screenshots can be stored.

        `configured` only says two settings are non-empty. It was true on a
        deployment whose key file did not exist, on one whose service account
        was not a member of the shared drive, and on one whose share was
        viewer-only -- each of which refused every upload with a 5xx while
        `/health` reported storage as configured. This is the check that
        distinguishes them, and it is cached so that a health poll never costs
        a Drive round trip on the request path.

        With `force`, runs synchronously on the caller's thread and returns
        the fresh result -- for the diagnostic script and for tests. Otherwise
        never blocks: it returns the cached result, refreshing a stale one on
        a background thread, and answers ``ok: None, reason: "pending"`` until
        the first probe after boot has finished. Never raises.

        :return: ``{"ok": bool|None, "checked_at": iso8601|None, "reason": code|None}``.
        """
        if force:
            return self._public_probe(self._run_probe())
        with self._probe_lock:
            cached = self._probe_result
        if cached is None:
            self._refresh_probe_in_background()
            return {"ok": None, "reason": "pending", "checked_at": None}
        age = time.time() - cached["_checked_epoch"]
        ttl = PROBE_TTL_SECONDS if cached["ok"] else PROBE_FAILURE_TTL_SECONDS
        if age > ttl:
            self._refresh_probe_in_background()
        return self._public_probe(cached)

    def _refresh_probe_in_background(self) -> None:
        with self._probe_lock:
            if self._probe_running:
                return
            self._probe_running = True
        thread = threading.Thread(
            target=self._run_probe, name="drive-probe", daemon=True
        )
        thread.start()

    def _run_probe(self) -> dict:
        started = time.time()
        result: dict
        try:
            if not self.configured:
                raise GoogleDriveError(self.unconfigured_reason() or "not configured")
            self.verify_root_access()
            result = {"ok": True, "reason": None}
            logger.info(
                "SCREENSHOT_STORAGE_PROBE ok=true root=%s elapsed_ms=%d",
                self.root_folder_id, int((time.time() - started) * 1000),
            )
        except Exception as exc:  # noqa: BLE001
            reason = self._classify_failure(exc)
            result = {"ok": False, "reason": reason}
            # The detail is for the log: it may name the key path or the
            # service account, which is exactly what an operator needs and
            # exactly what a public endpoint must not return.
            logger.error(
                "SCREENSHOT_STORAGE_PROBE ok=false reason=%s root=%s detail=%s",
                reason, self.root_folder_id, exc,
            )
        result["_checked_epoch"] = time.time()
        with self._probe_lock:
            self._probe_result = result
            self._probe_running = False
        return result

    @staticmethod
    def _public_probe(result: dict) -> dict:
        return {
            "ok": bool(result["ok"]),
            "reason": result.get("reason"),
            "checked_at": datetime.fromtimestamp(
                result["_checked_epoch"], tz=timezone.utc
            ).isoformat(timespec="seconds"),
        }

    def _find_folder(self, parent_id: str, name: str) -> Optional[str]:
        """The oldest folder with this name under `parent_id`, or None.

        Oldest, not first: if a race once created duplicates, every process
        must agree on which one is canonical, and creation time is the only
        ordering both of them can see.
        """
        query = (
            f"name = '{self._escape(name)}' and mimeType = '{FOLDER_MIME}' "
            f"and '{parent_id}' in parents and trashed = false"
        )
        response = self._client().files().list(
            q=query,
            fields="files(id, name, createdTime)",
            orderBy="createdTime",
            pageSize=10,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute(num_retries=DRIVE_READ_RETRIES)
        files = response.get("files", [])
        return files[0]["id"] if files else None

    def find_file(self, folder_id: str, file_name: str) -> Optional[str]:
        """The id of a non-folder object called `file_name` in `folder_id`.

        This is what makes an upload idempotent at the Drive level, not only
        at the database level. The row's unique `client_screenshot_id` stops
        a retry from storing a second *record*; it cannot stop a second
        *object* when the first attempt stored the bytes and then failed
        before the row was written -- a database blip, a process killed
        mid-request. The retry then finds no row, and without this lookup
        uploads the same image again next to the orphan. Screenshot file
        names carry the client id, so the name is the key.

        Oldest first, for the same reason as `_find_folder`: if two objects
        ever exist, every caller must settle on the same one.
        """
        query = (
            f"name = '{self._escape(file_name)}' and mimeType != '{FOLDER_MIME}' "
            f"and '{folder_id}' in parents and trashed = false"
        )
        response = self._client().files().list(
            q=query,
            fields="files(id, name, createdTime)",
            orderBy="createdTime",
            pageSize=5,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute(num_retries=DRIVE_READ_RETRIES)
        files = response.get("files", [])
        return files[0]["id"] if files else None

    def ensure_folder(self, parent_id: str, name: str) -> str:
        """Return the id of `name` under `parent_id`, creating it if missing."""
        key = (parent_id, name)
        cached = self._folder_cache.get(key)
        if cached:
            return cached

        existing = self._find_folder(parent_id, name)
        if existing:
            self._folder_cache[key] = existing
            return existing

        try:
            # Safe to let the library retry: a create that went through before
            # the response was lost is reconciled by the re-query below, which
            # keeps the oldest of any duplicates.
            created = self._client().files().create(
                body={"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]},
                fields="id",
                supportsAllDrives=True,
            ).execute(num_retries=DRIVE_READ_RETRIES)
            created_id = created.get("id") if isinstance(created, dict) else None
            logger.info("SCREENSHOT_FOLDER_CREATED name=%s parent=%s", name, parent_id)
        except Exception as exc:  # noqa: BLE001
            # Drive reports an unreachable parent as a plain 404 on the parent
            # id, which reads like "the file you asked for is missing" and sends
            # an operator looking in the wrong place entirely. Translate it once,
            # here, into the thing that is actually wrong.
            if "notFound" in str(exc) or "File not found" in str(exc):
                raise GoogleDriveNotAccessible(
                    f"cannot create '{name}': the parent folder '{parent_id}' is "
                    f"not visible to the service account "
                    f"{self._service_account_email()}. Share it with "
                    f"Editor/Content manager access"
                ) from exc
            raise

        # Re-query rather than trusting the id just created: another process
        # may have created the same folder in the same instant, and both must
        # settle on the same one. See the module docstring.
        canonical = self._find_folder(parent_id, name)
        if not canonical and created_id:
            # Drive's listing is eventually consistent: a folder created a
            # moment ago can be absent from a query that follows it, which
            # happens to be exactly the first time any new folder is made. The
            # id the create call returned is authoritative, so use it rather
            # than failing an upload over a lagging index. (A race that made a
            # duplicate is then not converged this once; the next lookup does.)
            canonical = created_id
        if not canonical:
            raise GoogleDriveError(f"could not create or locate the folder '{name}'")
        self._folder_cache[key] = canonical
        return canonical

    def user_folder_name(self, user_id: int, user_name: Optional[str] = None) -> str:
        """The per-user folder name: ``User_145_Hardik Raval``.

        The id leads, because it is the only part that is guaranteed unique and
        never changes — two people can share a display name, and one person can
        change theirs. The name follows so a human browsing Drive can tell
        whose folder they are looking at without cross-referencing the
        database, which was the whole point of adding it.

        Names are sanitised rather than trusted: a display name is user-editable
        text, and a ``/`` in it would read as a folder separator in the logical
        path stored on every row.
        """
        base = f"User_{user_id}"
        cleaned = _sanitize_folder_segment(user_name)
        return f"{base}_{cleaned}" if cleaned else base

    def ensure_screenshot_folder(
        self, user_id: int, captured_on: date, user_name: Optional[str] = None
    ) -> Tuple[str, str]:
        """
        Resolve (and create) `<root>/Year/Month/User_<id>_<name>/<date>`.

        A new year, month, user or date folder appears automatically the first
        time a screenshot needs it — there is nothing to provision by hand.

        Folders created before names were included are named plain ``User_<id>``.
        Rather than leaving one person's screenshots split across two folders
        that differ only in whether the name is present, a legacy folder is
        renamed in place the first time it is resolved: the id prefix makes the
        match unambiguous, and renaming preserves the folder id, so every
        stored file stays exactly where its database row says it is.

        :return: `(folder_id, logical_path)`. The logical path is stored on the
            row so a human can find the object in the Drive UI.
        """
        root = self.root_folder_id
        year = f"{captured_on.year:04d}"
        month = captured_on.strftime("%B")
        user = self.user_folder_name(user_id, user_name)
        day = captured_on.isoformat()

        year_id = self.ensure_folder(root, year)
        month_id = self.ensure_folder(year_id, month)
        user_id_folder = self._ensure_user_folder(month_id, user_id, user)
        day_id = self.ensure_folder(user_id_folder, day)
        return day_id, f"{year}/{month}/{user}/{day}"

    #: The top-level folder feedback attachments live under, beside the year
    #: folders the screenshots use. A fixed name rather than a setting: it is
    #: part of the layout operators browse and the orphan sweep walks.
    FEEDBACK_FOLDER_NAME = "Feedback"

    def ensure_feedback_folder(self, stored_on: date) -> Tuple[str, str]:
        """Resolve (and create) ``<root>/Feedback/<YYYY-MM>``.

        One folder per month, flat inside it: a feedback attachment is rare
        next to a screenshot (a handful a day at most), so per-user and per-day
        levels would be mostly one-file folders. The owner is in the object's
        name (``u<user>_<client_op>_<n>.<ext>``), which is what an operator
        browsing Drive needs.

        :return: `(folder_id, logical_path)`.
        """
        root = self.root_folder_id
        month = f"{stored_on.year:04d}-{stored_on.month:02d}"
        feedback_id = self.ensure_folder(root, self.FEEDBACK_FOLDER_NAME)
        month_id = self.ensure_folder(feedback_id, month)
        return month_id, f"{self.FEEDBACK_FOLDER_NAME}/{month}"

    def list_feedback_objects(self) -> list:
        """Every file under ``<root>/Feedback/*``, for the orphan sweep.

        Returns ``[{"id", "name", "createdTime", "folder"}]``. Read-only and
        paged; it never creates the folder tree, so a deployment that has not
        stored an attachment yet simply answers an empty list.
        """
        feedback_id = self._find_folder(self.root_folder_id, self.FEEDBACK_FOLDER_NAME)
        if not feedback_id:
            return []
        found: list = []
        months = self._client().files().list(
            q=(
                f"mimeType = '{FOLDER_MIME}' and '{feedback_id}' in parents "
                f"and trashed = false"
            ),
            fields="files(id, name)", pageSize=200,
            supportsAllDrives=True, includeItemsFromAllDrives=True,
        ).execute(num_retries=DRIVE_READ_RETRIES).get("files", [])
        for month in months:
            page_token = None
            while True:
                response = self._client().files().list(
                    q=(
                        f"mimeType != '{FOLDER_MIME}' and '{month['id']}' in parents "
                        f"and trashed = false"
                    ),
                    fields="nextPageToken, files(id, name, createdTime)", pageSize=200,
                    pageToken=page_token,
                    supportsAllDrives=True, includeItemsFromAllDrives=True,
                ).execute(num_retries=DRIVE_READ_RETRIES)
                for item in response.get("files", []):
                    found.append({**item, "folder": month["name"]})
                page_token = response.get("nextPageToken")
                if not page_token:
                    break
        return found

    def _ensure_user_folder(self, parent_id: str, user_id: int, wanted_name: str) -> str:
        """Resolve the per-user folder, adopting a legacy ``User_<id>`` one."""
        key = (parent_id, wanted_name)
        cached = self._folder_cache.get(key)
        if cached:
            return cached

        existing = self._find_folder(parent_id, wanted_name)
        if existing:
            self._folder_cache[key] = existing
            return existing

        legacy_name = f"User_{user_id}"
        if wanted_name != legacy_name:
            legacy = self._find_folder(parent_id, legacy_name)
            if legacy:
                try:
                    self._client().files().update(
                        fileId=legacy, body={"name": wanted_name}, fields="id",
                        supportsAllDrives=True,
                    ).execute(num_retries=DRIVE_READ_RETRIES)
                    logger.info(
                        "renamed screenshot folder %s to %s", legacy_name, wanted_name
                    )
                except Exception:  # noqa: BLE001
                    # Not fatal: keep using the folder under its old name so
                    # the upload still lands somewhere correct.
                    logger.warning(
                        "could not rename %s to %s; continuing with the existing folder",
                        legacy_name, wanted_name, exc_info=True,
                    )
                self._folder_cache[key] = legacy
                self._folder_cache[(parent_id, legacy_name)] = legacy
                return legacy

        return self.ensure_folder(parent_id, wanted_name)

    # ── Objects ───────────────────────────────────────────────────────────────

    def upload_file(
        self,
        folder_id: str,
        file_name: str,
        content: bytes,
        mime_type: str = "image/webp",
    ) -> str:
        """Store `content` in `folder_id`. Returns the Drive file id."""
        try:
            from googleapiclient.http import MediaIoBaseUpload  # type: ignore
        except ImportError as exc:
            raise GoogleDriveError(
                "google-api-python-client is not installed on this backend"
            ) from exc

        media = MediaIoBaseUpload(
            io.BytesIO(content), mimetype=mime_type, resumable=False
        )
        # No library-level retry on the media upload: see DRIVE_READ_RETRIES.
        created = self._client().files().create(
            body={"name": file_name, "parents": [folder_id]},
            media_body=media,
            fields="id",
            supportsAllDrives=True,
        ).execute()
        file_id = created.get("id")
        if not file_id:
            raise GoogleDriveError("Drive accepted the upload but returned no file id")
        return file_id

    def upload_file_idempotent(
        self,
        folder_id: str,
        file_name: str,
        content: bytes,
        mime_type: str = "image/webp",
    ) -> Tuple[str, bool]:
        """Store `content` as `file_name` in `folder_id` unless it is already there.

        :return: ``(file_id, reused)``. `reused` is True when an object of that
            name already existed in the folder and nothing was uploaded.
        """
        existing = self.find_file(folder_id, file_name)
        if existing:
            return existing, True
        return self.upload_file(folder_id, file_name, content, mime_type), False

    def download_file(self, file_id: str) -> bytes:
        """Read an object back, for the authenticated view endpoint.

        The bytes are proxied through this backend rather than handed out as a
        Drive link, so viewing a screenshot stays subject to Monitra's own
        organization and role checks and the Drive folder never has to be made
        public.

        Served from the in-process cache when possible. Screenshots are
        immutable, so a hit is always correct; a miss costs exactly what every
        read used to.
        """
        cached = self.image_cache.get(file_id)
        if cached is not None:
            return cached

        request = self._client().files().get_media(fileId=file_id, supportsAllDrives=True)
        buffer = io.BytesIO()
        try:
            from googleapiclient.http import MediaIoBaseDownload  # type: ignore
        except ImportError as exc:
            raise GoogleDriveError(
                "google-api-python-client is not installed on this backend"
            ) from exc

        downloader = MediaIoBaseDownload(buffer, request)
        done = False
        try:
            while not done:
                _, done = downloader.next_chunk(num_retries=DRIVE_READ_RETRIES)
        except GoogleDriveError:
            raise
        except Exception as exc:  # noqa: BLE001
            # A 404 from Drive is not an outage: the object is not there, and no
            # amount of retrying will bring it back. Reporting it as the same
            # "storage is temporarily unavailable" a 5xx gets made a deleted
            # image and a bad minute look identical to everyone downstream.
            if _http_status(exc) == 404:
                raise GoogleDriveFileNotFound(
                    f"Drive object {file_id} does not exist"
                ) from exc
            raise
        data = buffer.getvalue()
        self.image_cache.put(file_id, data)
        return data

    def stat_file(self, file_id: str) -> dict:
        """Does this Drive object exist, and is it an image? Read-only.

        Metadata only -- the bytes are not downloaded -- so an audit can check
        every screenshot of a day cheaply. ``state`` is one of ``ok``,
        ``missing`` (Drive answers 404), ``trashed``, ``forbidden`` (403: the
        service account cannot see it) or ``error`` (anything else, which says
        nothing about the object).
        """
        try:
            meta = self._client().files().get(
                fileId=file_id, fields="id, name, mimeType, size, trashed",
                supportsAllDrives=True,
            ).execute(num_retries=DRIVE_READ_RETRIES)
        except Exception as exc:  # noqa: BLE001
            status = _http_status(exc)
            if status == 404:
                return {"state": "missing"}
            if status == 403:
                return {"state": "forbidden"}
            return {"state": "error", "detail": type(exc).__name__}
        if meta.get("trashed"):
            return {"state": "trashed", "name": meta.get("name")}
        return {
            "state": "ok",
            "name": meta.get("name"),
            "mime_type": meta.get("mimeType"),
            "size": int(meta.get("size") or 0),
        }

    def list_screenshot_day_files(
        self, user_id: int, captured_on: date, user_name: Optional[str] = None
    ) -> Optional[list]:
        """Every object in one member's Drive folder for one IST day. Read-only.

        Walks ``<root>/<year>/<month>/User_<id>_<name>/<date>`` with lookups
        only -- it never creates a folder, unlike `ensure_screenshot_folder` --
        and answers None when any level is absent (nothing was stored for that
        day) so an audit can tell "no folder" from "an empty folder".

        Returns ``[{"id", "name", "mimeType", "size", "createdTime"}]``.
        """
        parent = self.root_folder_id
        for name in (f"{captured_on.year:04d}", captured_on.strftime("%B")):
            parent = self._find_folder(parent, name)
            if not parent:
                return None
        # Both spellings: the current one and the legacy `User_<id>` that is
        # renamed in place the first time it is resolved for an upload.
        user_folder = (
            self._find_folder(parent, self.user_folder_name(user_id, user_name))
            or self._find_folder(parent, f"User_{user_id}")
        )
        if not user_folder:
            return None
        day_folder = self._find_folder(user_folder, captured_on.isoformat())
        if not day_folder:
            return None
        found: list = []
        page_token = None
        while True:
            response = self._client().files().list(
                q=f"'{day_folder}' in parents and trashed = false and mimeType != '{FOLDER_MIME}'",
                fields="nextPageToken, files(id, name, mimeType, size, createdTime)",
                pageSize=200, pageToken=page_token,
                supportsAllDrives=True, includeItemsFromAllDrives=True,
            ).execute(num_retries=DRIVE_READ_RETRIES)
            found.extend(response.get("files", []))
            page_token = response.get("nextPageToken")
            if not page_token:
                return found

    def delete_file(self, file_id: str) -> None:
        """Remove an object. Used to roll back a file whose row could not be
        written, so a failed upload does not leave an orphan in Drive.

        Best-effort by design: the caller is already unwinding a failure and has
        nothing better to do with a second one. A deletion a *user* asked for
        must be able to fail loudly instead — that is `delete_file_strict`.
        """
        self.image_cache.discard(file_id)
        try:
            self._client().files().delete(fileId=file_id, supportsAllDrives=True).execute()
        except Exception:  # noqa: BLE001
            logger.warning("could not delete orphaned Drive file %s", file_id, exc_info=True)

    def delete_file_strict(self, file_id: str) -> None:
        """Remove an object, reporting what happened.

        The user-facing delete endpoint has to distinguish three outcomes that
        `delete_file` flattens into one: the file went away, the file was
        already gone, and Drive could not be reached. Only the third may leave
        the database row in place — swallowing it would report a successful
        deletion for an image that is still readable in Drive.

        :raises GoogleDriveFileNotFound: Drive says there is no such object.
            The bytes are already absent, so the caller may safely proceed to
            remove the metadata rather than being blocked forever by a file
            that no longer exists.
        :raises GoogleDriveError: the deletion could not be confirmed.
        """
        self.image_cache.discard(file_id)
        try:
            self._client().files().delete(fileId=file_id, supportsAllDrives=True).execute()
        except Exception as exc:  # noqa: BLE001
            # `googleapiclient` is imported lazily throughout this module, so the
            # status is read off the exception rather than by catching HttpError
            # — a backend without the client library installed must still get a
            # clear GoogleDriveError here instead of an ImportError.
            status_code = getattr(getattr(exc, "resp", None), "status", None)
            if status_code is None:
                status_code = getattr(exc, "status_code", None)
            if status_code in (404, 410):
                raise GoogleDriveFileNotFound(
                    f"Drive file {file_id} does not exist"
                ) from exc
            raise GoogleDriveError(f"Drive could not delete file {file_id}") from exc


#: Process-wide instance. The folder cache is what makes sharing it worthwhile.
drive_service = GoogleDriveService()

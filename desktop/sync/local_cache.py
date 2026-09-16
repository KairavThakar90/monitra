"""
local_cache — Repository API over the local SQLite database.

This is a repository, not a connection owner. All access goes through
`storage.manager.StorageManager`, which gives each thread its own connection
and provides real transactions. The previous implementation owned one shared
connection guarded by a global `threading.Lock`; see storage/manager.py for
why that was replaced.

The public method surface is unchanged so existing callers keep working. What
changed underneath:

  * multi-statement updates now run inside a transaction, so a concurrent
    reader can never observe a half-written cache (the "task name renders as
    ?" class of defect);
  * `close()` no longer yanks a connection out from under other threads —
    connection lifetime belongs to the StorageManager, and the runtime closes
    it only after every service thread has been confirmed stopped;
  * queue rows carry `session_generation`, `entity_type`/`entity_id` and
    `updated_at`, so stale work from a previous login can be identified and
    discarded rather than applied to the current session.
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any, Dict, List, Optional

from core.logging_setup import get_logger, session_generation
from storage.manager import StorageManager, cache_dir, db_path, get_storage_manager

log = get_logger("cache")

# Re-exported for callers that imported these helpers from this module.
_get_cache_dir = cache_dir
_get_db_path = db_path

#: `app_state` key naming the user the read-through caches belong to.
#:
#: The projects, tasks, task-status and time-entry caches carry no user column
#: -- they are a paint-the-dashboard-before-the-network store, replaced
#: wholesale on each refresh -- so this one row is how the client knows whose
#: rows are sitting in them. See `LocalCache.claim_cache_for`.
CACHE_OWNER_KEY = "cache_owner_user_id"

#: Largest number of telemetry rows one read hands to the sync consumer.
#:
#: This is load-bearing, not a tidiness bound. The backend caps an app-usage
#: and a URL-usage batch at 500 records (`MAX_BATCH_RECORDS` in
#: `backend/app/schemas/`), and the reads below used to return *everything*
#: pending. A day tracked offline produces far more than that — app-usage
#: segments alone are flushed at least once a minute and again on every
#: application switch — so the first batch after reconnecting was rejected
#: 422, retried unchanged until it exhausted its retry budget, and then marked
#: `failed`, where nothing picks a row up again. An entire offline day of
#: application and browser usage was lost that way, and the size that caused
#: it guaranteed the retries could never succeed.
#:
#: 200 sits comfortably under the server's 500 with room for the cap to be
#: lowered without a matching client release, and keeps one request small
#: enough that a slow link does not time it out. A backlog larger than this
#: drains over consecutive passes; `SyncService` shortens its interval while
#: one is outstanding, so draining stays prompt without ever building an
#: unbounded request.
TELEMETRY_FETCH_LIMIT = 200

#: Telemetry tables holding rows that are captured locally and uploaded by
#: `SyncService`. Kept in one place because they share a retry contract and
#: therefore share the launch-time recovery and the age sweep below.
TELEMETRY_TABLES = (
    "pending_app_usage",
    "activity_samples",
    "pending_url_usage",
    "pending_unwanted_activity",
    "pending_adjustments",
)

#: How long a telemetry row that has never uploaded is kept before it is
#: swept. Long enough to cover an outage measured in weeks; short enough that
#: `cache.db` cannot grow without bound on an install that runs for years.
TELEMETRY_MAX_AGE_SECONDS = 30 * 86400.0

#: Terminal + non-terminal states a queued action can hold.
PENDING = "pending"
PROCESSING = "processing"
RETRY = "retry"
FAILED = "failed"
CANCELLED = "cancelled"


class LocalCache:
    """
    Local persistence for projects, tasks, time entries, queued sync actions,
    session state and captured activity.
    """

    def __init__(
        self,
        db_path_override: Optional[str] = None,
        storage: Optional[StorageManager] = None,
    ) -> None:
        self._storage = storage or get_storage_manager(db_path_override)

    @property
    def storage(self) -> StorageManager:
        return self._storage

    # ── Session Persistence ───────────────────────────────────────────────────

    #: Session rows that are written only when the caller supplies them, so a
    #: caller that knows nothing about them (a re-save after /auth/me, say)
    #: cannot silently erase the session window it did not carry.
    _OPTIONAL_SESSION_KEYS = ("refresh_token", "session_created_at", "session_expires_at")

    def save_session(
        self,
        access_token: str,
        user_info: dict,
        refresh_token: Optional[str] = None,
        session_created_at: Optional[str] = None,
        session_expires_at: Optional[str] = None,
    ) -> None:
        """Persist the current auth session so the next launch can restore it.

        The session window (`session_created_at`/`session_expires_at`, both ISO-8601
        UTC) is stored alongside the tokens because it is the boundary the client
        enforces before it trusts anything else here.
        """
        now = time.time()
        optional = dict(zip(
            self._OPTIONAL_SESSION_KEYS,
            (refresh_token, session_created_at, session_expires_at),
        ))
        with self._storage.transaction() as conn:
            for key, value in (
                ("access_token", access_token),
                ("user_info", json.dumps(user_info)),
                *((k, v) for k, v in optional.items() if v is not None),
            ):
                conn.execute(
                    "INSERT OR REPLACE INTO session (key, value, updated_at) VALUES (?, ?, ?)",
                    (key, value, now),
                )

    def load_session(self) -> Optional[Dict[str, Any]]:
        """Load persisted session, or None if absent/corrupt."""
        rows = self._storage.query_all("SELECT key, value FROM session")
        if not rows:
            return None
        data = {row["key"]: row["value"] for row in rows}
        token = data.get("access_token")
        user_info_str = data.get("user_info")
        if not token or not user_info_str:
            return None
        try:
            user_info = json.loads(user_info_str)
        except (json.JSONDecodeError, TypeError):
            log.warning("persisted user_info is corrupt; ignoring stored session")
            return None
        return {
            "access_token": token,
            "user_info": user_info,
            **{key: data.get(key) for key in self._OPTIONAL_SESSION_KEYS},
        }

    def clear_session(self) -> None:
        """Clear persisted session on logout."""
        self._storage.execute("DELETE FROM session")

    def clear_user_scoped_cache(self) -> None:
        """
        Drop every cached row that belongs to whoever was signed in.

        The projects, tasks, task-status and time-entry caches are read
        straight back on the next login to paint the dashboard before the
        network answers. They carry no user column, so without this the next
        user to sign in on this machine is shown the previous user's
        projects and tasks until the first response arrives -- measured, and
        visibly wrong rather than merely stale.

        Deliberately not the durable queues (pending actions, activity
        samples, unwanted activity, adjustments): those are captured work
        that must still be uploaded, and they are already fenced off by the
        session generation.
        """
        with self._storage.transaction() as conn:
            for table in ("projects", "tasks", "task_cache_status",
                          "task_statuses", "time_entries_today"):
                conn.execute(f"DELETE FROM {table}")

    def claim_cache_for(self, user_id: Optional[int]) -> bool:
        """Bind the read-through caches to one user, clearing another's.

        `clear_user_scoped_cache` runs on logout, which covers the ordinary
        path. This covers the rest of them: a session that ended without a
        deliberate logout, a token swapped underneath the client, a crash
        between the two. The caches carry no user column, so the only way to
        know whose rows they are is to record it — this is the cache's
        `user_id` key, kept as one row rather than as a column on five tables.

        Returns whether another user's rows were dropped. A `None` id (a
        profile that arrived without one) claims nothing and clears nothing:
        it is not evidence that the owner changed, and clearing on it would
        throw away the cache the user is about to be shown.
        """
        if user_id is None:
            return False
        previous = self.load_app_state(CACHE_OWNER_KEY)
        cleared = previous is not None and previous != user_id
        if cleared:
            log.info(
                "local cache belonged to user %s; clearing it for user %s",
                previous, user_id,
            )
            self.clear_user_scoped_cache()
        self.save_app_state(CACHE_OWNER_KEY, user_id)
        return cleared

    # ── App State (Timer / Recovery) ──────────────────────────────────────────

    def save_app_state(self, key: str, value: Any) -> None:
        self._storage.execute(
            "INSERT OR REPLACE INTO app_state (key, value, updated_at) VALUES (?, ?, ?)",
            (key, json.dumps(value), time.time()),
        )

    def load_app_state(self, key: str) -> Optional[Any]:
        row = self._storage.query_one("SELECT value FROM app_state WHERE key = ?", (key,))
        if not row:
            return None
        try:
            return json.loads(row["value"])
        except (json.JSONDecodeError, TypeError):
            log.warning("app_state[%s] is corrupt; discarding", key)
            return None

    def clear_app_state(self, key: Optional[str] = None) -> None:
        if key:
            self._storage.execute("DELETE FROM app_state WHERE key = ?", (key,))
        else:
            self._storage.execute("DELETE FROM app_state")

    # ── Project Cache ─────────────────────────────────────────────────────────

    def cache_projects(self, projects: List[Dict[str, Any]]) -> None:
        """Atomically replace the projects cache."""
        now = time.time()
        with self._storage.transaction() as conn:
            conn.execute("DELETE FROM projects")
            for p in projects:
                conn.execute(
                    "INSERT OR REPLACE INTO projects (id, data, cached_at) VALUES (?, ?, ?)",
                    (p.get("id", 0), json.dumps(p), now),
                )

    def get_cached_projects(self) -> Optional[List[Dict[str, Any]]]:
        rows = self._storage.query_all("SELECT data FROM projects ORDER BY id")
        if not rows:
            return None
        return [json.loads(row["data"]) for row in rows]

    def projects_cache_age_seconds(self) -> Optional[float]:
        """How long ago the projects cache was last written, or None if never.

        The dashboard paints from this cache before the network answers and
        keeps it on screen when a refresh fails; this is what lets it say
        *how old* what it is showing is, rather than presenting a week-old
        list as current.
        """
        row = self._storage.query_one("SELECT MAX(cached_at) AS written FROM projects")
        if not row or row["written"] is None:
            return None
        return max(0.0, time.time() - float(row["written"]))

    # ── Task Cache ────────────────────────────────────────────────────────────

    def forget_project_tasks(self, project_id: int) -> None:
        """Drop one project's cached tasks and its freshness marker.

        For a project that has disappeared from the user's list -- archived,
        or their membership removed. Its rows would otherwise be painted
        again the next time something selected it, and `get_cached_tasks`
        would report them as a cached answer rather than "never cached".
        """
        with self._storage.transaction() as conn:
            conn.execute("DELETE FROM tasks WHERE project_id = ?", (project_id,))
            conn.execute("DELETE FROM task_cache_status WHERE project_id = ?", (project_id,))

    def cache_tasks(self, project_id: int, tasks: List[Dict[str, Any]]) -> None:
        """
        Atomically replace the task cache for one project.

        The delete, the inserts and the freshness marker are one transaction.
        Previously they were separate autocommitted statements, so a reader
        that ran between the DELETE and the INSERTs saw an empty or partial
        task list and rendered placeholder rows.
        """
        now = time.time()
        with self._storage.transaction() as conn:
            conn.execute("DELETE FROM tasks WHERE project_id = ?", (project_id,))
            for t in tasks:
                conn.execute(
                    "INSERT OR REPLACE INTO tasks (id, project_id, data, cached_at) "
                    "VALUES (?, ?, ?, ?)",
                    (t.get("id", 0), project_id, json.dumps(t), now),
                )
            conn.execute(
                "INSERT OR REPLACE INTO task_cache_status (project_id, synced_at) VALUES (?, ?)",
                (project_id, now),
            )

    def get_cached_tasks(self, project_id: int) -> Optional[List[Dict[str, Any]]]:
        """
        Return cached tasks for a project.

        None means "never cached"; [] means "cached, and the project has no
        tasks". Callers rely on that distinction to decide whether to show a
        loader or an empty state.
        """
        status = self._storage.query_one(
            "SELECT synced_at FROM task_cache_status WHERE project_id = ?", (project_id,)
        )
        if not status:
            return None
        rows = self._storage.query_all(
            "SELECT data FROM tasks WHERE project_id = ? ORDER BY id", (project_id,)
        )
        return [json.loads(row["data"]) for row in rows]

    def has_cached_tasks(self, project_id: int) -> bool:
        return self._storage.query_one(
            "SELECT 1 FROM task_cache_status WHERE project_id = ?", (project_id,)
        ) is not None

    def cache_task_statuses(self, statuses: List[Dict[str, Any]]) -> None:
        with self._storage.transaction() as conn:
            conn.execute("DELETE FROM task_statuses")
            for s in statuses:
                conn.execute(
                    "INSERT OR REPLACE INTO task_statuses (id, name, color) VALUES (?, ?, ?)",
                    (s.get("id"), s.get("name"), s.get("color")),
                )

    def get_cached_task_statuses(self) -> Optional[List[Dict[str, Any]]]:
        rows = self._storage.query_all("SELECT id, name, color FROM task_statuses ORDER BY id")
        if not rows:
            return None
        return [{"id": r["id"], "name": r["name"], "color": r["color"]} for r in rows]

    # ── Time Entry Cache ──────────────────────────────────────────────────────

    def cache_time_entries(self, target_date: str, entries: List[Dict[str, Any]]) -> None:
        now = time.time()
        with self._storage.transaction() as conn:
            conn.execute("DELETE FROM time_entries_today WHERE target_date = ?", (target_date,))
            for e in entries:
                conn.execute(
                    "INSERT OR REPLACE INTO time_entries_today (id, data, target_date, cached_at) "
                    "VALUES (?, ?, ?, ?)",
                    (e.get("id", 0), json.dumps(e), target_date, now),
                )

    def get_cached_time_entries(self, target_date: str) -> Optional[List[Dict[str, Any]]]:
        rows = self._storage.query_all(
            "SELECT data FROM time_entries_today WHERE target_date = ?", (target_date,)
        )
        if not rows:
            return None
        return [json.loads(row["data"]) for row in rows]

    def add_elapsed_to_cached_time_entry(
        self, target_date: str, task_id: Optional[int], elapsed_seconds: int
    ) -> None:
        """Fold newly tracked seconds into the cached entries for a date."""
        if elapsed_seconds <= 0:
            return
        now = time.time()
        with self._storage.transaction() as conn:
            rows = conn.execute(
                "SELECT id, data FROM time_entries_today WHERE target_date = ?", (target_date,)
            ).fetchall()

            for row in rows:
                data = json.loads(row["data"])
                if task_id is not None and data.get("task_id") == task_id:
                    data["total_seconds"] = data.get("total_seconds", 0) + elapsed_seconds
                    if data.get("net_seconds") is not None:
                        data["net_seconds"] = data["net_seconds"] + elapsed_seconds
                    data["status"] = "completed"
                    conn.execute(
                        "UPDATE time_entries_today SET data = ?, cached_at = ? WHERE id = ?",
                        (json.dumps(data), now, row["id"]),
                    )
                    return

            new_entry = {
                "id": -int(now * 1000) % 1000000,
                "task_id": task_id,
                "total_seconds": elapsed_seconds,
                "status": "completed",
                "target_date": target_date,
            }
            conn.execute(
                "INSERT OR REPLACE INTO time_entries_today (id, data, target_date, cached_at) "
                "VALUES (?, ?, ?, ?)",
                (new_entry["id"], json.dumps(new_entry), target_date, now),
            )

    # ── Pending Actions (durable sync queue) ──────────────────────────────────

    def enqueue_action(
        self,
        action_type: str,
        payload: Dict[str, Any],
        priority: int = 5,
        idempotency_key: Optional[str] = None,
        entity_type: Optional[str] = None,
        entity_id: Optional[str] = None,
    ) -> str:
        """
        Durably enqueue an action.

        Duplicate protection is enforced by a UNIQUE index on
        `idempotency_key` as well as the pre-check, so two threads racing on
        the same key cannot both insert.
        """
        now = time.time()
        if idempotency_key:
            existing = self._storage.query_one(
                "SELECT id FROM pending_actions WHERE idempotency_key = ? "
                "AND status IN ('pending', 'processing', 'retry')",
                (idempotency_key,),
            )
            if existing:
                return existing["id"]

        action_id = str(uuid.uuid4())
        try:
            self._storage.execute(
                """INSERT INTO pending_actions
                   (id, action_type, entity_type, entity_id, payload, priority,
                    created_at, updated_at, next_retry_at, status, idempotency_key,
                    session_generation)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
                (
                    action_id, action_type, entity_type, entity_id,
                    json.dumps(payload), priority, now, now, now,
                    idempotency_key, session_generation(),
                ),
            )
        except Exception:  # sqlite3.IntegrityError on the unique index
            row = self._storage.query_one(
                "SELECT id FROM pending_actions WHERE idempotency_key = ?", (idempotency_key,)
            )
            if row:
                return row["id"]
            raise
        log.info("enqueued %s priority=%d", action_type, priority, extra={"op": action_id})
        return action_id

    def get_next_pending_action(self) -> Optional[Dict[str, Any]]:
        """
        Claim the highest-priority action that is ready to run.

        The select and the claim are one transaction, so two consumers can
        never claim the same row.
        """
        now = time.time()
        with self._storage.transaction() as conn:
            row = conn.execute(
                """SELECT id, action_type, entity_type, entity_id, payload, priority,
                          created_at, retry_count, idempotency_key, session_generation,
                          defer_count
                   FROM pending_actions
                   WHERE status IN ('pending', 'retry') AND next_retry_at <= ?
                   ORDER BY priority ASC, created_at ASC
                   LIMIT 1""",
                (now,),
            ).fetchone()
            if not row:
                return None
            conn.execute(
                "UPDATE pending_actions SET status = 'processing', updated_at = ? WHERE id = ?",
                (now, row["id"]),
            )
            return {
                "id": row["id"],
                "action_type": row["action_type"],
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
                "payload": json.loads(row["payload"]),
                "priority": row["priority"],
                "created_at": row["created_at"],
                "retry_count": row["retry_count"],
                "idempotency_key": row["idempotency_key"],
                "session_generation": row["session_generation"],
                "defer_count": row["defer_count"],
            }

    def complete_action(self, action_id: str) -> None:
        self._storage.execute("DELETE FROM pending_actions WHERE id = ?", (action_id,))

    def defer_action(self, action_id: str, reason: str, delay_seconds: float = 2.0) -> int:
        """
        Reschedule an action whose prerequisites are not ready yet.

        Deliberately does **not** increment `retry_count`: waiting on an
        ordering dependency is not a failure and must not consume the retry
        budget that exists for genuine errors. `defer_count` is tracked
        separately so a dependency that never arrives cannot defer forever.

        :return: how many times this action has now been deferred.
        """
        now = time.time()
        self._storage.execute(
            "UPDATE pending_actions SET status = 'pending', next_retry_at = ?, "
            "error_message = ?, updated_at = ?, defer_count = defer_count + 1 "
            "WHERE id = ?",
            (now + delay_seconds, reason, now, action_id),
        )
        row = self._storage.query_one(
            "SELECT defer_count FROM pending_actions WHERE id = ?", (action_id,)
        )
        return row["defer_count"] if row else 0

    def cancel_action(self, action_id: str, reason: str = "") -> None:
        self._storage.execute(
            "UPDATE pending_actions SET status = 'cancelled', error_message = ?, updated_at = ? "
            "WHERE id = ?",
            (reason, time.time(), action_id),
        )

    def fail_action(
        self, action_id: str, error_message: str, max_retries: Optional[int] = 10
    ) -> bool:
        """
        Record a failure and schedule a retry with exponential backoff + jitter.

        Jitter matters at fleet scale: without it, every client that lost the
        backend at the same moment retries at exactly the same moment, which
        is a self-inflicted thundering herd on recovery.

        `max_retries=None` means the action is never parked as `failed`: it
        keeps retrying at the capped backoff for as long as the process runs.
        That is reserved for the timer's own start/stop actions, where giving
        up is worse than any amount of waiting -- an abandoned stop leaves the
        entry running on the backend, and the next launch adopts it as a
        timer the user never stopped.

        :return: True if the action will be retried.
        """
        import random

        now = time.time()
        row = self._storage.query_one(
            "SELECT retry_count FROM pending_actions WHERE id = ?", (action_id,)
        )
        if not row:
            return False

        retry_count = row["retry_count"] + 1
        if max_retries is not None and retry_count > max_retries:
            self._storage.execute(
                "UPDATE pending_actions SET status = 'failed', error_message = ?, updated_at = ? "
                "WHERE id = ?",
                (error_message, now, action_id),
            )
            log.error("action exhausted retries: %s", error_message, extra={"op": action_id})
            return False

        base = min(2 ** (retry_count - 1), 60)
        delay = base * (0.5 + random.random())  # 50%–150% jitter
        self._storage.execute(
            """UPDATE pending_actions
               SET status = 'retry', retry_count = ?, next_retry_at = ?,
                   error_message = ?, updated_at = ?
               WHERE id = ?""",
            (retry_count, now + delay, error_message, now, action_id),
        )
        log.info(
            "action retry %d/%s in %.1fs: %s",
            retry_count, max_retries if max_retries is not None else "unbounded",
            delay, error_message, extra={"op": action_id},
        )
        return True

    def get_pending_count(self) -> int:
        row = self._storage.query_one(
            "SELECT COUNT(*) AS cnt FROM pending_actions "
            "WHERE status IN ('pending', 'processing', 'retry')"
        )
        return row["cnt"] if row else 0

    def reset_processing_actions(self) -> None:
        """
        Return interrupted claims to the pending pool.

        Called once at startup: anything left in 'processing' belongs to a
        previous process that died mid-operation.
        """
        cursor = self._storage.execute(
            "UPDATE pending_actions SET status = 'pending' WHERE status = 'processing'"
        )
        if cursor.rowcount:
            log.info("recovered %d interrupted action(s) from previous run", cursor.rowcount)

    def clear_stale_actions(self, max_age_seconds: float = 86400.0) -> None:
        cutoff = time.time() - max_age_seconds
        self._storage.execute(
            "DELETE FROM pending_actions WHERE status IN ('failed', 'cancelled') AND created_at < ?",
            (cutoff,),
        )

    #: The actions that move tracked time. They share one retry contract:
    #: never abandoned on a transient failure, and revived at every launch.
    TIMER_ACTION_TYPES = ("start_timer", "stop_timer", "switch_timer")

    def requeue_timer_actions_for_new_run(self) -> int:
        """Give every timer action a fresh attempt at the next launch.

        A queued stop that exhausted its retries under an older build was
        parked as `failed`, and nothing reads a failed row again -- so the
        entry it names ran on the backend until the next launch adopted it
        as "a timer that was still running". The same reasoning as
        `requeue_telemetry_for_new_run`, applied to the rows that matter
        most: a launch is when the cause has plausibly been fixed, and the
        instant the user pressed Stop travels in the payload, so a late
        delivery still records the right end time.

        Called before `clear_stale_actions`, which would otherwise delete a
        failed stop older than a day.

        :return: how many rows were revived.
        """
        placeholders = ", ".join("?" for _ in self.TIMER_ACTION_TYPES)
        cursor = self._storage.execute(
            f"UPDATE pending_actions SET status = 'pending', retry_count = 0, "
            f"next_retry_at = ?, updated_at = ? "
            f"WHERE status = 'failed' AND action_type IN ({placeholders})",
            (time.time(), time.time(), *self.TIMER_ACTION_TYPES),
        )
        revived = cursor.rowcount or 0
        if revived:
            log.info("revived %d failed timer action(s) for a fresh attempt", revived)
        return revived

    def pending_stop_count(self, exclude_client_op: Optional[str] = None) -> int:
        """How many stops are still waiting to reach the backend.

        The exit path waits on this number, and a start defers on it: a stop
        that has not landed yet must reach the backend before the start that
        follows it, or the backend answers the start with a 409 for the very
        entry the stop is about to end. `exclude_client_op` leaves out the
        stop of one session -- the start's own, which by construction never
        precedes it.
        """
        rows = self._storage.query_all(
            "SELECT payload FROM pending_actions "
            "WHERE action_type = 'stop_timer' "
            "AND status IN ('pending', 'processing', 'retry')",
        )
        if exclude_client_op is None:
            return len(rows)
        count = 0
        for row in rows:
            try:
                payload = json.loads(row["payload"])
            except (json.JSONDecodeError, TypeError):
                count += 1
                continue
            if payload.get("client_op") != exclude_client_op:
                count += 1
        return count

    def has_pending_stop_for_client_op(self, client_op: Optional[str]) -> bool:
        """Whether a stop for this tracking session is still waiting to be sent.

        The entry-id form (`has_pending_stop_for_entry`) cannot see a stop
        queued before the backend issued an id -- a session started offline,
        or stopped a second after Start while the request was in flight. The
        backend's entry carries the session's `client_op`, so this is how a
        reconciliation recognises that the running entry it is being shown
        is one the user has already stopped.
        """
        if not client_op:
            return False
        return self.has_pending_action_for_client_op(client_op, "stop_timer")

    # ── Telemetry queue housekeeping ──────────────────────────────────────────

    def requeue_telemetry_for_new_run(self) -> int:
        """
        Give every unsent telemetry row a fresh attempt at the next launch.

        The screenshot queue has done this since it shipped
        (`requeue_screenshots_for_new_run`) and the reasoning transfers
        unchanged to the five tables that carry captured time: a row that
        exhausted its retries is parked as `failed`, and **nothing in the
        application ever reads a `failed` row again**. Those rows are real
        measured work — application usage, browsing, activity windows, the
        deductions taken off someone's tracked time — so leaving them parked
        discards data the user earned over a condition that has very likely
        been fixed since.

        A launch is the natural boundary, because it is when something has
        changed: the network is back, the backend was upgraded, the client was.
        The backoff is cleared for every row, and the retry counter is reset
        only for the exhausted ones, so a fresh attempt gets a full budget
        rather than immediately re-exhausting a spent one.

        Nothing is deleted here.

        :return: how many rows were returned to the queue.
        """
        requeued = 0
        for table in TELEMETRY_TABLES:
            cursor = self._storage.execute(
                f"UPDATE {table} "
                "SET status = 'pending', "
                "    retry_count = CASE WHEN status = 'failed' THEN 0 ELSE retry_count END, "
                "    next_retry_at = 0 "
                "WHERE status IN ('failed', 'pending') AND next_retry_at > 0",
                (),
            )
            requeued += cursor.rowcount or 0
        if requeued:
            log.info("requeued %d telemetry row(s) left unsent by the previous run", requeued)
        return requeued

    def purge_expired_telemetry(
        self, max_age_seconds: float = TELEMETRY_MAX_AGE_SECONDS
    ) -> int:
        """
        Delete telemetry rows too old to be worth uploading.

        This is the bound that stops `cache.db` growing forever. Every other
        path out of these tables is a *success* path — `complete_*` deletes a
        row once the backend has it — so a row that never uploads has no exit
        at all, and a backend that refuses one particular row would otherwise
        keep it, and every row queued behind it, on disk for the life of the
        installation.

        It is deliberately an age sweep and not a failure sweep. A row is
        removed because it is older than the backend will meaningfully accept
        it for, not because an upload failed: `requeue_telemetry_for_new_run`
        runs first at every launch, so a row only survives to be swept here
        after weeks of attempts across many launches.

        :return: how many rows were removed.
        """
        cutoff = time.time() - max_age_seconds
        removed = 0
        for table in TELEMETRY_TABLES:
            cursor = self._storage.execute(
                f"DELETE FROM {table} WHERE created_at < ?", (cutoff,)
            )
            removed += cursor.rowcount or 0
        if removed:
            log.warning(
                "purged %d telemetry row(s) older than %.0f days that never uploaded",
                removed, max_age_seconds / 86400.0,
            )
        return removed

    def has_pending_action_for_client_op(self, client_op: str, action_type: str) -> bool:
        """Whether an unfinished action of `action_type` carries this client op."""
        rows = self._storage.query_all(
            "SELECT payload FROM pending_actions "
            "WHERE action_type = ? AND status IN ('pending', 'processing', 'retry')",
            (action_type,),
        )
        for row in rows:
            try:
                if json.loads(row["payload"]).get("client_op") == client_op:
                    return True
            except (json.JSONDecodeError, TypeError):
                continue
        return False

    def has_pending_stop_for_entry(self, entry_id: int) -> bool:
        """Whether a stop for this backend entry is still waiting to be sent.

        The backend keeps reporting such an entry as `running` until the queued
        stop reaches it, so a client that asks "is a timer running?" during that
        window gets a yes for a timer the user already stopped. Restoring it
        would resurrect a finished session -- and, worse, re-anchor it to the
        original start, so the clock resumes from a total the user never
        tracked.
        """
        return self.pending_stop_payload_for_entry(entry_id) is not None

    def pending_stop_payload_for_entry(self, entry_id: int) -> Optional[Dict[str, Any]]:
        """The queued stop for this backend entry, if one is still waiting.

        Carries `stopped_at`, the instant the user actually stopped. A day's
        entries read from the backend while that stop is queued still show the
        entry running; overlaying this payload lets the dashboard show the
        entry as the backend *will* record it, instead of alternating between
        the banked estimate and a running entry with `total_seconds` 0.
        """
        if entry_id is None:
            return None
        rows = self._storage.query_all(
            "SELECT payload FROM pending_actions "
            "WHERE action_type = 'stop_timer' "
            "AND status IN ('pending', 'processing', 'retry')",
        )
        for row in rows:
            try:
                payload = json.loads(row["payload"])
            except (json.JSONDecodeError, TypeError):
                continue
            if payload.get("entry_id") == entry_id:
                return payload
        return None

    def resolve_entry_id_for_client_op(self, client_op: str, entry_id: int) -> int:
        """
        Fill in the backend entry id on queued actions awaiting it.

        When a timer is started offline, the local session has no entry id yet.
        If the user then stops it, the queued `stop_timer` has nothing to
        identify on the server. Both actions carry the same `client_op`, so once
        the queued `start_timer` succeeds the resulting entry id is written into
        every queued action still waiting for it.

        Without this the stop was sent with `entry_id = None`, which stopped
        nothing and left the entry running on the backend.

        :return: how many queued actions were resolved.
        """
        rows = self._storage.query_all(
            "SELECT id, payload FROM pending_actions "
            "WHERE status IN ('pending', 'retry')",
        )
        resolved = 0
        now = time.time()
        with self._storage.transaction() as conn:
            for row in rows:
                try:
                    payload = json.loads(row["payload"])
                except (json.JSONDecodeError, TypeError):
                    continue
                if payload.get("client_op") != client_op:
                    continue
                # Only actions that *consume* an entry id are resolved. The
                # `start_timer` that produces it has no `entry_id` key at all,
                # and must not be patched with the id it just created.
                if "entry_id" not in payload or payload.get("entry_id"):
                    continue
                payload["entry_id"] = entry_id
                conn.execute(
                    "UPDATE pending_actions SET payload = ?, entity_id = ?, updated_at = ? "
                    "WHERE id = ?",
                    (json.dumps(payload), str(entry_id), now, row["id"]),
                )
                resolved += 1
        if resolved:
            log.info("resolved entry %s onto %d queued action(s)", entry_id, resolved)
        return resolved

    def cancel_actions_for_generation(self, generation: int) -> int:
        """
        Cancel queued work belonging to an earlier session generation.

        Prevents user A's queued operations from being executed with user B's
        credentials after a logout/login.
        """
        cursor = self._storage.execute(
            "UPDATE pending_actions SET status = 'cancelled', error_message = 'stale session' "
            "WHERE session_generation < ? AND status IN ('pending', 'retry', 'processing')",
            (generation,),
        )
        return cursor.rowcount or 0

    # ── Application Usage ─────────────────────────────────────────────────────

    def save_app_usage(
        self,
        time_entry_id: Optional[int],
        application_name: str,
        window_title: Optional[str],
        duration_seconds: int,
        recorded_at: str,
        client_op: Optional[str] = None,
    ) -> str:
        """Queue one measured application-usage segment for upload.

        `time_entry_id` may be None for a segment captured before the
        backend issued one -- an offline start, or the first seconds of a
        session while the start request is still in flight. Such a row waits
        in the queue, is never read by the uploader, and is adopted by
        `bind_app_usage_to_entry` once the id arrives. `client_op` is the
        timer session's stable key and is what makes that adoption possible,
        so a row without an id and without a client_op could never be
        attributed and is not written at all (see `AppUsageService`).
        """
        record_id = str(uuid.uuid4())
        now = time.time()
        self._storage.execute(
            """INSERT INTO pending_app_usage
               (id, time_entry_id, client_op, application_name, window_title,
                duration_seconds, recorded_at, status, retry_count, next_retry_at,
                created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?)""",
            (record_id, time_entry_id, client_op, application_name, window_title,
             duration_seconds, recorded_at, now, now),
        )
        return record_id

    def bind_app_usage_to_entry(self, client_op: str, time_entry_id: int) -> int:
        """
        Attribute segments captured before the backend issued an entry id.

        The same problem, and the same answer, as
        `bind_screenshots_to_entry`: work measured in the first seconds of a
        session, or throughout an offline one, has no entry to belong to
        yet. Matching on the session's own `client_op` keeps the attribution
        honest -- only that session's segments are adopted -- and the
        ``time_entry_id IS NULL`` guard makes a repeated bind a no-op rather
        than a way to re-point rows that are already attributed.

        :return: how many rows were bound.
        """
        if not client_op:
            return 0
        cursor = self._storage.execute(
            "UPDATE pending_app_usage SET time_entry_id = ? "
            "WHERE time_entry_id IS NULL AND client_op = ?",
            (time_entry_id, client_op),
        )
        return cursor.rowcount or 0

    def get_pending_app_usage(self, limit: int = TELEMETRY_FETCH_LIMIT) -> List[Dict[str, Any]]:
        """App-usage segments ready to upload, oldest first.

        Bounded: see `TELEMETRY_FETCH_LIMIT`.

        Rows still waiting for their entry id are skipped rather than
        uploaded: the endpoint is per-entry, so there is nowhere to send
        them yet. They stay 'pending' and are picked up on a later pass,
        once `bind_app_usage_to_entry` has adopted them.
        """
        rows = self._storage.query_all(
            """SELECT id, time_entry_id, application_name, window_title,
                      duration_seconds, recorded_at, retry_count
               FROM pending_app_usage
               WHERE status = 'pending' AND next_retry_at <= ?
                 AND time_entry_id IS NOT NULL
               ORDER BY created_at ASC
               LIMIT ?""",
            (time.time(), limit),
        )
        return [dict(row) for row in rows]

    def get_unsynced_app_usage_between(
        self, start_utc_iso: str, end_utc_iso: str
    ) -> List[Dict[str, Any]]:
        """
        App-usage segments recorded in a UTC range that are **not yet uploaded**.

        This is the display counterpart of `get_pending_app_usage`, and it is
        deliberately a different query. The sync consumer wants rows it may
        upload *now*, so it filters on `status = 'pending'` and on the retry
        schedule. A day's totals want every row the backend cannot yet know
        about — including rows in flight and rows whose last upload failed and
        are waiting on a backoff. Those are real measurements; leaving them out
        would make an offline day's totals sag and then jump.

        Rows here and rows on the server are disjoint by construction: a row is
        deleted locally only once its upload has been acknowledged. So the
        caller may add the two without double counting, provided it reads the
        server first — see `background_services/activity/app_usage.py`.

        `recorded_at` is an ISO-8601 UTC string, so the half-open bounds are
        compared over the first 19 characters (`YYYY-MM-DDTHH:MM:SS`); that
        keeps the comparison exact whether or not a given row carries
        microseconds or a `+00:00` suffix.
        """
        rows = self._storage.query_all(
            """SELECT id, time_entry_id, application_name, window_title,
                      duration_seconds, recorded_at, status
               FROM pending_app_usage
               WHERE substr(recorded_at, 1, 19) >= ?
                 AND substr(recorded_at, 1, 19) < ?
               ORDER BY recorded_at ASC""",
            (start_utc_iso[:19], end_utc_iso[:19]),
        )
        return [dict(row) for row in rows]

    def mark_app_usage_processing(self, ids: List[str]) -> None:
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        self._storage.execute(
            f"UPDATE pending_app_usage SET status = 'processing' WHERE id IN ({placeholders})", ids
        )

    def complete_app_usage(self, ids: List[str]) -> None:
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        self._storage.execute(
            f"DELETE FROM pending_app_usage WHERE id IN ({placeholders})", ids
        )

    def fail_app_usage(self, ids: List[str], error_message: str, max_retries: int = 10) -> None:
        import random

        if not ids:
            return
        now = time.time()
        with self._storage.transaction() as conn:
            for record_id in ids:
                row = conn.execute(
                    "SELECT retry_count FROM pending_app_usage WHERE id = ?", (record_id,)
                ).fetchone()
                if not row:
                    continue
                retry_count = row["retry_count"] + 1
                if retry_count > max_retries:
                    conn.execute(
                        "UPDATE pending_app_usage SET status = 'failed' WHERE id = ?", (record_id,)
                    )
                else:
                    delay = min(2 ** (retry_count - 1), 60) * (0.5 + random.random())
                    conn.execute(
                        "UPDATE pending_app_usage SET status = 'pending', retry_count = ?, "
                        "next_retry_at = ? WHERE id = ?",
                        (retry_count, now + delay, record_id),
                    )

    def reset_processing_app_usage(self) -> None:
        self._storage.execute(
            "UPDATE pending_app_usage SET status = 'pending' WHERE status = 'processing'"
        )

    def clear_app_usage(self) -> None:
        self._storage.execute("DELETE FROM pending_app_usage")

    # ── Activity Samples ──────────────────────────────────────────────────────

    def save_activity_sample(
        self,
        time_entry_id: Optional[int],
        window_start: str,
        window_seconds: int,
        active_seconds: int,
        key_events: int = 0,
        mouse_events: int = 0,
        keyboard_strokes: int = 0,
        mouse_clicks: int = 0,
        mouse_movements: int = 0,
        activity_percent: Optional[int] = None,
        client_op: Optional[str] = None,
    ) -> str:
        """
        Persist one aggregated activity window.

        `activity_percent` is stored alongside the raw counts so the value the
        user sees is auditable against the inputs it was derived from.
        `keyboard_strokes`/`mouse_clicks`/`mouse_movements` are true event
        counts from the input counter; `key_events`/`mouse_events` are the
        seconds-with-input counters kept alongside them.

        `time_entry_id` may be None for a window measured before the backend
        issued one -- an offline start, or the first minute of a session while
        the start request is still in flight -- exactly as for
        `save_app_usage`. Such a row waits in the queue, is withheld from the
        uploader, and is adopted by `bind_activity_samples_to_entry` once the
        id arrives. `client_op` is the timer session's stable key and is what
        makes that adoption possible.
        """
        if activity_percent is not None:
            percent = max(0, min(100, activity_percent))
        elif window_seconds > 0:
            percent = max(0, min(100, round(active_seconds / window_seconds * 100)))
        else:
            percent = 0

        record_id = str(uuid.uuid4())
        now = time.time()
        self._storage.execute(
            """INSERT INTO activity_samples
               (id, time_entry_id, client_op, window_start, window_seconds, active_seconds,
                key_events, mouse_events, keyboard_strokes, mouse_clicks, mouse_movements,
                activity_percent, status, retry_count, next_retry_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?)""",
            (record_id, time_entry_id, client_op, window_start, window_seconds,
             active_seconds, key_events, mouse_events, keyboard_strokes,
             mouse_clicks, mouse_movements, percent, now, now),
        )
        return record_id

    def get_pending_activity_samples(
        self, limit: int = TELEMETRY_FETCH_LIMIT
    ) -> List[Dict[str, Any]]:
        """Activity windows ready to upload, oldest first.

        Bounded: see `TELEMETRY_FETCH_LIMIT`.

        Rows still waiting for their entry id are skipped rather than
        uploaded: the endpoint is per-entry, so there is nowhere to send them
        yet. They stay 'pending' and are picked up on a later pass, once
        `bind_activity_samples_to_entry` has adopted them. Same treatment, and
        the same reason, as `get_pending_app_usage`.
        """
        rows = self._storage.query_all(
            """SELECT id, time_entry_id, window_start, window_seconds, active_seconds,
                      key_events, mouse_events, keyboard_strokes, mouse_clicks, mouse_movements,
                      activity_percent, retry_count
               FROM activity_samples
               WHERE status = 'pending' AND next_retry_at <= ?
                 AND time_entry_id IS NOT NULL
               ORDER BY created_at ASC
               LIMIT ?""",
            (time.time(), limit),
        )
        return [dict(row) for row in rows]

    def bind_activity_samples_to_entry(self, client_op: str, time_entry_id: int) -> int:
        """
        Attribute activity windows captured before the backend issued an id.

        The same problem and the same answer as `bind_app_usage_to_entry`.
        The ``time_entry_id IS NULL`` guard makes a repeated bind a no-op
        rather than a way to re-point rows that are already attributed, so
        this is safe to call from both the live session (`bind_entry_id`) and
        the durable queue (`SyncService._adopt_session_telemetry`) -- and both
        are needed, because a start that failed over to the queue is confirmed
        only there, possibly after the session has already stopped.

        :return: how many rows were bound.
        """
        if not client_op:
            return 0
        cursor = self._storage.execute(
            "UPDATE activity_samples SET time_entry_id = ? "
            "WHERE time_entry_id IS NULL AND client_op = ?",
            (time_entry_id, client_op),
        )
        return cursor.rowcount or 0

    def count_unattributed_activity_samples(self) -> int:
        """Windows queued with no entry id, which only an adoption can
        release. Exists so this class of stall is visible rather than
        looking like an upload that is merely slow -- the same role
        `count_unattributed_screenshots` plays."""
        row = self._storage.query_one(
            "SELECT COUNT(*) AS n FROM activity_samples WHERE time_entry_id IS NULL"
        )
        return int(row["n"]) if row else 0

    def complete_activity_samples(self, ids: List[str]) -> None:
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        self._storage.execute(
            f"DELETE FROM activity_samples WHERE id IN ({placeholders})", ids
        )

    def fail_activity_samples(self, ids: List[str], error_message: str = "", max_retries: int = 10) -> None:
        import random

        if not ids:
            return
        now = time.time()
        with self._storage.transaction() as conn:
            for record_id in ids:
                row = conn.execute(
                    "SELECT retry_count FROM activity_samples WHERE id = ?", (record_id,)
                ).fetchone()
                if not row:
                    continue
                retry_count = row["retry_count"] + 1
                if retry_count > max_retries:
                    conn.execute(
                        "UPDATE activity_samples SET status = 'failed' WHERE id = ?", (record_id,)
                    )
                else:
                    delay = min(2 ** (retry_count - 1), 60) * (0.5 + random.random())
                    conn.execute(
                        "UPDATE activity_samples SET retry_count = ?, next_retry_at = ? "
                        "WHERE id = ?",
                        (retry_count, now + delay, record_id),
                    )

    def get_activity_percent_for_entry(self, time_entry_id: int) -> int:
        """
        Return the duration-weighted activity percentage for a time entry,
        computed from locally captured samples.

        Averages the same `activity_percent` each window recorded, weighted by
        the window's real length. It used to average `active_seconds` instead,
        which is presence — so an entry's figure was computed by a different
        rule than the per-window figures it was made of, and the two disagreed
        on the same screen.
        """
        row = self._storage.query_one(
            "SELECT SUM(activity_percent * window_seconds) AS weighted, "
            "       SUM(window_seconds) AS total "
            "FROM activity_samples WHERE time_entry_id = ?",
            (time_entry_id,),
        )
        if not row or not row["total"]:
            return 0
        return max(0, min(100, round(row["weighted"] / row["total"])))

    def get_day_activity_totals(self, start_utc_iso: str, end_utc_iso: str) -> Dict[str, int]:
        """
        Duration-weighted activity for the windows captured in a UTC range
        that have **not yet been uploaded**.

        These are exactly the windows the backend cannot know about: a sample
        row is deleted here only after its batch upload succeeded, so the
        local queue and the server's rows are disjoint and can be summed
        without double counting. Failed rows are included too — they are real
        measurements that are still waiting on a retry.

        `window_start` is stored as an ISO-8601 UTC string, so the bounds are
        compared on the first 19 characters (``YYYY-MM-DDTHH:MM:SS``); that
        keeps the comparison exact regardless of whether a given row happened
        to carry microseconds.
        """
        row = self._storage.query_one(
            """SELECT COALESCE(SUM(activity_percent * window_seconds), 0) AS weighted,
                      COALESCE(SUM(window_seconds), 0) AS measured
               FROM activity_samples
               WHERE window_seconds > 0
                 AND activity_percent BETWEEN 0 AND 100
                 AND substr(window_start, 1, 19) >= ?
                 AND substr(window_start, 1, 19) < ?""",
            (start_utc_iso[:19], end_utc_iso[:19]),
        )
        if not row:
            return {"weighted": 0, "measured": 0}
        return {
            "weighted": int(row["weighted"] or 0),
            "measured": int(row["measured"] or 0),
        }

    def clear_activity_samples(self) -> None:
        self._storage.execute("DELETE FROM activity_samples")

    # ── Unwanted Activity + Adjustments ──────────────────────────────────────
    #
    # Two small offline queues with the same pending/retry/backoff shape as
    # activity_samples. The row id doubles as the backend's client_event_id,
    # so a retried upload after a lost response can never double-insert an
    # event or -- worst of all -- apply the same deduction twice.

    def save_unwanted_activity(
        self,
        record_id: str,
        time_entry_id: int,
        activity_type: str,
        key_or_action: str,
        occurrence_count: int,
        alerted: bool,
        alert_count: int,
        recorded_at: str,
    ) -> str:
        self._storage.execute(
            """INSERT OR IGNORE INTO pending_unwanted_activity
               (id, time_entry_id, activity_type, key_or_action, occurrence_count,
                alerted, alert_count, recorded_at, status, retry_count,
                next_retry_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?)""",
            (record_id, time_entry_id, activity_type, key_or_action,
             occurrence_count, 1 if alerted else 0, alert_count, recorded_at,
             time.time(), time.time()),
        )
        return record_id

    def get_pending_unwanted_activity(
        self, limit: int = TELEMETRY_FETCH_LIMIT
    ) -> List[Dict[str, Any]]:
        """Unwanted-activity events ready to upload, oldest first.

        Bounded: see `TELEMETRY_FETCH_LIMIT`. These upload one request per
        event, so an unbounded read would also mean an unbounded number of
        round trips inside a single sync tick.
        """
        rows = self._storage.query_all(
            """SELECT id, time_entry_id, activity_type, key_or_action,
                      occurrence_count, alerted, alert_count, recorded_at, retry_count
               FROM pending_unwanted_activity
               WHERE status = 'pending' AND next_retry_at <= ?
               ORDER BY created_at ASC
               LIMIT ?""",
            (time.time(), limit),
        )
        return [dict(row) for row in rows]

    def complete_unwanted_activity(self, ids: List[str]) -> None:
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        self._storage.execute(
            f"DELETE FROM pending_unwanted_activity WHERE id IN ({placeholders})", ids
        )

    def fail_unwanted_activity(self, ids: List[str], max_retries: int = 10) -> None:
        self._fail_queue_records("pending_unwanted_activity", ids, max_retries)

    def save_adjustment(
        self,
        record_id: str,
        time_entry_id: int,
        adjustment_seconds: int,
        reason: str,
        source_activity_type: Optional[str],
        source_key_or_action: Optional[str],
        source_client_event_id: Optional[str],
        recorded_at: str,
    ) -> str:
        self._storage.execute(
            """INSERT OR IGNORE INTO pending_adjustments
               (id, time_entry_id, adjustment_seconds, reason,
                source_activity_type, source_key_or_action, source_client_event_id,
                recorded_at, status, retry_count, next_retry_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?)""",
            (record_id, time_entry_id, adjustment_seconds, reason,
             source_activity_type, source_key_or_action, source_client_event_id,
             recorded_at, time.time(), time.time()),
        )
        return record_id

    def get_pending_adjustments(
        self, limit: int = TELEMETRY_FETCH_LIMIT
    ) -> List[Dict[str, Any]]:
        """Time adjustments ready to upload, oldest first.

        Bounded: see `TELEMETRY_FETCH_LIMIT`. One request per adjustment, so
        the same round-trip argument as `get_pending_unwanted_activity`.
        """
        rows = self._storage.query_all(
            """SELECT id, time_entry_id, adjustment_seconds, reason,
                      source_activity_type, source_key_or_action,
                      source_client_event_id, recorded_at, retry_count
               FROM pending_adjustments
               WHERE status = 'pending' AND next_retry_at <= ?
               ORDER BY created_at ASC
               LIMIT ?""",
            (time.time(), limit),
        )
        return [dict(row) for row in rows]

    def complete_adjustments(self, ids: List[str]) -> None:
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        self._storage.execute(
            f"DELETE FROM pending_adjustments WHERE id IN ({placeholders})", ids
        )

    def fail_adjustments(self, ids: List[str], max_retries: int = 10) -> None:
        self._fail_queue_records("pending_adjustments", ids, max_retries)

    def _fail_queue_records(self, table: str, ids: List[str], max_retries: int) -> None:
        """Shared retry/backoff bookkeeping for the two queues above --
        exponential backoff with jitter, 'failed' after max_retries, same
        contract as fail_activity_samples."""
        import random

        if not ids:
            return
        now = time.time()
        with self._storage.transaction() as conn:
            for record_id in ids:
                row = conn.execute(
                    f"SELECT retry_count FROM {table} WHERE id = ?", (record_id,)
                ).fetchone()
                if not row:
                    continue
                retry_count = row["retry_count"] + 1
                if retry_count > max_retries:
                    conn.execute(
                        f"UPDATE {table} SET status = 'failed' WHERE id = ?", (record_id,)
                    )
                else:
                    delay = min(2 ** (retry_count - 1), 60) * (0.5 + random.random())
                    conn.execute(
                        f"UPDATE {table} SET retry_count = ?, next_retry_at = ? "
                        "WHERE id = ?",
                        (retry_count, now + delay, record_id),
                    )

    # ── URL Usage ─────────────────────────────────────────────────────────────

    def save_url_usage(
        self,
        time_entry_id: Optional[int],
        browser_name: str,
        domain: str,
        url: Optional[str],
        page_title: Optional[str],
        duration_seconds: int,
        recorded_at: str,
        client_event_id: Optional[str] = None,
        client_op: Optional[str] = None,
        is_private: Optional[bool] = None,
    ) -> str:
        """Queue one measured browser session for upload.

        `time_entry_id` may be None, exactly as in `save_app_usage`: the
        session is held against its timer session's `client_op` and adopted
        by `bind_url_usage_to_entry` when the backend issues the id.

        `is_private` is the browser's private/incognito state and is stored
        with three values, not two: True, False, and None for "could not be
        determined on this platform or browser". A private session is an
        ordinary URL row in every other respect — same queue, same retry, same
        idempotency key — because it is ordinary browsing that simply happened
        in a different kind of window.
        """
        record_id = str(uuid.uuid4())
        event_id = client_event_id or str(uuid.uuid4())
        now = time.time()
        self._storage.execute(
            """INSERT INTO pending_url_usage
               (id, time_entry_id, client_op, browser_name, domain, url, page_title,
                is_private, duration_seconds, recorded_at, client_event_id, status,
                retry_count, next_retry_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?)""",
            (record_id, time_entry_id, client_op, browser_name, domain, url, page_title,
             None if is_private is None else int(bool(is_private)),
             duration_seconds, recorded_at, event_id, now, now),
        )
        return record_id

    def bind_url_usage_to_entry(self, client_op: str, time_entry_id: int) -> int:
        """Attribute browser sessions captured before the entry id existed.

        The URL twin of `bind_app_usage_to_entry`; see that method for why
        the rows are held rather than dropped.
        """
        if not client_op:
            return 0
        cursor = self._storage.execute(
            "UPDATE pending_url_usage SET time_entry_id = ? "
            "WHERE time_entry_id IS NULL AND client_op = ?",
            (time_entry_id, client_op),
        )
        return cursor.rowcount or 0

    def get_pending_url_usage(
        self, limit: int = TELEMETRY_FETCH_LIMIT
    ) -> List[Dict[str, Any]]:
        """URL sessions ready to upload, oldest first.

        Bounded: see `TELEMETRY_FETCH_LIMIT`. Rows still waiting for their
        entry id are skipped, as in `get_pending_app_usage`.
        """
        rows = self._storage.query_all(
            """SELECT id, time_entry_id, browser_name, domain, url, page_title,
                      is_private, duration_seconds, recorded_at, client_event_id,
                      retry_count
               FROM pending_url_usage
               WHERE status = 'pending' AND next_retry_at <= ?
                 AND time_entry_id IS NOT NULL
               ORDER BY created_at ASC
               LIMIT ?""",
            (time.time(), limit),
        )
        return [dict(row) for row in rows]

    def get_unsynced_url_usage_between(
        self, start_utc_iso: str, end_utc_iso: str
    ) -> List[Dict[str, Any]]:
        """
        URL sessions recorded in a UTC range that are **not yet uploaded**.

        The URL twin of `get_unsynced_app_usage_between`, with the same
        reasoning: every row still present here is one the backend has not
        acknowledged, whatever its status, so it is exactly the set the day's
        remote totals are missing.
        """
        rows = self._storage.query_all(
            """SELECT id, time_entry_id, browser_name, domain, url, page_title,
                      is_private, duration_seconds, recorded_at, client_event_id, status
               FROM pending_url_usage
               WHERE substr(recorded_at, 1, 19) >= ?
                 AND substr(recorded_at, 1, 19) < ?
               ORDER BY recorded_at ASC""",
            (start_utc_iso[:19], end_utc_iso[:19]),
        )
        return [dict(row) for row in rows]

    def mark_url_usage_processing(self, ids: List[str]) -> None:
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        self._storage.execute(
            f"UPDATE pending_url_usage SET status = 'processing' WHERE id IN ({placeholders})", ids
        )

    def complete_url_usage(self, ids: List[str]) -> None:
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        self._storage.execute(
            f"DELETE FROM pending_url_usage WHERE id IN ({placeholders})", ids
        )

    def fail_url_usage(self, ids: List[str], error_message: str = "", max_retries: int = 10) -> None:
        import random

        if not ids:
            return
        now = time.time()
        with self._storage.transaction() as conn:
            for record_id in ids:
                row = conn.execute(
                    "SELECT retry_count FROM pending_url_usage WHERE id = ?", (record_id,)
                ).fetchone()
                if not row:
                    continue
                retry_count = row["retry_count"] + 1
                if retry_count > max_retries:
                    conn.execute(
                        "UPDATE pending_url_usage SET status = 'failed' WHERE id = ?", (record_id,)
                    )
                else:
                    delay = min(2 ** (retry_count - 1), 60) * (0.5 + random.random())
                    conn.execute(
                        "UPDATE pending_url_usage SET status = 'pending', retry_count = ?, next_retry_at = ? "
                        "WHERE id = ?",
                        (retry_count, now + delay, record_id),
                    )

    def reset_processing_url_usage(self) -> int:
        cursor = self._storage.execute(
            "UPDATE pending_url_usage SET status = 'pending' WHERE status = 'processing'"
        )
        return cursor.rowcount or 0

    def clear_url_usage(self) -> None:
        self._storage.execute("DELETE FROM pending_url_usage")

    # ── Screenshots ───────────────────────────────────────────────────────────
    #
    # The same pending/retry/backoff shape as the queues above, with one
    # difference that drives the whole design: a row here also owns a *file*.
    # The row is therefore the only record of what still has to be uploaded and
    # what may be deleted from disk, and it is written before the uploader can
    # ever see it. `complete_screenshot` is the single place a row is removed,
    # and it returns the path so the caller can delete the file only after the
    # backend has confirmed the upload -- never before.

    def save_screenshot(
        self,
        client_screenshot_id: str,
        local_file_path: str,
        captured_at: str,
        window_start: str,
        width: int,
        height: int,
        file_size_bytes: int,
        time_entry_id: Optional[int] = None,
        monitor_number: int = 1,
        client_op: Optional[str] = None,
        display_count: int = 1,
    ) -> str:
        """
        Register a captured screenshot for upload.

        Exactly one row per capture event, whatever the machine has plugged in.
        A three-monitor desk produces one merged image, one row here, one
        upload and one Drive file; `display_count` says how many displays that
        single image contains. It is metadata *about* the screenshot, and the
        moment it were allowed to become a row count instead, the queue, the
        backend's idempotency key and the grid would all start disagreeing
        about how many screenshots a window produced.

        `client_screenshot_id` is the UUID the backend de-duplicates on, and it
        is UNIQUE here too, so a retry that re-registers the same capture
        cannot produce two queue rows for one file.

        `client_op` is the timer session's own stable key. A capture taken
        before the backend has issued an entry id is held against it and
        adopted by `bind_screenshots_to_client_op` when the id arrives -- the
        same treatment `pending_app_usage` and `pending_url_usage` already get.
        """
        now = time.time()
        self._storage.execute(
            """INSERT OR IGNORE INTO pending_screenshots
               (id, client_screenshot_id, local_file_path, time_entry_id, client_op,
                captured_at, window_start, monitor_number, display_count, width, height,
                file_size_bytes, status, retry_count, next_retry_at,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?)""",
            (client_screenshot_id, client_screenshot_id, local_file_path, time_entry_id,
             client_op, captured_at, window_start, monitor_number,
             max(1, int(display_count or 1)), width, height,
             file_size_bytes, now, now, now),
        )
        return client_screenshot_id

    def get_pending_screenshots(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Screenshots ready to upload, oldest first.

        Rows with no `time_entry_id` are excluded: the backend authorises an
        upload against the entry it belongs to, so a capture taken before the
        entry id arrived has nothing to upload against yet. `bind_entry_id`
        fills those in, and they are picked up on the next pass.
        """
        rows = self._storage.query_all(
            """SELECT id, client_screenshot_id, local_file_path, time_entry_id,
                      captured_at, window_start, monitor_number, display_count,
                      width, height, file_size_bytes, retry_count
               FROM pending_screenshots
               WHERE status = 'pending' AND next_retry_at <= ?
                 AND time_entry_id IS NOT NULL
               ORDER BY created_at ASC
               LIMIT ?""",
            (time.time(), limit),
        )
        return [dict(row) for row in rows]

    def mark_screenshots_uploading(self, ids: List[str]) -> None:
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        self._storage.execute(
            f"UPDATE pending_screenshots SET status = 'uploading', updated_at = ? "
            f"WHERE id IN ({placeholders})",
            [time.time(), *ids],
        )

    def complete_screenshot(self, record_id: str) -> Optional[str]:
        """
        Remove an uploaded screenshot's queue row.

        :return: the local file path the row held, so the caller can delete the
            file now that the backend has confirmed it is stored. None if the
            row was already gone.
        """
        row = self._storage.query_one(
            "SELECT local_file_path FROM pending_screenshots WHERE id = ?", (record_id,)
        )
        self._storage.execute("DELETE FROM pending_screenshots WHERE id = ?", (record_id,))
        return row["local_file_path"] if row else None

    def fail_screenshot(self, record_id: str, error_message: str, max_retries: int = 12) -> bool:
        """
        Record an upload failure and schedule a jittered retry.

        :return: True if the screenshot will be retried. A row that exhausts
            its retries is parked as 'failed' rather than deleted — its file
            stays on disk, because discarding captured evidence of tracked work
            because the network was down for a day is not an acceptable
            outcome.
        """
        import random

        now = time.time()
        row = self._storage.query_one(
            "SELECT retry_count FROM pending_screenshots WHERE id = ?", (record_id,)
        )
        if not row:
            return False
        retry_count = row["retry_count"] + 1
        if retry_count > max_retries:
            self._storage.execute(
                "UPDATE pending_screenshots SET status = 'failed', last_error = ?, "
                "retry_count = ?, updated_at = ? WHERE id = ?",
                (error_message, retry_count, now, record_id),
            )
            return False
        delay = min(2 ** (retry_count - 1), 300) * (0.5 + random.random())
        self._storage.execute(
            "UPDATE pending_screenshots SET status = 'pending', retry_count = ?, "
            "next_retry_at = ?, last_error = ?, updated_at = ? WHERE id = ?",
            (retry_count, now + delay, error_message, now, record_id),
        )
        return True

    def drop_screenshot(self, record_id: str) -> Optional[str]:
        """Remove a row whose capture can never be uploaded (a missing or
        corrupt local file). Returns the path it held."""
        return self.complete_screenshot(record_id)

    def bind_screenshots_to_client_op(self, client_op: str, time_entry_id: int) -> int:
        """
        Attribute screenshots captured before the backend issued an entry id.

        The same problem the activity pipeline solves with held events: a
        capture taken in the first seconds of a session, or during an offline
        start, has no entry to belong to yet. Matching on the session's own
        `client_op` keeps the attribution honest — only captures this tracking
        session took are adopted, and a capture can never be attributed to a
        task that was not the one running when it was taken.

        This deliberately replaced an earlier binder keyed on `window_start`.
        That one could adopt only the single window tracking *began* in, so a
        queued start that took longer than ten minutes to land stranded every
        capture after the first window: `get_pending_screenshots` withholds a
        row with no entry id, nothing else ever filled it in, and the images
        sat on disk forever without reaching Drive or the database. Keying on
        the session covers every window the session actually ran for.

        :return: how many rows were bound.
        """
        if not client_op:
            return 0
        cursor = self._storage.execute(
            "UPDATE pending_screenshots SET time_entry_id = ?, updated_at = ? "
            "WHERE time_entry_id IS NULL AND client_op = ?",
            (time_entry_id, time.time(), client_op),
        )
        return cursor.rowcount or 0

    def count_unattributed_screenshots(self) -> int:
        """Captures that cannot upload because no entry id ever reached them.

        Reported separately from `count_screenshots_by_status`, where these
        rows appear as `pending` and so read as "about to upload". They are
        not: `get_pending_screenshots` withholds them, and only an adoption can
        release them. A non-zero count that does not fall is the signature of a
        capture stranded by a start whose confirmation never arrived, which is
        precisely the failure that used to be invisible.
        """
        row = self._storage.query_one(
            "SELECT COUNT(*) AS cnt FROM pending_screenshots WHERE time_entry_id IS NULL"
        )
        return int(row["cnt"]) if row else 0

    def reset_uploading_screenshots(self) -> int:
        """Return claims interrupted by a crash or shutdown to the pending pool."""
        cursor = self._storage.execute(
            "UPDATE pending_screenshots SET status = 'pending' WHERE status = 'uploading'"
        )
        return cursor.rowcount or 0

    def requeue_screenshots_for_new_run(self) -> int:
        """
        Retry every unsent screenshot promptly at the next launch.

        Two states need this, for the same reason. A screenshot that exhausted
        its retries is parked as `failed`, where nothing would ever pick it up
        again — so a day of captures would be silently discarded over a problem
        that has since been fixed. And a screenshot still `pending` can be
        sitting out a backoff of several minutes inherited from the previous
        run, which is time spent waiting for a condition that no longer holds.

        A launch is the natural boundary for both: it is when someone has
        changed something. The backoff is cleared for all of them, and the
        retry counter is reset only for the exhausted ones, so a fresh attempt
        gets a full budget rather than immediately re-exhausting a spent one.

        The files are all still on disk — nothing here discards a capture.
        """
        cursor = self._storage.execute(
            "UPDATE pending_screenshots "
            "SET status = 'pending', "
            "    retry_count = CASE WHEN status = 'failed' THEN 0 ELSE retry_count END, "
            "    next_retry_at = 0, updated_at = ? "
            "WHERE status IN ('failed', 'pending') AND next_retry_at > 0",
            (time.time(),),
        )
        return cursor.rowcount or 0

    def get_screenshot_backlog_paths(self) -> List[str]:
        """Local paths of every screenshot that has not been uploaded yet,
        including failed ones. These are the files that must not be deleted and
        whose day folders must not be pruned."""
        rows = self._storage.query_all(
            "SELECT local_file_path FROM pending_screenshots"
        )
        return [row["local_file_path"] for row in rows]

    def count_screenshots_by_status(self) -> Dict[str, int]:
        rows = self._storage.query_all(
            "SELECT status, COUNT(*) AS cnt FROM pending_screenshots GROUP BY status"
        )
        return {row["status"]: row["cnt"] for row in rows}

    def clear_screenshots(self) -> None:
        self._storage.execute("DELETE FROM pending_screenshots")

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def close(self) -> None:
        """
        Release this thread's database resources.

        Connection lifetime belongs to the StorageManager; the runtime closes
        it once, last, after all service threads have stopped. This method is
        retained so existing callers remain valid, and is a no-op beyond
        releasing the calling thread's connection.
        """
        self._storage.close_current_thread()

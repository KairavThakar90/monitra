"""
storage.manager — Controlled SQLite access with a per-thread connection model.

The audit found a single `sqlite3.Connection` opened with
`check_same_thread=False` and shared across the GUI thread, the sync thread,
the network thread and every ad-hoc worker, serialised behind one Python
`threading.Lock`. That design had three production consequences:

  1. Every database call in the process contended on one lock, so a slow write
     on a background thread stalled the GUI thread.
  2. `close()` set `_conn = None` while other threads were mid-statement,
     producing `NoneType` errors that were then swallowed by broad excepts —
     the source of silently degraded cached data.
  3. Interleaved reads and writes on one connection meant a reader could
     observe a partially applied multi-statement update.

The model here is the one SQLite itself recommends:

  * **One connection per thread**, created lazily and never shared.

    Connections are tracked in a dict keyed by `threading.get_ident()`, *not*
    in a `threading.local()`. That distinction is load-bearing here and cost a
    real leak to find: `threading.local` keys its storage on the thread
    *object* returned by `threading.current_thread()`. For a thread Python did
    not create — every Qt thread is one — CPython synthesises a `_DummyThread`
    on demand and lets it be garbage collected, so the next call gets a brand
    new thread object and therefore empty thread-local storage. The result was
    a fresh `sqlite3.connect()` on essentially every database call from a
    service thread: measured at 2,004 connections for 2,000 queued operations,
    climbing without bound. The OS thread id is stable for the life of the
    thread, so keying on it is correct where `threading.local` is not.
  * **WAL journal mode**, so readers never block the writer and vice versa.
  * **`busy_timeout`**, so concurrent writers wait rather than raising
    `database is locked`.
  * **Explicit transactions** via `transaction()`, so a multi-statement update
    is atomic and a reader can never see it half-applied.
  * **Ordered shutdown**: connections are closed by their owning thread, and
    the final close checkpoints the WAL. The 4 MB un-checkpointed WAL found in
    `~/.monitra` was direct evidence that the process was being killed rather
    than shut down.
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence

from core.logging_setup import get_logger
from core.paths import data_dir

log = get_logger("storage")


def cache_dir() -> Path:
    """
    Return the Monitra data directory, creating it if needed.

    Delegates to `core.paths.data_dir()` so the database, the sync queue and
    the logs can never disagree about where that directory is — and so a
    packaged build never tries to write inside its own (read-only,
    replaced-on-update) installation directory. See core/paths.py.
    """
    return data_dir()


def db_path() -> Path:
    """Return the path to the local SQLite database."""
    return cache_dir() / "cache.db"


#: The two telemetry tables whose `time_entry_id` is nullable, kept out of
#: `SCHEMA` as named DDL because `_relax_entry_id_constraint` has to recreate
#: one of them on an existing installation. Interpolated into `SCHEMA` below,
#: so a fresh database and a rebuilt table are created from the same text and
#: cannot drift apart.
PENDING_APP_USAGE_DDL = """
-- `time_entry_id` is nullable: a segment measured before the backend issued
-- an entry id (an offline start, or the first seconds of a session) is held
-- here against its session's `client_op` and adopted by
-- `LocalCache.bind_app_usage_to_entry` once the id arrives. Dropping those
-- segments instead is what used to lose an offline session's application
-- usage entirely. Same shape as `pending_screenshots`, for the same reason.
CREATE TABLE IF NOT EXISTS pending_app_usage (
    id TEXT PRIMARY KEY,
    time_entry_id INTEGER,
    client_op TEXT,
    application_name TEXT NOT NULL,
    window_title TEXT,
    duration_seconds INTEGER NOT NULL,
    recorded_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    retry_count INTEGER NOT NULL DEFAULT 0,
    next_retry_at REAL NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_app_usage_status ON pending_app_usage(status);
"""

PENDING_URL_USAGE_DDL = """
-- Nullable `time_entry_id` and `client_op`: see `pending_app_usage` above.
CREATE TABLE IF NOT EXISTS pending_url_usage (
    id TEXT PRIMARY KEY,
    time_entry_id INTEGER,
    client_op TEXT,
    browser_name TEXT NOT NULL,
    domain TEXT NOT NULL,
    url TEXT,
    page_title TEXT,
    -- Whether this browsing happened in a private/incognito window.
    -- Deliberately nullable: 1 and 0 are findings, NULL is "this platform or
    -- this browser could not tell us", which is a different thing from "no".
    -- Storing 0 for an unreadable state would assert something never observed.
    is_private INTEGER,
    duration_seconds INTEGER NOT NULL,
    recorded_at TEXT NOT NULL,
    client_event_id TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'pending',
    retry_count INTEGER NOT NULL DEFAULT 0,
    next_retry_at REAL NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_url_usage_status ON pending_url_usage(status);
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY,
    data TEXT NOT NULL,
    cached_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL,
    data TEXT NOT NULL,
    cached_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project_id);

CREATE TABLE IF NOT EXISTS task_cache_status (
    project_id INTEGER PRIMARY KEY,
    synced_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS task_statuses (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    color TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS time_entries_today (
    id INTEGER PRIMARY KEY,
    data TEXT NOT NULL,
    target_date TEXT NOT NULL,
    cached_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_entries_date ON time_entries_today(target_date);

CREATE TABLE IF NOT EXISTS pending_actions (
    id TEXT PRIMARY KEY,
    action_type TEXT NOT NULL,
    entity_type TEXT,
    entity_id TEXT,
    payload TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 5,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL DEFAULT 0,
    retry_count INTEGER NOT NULL DEFAULT 0,
    next_retry_at REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    idempotency_key TEXT,
    error_message TEXT,
    session_generation INTEGER NOT NULL DEFAULT 0,
    defer_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_pending_status ON pending_actions(status, priority, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_pending_idem
    ON pending_actions(idempotency_key) WHERE idempotency_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS session (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS app_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at REAL NOT NULL
);

%(pending_app_usage)s

CREATE TABLE IF NOT EXISTS activity_samples (
    id TEXT PRIMARY KEY,
    time_entry_id INTEGER NOT NULL,
    window_start TEXT NOT NULL,
    window_seconds INTEGER NOT NULL,
    active_seconds INTEGER NOT NULL,
    key_events INTEGER NOT NULL DEFAULT 0,
    mouse_events INTEGER NOT NULL DEFAULT 0,
    keyboard_strokes INTEGER NOT NULL DEFAULT 0,
    mouse_clicks INTEGER NOT NULL DEFAULT 0,
    mouse_movements INTEGER NOT NULL DEFAULT 0,
    activity_percent INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    retry_count INTEGER NOT NULL DEFAULT 0,
    next_retry_at REAL NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_activity_status ON activity_samples(status);
CREATE INDEX IF NOT EXISTS idx_activity_entry ON activity_samples(time_entry_id);

CREATE TABLE IF NOT EXISTS pending_unwanted_activity (
    id TEXT PRIMARY KEY,
    time_entry_id INTEGER NOT NULL,
    activity_type TEXT NOT NULL,
    key_or_action TEXT NOT NULL,
    occurrence_count INTEGER NOT NULL DEFAULT 0,
    alerted INTEGER NOT NULL DEFAULT 0,
    alert_count INTEGER NOT NULL DEFAULT 0,
    recorded_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    retry_count INTEGER NOT NULL DEFAULT 0,
    next_retry_at REAL NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_unwanted_status ON pending_unwanted_activity(status);

CREATE TABLE IF NOT EXISTS pending_adjustments (
    id TEXT PRIMARY KEY,
    time_entry_id INTEGER NOT NULL,
    adjustment_seconds INTEGER NOT NULL,
    reason TEXT NOT NULL,
    source_activity_type TEXT,
    source_key_or_action TEXT,
    source_client_event_id TEXT,
    recorded_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    retry_count INTEGER NOT NULL DEFAULT 0,
    next_retry_at REAL NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_adjustments_status ON pending_adjustments(status);

%(pending_url_usage)s

CREATE TABLE IF NOT EXISTS pending_screenshots (
    id TEXT PRIMARY KEY,
    client_screenshot_id TEXT NOT NULL UNIQUE,
    local_file_path TEXT NOT NULL,
    time_entry_id INTEGER,
    -- The timer session this capture belongs to. A capture taken before the
    -- backend has issued an entry id is adopted by this key once it arrives,
    -- exactly as `pending_app_usage` and `pending_url_usage` are. Window-based
    -- adoption preceded it and could only ever claim a session's first window.
    client_op TEXT,
    captured_at TEXT NOT NULL,
    window_start TEXT NOT NULL,
    monitor_number INTEGER NOT NULL DEFAULT 1,
    -- How many physical displays are composited into this one image. A
    -- screenshot event produces exactly one row whatever the display count;
    -- this describes that single image, it never multiplies it.
    display_count INTEGER NOT NULL DEFAULT 1,
    width INTEGER NOT NULL,
    height INTEGER NOT NULL,
    file_size_bytes INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    retry_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    next_retry_at REAL NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_screenshots_status ON pending_screenshots(status, next_retry_at);
CREATE INDEX IF NOT EXISTS idx_screenshots_entry ON pending_screenshots(time_entry_id);
""" % {
    "pending_app_usage": PENDING_APP_USAGE_DDL,
    "pending_url_usage": PENDING_URL_USAGE_DDL,
}

#: Columns added after the original schema shipped. Applied idempotently so an
#: existing ~/.monitra/cache.db upgrades in place without losing queued work.
MIGRATIONS = [
    ("pending_actions", "entity_type", "TEXT"),
    ("pending_actions", "entity_id", "TEXT"),
    ("pending_actions", "updated_at", "REAL NOT NULL DEFAULT 0"),
    ("pending_actions", "session_generation", "INTEGER NOT NULL DEFAULT 0"),
    ("pending_actions", "defer_count", "INTEGER NOT NULL DEFAULT 0"),
    # Keyboard/mouse event counts (pynput), alongside the original
    # presence-based seconds counters -- see activity/input_counter.py.
    ("activity_samples", "keyboard_strokes", "INTEGER NOT NULL DEFAULT 0"),
    ("activity_samples", "mouse_clicks", "INTEGER NOT NULL DEFAULT 0"),
    ("activity_samples", "mouse_movements", "INTEGER NOT NULL DEFAULT 0"),
    # The timer session key a segment captured before the backend issued an
    # entry id is adopted by. See the `pending_app_usage` schema comment.
    ("pending_app_usage", "client_op", "TEXT"),
    ("pending_url_usage", "client_op", "TEXT"),
    # The same key for screenshots. Existing rows keep NULL, which is honest:
    # a capture queued by an older build genuinely has no session key, and
    # `adopt_unattributed_screenshots` is what rescues those.
    ("pending_screenshots", "client_op", "TEXT"),
    # How many displays one merged capture contains. Rows queued by a build
    # that captured only the primary display default to 1, which is exactly
    # what they are — no backfill is needed or possible.
    ("pending_screenshots", "display_count", "INTEGER NOT NULL DEFAULT 1"),
    # Private/incognito browsing state. NULL on every existing row, meaning
    # "not observed", which is the truth for anything captured before this
    # could be detected — it must not read as "was not private".
    ("pending_url_usage", "is_private", "INTEGER"),
]

#: Indexes over columns `MIGRATIONS` adds, created after it has run.
POST_MIGRATION_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_app_usage_client_op ON pending_app_usage(client_op)",
    "CREATE INDEX IF NOT EXISTS idx_url_usage_client_op ON pending_url_usage(client_op)",
    "CREATE INDEX IF NOT EXISTS idx_screenshots_client_op ON pending_screenshots(client_op)",
)

#: Tables whose `time_entry_id` shipped as NOT NULL and must become nullable.
#:
#: SQLite cannot relax a column constraint in place, so these are rebuilt --
#: the standard create/copy/drop/rename, run inside one transaction so a
#: failure leaves the original table untouched rather than a half-migrated
#: one. Keyed by table name; the value is the column list to carry across,
#: which is every column of the *new* schema (`client_op` has already been
#: added by `MIGRATIONS` above by the time this runs).
NULLABLE_ENTRY_ID_REBUILDS = {
    "pending_app_usage": (
        PENDING_APP_USAGE_DDL,
        (
            "id", "time_entry_id", "client_op", "application_name", "window_title",
            "duration_seconds", "recorded_at", "status", "retry_count",
            "next_retry_at", "created_at",
        ),
    ),
    "pending_url_usage": (
        PENDING_URL_USAGE_DDL,
        (
            "id", "time_entry_id", "client_op", "browser_name", "domain", "url",
            "page_title", "is_private", "duration_seconds", "recorded_at",
            "client_event_id", "status", "retry_count", "next_retry_at",
            "created_at",
        ),
    ),
}


class StorageManager:
    """
    Owns the local database. Created once by the ApplicationRuntime.

    Thread-safety model: each thread gets its own connection, created on first
    use and closed when `close_current_thread()` (or `close()`, for the owning
    thread) is called. Connections are never passed between threads.
    """

    def __init__(self, path: Optional[str] = None) -> None:
        self._path = str(path or db_path())
        # Keyed by threading.get_ident(); see the module docstring for why this
        # is not a threading.local().
        self._conns: Dict[int, sqlite3.Connection] = {}
        self._conns_lock = threading.RLock()
        self._closed = False
        self._initialise_schema()

    @property
    def path(self) -> str:
        return self._path

    # ── Connections ───────────────────────────────────────────────────────────

    def _new_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=15.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        # WAL lets readers proceed during a write. Fall back gracefully on
        # filesystems that do not support it (e.g. some network shares).
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:
            try:
                conn.execute("PRAGMA journal_mode=DELETE")
            except sqlite3.DatabaseError:
                pass
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        log.debug(
            "opened sqlite connection for thread %s (%d)",
            threading.current_thread().name, threading.get_ident(),
        )
        return conn

    def connection(self) -> sqlite3.Connection:
        """Return this thread's connection, opening one if necessary."""
        if self._closed:
            raise RuntimeError("StorageManager is closed")
        ident = threading.get_ident()
        with self._conns_lock:
            conn = self._conns.get(ident)
            if conn is None:
                conn = self._new_connection()
                self._conns[ident] = conn
            return conn

    @property
    def connection_count(self) -> int:
        """Open connections. Bounded by the number of threads using storage."""
        with self._conns_lock:
            return len(self._conns)

    # ── Statements ────────────────────────────────────────────────────────────

    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        """Execute a single statement in autocommit mode."""
        return self.connection().execute(sql, params)

    def query_all(self, sql: str, params: Sequence[Any] = ()) -> List[sqlite3.Row]:
        return self.connection().execute(sql, params).fetchall()

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> Optional[sqlite3.Row]:
        return self.connection().execute(sql, params).fetchone()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """
        Run a block of statements atomically.

        Use this for every multi-statement update. A reader on another thread
        sees either none of the block or all of it — never a partial write.
        """
        conn = self.connection()
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.DatabaseError:
                log.exception("rollback failed")
            raise
        else:
            conn.execute("COMMIT")

    # ── Schema ────────────────────────────────────────────────────────────────

    def _initialise_schema(self) -> None:
        conn = self.connection()
        conn.executescript(SCHEMA)
        existing_tables = {
            row["name"] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        for table, column, decl in MIGRATIONS:
            if table not in existing_tables:
                continue
            columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            if column not in columns:
                log.info("migrating %s: adding column %s", table, column)
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        for table in NULLABLE_ENTRY_ID_REBUILDS:
            if table in existing_tables:
                self._relax_entry_id_constraint(conn, table)
        # Indexes over columns that `MIGRATIONS` adds. They cannot live in
        # SCHEMA, which runs before those columns exist on an upgraded
        # database, and they are recreated here after a rebuild drops them.
        for statement in POST_MIGRATION_INDEXES:
            conn.execute(statement)
        log.info("storage ready at %s", self._path)

    def _relax_entry_id_constraint(self, conn: sqlite3.Connection, table: str) -> None:
        """Make `table.time_entry_id` nullable, preserving every queued row.

        A no-op once the column is already nullable, so it costs one
        ``PRAGMA table_info`` per launch and runs its rebuild exactly once
        per installation. The rebuild is a single transaction: either the
        new table is in place with all the rows, or the original is still
        there untouched. Queued telemetry is measured time that cannot be
        recaptured, so it is never dropped to simplify a migration.
        """
        info = list(conn.execute(f"PRAGMA table_info({table})"))
        entry_id = next((row for row in info if row["name"] == "time_entry_id"), None)
        if entry_id is None or not entry_id["notnull"]:
            return

        ddl, column_names = NULLABLE_ENTRY_ID_REBUILDS[table]
        columns = ", ".join(column_names)
        staging = f"{table}_pre_nullable"
        # An index keeps its own name when its table is renamed, so the
        # originals have to go before the DDL below can recreate them under
        # the same names. Read while there is no transaction open.
        index_names = [
            row["name"] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=? "
                "AND name IS NOT NULL AND sql IS NOT NULL",
                (table,),
            )
        ]
        log.info("migrating %s: making time_entry_id nullable", table)
        conn.execute("BEGIN IMMEDIATE")
        try:
            # A previous attempt that died between the rename and the commit
            # would have been rolled back, but drop defensively rather than
            # failing the launch on a name collision.
            conn.execute(f"DROP TABLE IF EXISTS {staging}")
            conn.execute(f"ALTER TABLE {table} RENAME TO {staging}")
            for index_name in index_names:
                conn.execute(f"DROP INDEX IF EXISTS {index_name}")
            # Recreated from the same DDL a fresh installation uses, one
            # statement at a time -- `executescript` would commit the open
            # transaction out from under this rebuild.
            for statement in (s.strip() for s in ddl.split(";")):
                if statement:
                    conn.execute(statement)
            conn.execute(
                f"INSERT INTO {table} ({columns}) "
                f"SELECT {columns} FROM {staging}"
            )
            conn.execute(f"DROP TABLE {staging}")
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.DatabaseError:
                log.exception("rollback of the %s rebuild failed", table)
            raise
        else:
            conn.execute("COMMIT")

    # ── Shutdown ──────────────────────────────────────────────────────────────

    def close_current_thread(self) -> None:
        """
        Close the calling thread's connection, if it has one.

        Service threads call this as they stop, so a connection never outlives
        its thread — and so a recycled thread id can never inherit a connection
        belonging to a thread that has already exited.
        """
        ident = threading.get_ident()
        with self._conns_lock:
            conn = self._conns.pop(ident, None)
        if conn is None:
            return
        try:
            conn.close()
        except sqlite3.DatabaseError:
            log.exception("error closing connection for thread %d", ident)

    def close(self) -> None:
        """
        Close every connection and checkpoint the WAL.

        Called once, last, by the runtime's shutdown sequence — after all
        service threads have been confirmed stopped. Closing while other
        threads are still executing statements was one of the audited defects,
        so the runtime enforces that ordering.
        """
        if self._closed:
            return
        self._closed = True
        with self._conns_lock:
            conns = list(self._conns.values())
            self._conns.clear()

        # Checkpoint on whichever connection is still usable, so the WAL does
        # not keep growing across runs.
        for conn in conns:
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                break
            except sqlite3.DatabaseError:
                continue

        for conn in conns:
            try:
                conn.close()
            except sqlite3.DatabaseError:
                # A connection owned by a thread that has already exited may
                # refuse to close from here; the process is ending regardless.
                log.debug("connection close failed during shutdown", exc_info=True)
        log.info("storage closed (%d connection(s))", len(conns))


# ── Process-wide accessor ─────────────────────────────────────────────────────

_manager: Optional[StorageManager] = None
_manager_lock = threading.Lock()


def get_storage_manager(path: Optional[str] = None) -> StorageManager:
    """
    Return the process-wide StorageManager, creating it on first call.

    The ApplicationRuntime calls this during startup; everything else should
    receive the instance by injection rather than reaching for the global.
    """
    global _manager
    with _manager_lock:
        if _manager is None or _manager._closed:
            _manager = StorageManager(path)
        return _manager


def reset_storage_manager() -> None:
    """Drop the process-wide manager. Used by tests."""
    global _manager
    with _manager_lock:
        if _manager is not None:
            _manager.close()
        _manager = None

# Database connection lifecycle

Authoritative for how the backend holds PostgreSQL connections. Read it before
adding a route, a background task, a scheduled job, or any call that waits on
another system (SMTP, WFPM, Google Drive).

## The incident this exists for

PostgreSQL showed ~30 connections `idle in transaction` (`ClientRead`) for
minutes: every slot of the pool (2 workers × (5 + 10)) pinned, `QueuePool limit
... reached`, requests hanging until nginx answered 504. Three causes:

1. **Request-scoped `get_db`.** FastAPI keeps a `yield` dependency open until
   the response is sent *and the background tasks have run*. A request that did
   a SELECT kept its connection — and the transaction the SELECT opened —
   through the WFPM POST (10 s) and SMTP send (20 s) queued after it.
2. **Waiting inside a transaction.** The email/WFPM sweeps, the screenshot
   upload, and the screenshot downloads waited on SMTP / WFPM / Google Drive
   with a transaction open.
3. **Blocking the event loop.** The screenshot upload was `async def` and ran
   its Drive upload on the loop; sign-in ran a dozen queries on the loop. While
   a worker's loop is blocked, no response on it can be sent, so finished
   requests could not release their connections.

## The rules

- **`Depends(get_db, scope="function")` on every route and dependency.** Never
  bare `Depends(get_db)`. The scope runs `get_db`'s clean-up as soon as the
  route function — response serialisation included — returns. All sites must
  agree: FastAPI caches per scope, so a mix opens **two** sessions per request.
  `tests/test_db_lifecycle.py` fails on any unscoped site.
- **Before any slow call that does not need the database, call
  `end_transaction(db)`** (`app/core/database.py`), after reading into local
  variables whatever you still need — it commits, which expires every loaded
  instance. The next query checks out a new connection.
- **Routes that touch the database are plain `def`** (FastAPI runs them on the
  thread pool). An `async def` route that needs the database or any blocking
  client must hand that work to `run_in_threadpool`, as `AuthService` does.
- **Background tasks open their own session** and close it in `finally`
  (`deliver_in_background`, `evaluate_project_in_background`). Never pass a
  request's `db` to `BackgroundTasks`.
- **Background work that waits on a remote server goes through
  `run_blocking`** (`app/core/background.py`). A plain synchronous task runs on
  the same 40-thread pool as every synchronous route; during a burst, slow WFPM
  or SMTP waits filled it and requests queued for a thread *while holding the
  connection their auth query had checked out* (measured: one held for 6.1 s,
  the stand-in WFPM's delay). `run_blocking` caps those waits at
  `BACKGROUND_DELIVERY_CONCURRENCY` threads per worker.
- **Never store a Session** in a global, a singleton, or app state.

## Pool sizing

Each Uvicorn worker builds its own engine, so

    max connections = workers × (DB_POOL_SIZE + DB_MAX_OVERFLOW)

The `monitra-job@*` units are `curl` calls into the same workers
(`deploy/backend/scheduled-jobs/monitra-job.sh`); they add **nothing** to that
sum, but they do occupy a worker thread and a connection while they run.

| Setting (env) | Default | Meaning |
|---|---|---|
| `DB_POOL_SIZE` | 10 | connections kept per worker |
| `DB_MAX_OVERFLOW` | 5 | extra, per worker, under burst |
| `DB_POOL_TIMEOUT_SECONDS` | 10 | wait for a free connection, then 503 |
| `DB_POOL_RECYCLE_SECONDS` | 1800 | retire connections older than this |
| `DB_CHECKOUT_WARN_SECONDS` | 5 | log a connection held longer (0 = off) |
| `BACKGROUND_DELIVERY_CONCURRENCY` | 8 | background WFPM / email / budget threads per worker |
| `DB_IDLE_IN_TRANSACTION_TIMEOUT_MS` | 0 (off) | server-side backstop, see below |

Defaults: 2 workers × 15 = **30** connections (PostgreSQL allows 800). Serverless
(Vercel) multiplies by its instance count — set smaller values there.

## What to look for in the logs

- `DB_CONNECTION_HELD_LONG: held=… path=…` — a connection kept for seconds; the
  path is the route that held it. Each one is a leak to fix.
- `DB_POOL_EXHAUSTED: path=… pool=…` — a request waited the full pool timeout
  and was answered 503 + `Retry-After: 2`.
- `DB_CONNECTION_INVALIDATED` — the server dropped a connection (restart, or the
  idle-in-transaction timeout below).
- `/health` → `database_pool` — `checked_out` vs `max_connections` for the
  worker that answered.

## `idle_in_transaction_session_timeout` — enable last

It is a backstop, not the fix. **Do not enable it until the application fix is
deployed and the longest `idle in transaction` age is under ~15 s**, because the
sweeps and the screenshot path legitimately make slow calls; a server-side kill
mid-send turns "sent but not yet marked sent" into a duplicate email on retry.

Preferred: set `DB_IDLE_IN_TRANSACTION_TIMEOUT_MS=60000` in
`/etc/monitra/backend.env` and restart. It applies to this application's
connections only (not Alembic, not `psql`) and is reverted by unsetting it.
Role-wide alternative (affects *new* sessions of every client using the role,
including migrations if they run as `monitra_app`):

    ALTER ROLE monitra_app SET idle_in_transaction_session_timeout = '60s';

## Monitoring queries

    -- state summary
    SELECT state, count(*) FROM pg_stat_activity
    WHERE usename = 'monitra_app' GROUP BY state ORDER BY count(*) DESC;

    -- the goal: idle_in_transaction ~0, oldest_transaction in seconds
    SELECT count(*) AS total,
           count(*) FILTER (WHERE state = 'active') AS active,
           count(*) FILTER (WHERE state = 'idle') AS idle,
           count(*) FILTER (WHERE state = 'idle in transaction') AS idle_in_transaction,
           max(now() - xact_start) AS oldest_transaction
    FROM pg_stat_activity WHERE usename = 'monitra_app';

    -- who is holding a transaction open, and doing what
    SELECT pid, now() - xact_start AS xact_age, now() - state_change AS idle_for,
           left(query, 100) AS last_query
    FROM pg_stat_activity
    WHERE usename = 'monitra_app' AND state = 'idle in transaction'
    ORDER BY xact_age DESC;

Application side (`<api-unit>` is the systemd unit that runs Uvicorn):

    journalctl -u <api-unit> --since "1 hour ago" | grep -cE "DB_POOL_EXHAUSTED|QueuePool"
    journalctl -u <api-unit> --since "1 hour ago" | grep DB_CONNECTION_HELD_LONG | tail
    awk '$9 ~ /^50[234]/' /var/log/nginx/access.log | wc -l

## Acceptance

`DB_POOL_EXHAUSTED` = 0 and no `idle in transaction` older than 60 s under normal
load, then under `backend/tests/load/loadtest.py` at 50 / 100 / 150 (and 200)
users on a VM of production size against a non-production database.

## Connection establishment, unreachable databases and request ids

`DB_POOL_TIMEOUT_SECONDS` bounds the wait for a *pooled* connection; opening a *new* one had no limit,
so a database that was restarting, failing over or unreachable held a worker thread for the operating
system's TCP timeout. Every Postgres connection now has `connect_timeout` (`DB_CONNECT_TIMEOUT_SECONDS`,
10) and TCP keepalives (`DB_TCP_KEEPALIVES`, idle 30 s / interval 10 s / 3 probes). A statement timeout
exists (`DB_STATEMENT_TIMEOUT_MS`) but is **off**: the heaviest reports and the scheduled jobs share this
engine and have never been timed against a ceiling.

A lost or refused database connection is answered **503 + `Retry-After: 2`** (`DB_UNAVAILABLE`, with the
path and request id), not a plain-text 500 without CORS headers; a cancelled statement is a **504 +
`Retry-After: 30`** (`DB_STATEMENT_TIMEOUT`) that clients deliberately do not repeat. `get_db` no longer
returns the exception text (it can name the database host). The `DB_CONNECTION_HELD_LONG` and
`DB_POOL_EXHAUSTED` lines now carry `req=<X-Request-ID>`. `GET /health?deep=1` runs `SELECT 1` and
reports the latency (`status: degraded` when unreachable); the plain `/health` stays free of the
database. See [API_FAILURES.md](API_FAILURES.md) for how to follow one request through the logs.

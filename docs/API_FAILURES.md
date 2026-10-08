# API and data-load failures

How Monitra tells *why* a request failed, what each client does about it, and how to find one
request across the desktop, the browser, the web server and the database.

Design: [desktop/ARCHITECTURE.md](../desktop/ARCHITECTURE.md) ("Transient failures"),
[DB_CONNECTION_LIFECYCLE.md](DB_CONNECTION_LIFECYCLE.md). Idle-specific diagnostics:
[IDLE_DIAGNOSTICS.md](IDLE_DIAGNOSTICS.md).

## One request, one id

Every request carries `X-Request-ID` (the desktop: one id for all attempts of a request, with
`X-Monitra-Attempt: n` from the second; the web: a new id per attempt). The backend accepts a
well-formed id (8–64 of `A-Za-z0-9._:-`), otherwise mints one, **returns it on every response**, and
puts it in the `REQUEST_FAILED` / `SLOW_REQUEST` / `REQUEST_UNHANDLED` / `DB_*` log lines. A failed
response body also carries it (`{"detail": ..., "request_id": ...}`) for 500/503/504.

## The failure taxonomy

| Incident class | Desktop `FailureCode` (log: `API_FAIL … code=`) | Web `describeQueryError().kind` | Backend evidence |
|---|---|---|---|
| A DNS / network unreachable | `dns`, `unreachable` | `network` (`FETCH_ERROR`) | none (never arrived) |
| B connection refused | `refused` | `network` | none; nginx "connect() failed" |
| C connection timeout | `connect_timeout` | `network` / `timeout` | none |
| D read timeout | `read_timeout`, `write_timeout`, `pool_timeout` | `timeout` (`TIMEOUT_ERROR`) | `SLOW_REQUEST` (the work continues after the client gave up) |
| E/S 401, stale or expired session | `http_401` (`is_session_failure`) | `auth` | `AUTH_REFRESH_*` |
| F 403 | `http_403` | `auth` | `IDLE_…REJECTED`-style refusal lines per route |
| G/H 404 / 409 | `http_404` / `http_409` | `client` | route-specific |
| I 429 | `http_429` | `server` | (no rate limiter exists) |
| J 500 | `http_500` | `server` | `REQUEST_UNHANDLED`, `REQUEST_FAILED status=500` |
| K/L/M 502 / 503 / 504 | `http_502/503/504` | `server` | nginx error log; `DB_UNAVAILABLE`, `DB_POOL_EXHAUSTED`, `DB_STATEMENT_TIMEOUT` |
| N malformed response | `client` / PARSING_ERROR | `client` | — |
| O cancelled | `closed` (client shutting down) | RTK abort | — |
| P client exception | `client` | — | — |
| Q database failure/timeout | — | `server` (503/504) | `DB_UNAVAILABLE kind=connection`, `DB_STATEMENT_TIMEOUT` |
| R backend overloaded | `http_503` + `Retry-After` | `server` | `DB_POOL_EXHAUSTED`, `DB_CONNECTION_HELD_LONG`, `SLOW_REQUEST` |
| T unknown | `unknown` | `network` (no status) | — |
| connection dropped mid-request | `reset` | `network` | nginx "upstream prematurely closed" |
| server hung up with no answer | `protocol` | `network` | restart/deploy window; OOM kill |
| TLS / proxy | `tls`, `proxy` | `network` | — |

The sentence a user reads stays short ("Connection temporarily unavailable. Retrying…"); the
kind decides what is offered: a session that has ended gets sign-in, never Retry; a transient
failure gets Retry and a quiet bounded retry; nothing technical (status codes, hosts, ids) reaches
the screen.

## What each client does

**Desktop (`app/api/client.py`).** GET only; at most two re-sends; after a *fast* connection-level
failure (`dns`, `unreachable`, `refused`, `connect`, `reset`, `protocol`) or a 502/503/504 or a
429 with a short `Retry-After`; waits 0.3 s then 0.9 s (×50–150 %), 4 s in total; a reset also replaces
the connection pool. Never after a timeout, a slow failure, a definitive 4xx, a long `Retry-After`
(the backend's statement-timeout 504 sends 30 s precisely so it is not repeated), or for any write.
Writes (timer start/stop, idle answers, uploads) are repeated only by the durable queue, which has
idempotency keys. The network probe opts out (`retry=False`). Screens: projects/tasks show a
Retry link and retry on 3/6/12/24 s (spread out after the first); a 401 goes to sign-in.

**Web (`store/api/baseApi.ts`).** 45 s timeout; reads only; two re-sends after a network failure
or 502/503/504 (RTK's jittered backoff); never a timeout, 4xx or write. One shared
`renewSession()` (single-flight, Web Lock across tabs, adopts a token another tab already
replaced). Only the server's own 401/403 on the refresh ends the session. `LoadFailureNotice`
announces any failed read on screen with Retry and retries quietly at 5/15/45 s, then stops.

## "Projects would not load at 10:32" — finding the request

1. Desktop log (`<data dir>\logs\monitra.log`): `API_FAIL GET /api/v1/projects code=… attempt=n/3 elapsed_ms=… req=<id>`
   and `sync event=refresh.failed resource=projects code=… req=<id>`; `API_RECOVERED` if a retry worked.
2. Backend journal for the same `req=<id>`: `REQUEST_FAILED` / `SLOW_REQUEST` / `REQUEST_UNHANDLED`
   (with `user=` and `client=`), and the `DB_*` lines for it.
3. No `req=` line on the server for an id the client logged ⇒ the request never arrived (class A–C,
   or a reset before nginx): look at nginx's error log and the VM's restart history at that minute.

## Needs the owner (cannot be settled from the repository)

* `systemctl cat monitra-backend`, `nginx -T`, VM size and CPU-credit balance, OOM kills
  (`journalctl -k | grep -i oom`), Cloud SQL maintenance/failover events, GitHub Actions deploy times
  against incident times (every push to `main` touching `backend/` restarts the service without draining).
* Add `$request_time $upstream_response_time $http_x_request_id` to nginx's log format.
* Run `GET /health?deep=1` from a monitor: it is the only check that notices the database being
  unreachable.
* Consider `DB_STATEMENT_TIMEOUT_MS` (off by default) only after reading `SLOW_REQUEST` for a week.
* `GET /api/v1/react/dashboard` runs ~10–12 statements on one connection and its aggregate has no
  date predicate inside it; `EXPLAIN` it before changing anything.

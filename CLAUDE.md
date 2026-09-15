# CLAUDE.md — Rules of Engagement for This Repository

**Every agent working in this repository must read this file before making any change, and must satisfy the Definition of Done at the bottom before reporting work as complete.**

This project has a **working, verified, production-stable desktop runtime**. It got that way by
removing years of accumulated duplicate background implementations. The single most likely way
for future work to damage this project is an agent adding a "quick fix" — a new thread, a new
timer, a second cache, a direct SQLite call — next to the existing architecture instead of
going through it.

The rule is simple: **add your work inside the existing architecture. Do not build alongside it.**

---

## 1. Repository map

| Path | What it is | Stack |
|---|---|---|
| `desktop/` | Monitra PySide6 desktop client. **Stability-critical — see §3.** | Python 3, PySide6, SQLite |
| `backend/` | FastAPI service: `app/api` (routes), `app/services` (logic), `app/repositories` (data), `app/models`, `app/schemas`, `alembic/` (migrations) | Python 3, FastAPI, SQLAlchemy, Alembic |
| `frontend/` | Web client | React + TypeScript + Vite |
| `api/index.py` | Vercel serverless entrypoint that wraps the backend | — |
| `docs/` | Project documentation | — |

Layering in `backend/` is strict and already consistent: **api → services → repositories → models.**
Routes must not touch the database directly, and repositories must not import routes.

---

## 2. Non-negotiable rules (all areas)

1. **Understand before you change.** Read the surrounding module and its tests first. If a
   mechanism already exists for what you need, use it. Never introduce a second mechanism for a
   job something already does.
2. **Do not "fix" a symptom you have not diagnosed.** If a value looks wrong on screen, trace the
   data path. **Never hardcode, mock, fake, or fall back to placeholder data to make the UI look
   correct.** An honest empty state is always better than fabricated data. (Mock screenshot and
   URL data was removed from this app for exactly this reason — do not reintroduce it.)
3. **Do not delete or weaken tests, guards, or the architecture checker to make something pass.**
   If a guard blocks you, the design is telling you something. Fix the approach, or raise it with
   the user.
4. **Stay inside the scope you were asked for.** Do not opportunistically refactor working code,
   reformat files, or "clean up" modules you were not asked to touch. Unrelated churn is how
   working systems break.
5. **Commit your work in logical increments as you go.** An earlier version of the desktop
   refactor was lost because it was left uncommitted. Do not leave large work sitting in the
   working tree. Do **not** push or open PRs unless the user explicitly asks.
6. **Never commit secrets or generated artifacts.** `.env*` files, build output, `__pycache__`,
   soak/run reports, and databases stay out of git.
7. **Report honestly.** If something is unfinished, blocked, or unverified, say so explicitly and
   say why. Never describe untested work as verified.

---

## 3. `desktop/` — the stability-critical zone

Before changing **anything** in `desktop/`, read both of these in full:

- **[desktop/ARCHITECTURE.md](desktop/ARCHITECTURE.md)** — how the runtime is designed and why.
- **[desktop/DO_NOT_DO.md](desktop/DO_NOT_DO.md)** — every anti-pattern that caused a real
  production failure here, paired with the failure it caused. This is a list of mistakes already
  made and paid for. Do not repeat them.

These two documents are authoritative for `desktop/`. Where anything conflicts with them, they win.

### 3.1 The one-owner rule

Exactly one component owns each concern. Route your work through the owner; never open a second path.

| Concern | Sole owner |
|---|---|
| Application lifetime, service start/stop order | `core/runtime.py` (`ApplicationRuntime`) |
| Background execution | `core/tasks.py` (`TaskRunner`) and `core/service.py` (`LoopService`) |
| SQLite access | `storage/manager.py` (`StorageManager`) |
| Cache / durable queue rows | `sync/local_cache.py` |
| Queue consumption + upload | `background_services/sync/sync_service.py` |
| Tracked time (the only source of truth) | `background_services/timer/timer_service.py` |
| Online/offline state | `background_services/network/network_service.py` |
| Activity capture | `background_services/activity/` |
| Tray + notifications | `background_services/notifications/` |
| "A new version is available" notices | `background_services/update/update_service.py` |
| Crash/session recovery | `background_services/recovery/` |
| The UI's entire view of the above | `background_services/public_api.py` (`BackgroundApi`) |

### 3.2 Hard prohibitions in `desktop/`

These are enforced by `tools/check_architecture.py`, which runs in CI. Violations fail the build.

- **No `QThread`, `QThreadPool`, or `QRunnable`** outside `core/`, `background_services/`, and
  `storage/`. Background work goes through `TaskRunner` or a `LoopService` — always.
- **UI code must not import service internals.** UI talks to `BackgroundApi` only.
- **No direct `sqlite3.connect()` anywhere.** Go through `StorageManager`, which owns per-thread
  connections, WAL mode, and transactions. (`threading.local()` is unsafe for Qt threads — it
  silently leaked a connection per call. Connections are keyed on `threading.get_ident()`.)
- **Removed modules stay removed.** Do not recreate `sync/sync_queue.py`,
  `sync/network_monitor.py`, `tracking/manager.py`, `tracking/app_usage_tracker.py`,
  `ui/notification_manager.py`, `ui/workers.py`, or `app/timer/engine.py`. Every one of them was
  a duplicate background implementation competing with the runtime.

### 3.3 Rules the checker cannot enforce — you must uphold these yourself

- **Never subclass `QThread` and override `run()`**, and never declare a `finished` signal on a
  QThread subclass. It shadows `QThread.finished` and produces
  `QThread: Destroyed while thread is still running`. Use the QObject-worker-on-QThread pattern.
- **Never block the UI thread.** No network calls, no `time.sleep()`, no `wait()`, no long loops
  in slots or in `closeEvent`. If it can take more than a few milliseconds, it goes to a service
  or the task pool.
- **Signals that trigger work must be edge-triggered, not level-triggered.** A signal that fires
  on every poll of an unchanged state caused a permanent two-threads-per-second worker storm.
  Emit on *transition* only.
- **Elapsed time is derived from timestamps** (`now_utc − started_at_utc`), never from
  `time.monotonic()` counters and never mirrored into a second counter. There were once four
  competing counters; there is now one. Keep it that way.
- **Every retry needs backoff with jitter**, and every queued operation needs an idempotency key.
  Synchronised retries are a self-inflicted load test.
- **Anything registered with the runtime must stop deterministically** within its
  `stop_timeout_ms`, and must release its own thread's resources before the thread exits. Shutdown
  must never escalate to `terminate()`.
- **New background work is registered with `ApplicationRuntime`** in the correct start order —
  it is not started ad hoc. Shutdown is the exact reverse of start order.

---

## 3.3 Tracked time — one clock, one duration

**[docs/TIMING_MODEL.md](docs/TIMING_MODEL.md) is authoritative** for anything
that starts, stops, stores, sums or displays tracked time, in any layer. The
rules most likely to be broken by a well-meaning change:

- A duration is `round(end_time − start_time)` from the persisted `timestamptz`
  columns. Never persist a client counter, and never rebuild seconds from
  two-decimal hours.
- The server clock is the reference. Clients send the event instant **and**
  their own clock (`client_time`); the server places the event by age. Never
  compare a client instant to a server instant directly.
- Start is idempotent on `client_op`; stop is idempotent on the entry. Never
  treat a 409 on a start as "done" — it names the entry that *is* running.
- The desktop keeps its local anchor for a session it started; the dashboard
  re-reads the day on `timer_finalized`, not on the local stop.

## 3.4 Input validation — all three layers

Every user-controlled input goes through the centralised validation framework.
**[docs/VALIDATION.md](docs/VALIDATION.md) is authoritative**; read it before adding
any field, form, dialog, or endpoint.

| Layer | Package |
|---|---|
| Backend | `backend/app/core/validation/` |
| Desktop | `desktop/core/validation/` |
| Frontend | `frontend/src/validation/` |

The rules that most often get broken by a well-meaning change:

- **Never write a one-off validator, length cap, or email regex.** Pick a rule from
  the catalogue. A field needing a different limit uses a `max_length` override, not
  a new rule.
- **The backend validates everything, always** — even when both clients already did.
  It also serves `curl`, replayed requests, and custom HTTP clients.
- **A client must never be stricter than the backend**, or the field becomes
  unusable and the bug arrives as "the app won't let me save my work".
- **Never trim, normalise, log, or content-check a password.** Length only. A
  password legitimately contains `<`, `>`, `{`, `}`, spaces and accents; every other
  transformation silently changes a secret and reads to the user as "wrong password".
  Login uses `Credential` (upper bound only), not `Password` — enforcing the minimum
  at sign-in locks out every account older than the policy.
- **Reject invalid input; never scrub it and continue.** Storing a quietly-edited
  version of what someone wrote is a data-integrity bug wearing a security costume.
- **Any user text reaching a SQL `LIKE` goes through `like_pattern()`** paired with
  `escape=LIKE_ESCAPE_CHARACTER`. Unescaped, a search for `100%` matches every row.

The desktop and backend catalogues are kept in step mechanically:
`desktop/tests/test_validation_framework.py` reads both `rules.py` files and fails
if any shared limit or pattern differs. Change a limit in one layer and you must
change it in all three.

---

## 4. `backend/` and `frontend/`

- **Backend:** respect the api → services → repositories → models layering. Any schema change
  requires an Alembic migration in `backend/alembic/` — never edit a model without one. Keep
  Pydantic schemas in `app/schemas` in sync with what routes actually return.
- **Backend ↔ desktop contract:** if you change a response shape the desktop consumes, you must
  update the desktop side in the same change, or the desktop's optimistic UI and sync queue will
  silently diverge.
- **Frontend:** `npm run build` must pass (it runs `tsc -b`), and `npm run lint` must be clean.
  Do not add new `.backup.tsx` or one-off `.cjs` codemod scripts — several already litter the
  tree; do not add more.

---

## 5. Known open items — do not treat these as bugs

These are **unimplemented features**, deliberately left out of the stability work. Do not
"fix" them with placeholder data, and do not start them without the user asking.

1. **Activity upload — implemented (2026-08-31).** `TimeEntryService.batch_sync_activity` now
   exists and `SyncService._sync_activity` uploads windows to
   `POST /time-entries/{id}/activity/batch` (backend `time_entry_activity`). Keyboard/mouse
   *counts* come from `background_services/activity/input_counter.py` (pynput, both platforms;
   listeners run only while a timer runs), and unwanted-activity detection with auditable time
   deductions lives in `background_services/activity/unwanted_activity.py` +
   `time_entry_adjustments` — deductions never modify `time_entries.total_seconds`.
2. **URL tracking — implemented (2026-09-01).** The URL actually shown in the browser's address
   bar is read through Windows UI Automation (`tracking/browsers/uia.py`, pure ctypes — no new
   dependency, no extension, no debugging port) and captured into segments by
   `background_services/activity/url_usage_service.py`. Chromium browsers (Chrome, Edge, Brave,
   Vivaldi, Opera) report a real URL; Firefox only does so when accessibility is enabled, and
   macOS/Linux have no address-bar reader at all. Where no URL can be read, the observation is
   `UrlSource.UNAVAILABLE` and **no URL record is written** — the time is still captured as
   application usage against the browser. Never substitute a placeholder domain here.
3. **Screenshot capture** was never implemented client-side. That tab shows an honest empty
   state.
4. **Tray/taskbar behaviour is unverified on a real display** — all automated runs are headless.
5. `.github/CODEOWNERS` still contains placeholder handles.
6. **Auto-update — implemented (2026-09-08), but not yet safe to publish for real
   users.** Releases live in `desktop_releases` (one row per artifact, with its own
   SHA-256 and status); `background_services/update/` checks every ten hours against a
   persisted timestamp, and downloads, verifies and installs only when the user chooses
   to. Nothing without a matching SHA-256 is ever executed, and a failed update always
   leaves the installed application working. **The prerequisite recorded in
   `docs/Desktop_Update_Distribution_Decisions.md` §2 is still outstanding: the
   artifacts are unsigned.** Registering releases as drafts and piloting them is fine;
   publishing one to real users is not, until Windows code signing and macOS
   notarization are in place. The macOS update path has also never run on real hardware.

---

## 6. Definition of Done — the verification gate

**No agent may report work in this repository as complete without running the checks for the
areas it touched and pasting the actual output.** "Should work" is not done.

If you touched `desktop/` — from `desktop/`:

```bash
python -m pytest tests/ -q            # must pass (104 tests at time of writing; the count only goes up)
python tools/check_architecture.py    # must print: Architecture boundaries OK
```

If you touched anything concurrency-, timer-, sync-, storage-, or shutdown-related, **also**:

```bash
python tests/soak/run_launch_cycles.py --cycles 10   # expect 10/10 clean
python tests/soak/run_soak.py --duration 60          # expect: PASS — no thread growth,
                                                     # no duplicates, queue drained, clean shutdown
```

If you touched `backend/`:

```bash
python -m pytest tests/ -q
```

If you touched `frontend/`:

```bash
npm run build && npm run lint
```

**Additionally, for any change to `desktop/`:** add or extend a test that would have caught the
bug you fixed or that covers the behaviour you added. The connection leak, the offline
start/stop ordering bug, and the offline capture gap were all found by the soak and the tests —
not by reading code. That safety net only keeps working if it grows with the code.

---

## 7. If a rule here blocks you

Do not work around it, and do not silently ignore it. Stop and tell the user what the rule is,
what you were trying to do, and what you would recommend instead. These rules exist because each
one maps to a failure this project already suffered in production.

# WFPM ↔ Monitra integration

This document is the contract between Monitra and WFPM. It is written for two
readers: the WFPM developers building their side, and whoever maintains
Monitra's side later. Change a route, a payload or a status code in
`backend/app/WFPM/` and you must change this file in the same commit, because
the WFPM side is written against it.

Everything about the integration lives in one folder, `backend/app/WFPM/`, and
every log line it writes starts with `WFPM_`.

---

## 1. How the two systems are linked

Two columns hold the mapping, both on the Monitra side:

| Monitra table | Column | Holds |
|---|---|---|
| `projects` | `wfpm_project_id` | The id this project has in WFPM |
| `tasks` | `wfpm_task_id` | The id this task has in WFPM |

WFPM does not need to store any Monitra id. It creates a project or task in
Monitra naming **its own id**, and from then on addresses that record by the
same id.

**What a WFPM id may be.** Monitra treats it as opaque text and stores it as a
string. Both of these are accepted and mean the same record:

- a JSON number: `55` (must be a positive whole number)
- a JSON string: `"55"`, `"TASK-55"` — 1 to 255 characters from
  `A–Z a–z 0–9 . _ : -`

Anything else (spaces, slashes, markup, `0`, a negative number, a boolean) is
refused with `422`. Responses always return the id as a **string**.

A WFPM id is unique per organization: one WFPM project is linked to at most
one Monitra project, and one WFPM task to at most one Monitra task.

Projects and tasks that were created in Monitra itself have no WFPM id. They
are unaffected by everything in this document.

---

## 2. WFPM → Monitra

### 2.1 Authentication

Every request carries a Monitra access token:

```
Authorization: Bearer <Monitra JWT>
```

It is the same token the user gets from Monitra's `/auth` sign-in; there is no
separate WFPM credential. The request therefore acts **as that user**: Monitra
applies the user's own role to it, exactly as if they had done the same thing
in the Monitra app. A request without a valid token is answered `401`.

### 2.2 Routes

Base path: `/WFPM/sync`. `{wfpm_project_id}` and `{wfpm_task_id}` are always
**WFPM ids**, never Monitra ids.

| # | Operation | Method and path | Permission needed |
|---|---|---|---|
| 1 | Create project | `POST /WFPM/sync/projects` | `wfpm:projects:create` |
| 2 | Get project | `GET /WFPM/sync/projects/{wfpm_project_id}` | `projects:view` |
| 3 | Update project | `PATCH /WFPM/sync/projects/{wfpm_project_id}` | `projects:update` |
| 4 | Assign member(s) to project | `POST /WFPM/sync/projects/{wfpm_project_id}/members` | `project_members:manage` |
| 5 | Remove member from project | `DELETE /WFPM/sync/projects/{wfpm_project_id}/members/{member_id}` | `project_members:manage` |
| 6 | Create task | `POST /WFPM/sync/projects/{wfpm_project_id}/tasks` | `tasks:create` |
| 7 | Get task | `GET /WFPM/sync/tasks/{wfpm_task_id}` | `tasks:view` |
| 8 | Update task | `PATCH /WFPM/sync/tasks/{wfpm_task_id}` | `tasks:update` |
| 9 | Assign task | `PUT /WFPM/sync/tasks/{wfpm_task_id}/assignee` | `tasks:update` |
| 10 | Remove assignee | `DELETE /WFPM/sync/tasks/{wfpm_task_id}/assignee` | `tasks:update` |

Which role holds which permission (from `backend/app/core/permissions.py`):

| Role | 1 create project | 2, 7 read | 3 update project | 4, 5 members | 6 create task | 8, 9, 10 update / assign task |
|---|---|---|---|---|---|---|
| administrator, org_admin, super_admin | yes | yes | yes | yes | yes | yes |
| leader, project_leader | yes | yes | yes — own projects | yes — projects they lead | yes | yes |
| manager | yes | yes | yes | no ¹ | yes | yes |
| hr | yes | yes | no | no | no | no |
| employee | yes | yes — projects they belong to | no | no | yes — projects they belong to | yes — their own tasks |

¹ A manager holds the permission, but Monitra's member rule allows only an
administrator or the project's own leader to change membership, so the request
is refused with `403`. The same applies to a leader on a project somebody else
leads.

Being allowed to call a route is not the same as being allowed to touch a
given record. A user can only reach a project or task through its WFPM id if
they could open it in Monitra. If they cannot, the answer is `404` — the same
answer as for an id that is not linked at all.

All `member_id` / `assignee_id` / `employee_ids` values are **Monitra user
ids** (the `id` returned by Monitra's sign-in and member endpoints).

#### 1. Create project — `POST /WFPM/sync/projects`

```json
{
  "wfpm_project_id": 55,
  "project_name": "Website rebuild",
  "description": "Optional",
  "employee_ids": [101, 102],
  "deadline": "2026-12-31",
  "billing_type": "free"
}
```

| Field | Required | Notes |
|---|---|---|
| `wfpm_project_id` | yes | See §1 |
| `project_name` | yes | 1–150 characters, not blank |
| `description` | no | Up to 5000 characters |
| `employee_ids` | no | Monitra user ids to add as members; no duplicates |
| `deadline` | no | `YYYY-MM-DD`, not in the past. Omit or send `null` for none |
| `billing_type` | yes | `"free"`. (`"fixed"` needs an hours budget, which this API does not accept, so it is refused) |

Not chosen by the caller: the project starts in Monitra's **Active** status,
with no owner, and led by the Monitra user the product has fixed as the leader
of every WFPM project (`DEFAULT_PROJECT_LEADER_ID` in
`backend/app/WFPM/service.py`) — unless the caller is themselves a leader, in
which case they lead it. Monitra also seeds its four default tasks, as it does
for every project; those have no WFPM id.

| Status | Meaning |
|---|---|
| `201` | Created. Body is the project (see §2.3) |
| `200` | **This WFPM id was already linked.** Body is the existing project, unchanged — nothing in this request was applied. Safe to treat as success |
| `409` | This WFPM id is linked to a project that is archived, or that this user cannot access |
| `400` | A Monitra rule refused it (an `employee_ids` entry is not an active user of the organization, …) |
| `403` | The user's role may not create a project through WFPM |
| `422` | A field is malformed |

**Create is idempotent on `wfpm_project_id`.** If WFPM does not receive a
reply, it should simply send the same request again: it will get `200` and the
project that the first attempt created, never a duplicate.

#### 3. Update project — `PATCH /WFPM/sync/projects/{wfpm_project_id}`

Send only the fields to change.

```json
{ "project_name": "Website rebuild (phase 2)", "status": "completed" }
```

| Field | Notes |
|---|---|
| `project_name` | 1–150 characters |
| `description` | `null` clears it |
| `deadline` | `null` clears it; a date must not be in the past |
| `status` | `"active"`, `"paused"` or `"completed"` |

Members, leader, owner and billing are not changed by this route.
`200` with the project; `404` if the id is not linked or not accessible.

#### 4. Assign members — `POST /WFPM/sync/projects/{wfpm_project_id}/members`

```json
{ "member_ids": [101, 102] }
```

1 to 200 ids. `200`:

```json
{
  "message": "Members added successfully",
  "project_id": 2467,
  "added_member_ids": [102],
  "already_assigned_member_ids": [101]
}
```

Adding someone who is already a member is not an error. `404` if an id is not
an active user of the organization (nobody is added in that case).

#### 5. Remove member — `DELETE /WFPM/sync/projects/{wfpm_project_id}/members/{member_id}`

`204` with no body. `404` if that user is not a member of the project — so a
repeated removal answers `404`, which is safe to treat as "already removed".
`400` if the id is not a user of this organization. Removing a member does not
unassign their tasks.

#### 6. Create task — `POST /WFPM/sync/projects/{wfpm_project_id}/tasks`

```json
{
  "wfpm_task_id": 900,
  "name": "Design the homepage",
  "assignee_id": 101,
  "estimated_hours": 12.5
}
```

| Field | Required | Notes |
|---|---|---|
| `wfpm_task_id` | yes | See §1 |
| `name` | yes | 1–150 characters, not blank |
| `assignee_id` | no | Must be an active **employee** who is a **member of this project** |
| `estimated_hours` | no | 0 – 999.99 |

The task starts in Monitra's **Todo** status. If no `assignee_id` is sent and
the caller is an employee, the task is assigned to the caller.

`201` created · `200` the WFPM task id was already linked (existing task,
unchanged) · `409` the id is linked under a different project, or to a task
that is archived or not accessible · `404` the project id is not linked ·
`400` the assignee is not valid for this project. Idempotent on
`wfpm_task_id`, exactly like project create.

#### 8. Update task — `PATCH /WFPM/sync/tasks/{wfpm_task_id}`

```json
{ "name": "Design the homepage (v2)", "status": "in_progress" }
```

| Field | Notes |
|---|---|
| `name` | 1–150 characters |
| `status` | `"todo"`, `"in_progress"` or `"completed"` |
| `estimated_hours` | `null` clears it |

#### 9. Assign task — `PUT /WFPM/sync/tasks/{wfpm_task_id}/assignee`

```json
{ "assignee_id": 101 }
```

Replaces the current assignee. A Monitra task has **one** assignee. The user
must be an active employee and a member of the task's project, otherwise
`400`. `200` with the task.

#### 10. Remove assignee — `DELETE /WFPM/sync/tasks/{wfpm_task_id}/assignee`

`200` with the task, now unassigned. In Monitra an unassigned task is shared
project work: every member of the project can see it and track time on it.
Repeating the call changes nothing and answers the same way.

### 2.3 Response bodies

Project (routes 1–3):

```json
{
  "id": 2467,
  "wfpm_project_id": "55",
  "project_name": "Website rebuild",
  "description": null,
  "status": { "id": 5, "name": "Active", "color": "#22C55E" },
  "owner": null,
  "leader": { "id": 279, "name": "…", "email": "…", "role": "administrator" },
  "employees": [ { "id": 101, "name": "…", "email": "…", "role": "employee" } ],
  "deadline": "2026-12-31",
  "billing_type": "free",
  "fixed_hours": null,
  "organization_id": 1,
  "created_at": "2026-09-30T10:00:00Z",
  "updated_at": "2026-09-30T10:00:00Z",
  "tasks": [ /* task objects, each with its own wfpm_task_id (null if not linked) */ ]
}
```

Task (routes 6–10):

```json
{
  "id": 848,
  "wfpm_task_id": "900",
  "project_id": 2467,
  "name": "Design the homepage",
  "assignee_id": 101,
  "assignee": { "id": 101, "name": "…", "email": "…", "role": "employee" },
  "status": { "id": 1, "name": "Todo", "color": "#CBD5E1" },
  "estimated_hours": 12.5,
  "created_at": "2026-09-30T10:00:00Z",
  "updated_at": "2026-09-30T10:00:00Z"
}
```

`id` and `project_id` are Monitra's own ids, returned for reference.

Errors use one shape throughout: `{"detail": "…"}` for `400/401/403/404/409`,
and `{"detail": [ … ]}` (a list of field errors) for `422`.

### 2.4 The older routes

Five routes existed before the mapping and are unchanged:

```
GET  /WFPM/projects                     POST /WFPM/projects
GET  /WFPM/projects/{project_id}
GET  /WFPM/projects/{project_id}/tasks  POST /WFPM/projects/{project_id}/tasks
```

Here `{project_id}` is a **Monitra** id, and a project or task created through
them has **no WFPM id recorded** — so it cannot be addressed through
`/WFPM/sync`, and a timer on such a task starts no WFPM timer. New integration
work should use `/WFPM/sync`.

---

## 3. Monitra → WFPM: timer start

When a user starts a timer in Monitra on a task that has a `wfpm_task_id`,
Monitra calls WFPM so the matching timer starts there.

```
User starts a task timer in Monitra
        ↓
Monitra saves the time entry and answers the user
        ↓
Monitra looks up the task's wfpm_task_id      (no id → nothing is sent)
        ↓
Monitra POSTs to WFPM's timer endpoint
        ↓
WFPM starts the timer for that task
```

### 3.1 What WFPM has to provide

One HTTP endpoint that accepts the request below. Monitra is configured with
its full URL and, if WFPM requires one, a token (§4).

### 3.2 The request Monitra sends

```
POST <WFPM_TIMER_START_URL>
Content-Type: application/json
Authorization: Bearer <WFPM_API_TOKEN>        (omitted if no token is configured)
Idempotency-Key: monitra:timer_start:3559
```

```json
{
  "event": "timer_start",
  "event_id": "monitra:timer_start:3559",
  "wfpm_task_id": "900",
  "wfpm_project_id": "55",
  "started_at": "2026-09-30T10:33:55.348000+00:00",
  "stopped_at": null,
  "user_email": "person@example.com",
  "user_name": "Person Name",
  "monitra_user_id": 101,
  "monitra_time_entry_id": 3559,
  "monitra_task_id": 848,
  "monitra_project_id": 2467
}
```

| Field | Meaning |
|---|---|
| `event` | Always `"timer_start"` |
| `event_id` | Identifies this timer start. **The same value is sent on every retry** (and in the `Idempotency-Key` header) |
| `wfpm_task_id` | The task to start the timer against. Always a string |
| `wfpm_project_id` | The task's project in WFPM, or `null` if the Monitra project is not linked |
| `started_at` | When the user pressed Start, UTC, ISO 8601. Use this as the timer's start, not the time the request arrives — a retry can arrive minutes later |
| `stopped_at` | `null` in the normal case. Set only when a **delayed retry** is delivered after the Monitra timer has already been stopped — see §3.4 |
| `user_email` | The Monitra user who started the timer. The email is the identity both systems share |
| `monitra_*` | Monitra's own ids, for reference and support |

### 3.3 What WFPM should answer

| WFPM answers | Monitra does |
|---|---|
| Any `2xx` | Marks the event delivered. Done |
| `400`, `404`, `409`, `422`, other `4xx` | Treats the event as **refused**: parks it as `rejected` and does not retry. Use these when the request can never succeed (unknown task id, malformed body) |
| `401`, `403` | Retries — treated as a token problem that someone will fix |
| `408`, `429`, any `5xx`, a redirect, a timeout, no connection | Retries |

Monitra waits at most `WFPM_REQUEST_TIMEOUT_SECONDS` (10 s) for an answer.

**WFPM must treat a repeated `event_id` as already handled** and answer `2xx`
without starting a second timer. Delivery is at-least-once: if WFPM starts the
timer but its reply is lost, Monitra will send the same event again.

### 3.4 Retries

A failed attempt is retried by a scheduled sweep (once a minute), with
exponential backoff and jitter: the waits are random, between 15 s and a
ceiling that doubles each time — 30 s, 1 min, 2 min, 4 min, 8 min — for up to
6 attempts in total (`WFPM_TIMER_MAX_ATTEMPTS`). After the last attempt the
event is parked as `failed` and is not sent again.

Because a retry can be late, the user may already have stopped the Monitra
timer by the time WFPM receives the start. In that case `stopped_at` carries
the stop time, so WFPM can record a finished session (`started_at` →
`stopped_at`) instead of starting a timer that nothing will stop.

### 3.5 What is *not* sent

- **Timer stop.** Only the start is announced. Apart from the late-retry case
  above, Monitra does not tell WFPM when a timer stops. If WFPM's timer should
  stop when Monitra's does, that is a second event that has to be agreed and
  added (`wfpm_timer_events.event_type` was designed to carry it).
- **Timers on tasks with no `wfpm_task_id`** — Monitra's default project
  tasks, and anything created in Monitra itself.
- **Manual time entries** and idle-time adjustments. They are not timers.

Starting a timer in Monitra never waits for WFPM and never fails because of
it. The user's timer is saved and answered first; WFPM is told afterwards.

---

## 4. Configuration (Monitra backend)

Set in the backend's environment (`/etc/monitra/backend.env` in production,
`backend/.env` locally). See `backend/.env.example`.

| Variable | Default | Purpose |
|---|---|---|
| `WFPM_TIMER_START_URL` | empty | Full URL of WFPM's timer endpoint. **Empty = the timer integration is off**: nothing is queued or sent |
| `WFPM_API_TOKEN` | empty | Sent as `Authorization: Bearer`. Issued by WFPM |
| `WFPM_REQUEST_TIMEOUT_SECONDS` | `10` | Per-attempt timeout |
| `WFPM_TIMER_MAX_ATTEMPTS` | `6` | Attempts before an event is parked as `failed` |
| `WFPM_TIMER_RETRY_BASE_DELAY_SECONDS` | `30` | First retry delay; doubles each attempt |
| `WFPM_TIMER_RETRY_MAX_DELAY_SECONDS` | `900` | Cap on the retry delay |
| `WFPM_TIMER_DISPATCH_BATCH_SIZE` | `50` | Events attempted per sweep |

The `/WFPM/sync` routes need no configuration; they work as soon as the
migration is applied.

**Scheduler.** Retries are driven by `POST /internal/wfpm/timer-events/dispatch`,
called every minute by the `wfpm-timer-dispatch` systemd timer
(`deploy/backend/scheduled-jobs/`) and mirrored in `vercel.json`. It is
authenticated with `EMAIL_DISPATCH_TOKEN`, like every other scheduled job.
Without that timer, a timer start whose first attempt fails is never retried.

**Deploying.**

1. Deploy the backend. `deploy/backend/deploy.sh` runs `alembic upgrade head`,
   which applies migration `c4e6a8b0d2f4` (the two columns and the
   `wfpm_timer_events` table). The new code cannot run without it.
2. Re-run `sudo bash scheduled-jobs/install.sh` on the VM to install the new
   timer.
3. When WFPM's endpoint exists, set `WFPM_TIMER_START_URL` (and
   `WFPM_API_TOKEN`) and restart the backend. `/health` then reports
   `"wfpm_timer_sync": {"configured": true, "token_present": true}`.

---

## 5. Finding out what happened

**Logs.** `grep WFPM_` over the backend log is the whole story.

| Line | Meaning |
|---|---|
| `WFPM_SYNC_PROJECT_CREATED` / `_REPLAYED` / `_UPDATED` | A project create, a repeated create, an update |
| `WFPM_SYNC_MEMBERS_ADDED` / `WFPM_SYNC_MEMBER_REMOVED` | Membership changes |
| `WFPM_SYNC_TASK_CREATED` / `_REPLAYED` / `_UPDATED` / `_ASSIGNED` / `_UNASSIGNED` | Task operations |
| `WFPM_SYNC_NOT_FOUND … reason=not_linked` | WFPM used an id Monitra has no record of |
| `WFPM_SYNC_NOT_FOUND … reason=archived_or_out_of_scope` | The id is linked, but archived or not visible to that user |
| `WFPM_TIMER_QUEUED` | A timer start was queued for WFPM |
| `WFPM_TIMER_SENT` | WFPM accepted it |
| `WFPM_TIMER_RETRY` | An attempt failed and will be retried; the line says why |
| `WFPM_TIMER_REJECTED` | WFPM refused it (`4xx`); not retried |
| `WFPM_TIMER_FAILED` | Out of attempts |
| `WFPM_TIMER_QUEUE_FAILED` | The event could not be queued. The Monitra timer still started |

**The queue.** One row per timer start in `wfpm_timer_events`:

```sql
-- everything WFPM was not told, and why
SELECT id, time_entry_id, wfpm_task_id, status, attempt_count,
       response_status, last_error, created_at
FROM wfpm_timer_events
WHERE status IN ('pending', 'failed', 'rejected')
ORDER BY created_at DESC;
```

| `status` | Meaning |
|---|---|
| `pending` | Waiting for its next attempt (`next_attempt_at`) |
| `sent` | WFPM answered `2xx` |
| `rejected` | WFPM refused it; see `response_status` and `last_error` |
| `failed` | Ran out of attempts |

To send a `failed` or `rejected` event again after the cause is fixed:

```sql
UPDATE wfpm_timer_events
SET status = 'pending', attempt_count = 0, next_attempt_at = now()
WHERE id = <id>;
```

**The mapping.**

```sql
SELECT id, project_name, wfpm_project_id FROM projects WHERE wfpm_project_id = '55';
SELECT id, task_name, project_id, wfpm_task_id FROM tasks WHERE wfpm_task_id = '900';
```

---

## 6. Status of this integration

| Part | Status |
|---|---|
| Schema, `/WFPM/sync` routes, timer queue | Implemented; automated tests; verified over real HTTP against the development database |
| Timer delivery to WFPM | Implemented and verified against a **local stand-in** for WFPM's endpoint. **It has never called the real WFPM**, whose endpoint did not exist when this was written. §3 is therefore a proposal until the WFPM team confirms it or asks for changes |

To repeat the verification (development database only; it starts its own
backend, uses disposable `@e2e.invalid` users and removes everything it
creates):

```bash
cd backend && python scripts/smoke_wfpm_integration.py
```

Questions for the WFPM team that would change this document:

1. Are WFPM ids numbers or strings, and does the timer endpoint accept
   `wfpm_task_id` as a JSON string (§1, §3.2)?
2. How should Monitra authenticate to WFPM — is a bearer token right (§3.2)?
3. Should WFPM be told when a timer **stops** (§3.5)?
4. Does WFPM identify users by email (§3.2)?

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
| 11 | Set the whole assignee list | `PUT /WFPM/sync/tasks/{wfpm_task_id}/assignees` | `tasks:update` |
| 12 | Read the assignees | `GET /WFPM/sync/tasks/{wfpm_task_id}/assignees` | `tasks:view` |
| 13 | Add assignees | `POST /WFPM/sync/tasks/{wfpm_task_id}/assignees` | `tasks:update` |
| 14 | Remove one assignee | `DELETE /WFPM/sync/tasks/{wfpm_task_id}/assignees/{member_id}` | `tasks:update` |

Which role holds which permission (from `backend/app/core/permissions.py`):

| Role | 1 create project | 2, 7, 12 read | 3 update project | 4, 5 members | 6 create task | 8–11, 13, 14 update / assign task |
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

On routes 11 and 6, the optional `add_missing_members` flag additionally needs
`project_members:manage` and that same member rule (so: an administrator, or the
project's own leader).

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
| `billing_type` | yes | `"free"` (flexible time) or `"non_billing"` (not billed). Neither has an hours budget. (`"fixed"` needs one, which this API does not accept, so it is refused) |

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

or, for several assignees:

```json
{ "wfpm_task_id": 900, "name": "Design the homepage", "assignee_ids": [101, 102] }
```

| Field | Required | Notes |
|---|---|---|
| `wfpm_task_id` | yes | See §1 |
| `name` | yes | 1–150 characters, not blank |
| `assignee_id` | no | Must be an active **employee** who is a **member of this project** |
| `assignee_ids` | no | Several assignees, in order — the first is the primary — with the same rules and the same cap (50) as route 11. **If both this and `assignee_id` are sent, `assignee_ids` wins** (`[]` included: it means nobody) |
| `add_missing_members` | no | As on route 11. Applies to whichever of the two assignee fields is sent. Not applied when the create is a replay |
| `estimated_hours` | no | 0 – 999.99 |

The task starts in Monitra's **Todo** status. If no assignee is sent and the
caller is an employee, the task is assigned to the caller. Naming several
assignees is held to Monitra's own rule for it: it needs
`task_assignees:manage` (administrators, managers and leaders), so an employee
who sends `assignee_ids` is refused with `403` exactly as in Monitra — an
employee can still send the single `assignee_id`.

`201` created · `200` the WFPM task id was already linked (existing task,
unchanged) · `409` the id is linked under a different project, or to a task
that is archived or not accessible · `404` the project id is not linked ·
`400` an assignee is not valid for this project (the `detail` names the ids;
no task is created) · `403` `assignee_ids` by someone without
`task_assignees:manage`, or `add_missing_members` without the right to add
members · `422` a malformed or over-long list. Idempotent on `wfpm_task_id`,
exactly like project create: a replay (`200`) returns the task as it stands,
`assignees` included, and applies nothing from the repeated request.

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

Replaces the current assignee. This route deals in **one** assignee: it
means "the assignee set becomes just this one person", so mixing it with the
list routes below is predictable — whoever else held the task is removed, and
the person named becomes the primary. The user must be an active employee and a
member of the task's project, otherwise `400`. `200` with the task. (For
several people use route 11.)

#### 10. Remove assignee — `DELETE /WFPM/sync/tasks/{wfpm_task_id}/assignee`

`200` with the task, now unassigned. In Monitra an unassigned task is shared
project work: every member of the project can see it and track time on it.
Repeating the call changes nothing and answers the same way.

#### 11. Set the assignee list — `PUT /WFPM/sync/tasks/{wfpm_task_id}/assignees`

The main route for a task held by several people. Send the task's **complete**
assignee list; Monitra makes its own list exactly that.

```json
{ "assignee_ids": [101, 102, 103] }
```

| Field | Required | Notes |
|---|---|---|
| `assignee_ids` | yes | Monitra user ids, **in order**. The first is the **primary** assignee. Duplicates are ignored (the first occurrence counts). At most **50** ids. `[]` removes everyone |
| `add_missing_members` | no | `true` also adds any listed user who is not yet a member of the project (see below). Default `false` |

- **Replace, not merge, and idempotent.** The task's entire assignee set becomes
  this list, so sending the same list again changes nothing — no row is
  rewritten and nothing is added to the activity trail. The stored order is the
  order you sent, and is the order `GET` and every task body return.
- **`[]`** leaves the task unassigned, which in Monitra is shared project work:
  every member of the project can see it and track time on it. This is the same
  as route 10.
- **The primary.** The first id becomes the task's primary assignee —
  `assignee_id` and `assignee` in the task body — whoever held that role before.
  Reordering a list therefore changes the primary.
- **Who may be added.** Each id **newly added** must be an active **employee**
  who is a **member of the task's project** — the same rule as route 9. Someone
  who already holds the task is kept without being re-checked, so repeating a
  list never fails because of a person the call did not change (for instance
  someone removed from the project since). Removing anyone is always allowed.
- **All or nothing.** If any id is not valid, the answer is `400`, the `detail`
  names the offending ids (`"Task assignees must be assigned to this project:
  [103]."`), and **nothing is changed** — not even the valid ids.
- **Time.** Every assignee, not only the primary, can start a timer on the task.
  Removing someone from the list **never deletes the time they already
  tracked**: their time entries stay exactly as they were.

| Status | Meaning |
|---|---|
| `200` | The task (§2.3), with the new `assignees` |
| `400` | An id is unknown, in another organization, not an employee, or not a member of the project. Nothing changed |
| `403` | `add_missing_members` was sent without `project_members:manage`, or by someone who may not change this project's members (see below) |
| `404` | The task id is not linked, or the caller cannot open that task |
| `422` | `assignee_ids` is missing, not a list of positive whole numbers, or longer than 50 |

**`add_missing_members`** is a convenience for the call WFPM otherwise makes
first (route 4). It needs the same permission as route 4 (`project_members:manage`)
**and** the same rule — only an administrator or the project's own leader may
change a project's members — otherwise `403`. Every listed id must be an active
employee (else `400`, checked before anything is written, so a bad id never
leaves a stray membership behind); those not yet on the project are then added,
and the assignment follows. Without the flag, a listed non-member is a `400`.

#### 12. Read the assignees — `GET /WFPM/sync/tasks/{wfpm_task_id}/assignees`

For checking and support. `200` with the ordered list, primary first:

```json
[
  { "id": 101, "name": "…", "email": "…", "role": "employee" },
  { "id": 102, "name": "…", "email": "…", "role": "employee" }
]
```

`[]` for an unassigned task. `404` as above.

#### 13. Add assignees — `POST /WFPM/sync/tasks/{wfpm_task_id}/assignees`

```json
{ "assignee_ids": [104] }
```

Adds to whoever already holds the task; the existing assignees keep their
places (so the primary does not change), and the new ones follow in the order
sent. Someone already assigned is **not an error**. If the task had no
assignee, the first id sent becomes the primary. The same validation and
all-or-nothing rule as route 11 (`400` naming the offending ids; `422` for an
empty or malformed list). `200` with the task.

#### 14. Remove one assignee — `DELETE /WFPM/sync/tasks/{wfpm_task_id}/assignees/{member_id}`

`204` with no body. `404` if that user is not assigned to the task (so a
repeated removal answers `404`, which is safe to treat as "already removed"). If
the primary was removed, the next assignee in order becomes the primary; if it
was the last one, the task is unassigned. The person's time entries are kept.

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

Task (routes 6–11 and 13):

```json
{
  "id": 848,
  "wfpm_task_id": "900",
  "project_id": 2467,
  "name": "Design the homepage",
  "assignee_id": 101,
  "assignee": { "id": 101, "name": "…", "email": "…", "role": "employee" },
  "assignees": [ { "id": 101, "name": "…", "email": "…", "role": "employee" } ],
  "status": { "id": 1, "name": "Todo", "color": "#CBD5E1" },
  "estimated_hours": 12.5,
  "created_at": "2026-09-30T10:00:00Z",
  "updated_at": "2026-09-30T10:00:00Z"
}
```

`id` and `project_id` are Monitra's own ids, returned for reference.
`assignees` lists everyone holding the task, **in order, primary first**; it is
additive, so a consumer that only reads `assignee` is unaffected.
`assignee_id` and `assignee` are always the **first** (primary) assignee, and
`null` when the task has none — exactly as when a task could have only one.
Every task body carries `assignees`, including the ones inside a project (routes
1–3) and a replayed create.

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
POST <WFPM_TIMER_START_URL>            (including any ?key=… it carries)
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

### 3.5 Timer stop

When a timer ends in Monitra on a task that has a `wfpm_task_id`, Monitra calls
WFPM so the matching timer stops there. It is a second, independent event
alongside the start: its own queue row, its own URL, its own `event_id`.

Every way a Monitra timer can end sends it, because they all finalize the entry
through the same code: the user pressing Stop, the idle popup's Stop (and the
desktop's automatic stop, which uses the same call), and a member being
deactivated while their timer runs. Starting a timer while another is running
does not stop the other — Monitra refuses with `409` and the client stops the
first one itself, which is an ordinary stop. `stopped_at` is the entry's stored
`end_time`, so it is the instant Monitra's own duration was computed from.

```
POST <WFPM_TIMER_STOP_URL>            (including the ?key=… it carries — never changed)
Content-Type: application/json
Idempotency-Key: monitra:timer_stop:3559
```

No `Authorization` header is sent while `WFPM_API_TOKEN` is empty, which is how
WFPM's stop endpoint is configured: it authorises by the `?key=` in the URL.

```json
{
  "event": "timer_stop",
  "event_id": "monitra:timer_stop:3559",
  "wfpm_task_id": "1548",
  "wfpm_project_id": "390",
  "started_at": "2026-10-01T10:33:55+00:00",
  "stopped_at": "2026-10-01T10:45:10+00:00",
  "monitra_user_id": 238,
  "monitra_time_entry_id": 3559,
  "monitra_task_id": 5230,
  "monitra_project_id": 3383
}
```

| Field | Meaning |
|---|---|
| `event` | Always `"timer_stop"` |
| `event_id` | Identifies this stop: `monitra:timer_stop:<time_entry_id>`. **The same value on every retry**, and in the `Idempotency-Key` header |
| `wfpm_task_id`, `wfpm_project_id` | As in §3.2. Frozen when the stop was queued |
| `started_at` | The entry's persisted start, UTC, ISO 8601 |
| `stopped_at` | **Required.** The entry's persisted end, UTC, ISO 8601. WFPM uses it as the end time, so its hours match Monitra's: `round(stopped_at − started_at)` is the entry's `total_seconds` |
| `monitra_user_id` | How WFPM finds the user (the `monitra_id` on their WFPM account). **No email or name is sent** |
| `monitra_*` | Monitra's own ids, for reference and support |

Exactly one stop event is queued per time entry (the table's unique key is
`(event_type, time_entry_id)`), and only when all of these hold:

- `WFPM_TIMER_STOP_URL` is set. Empty → nothing is queued, exactly as with the start URL;
- the entry's task has a `wfpm_task_id`;
- the entry has an end time.

**How WFPM answers** is §3.3, unchanged: any `2xx` marks the event sent — this
includes a repeated `event_id` (`{"status":"duplicate"}`) and a stop with no
running timer; `401`, `403`, `408`, `425`, `429`, `5xx`, a timeout or no
connection are retried with the backoff of §3.4; any other `4xx` (an unknown
task or user, a missing field) parks the event as `rejected` and is not retried.
The reason WFPM gives is in `wfpm_timer_events.last_error`.

**Ordering.** Start and stop are delivered independently and either can be late.
A start delivered after the timer ended already carries `stopped_at` (§3.4), so
WFPM records a finished session; a stop that arrives before its start is
harmless on WFPM's side — it answers `200` and does nothing. Monitra does not
hold either event back for the other.

**Timing.** Delivery of a stop runs right after the stop request is answered,
exactly as a start does, so the user never waits for WFPM. The one exception is
a stop that is not a user request — a deactivated member's timer — which has no
response to follow and is delivered by the next minute's sweep.

### 3.6 What is *not* sent

- **Timers on tasks with no `wfpm_task_id`** — Monitra's default project
  tasks, and anything created in Monitra itself.
- **Manual time entries** and idle-time adjustments. They are not timers.
- **Idle time reassigned to another task.** That creates an already-finished
  entry on the destination task; WFPM never saw it start, so it is not
  announced.

Starting or stopping a timer in Monitra never waits for WFPM and never fails
because of it. The user's timer is saved and answered first; WFPM is told
afterwards.

---

## 4. Configuration (Monitra backend)

Set in the backend's environment (`/etc/monitra/backend.env` in production,
`backend/.env` locally). See `backend/.env.example`.

| Variable | Default | Purpose |
|---|---|---|
| `WFPM_TIMER_START_URL` | empty | Full URL of WFPM's timer endpoint, **including a `?key=…` if WFPM authorises callers that way**. **Empty = the timer integration is off**: nothing is queued or sent. Treat it as a secret when it carries a key |
| `WFPM_TIMER_STOP_URL` | empty | Full URL of WFPM's timer-**stop** endpoint, **including its own `?key=…`** (the start and stop keys are separate). **Empty = stops are not announced**: nothing is queued or sent for a stop, independently of the start URL. A secret, like the start URL |
| `WFPM_API_TOKEN` | empty | Sent as `Authorization: Bearer`. Leave it **empty** (nothing after the `=`) when WFPM authorises by the key in the URL: no `Authorization` header is sent then. Do not type placeholder text such as `(leave empty)` — it would be sent as the token |
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
3. When WFPM's endpoints exist, set `WFPM_TIMER_START_URL` and
   `WFPM_TIMER_STOP_URL` (and `WFPM_API_TOKEN` if WFPM wants one — for the
   `?key=` endpoints leave it empty) and restart the backend.
   A key carried in the URL's query string is never written to the log or to
   `wfpm_timer_events.last_error`: those show the URL as `…/timer?<redacted>`. `/health` then reports
   `"wfpm_timer_sync": {"configured": true, "stop_configured": true, "token_present": true}`
   (`configured` is the start URL; `stop_configured` the stop URL).

---

## 5. Finding out what happened

**Logs.** `grep WFPM_` over the backend log is the whole story.

| Line | Meaning |
|---|---|
| `WFPM_SYNC_PROJECT_CREATED` / `_REPLAYED` / `_UPDATED` | A project create, a repeated create, an update |
| `WFPM_SYNC_MEMBERS_ADDED` / `WFPM_SYNC_MEMBER_REMOVED` | Membership changes |
| `WFPM_SYNC_TASK_CREATED` / `_REPLAYED` / `_UPDATED` / `_ASSIGNED` / `_UNASSIGNED` | Task operations |
| `WFPM_SYNC_TASK_ASSIGNEES_SET: wfpm_task=… assignees=[…]` | Route 11 applied; `assignees` is the resulting ordered list (logged on a repeat too, with the same list) |
| `WFPM_SYNC_TASK_ASSIGNEES_ADDED` / `WFPM_SYNC_TASK_ASSIGNEE_REMOVED` | Routes 13 and 14 |
| `WFPM_SYNC_MEMBERS_ADDED … via=add_missing_members` | Members added by the `add_missing_members` flag |
| `WFPM_SYNC_NOT_FOUND … reason=not_linked` | WFPM used an id Monitra has no record of |
| `WFPM_SYNC_NOT_FOUND … reason=archived_or_out_of_scope` | The id is linked, but archived or not visible to that user |
| `WFPM_TIMER_QUEUED` | A timer start or stop was queued for WFPM (`event=timer_start` / `timer_stop`) |
| `WFPM_TIMER_SENT` | WFPM accepted it |
| `WFPM_TIMER_RETRY` | An attempt failed and will be retried; the line says why |
| `WFPM_TIMER_REJECTED` | WFPM refused it (`4xx`); not retried |
| `WFPM_TIMER_FAILED` | Out of attempts |
| `WFPM_TIMER_QUEUE_FAILED` | The event could not be queued. The Monitra timer still started or stopped |

**The queue.** One row per timer start and one per timer stop (`event_type`) in `wfpm_timer_events`:

```sql
-- everything WFPM was not told, and why
SELECT id, event_type, time_entry_id, wfpm_task_id, status, attempt_count,
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
| Several assignees per task (routes 11–14, `assignee_ids` on route 6) | Implemented; automated tests; verified over real HTTP against the development database. Not yet exercised by WFPM itself |
| Timer start delivery to WFPM | Implemented; WFPM has confirmed it works end to end |
| Timer stop delivery to WFPM (§3.5) | Implemented to WFPM's specification; automated tests, and verified over real HTTP against a **local stand-in** for WFPM's stop endpoint. **Not yet exercised against the real WFPM** — that needs the deployed backend with `WFPM_TIMER_STOP_URL` set, then a start-and-stop from Monitra checked in WFPM for a matching duration |

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
3. ~~Should WFPM be told when a timer **stops**?~~ Yes — answered by §3.5.
4. Does WFPM identify users by email (§3.2)?

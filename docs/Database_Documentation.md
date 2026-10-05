# Database Documentation

## Database

PostgreSQL (Neon) — `public` schema.

## Purpose

This document is the authoritative reference for the current live schema. It is generated
directly from the Neon database DDL (captured 2026-08-27) rather than from the SQLAlchemy models,
so it also lists tables the ORM does not yet model (`hours_tracking`, `import_jobs`, `members`,
`task_mappings`, `project_statuses`, `task_statuses`, `refresh_tokens`, `api_error_logs`).

**Before adding any table, column, or endpoint that touches the database:** check this file
first. If the data already has a home here, extend it — do not create a parallel table or a
second column that means the same thing. Any schema change still requires an Alembic migration
in `backend/alembic/` per [CLAUDE.md](../CLAUDE.md) §4 — this file describes what exists, it does
not replace the migration.

---

## Table of contents

Core: [organizations](#organizations) · [users](#users) · [projects](#projects) ·
[project_statuses](#project_statuses) · [project_members](#project_members) ·
[tasks](#tasks) · [task_statuses](#task_statuses) · [task_assignees](#task_assignees)

Time tracking: [time_entries](#time_entries) · [manual_time_entries](#manual_time_entries) ·
[time_entry_activity](#time_entry_activity) · [time_entry_app_usage](#time_entry_app_usage) ·
[time_entry_url_usage](#time_entry_url_usage) · [time_entry_screenshots](#time_entry_screenshots)

Auth: [refresh_tokens](#refresh_tokens)

Legacy CSV import pipeline: [members](#members) · [import_jobs](#import_jobs) ·
[hours_tracking](#hours_tracking) · [task_mappings](#task_mappings)

Logging: [activity_logs](#activity_logs) · [api_error_logs](#api_error_logs)

`alembic_version` (bookkeeping table for the migration tool — not application data) is omitted
below.

---

## organizations

Tenant root. Every multi-tenant table below carries an `organization_id` FK to this table with
`ON DELETE CASCADE`.

| Column | Type | Notes |
|---|---|---|
| id | bigint identity | PK |
| name | varchar(150) | NOT NULL |
| slug | varchar(150) | NOT NULL, UNIQUE |
| logo_url | text | |
| timezone | varchar(100) | default `'UTC'` |
| currency | char(3) | default `'USD'` |
| status | varchar(20) | default `'active'`; CHECK IN (`active`, `trial`, `inactive`, `suspended`) |
| created_at / updated_at | timestamptz | default `now()` |

Indexes: `name`, `status`.

---

## users

| Column | Type | Notes |
|---|---|---|
| id | bigint identity | PK |
| organization_id | bigint | NOT NULL, FK → organizations, CASCADE |
| hubstaff_user_id | varchar | UNIQUE, nullable |
| username | varchar | NOT NULL |
| email | varchar | NOT NULL, UNIQUE |
| name | varchar | NOT NULL |
| designation | varchar | |
| role_name | varchar | NOT NULL |
| permissions | jsonb | default `{}`. **Server-derived from the role→permission mapping — never client-writable.** |
| idle_enabled | boolean | default true |
| idle_minutes | integer | default 5 |
| capture_frequency | integer | NOT NULL |
| status | varchar | default `'active'` |
| password_hash | varchar | nullable (external/SSO users may have none) |
| wp_capabilities | jsonb | WordPress/Hubstaff capability mirror, external-auth related |
| is_active | boolean | default true |
| date_of_joining | date | |
| date_of_birth | date | |
| created_at / updated_at | timestamptz | default `now()` |

Indexes: `(organization_id, role_name)`, `(organization_id, status)`.

---

## projects

| Column | Type | Notes |
|---|---|---|
| id | bigint identity | PK |
| organization_id | bigint | NOT NULL, FK → organizations, CASCADE |
| project_name | varchar(150) | NOT NULL; UNIQUE with `organization_id` (`uq_projects_org_name`) |
| description | text | |
| status | varchar(20) | default `'planning'`; CHECK IN (`planning`, `active`, `pending`, `todo`, `completed`, `cancelled`, `archived`) |
| status_id | bigint | FK → project_statuses (newer, color-tagged status alongside the legacy `status` string) |
| start_date | date | |
| deadline | date | |
| completed_at | timestamptz | |
| is_billable | boolean | default true |
| billing_type | varchar(20) | default `'free'` |
| category | varchar(20) | Optional tag: `'kyle'` or `'st'` (nullable; NULL = uncategorised). Allowed values are enforced by `ProjectCategory` in `app/schemas/project_management.py`, not by a DB constraint |
| fixed_hours | numeric(8,2) | |
| time_tracked_seconds | integer | default 0 — denormalized rollup, kept in sync by the time-entry write path, not computed on read |
| leader_id | bigint | FK → users (nullable) |
| wfpm_project_id | varchar(255) | The id this project has in WFPM (nullable). UNIQUE with `organization_id` where set (`uq_projects_org_wfpm_project_id`). See `docs/WFPM_INTEGRATION.md` |
| created_by | bigint | NOT NULL, FK → users (no ON DELETE rule declared) |
| created_at / updated_at | timestamptz | default `now()` |

Indexes: `is_billable`, `created_by`, `deadline`, `project_name`, `(organization_id, leader_id)`,
`(organization_id, status_id)`, `organization_id`, `start_date`, `status`.

Note: both a free-text `status` (with its own CHECK) and a normalized `status_id` →
`project_statuses` exist simultaneously. Treat `status_id`/`project_statuses` as the
source of truth for anything color/UI-driven; `status` looks like a legacy column kept for
backward compatibility. Confirm with the team before writing new code against `status` alone.

---

## project_statuses

Lookup table for project status labels + a display color, referenced by `projects.status_id`.

| Column | Type | Notes |
|---|---|---|
| id | bigserial | PK |
| name | varchar(50) | NOT NULL, UNIQUE |
| color | varchar(7) | e.g. `#RRGGBB` |

---

## project_members

Join table: which users belong to which project.

| Column | Type | Notes |
|---|---|---|
| id | bigint identity | PK |
| organization_id | bigint | NOT NULL, FK → organizations, CASCADE |
| project_id | bigint | NOT NULL, FK → projects, CASCADE |
| user_id | bigint | NOT NULL (FK not declared in DDL, but is a users.id in practice) |
| joined_at | date | default `CURRENT_DATE` |
| created_by | bigint | NOT NULL |
| created_at | timestamptz | default `now()` |

Unique: `(project_id, user_id)` — a user can only be added to a project once.

---

## tasks

| Column | Type | Notes |
|---|---|---|
| id | bigint identity | PK |
| organization_id | bigint | NOT NULL, FK → organizations, CASCADE |
| project_id | bigint | NOT NULL, FK → projects, CASCADE |
| task_name | varchar(150) | NOT NULL |
| description | text | |
| status | varchar(20) | default `'todo'`; CHECK IN (`todo`, `in_progress`, `completed`, `archived`) |
| status_id | bigint | FK → task_statuses (parallel normalized status, same pattern as projects) |
| assignee_id | bigint | FK → users (nullable; single-assignee convenience column alongside the many-to-many `task_assignees`) |
| start_date / due_date | date | CHECK: `due_date >= start_date` when both set |
| estimated_hours | numeric(5,2) | CHECK: `>= 0` |
| time_tracked_seconds | integer | default 0 — denormalized rollup |
| completed_at | timestamptz | |
| completed_by | bigint | |
| is_duplicate | boolean | default false |
| wfpm_task_id | varchar(255) | The id this task has in WFPM (nullable). UNIQUE with `organization_id` where set (`uq_tasks_org_wfpm_task_id`). A timer on a task that has one is announced to WFPM. See `docs/WFPM_INTEGRATION.md` |
| created_by | bigint | NOT NULL |
| created_at / updated_at | timestamptz | default `now()` |

Indexes: `created_by`, `due_date`, `task_name`, `organization_id`, `project_id`,
`(project_id, status_id)`, `start_date`, `status`.

Same status caveat as `projects`: both a legacy `status` string and normalized
`status_id`/`task_statuses` exist. Also note `assignee_id` (single) vs. `task_assignees` (many) —
check which the feature you're building actually needs before writing to either.

---

## task_statuses

| Column | Type | Notes |
|---|---|---|
| id | bigserial | PK |
| name | varchar(50) | NOT NULL, UNIQUE |
| color | varchar(7) | |

---

## task_assignees

Join table: many users assigned to one task.

| Column | Type | Notes |
|---|---|---|
| id | bigint identity | PK |
| task_id | bigint | NOT NULL, FK → tasks, CASCADE |
| user_id | bigint | NOT NULL |
| assigned_by | bigint | NOT NULL |
| assigned_at | timestamptz | default `now()` |

Unique: `(task_id, user_id)`.

---

## time_entries

The automatic (timer-driven) work-session table — the source of truth the desktop client writes
to for tracked time. See [CLAUDE.md](../CLAUDE.md) §3 — `timer_service.py` is the sole owner of
tracked time on the desktop side; this table is what it syncs to.

| Column | Type | Notes |
|---|---|---|
| id | bigint identity | PK |
| organization_id | bigint | NOT NULL, FK → organizations, CASCADE |
| project_id | bigint | NOT NULL, FK → projects, CASCADE |
| task_id | bigint | NOT NULL (FK not declared in DDL, but is a tasks.id in practice) |
| user_id | bigint | NOT NULL |
| start_time | timestamptz | NOT NULL |
| end_time | timestamptz | nullable — null while the timer is running |
| total_seconds | integer | default 0 |
| status | varchar(20) | default `'running'`; CHECK IN (`running`, `stopped`) |
| is_manual | boolean | default false |
| is_billable | boolean | default true |
| description | text | |
| created_at / updated_at | timestamptz | default `now()` |

Constraint: `uq_active_time_entry` — a **unique index on `user_id` alone**. This means a user can
have at most one `time_entries` row total that matches whatever partial/unique semantics were
intended for "one active timer per user" — verify the exact index definition (it may be a plain
unique index, not a partial one filtered to `status = 'running'`) before assuming a user can have
more than one historical row; check `\d time_entries` against a real connection if a feature needs
to insert a second row per user.

Indexes: `project_id`, `start_time`, `status`, `task_id`, `user_id`.

---

## manual_time_entries

Manually logged (non-timer) work sessions, subject to an approval workflow.

| Column | Type | Notes |
|---|---|---|
| id | bigint identity | PK |
| organization_id | bigint | NOT NULL, FK → organizations, CASCADE |
| project_id | bigint | NOT NULL, FK → projects, CASCADE |
| task_id | bigint | NOT NULL, FK → tasks, CASCADE |
| user_id | bigint | NOT NULL |
| work_date | date | NOT NULL |
| start_time / end_time | timestamptz | NOT NULL; CHECK `end_time >= start_time` |
| total_seconds | integer | default 0; CHECK `>= 0` |
| description | text | |
| is_billable | boolean | default true |
| approval_status | varchar(20) | default `'pending'`; CHECK IN (`pending`, `approved`, `rejected`) |
| approved_by | bigint | nullable |
| approved_at | timestamptz | nullable |
| created_at / updated_at | timestamptz | default `now()` |

Indexes: `work_date`, `project_id`, `approval_status`, `task_id`, `user_id`.

---

## time_entry_activity

Per-`time_entries` productivity snapshots (keyboard/mouse activity samples).

| Column | Type | Notes |
|---|---|---|
| id | bigint identity | PK |
| organization_id | bigint | NOT NULL, FK → organizations, CASCADE |
| time_entry_id | bigint | NOT NULL, FK → time_entries, CASCADE |
| recorded_at | timestamptz | default `now()` |
| keyboard_strokes | integer | default 0 |
| mouse_clicks | integer | default 0 |
| mouse_movements | integer | default 0 |
| activity_percentage | smallint | default 0; CHECK 0–100 |
| created_at / updated_at | timestamptz | default `now()` |

Per [CLAUDE.md](../CLAUDE.md) §5.1: the desktop captures a real activity percentage but the
backend currently has no batch endpoint/service wired to write it here at scale
(`TimeEntryService.batch_sync_activity` doesn't exist yet, `SyncService._sync_activity` is a
guarded no-op). Don't assume this table is being populated end-to-end today — verify before
building a feature on top of it.

---

## time_entry_app_usage

| Column | Type | Notes |
|---|---|---|
| id | bigint identity | PK |
| organization_id | bigint | NOT NULL, FK → organizations, CASCADE |
| time_entry_id | bigint | NOT NULL, FK → time_entries, CASCADE |
| application_name | varchar(255) | NOT NULL |
| window_title | text | |
| duration_seconds | integer | default 0; CHECK `>= 0` |
| recorded_at | timestamptz | default `now()` |
| created_at / updated_at | timestamptz | default `now()` |

---

## time_entry_url_usage

| Column | Type | Notes |
|---|---|---|
| id | bigint identity | PK |
| organization_id | bigint | NOT NULL, FK → organizations, CASCADE |
| time_entry_id | bigint | NOT NULL, FK → time_entries, CASCADE |
| browser_name | varchar(100) | NOT NULL |
| domain | varchar(255) | NOT NULL |
| url | text | |
| page_title | text | |
| duration_seconds | integer | default 0; CHECK `>= 0` |
| recorded_at | timestamptz | default `now()` |
| client_event_id | varchar(255) | UNIQUE — client-supplied idempotency key for upload retries |
| created_at | timestamptz | default `now()` |

**Update (2026-08-28): this table is no longer empty.** [CLAUDE.md](../CLAUDE.md) §5.2 still says
URL tracking was never implemented client-side, but a later merge to `main` shipped real desktop
capture (`desktop/background_services/activity/url_usage.py`, a fully wired
`URLUsageService`) — the dev DB now has genuine rows here. Treat CLAUDE.md's §5.2 claim as stale
for this table specifically until that doc is updated; verify row counts before assuming either
way rather than trusting either document blindly.

---

## time_entry_screenshots

| Column | Type | Notes |
|---|---|---|
| id | bigint identity | PK |
| organization_id | bigint | NOT NULL, FK → organizations, CASCADE |
| time_entry_id | bigint | NOT NULL, FK → time_entries, CASCADE |
| captured_at | timestamptz | default `now()` |
| file_path | text | NOT NULL — actual image bytes are stored outside the database |
| monitor_number | smallint | default 1 |
| created_at | timestamptz | default `now()` |

Per [CLAUDE.md](../CLAUDE.md) §5.2: screenshot capture was never implemented client-side —
expect it empty; do not fabricate rows here to make a UI look populated.

---

## refresh_tokens

| Column | Type | Notes |
|---|---|---|
| id | bigint identity | PK |
| user_id | bigint | NOT NULL, FK → users, CASCADE |
| token_hash | varchar | NOT NULL — store only the hash, never the raw token |
| expires_at | timestamptz | NOT NULL |
| revoked_at | timestamptz | nullable |
| created_at | timestamptz | default `now()` |

---

## members, import_jobs, hours_tracking, task_mappings

A separate, older subsystem for **bulk CSV time-report imports**, distinct from the
`time_entries`/`manual_time_entries` live-tracking path. Uses plain `serial` PKs (not the
`bigint identity` style of the rest of the schema) and its own `members` table rather than
`users` — this looks like it predates the current multi-tenant `organizations`/`users` design and
was never merged into it. Treat as a separate legacy pipeline; don't assume `members.id` lines up
with `users.id`, and don't route new employee-facing features through it without checking with
the team first.

**import_jobs** — one row per CSV import batch run.

| Column | Type | Notes |
|---|---|---|
| id | serial | PK |
| import_date | varchar | NOT NULL |
| files_included | json | NOT NULL |
| status | varchar | NOT NULL |
| total_rows / records_created / records_skipped / records_updated | integer | |
| errors | json | |

**members** — flat name list used only by this import pipeline.

| Column | Type | Notes |
|---|---|---|
| id | serial | PK |
| name | varchar | NOT NULL, UNIQUE |

**hours_tracking** — one row per imported CSV time-report line.

| Column | Type | Notes |
|---|---|---|
| id | serial | PK |
| import_job_id | integer | NOT NULL, FK → import_jobs |
| member_id | integer | NOT NULL, FK → members |
| project_id | integer | NOT NULL, FK → projects |
| task_id | integer | NOT NULL, FK → tasks |
| work_date | date | NOT NULL |
| regular_hours / total_hours | numeric(5,2) | NOT NULL |
| activity_percentage / idle_percentage / idle_hours | numeric(5,2) | |
| team | varchar | |
| csv_task_id / csv_task_name | varchar | raw values as they appeared in the source CSV |
| total_earned / regular_earned | numeric(10,2) | |
| currency | varchar(10) | |

Unique: `(work_date, member_id, project_id, task_id, regular_hours, total_hours)` — re-importing
the same CSV row is a no-op rather than a duplicate.

**task_mappings** — maps a project's raw CSV task name to a canonical `tasks` row.

| Column | Type | Notes |
|---|---|---|
| id | serial | PK |
| project_id | integer | NOT NULL, FK → projects |
| csv_task_name | varchar | NOT NULL |
| canonical_task_id | integer | NOT NULL, FK → tasks |

Unique: `(project_id, csv_task_name)`.

---

## activity_logs

Audit trail (login, logout, task/project/employee changes, etc.).

| Column | Type | Notes |
|---|---|---|
| id | bigint identity | PK |
| organization_id | bigint | NOT NULL, FK → organizations, CASCADE |
| project_id | bigint | FK → projects, CASCADE, nullable |
| task_id | bigint | FK → tasks, CASCADE, nullable |
| user_id | bigint | NOT NULL |
| module | varchar(50) | NOT NULL — e.g. `projects`, `tasks`, `auth` |
| action | varchar(50) | NOT NULL — e.g. `create`, `update`, `delete`, `login` |
| entity_id | bigint | id of the affected row, nullable |
| description | text | |
| ip_address | inet | |
| created_at | timestamptz | default `now()` |

Indexes: `created_at`, `module`, `organization_id`, `project_id`, `task_id`, `user_id`.

---

## api_error_logs

Captures failed API requests for debugging.

| Column | Type | Notes |
|---|---|---|
| id | bigint identity | PK |
| organization_id | bigint | nullable |
| user_id | bigint | nullable |
| request_id | uuid | default `gen_random_uuid()` |
| endpoint | varchar(255) | NOT NULL |
| http_method | varchar(10) | NOT NULL |
| request_headers / request_body | jsonb | — be careful never to log secrets/credentials here |
| response_status | smallint | NOT NULL |
| error_code | varchar(100) | |
| error_message | text | NOT NULL |
| stack_trace | text | |
| ip_address | inet | |
| user_agent | text | |
| created_at | timestamptz | default `now()` |

Indexes: `created_at`, `endpoint`, `organization_id`, `response_status`, `request_id`, `user_id`.

---

## Relationship overview

```
organizations
│
├── users
├── projects ── project_statuses (status_id, lookup)
│   ├── project_members ── users
│   └── tasks ── task_statuses (status_id, lookup)
│       ├── task_assignees ── users
│       └── time_entries
│           ├── time_entry_activity
│           ├── time_entry_app_usage
│           ├── time_entry_url_usage
│           └── time_entry_screenshots
├── manual_time_entries (project_id, task_id)
├── activity_logs (project_id?, task_id?)
└── api_error_logs

users
└── refresh_tokens

Legacy CSV import pipeline (not connected to organizations/users):
import_jobs ── hours_tracking ── members
                              └── projects / tasks (FK only, no organization scoping)
task_mappings ── projects, tasks
```

---

## Multi-tenancy rule

Every table under an organization carries `organization_id` with `ON DELETE CASCADE` back to
`organizations`. **Any new query or endpoint must filter by `organization_id`** — do not add a
table or a query path that skips this, or one tenant's data becomes reachable from another's
request. The legacy import-pipeline tables (`members`, `import_jobs`, `hours_tracking`,
`task_mappings`) are the one exception — they predate multi-tenancy and are not organization
scoped; do not build new multi-tenant features on top of them without adding that scoping first.

---

## Development rules for any new database work

1. **Check this document first.** If the data already has a column or table, extend it — do not
   create a second one that means the same thing.
2. Reuse existing FK relationships and the existing `organization_id` scoping pattern.
3. Follow the naming conventions already in place (`snake_case`, `<table>_id` FKs,
   `fk_<table>_<ref>` / `uq_<table>_<cols>` / `chk_<table>_<rule>` constraint naming, `idx_<table>_<cols>` index naming).
4. Every schema change goes through an Alembic migration in `backend/alembic/` — never hand-edit
   the live schema, and never edit a SQLAlchemy model without a matching migration
   ([CLAUDE.md](../CLAUDE.md) §4).
5. Never duplicate data that can be derived or joined.
6. Keep the schema normalized; where a denormalized rollup already exists
   (`projects.time_tracked_seconds`, `tasks.time_tracked_seconds`), keep it in sync at the write
   path rather than adding a second rollup elsewhere.
7. Before adding a table for a new feature, check whether one of the two parallel status patterns
   (legacy `status` varchar + CHECK vs. normalized `status_id` → `*_statuses` lookup) or the two
   time-entry systems (`time_entries` timer-driven vs. `manual_time_entries`) already covers it.

---

## Future database extensions

Possible future modules — add only after confirming the schema above doesn't already support it,
and only when the user asks for that feature:

- Attendance
- Leave management
- Payroll integration
- AI productivity scoring
- OCR screenshot analysis
- AI daily summary
- Microsoft Teams integration
- Jira integration
- Email automation
- Calendar integration

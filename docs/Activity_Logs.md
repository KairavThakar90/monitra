# Activity logs

One row in `activity_logs` for every thing a person did, and a Logs page that
reads them employee by employee.

`activity_logs` is the audit table the base schema has always had (see
[Database_Documentation.md](Database_Documentation.md)); its columns are not
changed. `module` and `action` carry the vocabulary below, `user_id` is who
acted, `project_id` / `task_id` / `entity_id` say what it concerned,
`description` is the sentence shown on the page, and `ip_address` is the
caller's address when the request had a valid one.

## What is recorded

| Module | Action | Recorded when | Hook |
|---|---|---|---|
| `auth` | `login` | A session is issued — web or desktop, staff or client. A token refresh is not a sign-in. | `AuthService._record_sign_in` |
| `auth` | `logout` | A session is revoked by an explicit sign-out. A replayed logout records nothing. | `AuthService.revoke_session` |
| `desktop` | `app_opened` | The desktop launches with a restored session. | desktop `client_event` |
| `desktop` | `app_closed` | The desktop is quit (not an update restart, not an OS shutdown). | desktop `client_event` |
| `timer` | `timer_started` / `timer_stopped` | A start creates an entry; a stop finalizes one. Idempotent replays record nothing. | `TimeEntryService.start_timer` / `stop_timer` |
| `timer` | `entry_transferred` | A recorded entry is moved to another project/task. | `TimeEntryService.transfer_entry` |
| `manual_time` | `manual_time_requested` / `_approved` / `_rejected` / `_withdrawn` | A manual time request and the decision on it. | `ManualTimeEntryService` |
| `project` | `project_created` / `_updated` / `_archived` | Project management. | `ProjectManagementService` |
| `task` | `task_created` / `_updated` / `_archived` | Tasks, from the web or the desktop. A replayed create records nothing. | `ProjectManagementService` |
| `member` | `member_created` / `_updated` / `_deactivated`, `login_excluded` / `login_allowed`, `add_tasks_excluded` / `add_tasks_allowed` | The member directory. A switch is recorded only when it actually moved. | `MemberService` |
| `feedback` | `feedback_status_changed` | An administrator marks feedback Working or Resolved. | `FeedbackService.update_status` |
| `system` | `maintenance_enabled` / `_disabled` | The maintenance notice (predates this document). | `MaintenanceModeService` |

The row belongs to the **actor**. "Grace excluded Alice from signing in" is
Grace's row; Alice is named in the description.

## The rules the writer keeps

Everything is written through `ActivityLogService.capture` (or `record`) in
`backend/app/services/activity_log.py`.

- **Recording never fails the action.** `capture` never raises; a row it could
  not write is logged as `ACTIVITY_LOG_WRITE_FAILED` and the request carries on.
  That covers building the description too, which is why hooks pass a callable.
- **Recorded after the change, in a session of its own.** Each hook runs once
  the change is committed, and the row is committed through a separate
  short-lived session. The caller's session is never added to, committed or
  rolled back on the trail's account.
- **The client is observed, not claimed.** `app/core/request_context.py`
  derives `desktop` / `web` / `api` from the `User-Agent`
  (`Monitra/<version>` is the desktop) and the address from
  `X-Forwarded-For`, validated as an IP because the column is `inet`.
- **Add a hook, not a second table.** To record a new action: add the
  constant to `ActivityLogAction`, call `capture` after the service's commit,
  and add a label in `frontend/src/features/admin/activityLogFormat.ts` (an
  action without one is still shown, in its own words).

## Reading: `GET /api/v1/activity-logs`

Requires `view_employees` — Admin, HR, Leader. A leader is answered with their
own team (`member_scope.visible_member_ids`); an employee gets 403.

Query: `start`, `end` (IST days; default the last seven), `member_id`,
`module`, `search`. The response groups rows by employee, newest first, with
project and task names joined in and the client split out of the description.
At most 5,000 rows are returned; `truncated` says when the window held more.

The page is **Logs** in the sidebar (`/admin/logs`,
`frontend/src/features/admin/AdminActivityLogs.tsx`): one collapsed accordion
per employee, opening onto their actions split by IST day, with search,
category, date and employee filters and a CSV export.

## The desktop's own events: `POST /api/v1/activity-logs/client-events`

`{ "event": "app_opened" | "app_closed", "occurred_at": "<ISO-8601 UTC>" }`,
from any signed-in user, for themselves. Idempotent on (user, event, instant),
because the desktop delivers it through its durable queue and may send it more
than once. See `desktop/ARCHITECTURE.md` §3 for the client side: the report is
queued, never sent from the GUI thread, never ahead of a stop, and a quit waits
for it only briefly.

## Tests

- `backend/tests/test_activity_log_trail.py` — writing, never failing, the
  hooks, scoping, the HTTP routes.
- `frontend/src/features/admin/__tests__/activityLogs.test.tsx` — the page and
  its wording.
- `desktop/tests/test_client_events.py` — queuing, the exit wait, attribution,
  and that the event names and endpoint match the backend's source.

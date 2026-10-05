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
| `project` | `project_status_changed` / `_leader_changed` / `_owner_changed` | A project's status, leader or owner moves. The row says from what to what: *Changed the status of the project "Apollo" from Active to Paused*. | `ProjectManagementService.update` via `ProjectActivity` |
| `project` | `project_member_assigned` / `_member_removed` | People put on, or taken off, a project — by name — from the project edit, at creation, or the add / remove-member routes. | `ProjectManagementService`, `ProjectMemberService` via `ProjectActivity` |
| `task` | `task_created` / `_updated` / `_archived` | Tasks, from the web or the desktop. A replayed create records nothing. | `ProjectManagementService` |
| `task` | `task_status_changed` / `_assigned` / `_unassigned` | A task's status moves; people are given a task, or taken off it (Assign Tasks, the task edit, the task-assignee routes, unassigning). Also written when a new task is created already held by someone else. | `ProjectManagementService`, `TaskAssigneeService` via `ProjectActivity` |
| `client` | `client_invited` / `client_invitation_resent` / `client_access_changed` / `client_deactivated` | An administrator invites a client, resends the invitation, changes which projects and what they may see, or deactivates them. A resend is its own row, not also an invite. | `ClientInvitationService` |
| `screenshot` | `screenshot_deleted` | An administrator or HR permanently deletes a screenshot. Names whose screenshot and when it was taken. A deletion that failed is not recorded. | `TimeEntryScreenshotService.delete_screenshot` |
| `member` | `member_created` / `_updated` / `_deleted` (and `_deactivated`, written before Delete became permanent), `login_excluded` / `login_allowed`, `add_tasks_excluded` / `add_tasks_allowed` | The member directory. A switch is recorded only when it actually moved. | `MemberService` |
| `feedback` | `feedback_status_changed` | An administrator marks feedback Working or Resolved. | `FeedbackService.update_status` |
| `screenshot` | `screenshot_notice_sent` | A reviewer emails a notice about an employee's screenshot. | `TimeEntryScreenshotService.send_notice` |
| `system` | `maintenance_enabled` / `_disabled` | The maintenance notice (predates this document). | `MaintenanceModeService` |

The row belongs to the **actor**. "Grace excluded Alice from signing in" is
Grace's row; Alice is named in the description.

### Real changes only

A project or task edit is compared with what it was (`ProjectActivity`, in
`backend/app/services/project_activity.py`): the old values are read *before*
the edit is saved (`ActivityLogService.snapshot`) and the rows are built from
the difference afterwards. So a form that re-sends every field does not claim
to have changed them, an edit that changed nothing records nothing, and one
edit that moves the status *and* the team is two rows (`capture_many`), each
filterable by its action. If the old values cannot be read the edit still
saves and records the plain *Updated the project "X"* row instead.

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

Requires `view_employees` — Admin, HR, Leader; an employee gets 403.

| Who | What they read |
|---|---|
| Admin, HR | The whole organization. HR is read-only here, like everywhere else in the directory. |
| Leader | Their own team (`member_scope.visible_member_ids`) **plus** the project and task rows about a project they lead, **whoever made the change** (`member_scope.led_project_ids`) — an administrator moving their project to On hold or assigning a member shows up, grouped under the administrator. Nothing else the administrator did does: not their sign-ins, timer, member-directory or client actions, nor changes to projects the leader does not lead. |

Query: `start`, `end` (IST days; default the last seven), `member_id`,
`module`, `search`. The response groups rows by employee, newest first, with
project and task names joined in and the client split out of the description.
At most 5,000 rows are returned; `truncated` says when the window held more.
(`module` is still accepted by the API; the page no longer offers a category
picker, so it does not send it.)

The page is **Logs** in the sidebar (`/admin/logs`,
`frontend/src/features/admin/AdminActivityLogs.tsx`): one collapsed accordion
per employee, opening onto their actions split by IST day, with search, date
and employee filters, an Expand All / Collapse All button styled like the one
on Assign Tasks, and a CSV export.

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
- `backend/tests/test_activity_log_changes.py` — what a project or task change
  says (from → to, people by name), the assignment routes, clients,
  screenshot deletion, and a leader reading what anyone changed on their
  project and nothing more.
- `frontend/src/features/admin/__tests__/activityLogs.test.tsx` — the page and
  its wording.
- `desktop/tests/test_client_events.py` — queuing, the exit wait, attribution,
  and that the event names and endpoint match the backend's source.

# Member Activity Log API — one member, one day, one request

Backend for the "User Daily Activity / Logs" page: everything the page shows for
one member on one calendar day, computed server-side and returned in a single
response.

Lives at `backend/app/react_apis/member_activity_log.py` (router),
`backend/app/services/member_activity_log.py` (the calculations) and
`backend/app/repositories/member_activity_log.py` (the queries), with the
response contract in `backend/app/schemas/member_activity_log.py`. Registered
at `/api/v1/members/*` beside the member-details endpoint
(`docs/Member_Usage_API.md`); `/activity-log` is a distinct path suffix, not a
collision with the members CRUD router.

---

## Auth

Requires a bearer token and the `view_employees` permission — the same gate as
`GET /api/v1/members/{member_id}` and `GET /api/v1/members/{member_id}/details`.
Every role holding it also holds `time_entries:view_all` (`app/core/permissions.py`).

Scope is enforced by `MemberService.get`, the same helper every member read
uses: a member outside the caller's organisation, or outside a **leader's**
own team, is answered `404` rather than `403`, so the response never confirms
that a person the caller cannot see exists.

| Outcome | Status |
|---|---|
| No / invalid token | `401` |
| Token without `view_employees` (e.g. `employee`) | `403` |
| Member not in the caller's organisation or visible team | `404` |
| `member_id` not an integer, or `date` not `YYYY-MM-DD` | `422` |

Service credentials (API keys) authenticate like any other bearer token; the
only one that exists today (`release_bot`) does not hold `view_employees`.

---

## `GET /api/v1/members/{member_id}/activity-log`

| Param | Type | Required | Notes |
|---|---|---|---|
| `date` | `YYYY-MM-DD` | no | The IST calendar day to read. Defaults to **today in Asia/Kolkata**. |

```
GET /api/v1/members/174/activity-log?date=2026-09-18
```

### Timezone

Monitra reports in one organisation-wide zone, `Asia/Kolkata`
(`app/core/time_format.py`); users carry no timezone of their own. The day is
the half-open UTC window `[00:00 IST, 24:00 IST)` of the requested date, built
with `ist_day_start_utc` / `ist_day_end_utc`, and `user.timezone` names the
zone so the client can render instants in it. Every timestamp in the response
is an aware UTC instant.

### Midnight

Entries are **clipped to the day**, never counted whole. `23:50 → 00:20` is
ten minutes on the first day and twenty on the second, and the same minute is
never counted twice. This differs from the Reports and Time Tracking pages,
which attribute an entry whole to the day it *started*; for an entry that lies
inside one day — the overwhelmingly common case — the two agree to the second.

Midnight is applied in whole seconds (the unit every duration is rounded to):
an entry stopped 338 ms after midnight does not "continue" into the next day.
It keeps its real stop instant, and its adjustments stay with it. Rows that do
cross carry `continues_from_previous_day` / `continues_into_next_day`.

### Running timer

A running entry is measured against the **server clock** at the moment of the
request (`now − start_time`, net of adjustments, exactly as
`TimeEntryRead.net_seconds`), clipped to the day like any other entry, and
counted once. It appears in the timeline with `is_running: true` and
`end_time: null`, in the project breakdown, and in every total; nothing waits
for a stop event. `current_state` reports it whatever day was requested.

---

## Response

```jsonc
{
  "user": { "id": 174, "name": "…", "email": "…", "role": "employee",
            "designation": "…", "date": "2026-09-18", "timezone": "Asia/Kolkata" },

  "summary": {
    "first_start_time": "2026-09-18T06:01:15.940816Z",
    "last_stop_time":   "2026-09-18T18:30:00.338760Z",
    "last_stop_reason": "idle_stop",
    "total_worked_seconds": 27403,      "total_worked_time": "07:36:43",
    "total_tracked_seconds": 27403,     "total_tracked_time": "07:36:43",
    "total_active_work_seconds": 7351,  "total_active_work_time": "02:02:31",
    "total_manual_seconds": 0,          "total_manual_time": "00:00:00",
    "total_idle_seconds": 37002,        "total_idle_time": "10:16:42",
    "idle_kept_seconds": 20052, "idle_discarded_seconds": 16950,
    "idle_reassigned_seconds": 0, "idle_pending_seconds": 0,
    "total_break_seconds": 0,           "total_break_time": "00:00:00",
    "activity_percentage": 8, "activity_measured_seconds": 33772,
    "screenshot_count": 57, "application_count": 12,
    "project_count": 2, "task_count": 4
  },

  "projects": [ { "project_id", "project_name", "total_seconds", "total_time",
                  "active_seconds", "active_time", "manual_seconds", "manual_time",
                  "task_count", "tasks": [ { "task_id", "task_name", "status",
                  "total_*", "active_*", "manual_*", "first_start_time", "last_stop_time" } ] } ],

  "timeline":    [ /* ActivityLogEvent, start_time ascending — see below */ ],
  "screenshots": [ /* ActivityLogScreenshot, captured_at ascending */ ],

  "activity": {
    "activity_percentage": 8, "activity_measured_seconds": 33772,
    "keyboard_event_count": 1234, "mouse_click_count": 456,
    "mouse_movement_count": 7890, "mouse_event_count": 8346,
    "active_application_count": 12,
    "top_applications": [ { "application_name", "duration_seconds", "duration",
                            "segment_count", "start_time", "end_time", "urls": [ … ] } ],
    "top_urls": [ { "browser_name", "domain", "url", "page_title", "duration_seconds", "duration" } ]
  },

  "current_state": { "currently_tracking": false, "currently_idle": false,
                     "currently_on_break": false, "current_entry_id": null,
                     "current_project_id": null, "current_project_name": null,
                     "current_task_id": null, "current_task_name": null,
                     "current_started_at": null, "current_elapsed_seconds": null,
                     "current_idle_since": null, "server_time": "…" },

  "data_quality": { "last_sync_time": "…", "has_running_entry": false,
                    "has_pending_idle": false, "data_complete": true }
}
```

Every duration is carried twice, as the rest of this API does it: an exact
integer `*_seconds` and the same value as `HH:MM:SS` in `*_time` / `duration`.
IDs are integers. Absent values are `null`, never `0`.

### What each total includes

All figures are clipped to the day and include a running entry up to now.

| Field | Includes | Excludes |
|---|---|---|
| `total_tracked` | timer entries, **net** of `time_entry_adjustments` (discarded idle, reassigned idle, unwanted-activity deductions), floored at zero per entry; kept idle time; idle-reassignment target entries | manual time |
| `total_manual` | `time_entries` rows with `is_manual` (mirrored approvals) and approved `manual_time_entries` never mirrored | — |
| `total_worked` | `total_tracked + total_manual` — the figure Reports / Time Tracking show for the same day | — |
| `total_idle` | every idle period on the day's entries, clipped: kept + discarded + reassigned + pending (the four sub-fields always add up to it) | — |
| `total_active_work` | `total_tracked` minus every idle second still inside it (kept idle, unanswered idle, and the whole of any idle-reassignment entry) | — |
| `total_break` | **always 0** — Monitra records no breaks; a timer is running or stopped, and an untracked gap is not recorded as anything | — |
| `activity_percentage` | duration-weighted average of the day's `time_entry_activity` windows: `SUM(pct × window_seconds) / SUM(window_seconds)`, the product-wide definition; `activity_measured_seconds = 0` means "nothing was measured", not 0% | manual time (never sampled) |

For an entry that crosses midnight, each adjustment is attributed to the day
containing the instant it was recorded (clamped into the entry's own span), so
the two days add up to the entry's net total.

### Timeline rows (`ActivityLogEvent`)

| `entry_type` | Meaning |
|---|---|
| `tracked` | a timer entry (`source`: `timer`, or `idle_reassignment` for the already-stopped entry an idle reassignment creates) |
| `manual` | approved manual time (`source: manual_entry`); `entry_id` is the mirror row, or `null` with `manual_entry_id` set for a pre-mirror legacy row |
| `idle` | an idle period, overlapping the `tracked` row it belongs to and never added to it; `idle` carries the backend's ruling (`counted`, `keep_idle_time`, `action`, reassignment) |

`break` is deliberately never emitted. `duration_seconds` is the reportable
figure inside the day (`measured_seconds + adjustment_seconds`, floored at
zero); `is_running` rows have `end_time: null`.

`stop_reason` is **derived**, because the backend stores none:

| Value | Derived from |
|---|---|
| `stop` | an explicit stop from the client — the Stop button, a quit, a window close, or a stop replayed from the offline queue. The backend cannot tell these apart and does not pretend to. |
| `idle_stop` | an idle period on the entry answered with `action = stop` (chosen in the popup, or forced by a stop while the popup was unanswered) |
| `reassignment` | the entry is an idle-reassignment target, created already stopped |

### Screenshots

Metadata only, from `time_entry_screenshots`; no Drive call and no bytes.
`image_url` is `/time-entry-screenshots/{id}/view`, the existing streaming
endpoint, under the same scope check as this response. `activity_percentage`
is the duration-weighted activity of the capture window the screenshot falls
in, bucketed exactly as the Screenshots timeline does it (the member's own
`capture_frequency`), so the two pages show the same number for the same
capture. `project_id`/`task_id` are the screenshot's own entry's, resolved
server-side.

### Activity and privacy

`time_entry_activity` holds **counts per window** — keyboard strokes, mouse
clicks, mouse movements — and nothing else; no key identity is stored anywhere
in the database, so none can be returned. Per-application keyboard/mouse
counts do not exist (counts are per window, not per application) and are
therefore not reported rather than estimated. `urls` under a browser
application are the day's URL segments recorded under that browser name.

### `current_state`

From the backend's source of truth for the timer: the member's running entry
(`uq_active_time_entry` guarantees at most one) and any unanswered idle period
on it. `current_elapsed_seconds` is server-measured and net of adjustments.
`currently_on_break` is always `false` (no break concept).

### `data_quality`

Only what the database holds. `last_sync_time` is the latest instant the
backend received any record for the day (entry write, activity window, app/URL
segment, screenshot). `data_complete` means the day is closed from the
backend's point of view — in the past, nothing still running into it, no idle
period unanswered; it says nothing about what a desktop may still hold in its
offline queue. `offline_events_count` and `unsynced_events_count` are
deliberately absent: the backend cannot determine them.

---

## Performance

Ten SQL statements per request, whatever the day holds (measured against the
development database with the busiest member-day it has): the member lookup,
the day's entries with project/task/status joined, adjustments and idle periods
for those entries by `IN (…)`, legacy manual entries, the three telemetry
streams, screenshots, and the running entry (+ its pending idle period). The
per-entry loops never touch the database.

The three telemetry streams are read as day-scoped rows rather than SQL
aggregates: the desktop caps every segment/window at sixty seconds, so one
member's day is a few hundred rows per stream, and the service needs the
individual segments to place each application's real first/last instant and
to attribute each screenshot to its capture window. No table is read twice.

Existing indexes cover the access pattern (`time_entries(user_id)`,
`(start_time)`, and the `recorded_at` / `captured_at` / `time_entry_id` indexes
on the telemetry tables); no migration was needed or added.

## Tests

`backend/tests/test_member_activity_log.py` — the pure day/midnight/adjustment
rules, the service on mocked rows (running timer, manual, idle kept/discarded/
pending, reassignment, midnight crossing, stop reasons, aggregation, no raw
keystrokes), the real queries on a SQLite session (overlap semantics, fixed
statement count independent of volume), and the route through the real router
(401/403/404/422, default date).

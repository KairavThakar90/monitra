# Desktop notifications (administrator-managed)

An administrator decides, from the web app, which notifications the desktop
shows and when. Switched off means **never sent**: the desktop reads the
schedule and shows only what is on, at the time the schedule names.

Two kinds, one schedule:

* **Built-in reminders** — the ones the desktop already has
  (`desktop/background_services/wellbeing/reminders.py`): the interval
  reminders ("every 20 minutes of a working session") and the four daily break
  times. An administrator can switch each off, restrict it to certain weekdays,
  and move a daily one to another time. The *wording* stays the desktop's.
* **Custom notifications** — a title and a message the administrator writes,
  shown once a day at a chosen time on chosen weekdays.

Scope is deployment-wide: one schedule for every desktop, like maintenance mode.

## Units — read this before touching any time

* **Time of day is `HH:MM`, 24-hour, IST (Asia/Kolkata).** The same timezone
  the rest of the application reports in, and the one the desktop's daily
  reminders already use. No seconds, no AM/PM, no single-digit hour.
* **Weekdays are integers `0`–`6`, Monday = `0` … Sunday = `6`** — exactly
  `datetime.weekday()`. The weekday is the **IST** weekday of the moment, not
  the viewer's local one.
* A built-in reminder with no stored change is **on, at its default time, every
  day**. So an untouched deployment behaves exactly as the desktop did before
  this feature, and a reminder added to the desktop later arrives switched on.

## Where it lives

One row in `system_settings`, key `desktop_notifications` (JSONB) — the table
and row-locking pattern maintenance mode uses. **No migration.**

```json
{"version": 7,
 "builtin": {"lunch": {"enabled": false, "time": "13:45", "weekdays": [0,1,2,3,4]}},
 "custom":  [{"id": "a1b2c3d4e5f6", "title": "Standup", "message": "Daily standup in 5 minutes.",
              "time": "10:25", "weekdays": [0,1,2,3,4], "enabled": true,
              "created_at": "...", "updated_at": "...", "created_by": "dev"}]}
```

`builtin` holds only what an administrator *changed*. `version` rises by one on
every real change and is `0` before the first.

## API

Every route is also served under `/api/v1`.

### `GET /desktop-notifications/schedule` — any signed-in client

What every desktop polls.

```json
{"version": 7,
 "updated_at": "2026-10-01T09:00:00Z",       // null while version is 0
 "server_time": "2026-10-01T09:30:12Z",
 "builtin": [{"key": "lunch", "enabled": false, "time": "13:45", "weekdays": [0,1,2,3,4]}],
 "custom":  [{"id": "a1b2c3d4e5f6", "title": "Standup", "message": "Daily standup in 5 minutes.",
              "time": "10:25", "weekdays": [0,1,2,3,4]}]}
```

* `builtin` has **one entry per catalogue key**, defaults filled in. `time` is
  `null` for an interval reminder. A key the desktop does not know is ignored; a
  key the response omits is treated as the default (on, every day).
* `custom` lists **only notifications that are switched on**.

### Administrator routes — `administrator`, `org_admin`, `super_admin` only (403 otherwise)

| Route | Body | Notes |
|---|---|---|
| `GET /desktop-notifications` | — | the admin view: labels, descriptions, kind, cadence, defaults, state |
| `PUT /desktop-notifications/builtin/{key}` | `{enabled?, time?, weekdays?}` | only what is sent changes; `time` only for daily reminders (400 for an interval one); 404 for an unknown key |
| `POST /desktop-notifications/custom` | `{title, message, time, weekdays, enabled=true}` | 201; at most 50 (409 beyond) |
| `PATCH /desktop-notifications/custom/{id}` | any of the five | 404 if absent |
| `DELETE /desktop-notifications/custom/{id}` | — | 404 if absent |

Every write returns the full admin view. A request that changes nothing is
answered with the current state and records nothing (idempotent).

### Validation (the shared catalogue — `docs/VALIDATION.md`)

| Field | Rule |
|---|---|
| `title` | `NAME`, `max_length` 80 |
| `message` | `PLAIN_TEXT`, `max_length` 300, required |
| `time` | `TIME_OF_DAY` (new rule, all three layers) |
| `weekdays` | `ENUM` per member, `0`–`6`, at least one; sorted and de-duplicated |
| `enabled` | boolean |

A field that is sent is validated in full — an explicit `null` or a blank is
**rejected**, never treated as "unchanged". Omit a field to leave it alone.

## What the desktop does with it

* **`NotificationScheduleService`** (a `LoopService`) polls the schedule about
  every 30 seconds (jittered; the cadence of the maintenance notice), holds
  while signed out or offline, asks a backend that does not have the endpoint
  (404) only every five minutes, keeps the last good schedule when a poll
  fails (and tries again at the next 30-second poll), and persists the last
  good one so a start while offline uses it. A poll is one small read, and
  nothing is written unless the version changed.
* **`WellbeingService`** stays the scheduler and stays free of network work. It
  reads the current schedule on every tick:
  * an **interval** reminder is shown only if it is on **and** today's IST
    weekday is allowed; a suppressed one advances its grid exactly as a shown
    one would, so switching it on later does not release a backlog;
  * a **daily** reminder uses the schedule's time, its weekdays and its
    on/off state;
  * a **custom** notification is a daily reminder whose title and body come
    from the schedule, keyed `custom:<id>`.
* The existing rules still apply: a daily reminder more than
  `DAILY_GRACE_SECONDS` late is recorded as missed and not shown; one fires
  once per IST day; nothing is shown while signed out; nothing is shown within
  `MIN_SPACING_SECONDS` of the previous reminder.
* With no schedule at all (never fetched, nothing persisted, endpoint absent)
  the desktop behaves exactly as it did before: every built-in reminder on.
  There is no placeholder schedule.

Consequences worth knowing:

* Changing a daily time to **later today** after it already fired today does not
  fire it again today (one per IST day, per reminder key).
* Changing a time to **earlier than now** is "missed" if it is more than the
  grace window ago, and is not shown late.
* A desktop picks a change up on its next poll: within about 30 seconds (35 at
  the longest jitter), not instantly. A notification saved for a time less than
  that ahead is shown a little after its time -- still inside the grace window
  -- never silently dropped; to see one on the minute, save it at least a minute
  ahead. (It used to poll every five minutes: a notification saved at 11:12:13
  for 11:13 was shown at 11:15:17, because the desktop had last asked at
  11:09:54 and next asked at 11:15:16.)

## Audit

Every real change writes an `activity_logs` row (module `system`, action
`desktop_notification_created` / `_updated` / `_deleted`) naming the
administrator, stamps `updated_by_username`/`updated_at` on the settings row,
and logs `DESKTOP_NOTIFICATIONS_CHANGED`.

## Tests that pin this

* `backend/tests/test_desktop_notifications.py` — the service and the routes.
* `desktop/tests/test_notification_schedule_contract.py` — the backend catalogue
  and the desktop catalogue agree (keys, kinds, cadences, default times,
  descriptions).
* `desktop/tests/test_notification_schedule.py` — the fetch/hold/persist
  behaviour and the scheduler's use of the schedule.
* `frontend/src/features/admin/__tests__/desktopNotifications.test.tsx` — the
  admin page.

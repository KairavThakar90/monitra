# Desktop notifications (administrator-managed)

An administrator decides, from the web app, which notifications the desktop
shows and when. Switched off means **never sent**: the desktop reads the
schedule and shows only what is on, at the time the schedule names.

Two kinds, one schedule:

* **Built-in reminders** — the ones the desktop already has
  (`desktop/background_services/wellbeing/reminders.py`): the interval
  reminders ("every 20 minutes of a working session") and the four daily break
  times. An administrator can switch each off, restrict it to certain weekdays,
  and give it a time: a daily one is moved to another time, and an interval one
  can be fixed to a time of day, when it is shown once a day instead of
  repeating. So **every notification can be managed by time**. The *wording*
  stays the desktop's.
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
 "max_per_hour": 4,
 "builtin": {"lunch": {"enabled": false, "time": "13:45", "weekdays": [0,1,2,3,4]}},
 "custom":  [{"id": "a1b2c3d4e5f6", "title": "Standup", "message": "Daily standup in 5 minutes.",
              "time": "10:25", "weekdays": [0,1,2,3,4], "enabled": true,
              "created_at": "...", "updated_at": "...", "created_by": "dev"}]}
```

`builtin` holds only what an administrator *changed*, and `max_per_hour` is present only when
an administrator chose a number other than the default (2) -- so existing rows need nothing.
`version` rises by one on every real change and is `0` before the first.

## API

Every route is also served under `/api/v1`.

### `GET /desktop-notifications/schedule` — any signed-in client

What every desktop polls.

```json
{"version": 7,
 "updated_at": "2026-10-01T09:00:00Z",       // null while version is 0
 "server_time": "2026-10-01T09:30:12Z",
 "max_per_hour": 2,
 "builtin": [{"key": "lunch", "enabled": false, "time": "13:45", "weekdays": [0,1,2,3,4]}],
 "custom":  [{"id": "a1b2c3d4e5f6", "title": "Standup", "message": "Daily standup in 5 minutes.",
              "time": "10:25", "weekdays": [0,1,2,3,4]}]}
```

* `builtin` has **one entry per catalogue key**, defaults filled in. `time` is
  `null` for an interval reminder that is still repeating, and the `HH:MM` an
  administrator fixed it to otherwise (the desktop then shows it once a day at
  that time and does not repeat it). A key the desktop does not know is ignored; a
  key the response omits is treated as the default (on, every day).
* `custom` lists **only notifications that are switched on**.
* `max_per_hour` is how many notifications the desktop may show in any rolling hour. It is
  always present (2 until an administrator chooses another number, 1-6). A desktop that
  receives none, or a number outside what it accepts, uses its own default of 2; a desktop
  that predates the field ignores it.

### Administrator routes — `administrator`, `org_admin`, `super_admin` only (403 otherwise)

| Route | Body | Notes |
|---|---|---|
| `GET /desktop-notifications` | — | the admin view: labels, descriptions, kind, cadence, defaults, state, and the limit with its default and range |
| `PUT /desktop-notifications/builtin/{key}` | `{enabled?, time?, weekdays?, repeat?}` | only what is sent changes; `time` moves a daily reminder and fixes an interval one to a time of day; `repeat: true` puts an interval reminder back on its cadence (removes its time) -- 400 for a daily one, 422 with `time`, 422 for `false`/`null`; 404 for an unknown key |
| `PUT /desktop-notifications/limit` | `{max_per_hour}` | 1-6; choosing the default stores nothing; 422 for anything else |
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
| `repeat` | boolean, only `true` ("go back to repeating"); not with `time`; an explicit `false` or `null` is a 422 |
| `max_per_hour` | `INTEGER` (`integer_field`), 1-6 -- the shared rule with this field's range, not a new rule |

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
  * an **interval** reminder repeats only if it is on, today's IST weekday is
    allowed **and the schedule has not fixed it to a time of day**; a
    suppressed one advances its grid exactly as a shown one would, so switching
    it on (or back to repeating) later does not release a backlog;
  * an interval reminder **with a time** stops repeating and becomes a
    time-of-day notification at that time with its own wording -- the same rules
    as a daily one below (once per IST day, late beyond the grace window is
    missed, recorded as `hydrate` -> `2026-10-08@12:40`, shown at its time
    whatever the hourly limit says, and a place is held for it in the hour
    before). Removing the time puts it back on its own cadence, with no backlog;
  * a **daily** reminder uses the schedule's time, its weekdays and its
    on/off state;
  * a **custom** notification is a daily reminder whose title and body come
    from the schedule, keyed `custom:<id>`.
* The existing rules still apply: a daily reminder more than
  `DAILY_GRACE_SECONDS` late is recorded as missed and not shown; nothing is
  shown while signed out; nothing is shown within `MIN_SPACING_SECONDS` of the
  previous reminder.
* **A daily notification fires once per IST day at its current time.** The record of it
  is the date *and the time it was set to* (`custom:8f747c55a50e` -> `2026-10-08@12:40`),
  so moving a notification re-arms it: one that fired at 11:13 and was then edited to
  12:40 fires again at 12:40. (The record was the date alone, so it never did -- the
  administrator saw nothing at the time they had just set.) The same time is still shown
  only once, whatever else is edited; and a date-only record written before this change is
  honoured for today, so an upgrade does not repeat what was already shown.
* **The hourly limit.** The desktop shows at most `max_per_hour` notifications in any
  rolling hour (2 unless an administrator chose another number). What counts, and what is
  never held back:
  * An administrator's own notification, the daily break times and any repeating reminder
    **given a time** are **shown at their time whatever the limit says** -- they are scheduled events, and holding one back
    would be the bug above again. They count toward the hour.
  * The **repeating reminders** (every 20, 30, 60 minutes...) share what is left. The
    scheduler holds a place for each scheduled notification that is still to come in the
    next hour, so two repeating reminders cannot use the hour up and make a notification
    set for 12:40 arrive as the third.
  * A repeating reminder the limit holds back is **not dropped and not re-gridded**: it
    stays due, and when room opens the one that has waited longest goes first.
  * More scheduled notifications in one hour than the limit are all shown (the
    administrator wrote them); no repeating reminder is shown in that hour.
  * The count is kept in memory: a restart starts the hour afresh, and the repeating
    cadence restarts with it, so the first repeating reminder is at least its own
    interval into the new session.
  * This governs the wellbeing reminders and the administrator's notifications only. The
    application's own messages (an error, an update, the maintenance notice, "Screenshot
    captured", "Task updated") are not part of this list and are not counted.
* With no schedule at all (never fetched, nothing persisted, endpoint absent)
  the desktop behaves exactly as it did before: every built-in reminder on.
  There is no placeholder schedule.

Consequences worth knowing:

* Changing a daily time to **later today** after it already fired today fires it again
  at the new time (the record names the time it fired for). Editing only the wording
  does not.
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
and logs `DESKTOP_NOTIFICATIONS_CHANGED`. Setting the limit is audited the same way
("Set the desktop notification limit (4 per hour)").

## Tests that pin this

* `backend/tests/test_desktop_notifications.py` — the service and the routes.
* `desktop/tests/test_notification_schedule_contract.py` — the backend catalogue
  and the desktop catalogue agree (keys, kinds, cadences, default times,
  descriptions, the default limit and the range an administrator may choose).
* `desktop/tests/test_notification_schedule.py` — the fetch/hold/persist
  behaviour and the scheduler's use of the schedule.
* `desktop/tests/test_notification_hourly_limit.py` — a moved notification re-arms; the
  hourly limit, its reserved places, fairness and the schedule's `max_per_hour`.
* `frontend/src/features/admin/__tests__/desktopNotifications.test.tsx` — the
  admin page.

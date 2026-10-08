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
* **Pushed messages** (the route and the desktop side exist; **the admin web page has
  no button for them** -- the buttons were taken out at the owner's request,
  2026-10-08) — a title and a message shown on every signed-in desktop
  *now*: a connected desktop shows it within a second or two. A desktop that
  is away hears about it when it comes back, for up to ten minutes; after that
  the message is dropped ("now" is wrong ten minutes later).

A desktop no longer learns about a change only by asking every half minute: it
keeps a small event stream open (`GET /desktop-notifications/stream`) and is
told the moment the schedule's `version` moves. The stream is a signal, never
the schedule; the poll remains as the fallback and the ceiling.

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
              "created_at": "...", "updated_at": "...", "created_by": "dev"}],
 "pushes":  [{"id": "9c1d2e3f4a5b", "title": "Server restart", "message": "Please save your work.",
              "sent_at": "2026-10-08T09:30:12+00:00", "sent_by": "dev"}]}
```

`builtin` holds only what an administrator *changed*, `max_per_hour` is present only when
an administrator chose a number other than the default (2), and `pushes` only once a message
has been pushed (the last 20, oldest first; one older than ten minutes stays on the row until
it is pushed out, but is never sent to a desktop) -- so existing rows need nothing.
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
              "time": "10:25", "weekdays": [0,1,2,3,4]}],
 "pushes":  [{"id": "9c1d2e3f4a5b", "title": "Server restart", "message": "Please save your work.",
              "seconds_ago": 4}]}
```

* `builtin` has **one entry per catalogue key**, defaults filled in. `time` is
  `null` for an interval reminder that is still repeating, and the `HH:MM` an
  administrator fixed it to otherwise (the desktop then shows it once a day at
  that time and does not repeat it). A key the desktop does not know is ignored; a
  key the response omits is treated as the default (on, every day).
* `custom` lists **only notifications that are switched on**.
* `pushes` lists the messages pushed in the last ten minutes (`PUSH_TTL_SECONDS`, the same
  number on both sides, checked by the contract test), oldest first. `seconds_ago` is **measured
  by the server**: the desktop never compares its clock with the server's, it anchors the
  remaining lifetime to its own monotonic clock when it parses the response. Empty most of the
  time; absent from an older backend, which a desktop reads as empty.
* `max_per_hour` is how many notifications the desktop may show in any rolling hour. It is
  always present (2 until an administrator chooses another number, 1-6). A desktop that
  receives none, or a number outside what it accepts, uses its own default of 2; a desktop
  that predates the field ignores it.

### `GET /desktop-notifications/stream?since=<version>` — any signed-in client

How a desktop hears about a change the moment it is made. A `text/event-stream`
(plain HTTP, no WebSocket, nothing to configure) that the backend holds open for
at most about 25 seconds:

* if the stored `version` already differs from `since` (the client missed a change
  while disconnected) one event is sent at once and the stream ends;
* otherwise a comment line (`: ping`) every 2 seconds until the version moves, then
  **one** `schedule` event (`data: {"version": 8}`) and the stream ends. The
  client fetches `GET /schedule` and opens a new stream. The stream never carries
  the schedule itself, so the schedule route stays the single source of truth;
* a desktop whose stream fails, or that is talking to a backend without this route
  (404), keeps polling `/schedule` every half minute as before.

The pings are what let the desktop stop within seconds: a request held open and
silent could not be abandoned from outside. `Cache-Control: no-cache, no-transform`
and `X-Accel-Buffering: no` keep a proxy from holding the pings back; the desktop
asks with `Accept-Encoding: identity` so nothing compresses them.

Behind it, one watcher per worker process reads the stored `version` -- one single-row
query on a short-lived session, never a connection held across a wait -- every 2
seconds **while any stream is open**, and wakes every stream of that process when it
moves. The worker that made the change wakes its own watcher at once, so a change
reaches a desktop on that worker in milliseconds and one on the other worker within
about 2 seconds. With no stream open there is no query. A stream holds **no database
connection** while it waits (the authenticating session is released before the first
byte; `docs/DB_CONNECTION_LIFECYCLE.md`). Every stream ends within a heartbeat of the
process being told to stop, so a deploy's restart is not held up by open streams; the
25-second bound is the backstop if that hook cannot be installed.

### Administrator routes — `administrator`, `org_admin`, `super_admin` only (403 otherwise)

| Route | Body | Notes |
|---|---|---|
| `GET /desktop-notifications` | — | the admin view: labels, descriptions, kind, cadence, defaults, state, and the limit with its default and range |
| `PUT /desktop-notifications/builtin/{key}` | `{enabled?, time?, weekdays?, repeat?}` | only what is sent changes; `time` moves a daily reminder and fixes an interval one to a time of day; `repeat: true` puts an interval reminder back on its cadence (removes its time) -- 400 for a daily one, 422 with `time`, 422 for `false`/`null`; 404 for an unknown key |
| `PUT /desktop-notifications/limit` | `{max_per_hour}` | 1-6; choosing the default stores nothing; 422 for anything else |
| `POST /desktop-notifications/push` | `{title, message}` | shown now on every signed-in desktop (within a second or two when connected; within ten minutes if away); the same title/message rules as a custom notification; two pushes with the same words are two pushes; bumps `version`; audited as `desktop_notification_pushed` |
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
| `since` (stream) | integer >= 0, the version the client has applied; 422 otherwise |
| `max_per_hour` | `INTEGER` (`integer_field`), 1-6 -- the shared rule with this field's range, not a new rule |

A field that is sent is validated in full — an explicit `null` or a blank is
**rejected**, never treated as "unchanged". Omit a field to leave it alone.

## What the desktop does with it

* **`NotificationScheduleService`** (a `LoopService`) fetches the schedule, holds
  while signed out or offline, asks a backend that does not have the endpoint
  (404) only every five minutes, keeps the last good schedule when a fetch
  fails, and persists the last good one so a start while offline uses it. A
  fetch is one small read, and nothing is written unless the version changed.
  Between fetches it **listens to the change stream** on the same loop thread
  (no new thread, no second owner of the schedule): an event means "fetch now"
  (300 ms later, so a burst of saves is one fetch). The stream is an accelerator
  and never the only path:
  * the whole schedule is still fetched on every event, at least every five
    minutes while the stream is quiet, and as the ~30-second poll whenever the
    stream is not in use -- a missed event costs minutes, never correctness;
  * the listen returns at every ping (2 s), so the service stops, holds on
    sign-out and holds when the network goes within a ping; silence for 7 s is a
    dead stream;
  * a failing stream backs off (5 s, doubling to 5 min, jittered) while the poll
    carries on; a 404 is not retried for five minutes; a stream that ends within
    5 s without an event counts as failing; the same version signalled twice
    with no progress counts as failing -- there is no reconnect loop;
  * `NOTIFICATION_STREAM_UP` / `_EVENT` / `_FAILED` / `_UNAVAILABLE` log lines
    are written on the edge, never per listen.
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
    from the schedule, keyed `custom:<id>`;
  * a **pushed message** is shown once, on the first tick that finds it, before
    the daily and repeating reminders, keyed `push:<id>`; recorded by id in
    `app_state` (`wellbeing.pushes_seen`) so a restart or a second delivery
    never shows it again; never held back by the hourly limit (it is the
    administrator's own message) but counted toward it; subject to the
    60-second spacing like everything else; dropped if its lifetime ran out
    before it could be shown.
* **Every notification is the platform's own by default**, the schedule's
  included (`NotificationService.NATIVE_BY_DEFAULT`): on Windows a toast with the
  standard information icon, the title, the message and the time, headed
  "Monitra — Staff Management" beside the Monitra logo (the executable's file description, `NOTIFICATION_HEADER_NAME` in
  `version.py`) and kept in the Action Center afterwards. The platform decides how
  long it stays on screen (the user's accessibility setting, five seconds by
  default). A machine with no tray gets the in-app card instead.
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
* A connected desktop hears a change within a second or two (the stream; about
  2 seconds when another worker process made the change). A desktop that cannot
  use the stream -- an older desktop, a proxy that buffers, a stream that is
  failing -- picks it up on its next poll, within about 30 seconds (35 at the
  longest jitter). So a notification saved for a time less than half a minute
  ahead is shown on the minute on a connected desktop and a little after its
  time -- still inside the grace window, never silently dropped -- on one that
  is polling; to be sure of the minute everywhere, save it at least a minute
  ahead. (It used to poll every five minutes: a notification saved at 11:12:13
  for 11:13 was shown at 11:15:17, because the desktop had last asked at
  11:09:54 and next asked at 11:15:16.)
* A pushed message is shown once per desktop, even if it is still in the
  schedule on the next fetch or after a restart; two pushes with the same words
  are shown twice, a minute apart (the spacing rule).

## Audit

Every real change writes an `activity_logs` row (module `system`, action
`desktop_notification_created` / `_updated` / `_deleted` / `_pushed`) naming the
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

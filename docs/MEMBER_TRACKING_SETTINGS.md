# Per-member desktop settings

**Authoritative** for the four things an administrator can switch off for one member, how
each reaches the desktop, and exactly what each one stops. Read with
[SCREENSHOT_SETTINGS.md](SCREENSHOT_SETTINGS.md) (size and quality), [DESKTOP_NOTIFICATIONS.md](DESKTOP_NOTIFICATIONS.md)
(the schedule) and [IDLE_DIAGNOSTICS.md](IDLE_DIAGNOSTICS.md).

## The four switches

| Switch | Where it is set | Column | Stops, on the member's desktop |
|---|---|---|---|
| **Screenshot capture** | User Management | `users.screenshots_enabled` | Every screenshot. Nothing is read, queued or reported. Existing screenshots are kept and still upload. |
| **Desktop notifications** | User Management | `users.notifications_enabled` | Every *informational* notification the desktop would show: the administrator's reminders, custom notifications and pushed messages, and the routine notices ("Logged in", "Pending activity synced", "Back online", "Screenshot captured", an update being available). |
| **Activity tracking** | User Management | `users.activity_tracking_enabled` | Keyboard and mouse activity (and the unwanted-activity rules that read it), and the applications and websites used. |
| **Idle detection** | Screenshot Management → Idle detection (only there; User Management no longer edits it) | `users.idle_enabled`, `users.idle_minutes` | Watching the member for inactivity: no idle period is detected, reported, or prompted for. |

All four default to **on**. Switching one off never deletes anything already recorded, and
each one acts on its **own feature only** -- the desktop tests apply every combination of the
four and assert each feature follows only its own switch.

### What each one deliberately does *not* stop

* **Notifications** silence only the *informational* ones (`INFO`, `SUCCESS`). Anything at `WARNING` or
  `ERROR` is still shown, because it tells the person something is wrong or needs them: the idle
  prompt's fallback alert, a missing permission, a refused action, being offline. Silencing those would
  hide a fault rather than remove noise. (An earlier version of the switch held back only the scheduled
  reminders, so a member who was off still saw every other toast; the gate now sits in
  `NotificationService.notify`, the one place every notification passes through.)
* **Activity tracking** does not stop the timer, screenshots or idle detection. Idle detection
  reads `ActivityService.idle_seconds()`, a plain system call that works whether or not a session
  is being tracked.
* **Idle detection** does not stop activity tracking: the two are measured independently.
* **Screenshots** do not depend on activity tracking; the privacy rules that blank an application
  read the foreground window themselves.

"Activity" is everything `desktop/background_services/activity/` measures of what the person does
at the machine, which is why applications and websites follow the activity switch (the owner of
"activity capture" in `CLAUDE.md` §3.1 is that package). If a narrower meaning is wanted -- keyboard
and mouse only -- it is a one-line change in each of `AppUsageService` / `UrlUsageService`: stop
asking the activity service.

## How it is saved

One write path for the first three: the ordinary member update, `PATCH /members/{id}`
(`manage_employees`, the permission User Management already needs).

```
PATCH /members/{id}   { "screenshots_enabled": false }                    # one switch
PATCH /members/{id}   { "notifications_enabled": true, "activity_tracking_enabled": false }
```

* Each is an optional **strict** boolean: `"yes"`, `1` and `"false"` are a 422, `null` and an
  omitted key mean "leave it". A request that carries one switch never changes another.
* A switch is audited (`activity_logs`, module `member`) only when it actually moves, as
  `screenshots_enabled|disabled`, `notifications_enabled|disabled` or
  `activity_tracking_enabled|disabled`, with the member named. None of them sends the member an
  email (only the sign-in and Add Task switches do).
* `GET /members` and the update's response carry all three; an absent value means on.

Idle detection keeps the fields it always had (`idle_enabled`, `idle_minutes`, 1-120) and the
same endpoint.

## How it reaches the desktop

Each reaches a running desktop through an endpoint it already polled, and all of them are also on the
sign-in profile (`GET /auth/me`), so a switch is in effect **before** the first session of the day.

| Switch | Poll | Refresh |
|---|---|---|
| Screenshot capture | `GET /screenshots/config` → `screenshots_enabled` | every 3 min |
| Desktop notifications | `GET /desktop-notifications/schedule` → `enabled` | the schedule poll (at most 5 min; sooner on a push) |
| Activity tracking | `GET /activity/config` → `activity_tracking_enabled` (new) | every 3 min |
| Idle detection | `GET /idle-periods/config` → `idle_enabled`, `idle_minutes` | every 3 min |

**A change is therefore not instant: it reaches a running desktop within a few minutes.**

Rules every one of them follows:

* **Only an explicit `false` turns a feature off.** A missing key (a backend older than the switch),
  a `null` or any non-boolean leaves it where it was, so a damaged answer can never silence anyone.
* **A profile with no switch means on,** so one member's "off" is never inherited by the next
  person to sign in on the same machine.
* **Edge-triggered:** only a change does anything; an unchanged answer polled every few minutes is
  a no-op.
* **A failed refresh keeps the last known setting.**
* **Mid-session changes take effect mid-session.** Turning activity tracking off writes out what was
  measured and stops the listeners; turning it back on resumes against the same entry. Turning
  notifications off holds the whole reminder scheduler; turning it on restarts its cadence.

### Notifications, specifically

The schedule is the deployment's but the caller is not, so the answer is per caller. For a member who
is off the backend sends `enabled: false` **and an emptied schedule** (every built-in reminder off,
no custom notification, no push). So a desktop that predates the switch shows that member nothing
either; a desktop that knows it also holds the scheduler back and resumes the moment it turns true.
`version` stays the real one, because the schedule itself did not change.

On the desktop the switch is read by two things: `WellbeingService._gate_reason` (the scheduler stops
asking for reminders) and `NotificationService.notify` (informational notifications are dropped before
de-duplication, so a message that is not shown does not use the rate limit; the switch is read at the
moment of each notification, so turning it back on shows the next one at once).

The member's own value is stored apart from the schedule snapshot (`notifications.member_enabled` in
`app_state`), which is wiped at sign-out; the snapshot is the deployment's and is kept.

## Rollout

* Deploy the **backend first** (migration `d1a8f3c5e7b9`, additive, defaults on), then the frontend.
* A switch takes effect only on desktops that contain this change, **except notifications**, which
  the backend empties server-side and so also reaches older desktops. Staff run tagged releases, not
  `main`; check `GET /desktop/client-versions` before relying on a screenshot or activity switch for
  a particular person.
* The backend deliberately does not refuse uploads from a member who is switched off: an older desktop
  would park every refused item on disk and retry it hourly.

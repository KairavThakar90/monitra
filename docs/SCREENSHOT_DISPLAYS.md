# Multi-screen screenshot management

**Authoritative** for how an administrator sees the screens each member uses and chooses which of them
are captured. Read with [MEMBER_TRACKING_SETTINGS.md](MEMBER_TRACKING_SETTINGS.md) (the per-member switches),
[SCREENSHOT_SETTINGS.md](SCREENSHOT_SETTINGS.md) (size and quality) and
[SCREENSHOT_PERSISTENCE.md](SCREENSHOT_PERSISTENCE.md) (everything after capture).

## What it does

A desktop already captures **every** screen of the computer and merges them into **one** screenshot (one
image, one upload, one Drive object, one database row -- see `desktop/background_services/screenshot/capture.py`).
This feature adds two things on top, and changes neither of those guarantees:

1. **Visibility.** The desktop reports the screens it can see. Screenshot Management -> **Multi-screen** lists
   the members who have reported, says how many screens each is using *right now*, and shows each screen's
   number, size and whether it is the primary. "Using multiple screens" (more than one *connected* screen) is
   the default view; "All members" lists everyone who has reported.
2. **Choice.** The administrator switches capture off for individual screens. A member with three screens can
   be captured from one: the desktop leaves the other two out **before reading them** (their pixels never
   exist in the process), and composes the image from the screens that remain, sized to what is captured.

The screenshot is still one image per capture instant. Switching screens off never produces a second image, and
a deliberate omission is not recorded as an incomplete capture (`display_count` and `displays_expected` both
count only the screens chosen).

> **What this is not.** It does not capture "on demand" from the admin panel: capture stays on the existing
> schedule (one at a random instant in every window while a timer runs). It does not replace the member's
> *screenshot capture* switch -- switching every screen off is that switch, and the page says so.

## The rules

| Rule | Why |
|---|---|
| A screen nobody decided about is **captured**. | Deploying this changes nothing for any member until an administrator chooses. |
| A desktop's report is **facts and never changes a choice**. | A report must not be able to quietly switch a screen back on. |
| An unplugged screen **keeps its row** (`connected = false`) and its choice. | The choice is still there when the monitor comes back. |
| **At least one connected screen stays on** (422 otherwise). | Switching them all off is the member's screenshot switch, not this. Only *connected* screens count, so a stale row never lets the last live screen be switched off. |
| Choosing for a screen the desktop **never reported** is a 422. | There is no such screen to switch. |
| Only administrators (`administrator`, `org_admin`, `super_admin`, by role name) choose. Another organisation's member, a client and the release account are **404**. | Same gate as the rest of Screenshot Management. |
| A choice that changes nothing writes nothing and is **not audited**. | Idempotent. |
| A change is audited (`activity_logs`, module `member`, action `screenshot_displays_updated`, the member named) and logged as `SCREENSHOT_DISPLAYS_CHANGED`. | Same trail as the other per-member switches. |

**Screen numbers are the operating system's own ordering** (the 1-based position in `mss.monitors[1:]`) and can
change when a monitor is replugged or rearranged. The page shows each screen's size and primary flag beside
its number so an administrator can recognise it, and a screen that appears under a different number is a new
screen as far as this feature is concerned -- the old number keeps its choice. If a machine's numbering is
unstable, check the Multi-screen page after a hardware change.

## Data

`member_displays` (migration `e3b7c1a9d5f2`, additive, creates the table only if it is missing): one row per
member per screen number -- geometry as the OS reported it (`left`/`top` are negative for a screen to the left
of / above the primary), `is_primary`, `connected`, `capture_enabled`, `last_seen_at`, `updated_at`,
`updated_by_user_id`. Unique on `(user_id, number)`; cascades with the user and organisation.

## API

| Call | Who | What |
|---|---|---|
| `PUT /screenshots/displays` (also under `/api/v1`) | the member's desktop | `{"displays": [{number, width, height, left, top, is_primary}, ...]}` -- 1 to 16 screens, each number once. Marks those connected and every other known screen disconnected; never touches `capture_enabled`. Replies `{"excluded_displays": [..]}`. Idempotent. |
| `GET /api/v1/screenshot-displays` | administrators | Members with at least one reported screen (active members of the organisation; never a client or the release account) and their screens. |
| `PUT /api/v1/screenshot-displays/{user_id}` | administrators | `{"screens": [{number, capture_enabled}]}` -- **only the screens listed change**. Replies with that member's screens. |
| `GET /api/v1/screenshot/privacy-config` | the member's desktop | now also carries `excluded_displays`: the screen numbers to leave out. |

Every number and the boolean are bounded and strict (`app/schemas/member_display.py`): a screen number outside
1-16, a duplicate, a non-boolean choice or a size outside 1-100000 is a 422, never clamped.

## How the desktop hears about it

The excluded numbers ride on the **privacy configuration** -- the member-specific "what is not captured"
answer the desktop already fetches at sign-in and every three minutes, and which capture **waits for** before its
first read of the screen. So there is no new channel, and no window in which the first capture of a session can
happen before the instruction arrives. **A change reaches a connected desktop within a second or two, and any desktop within three minutes.** Every administrator's change to something a desktop polls for raises the deployment's *desktop configuration revision* (`system_settings` key `desktop_config`, `backend/app/services/desktop_config_revision.py`), which rides on the notification change stream the desktop already holds open (`GET /desktop-notifications/stream?since_config=`). The stream says whether the change concerns this desktop (its member, or every desktop), and if it does, `NotificationScheduleService.config_changed` makes the three owners re-read their polls at once -- the same de-duplicated requests the three-minute timers make. The stream is the accelerator and never the only path: the polls remain the guarantee, a reconnect re-reads all three, and a desktop that cannot hold a stream (an older build, a proxy that buffers) is exactly where it was: within three minutes.

At sign-out the privacy configuration, the excluded screens with it, is forgotten (`ScreenshotService.reset_session`): the next member's captures wait for their own answer rather than running under the previous member's exclusions.

| Desktop behaviour | Detail |
|---|---|
| Reporting | `ScreenshotService._report_displays`, on the `TaskRunner` (key `screenshot-displays`): at sign-in (`apply_user_profile`) and on every configuration refresh. **Edge-triggered** per member: an unchanged desk is a no-op, a plugged-in monitor or a different member signing in reports again. A failed report keeps the old signature, so the next refresh sends it again -- it is state, not a queued operation. Nothing is reported for a member whose screenshots are switched off, a machine that cannot capture, or when nobody is signed in. Only number, size, position and primary flag are sent -- never pixels. |
| Capturing | `capture_all_displays(excluded=...)` drops the excluded numbers after enumerating and before any read. With nothing excluded the call is exactly what it always was. |
| Only damaged answers | Anything that is not a list of positive whole numbers excludes nothing (an older backend sends no key). |
| **Every attached screen excluded** | A decision, not a fault: `AllDisplaysExcluded`. Nothing is read, nothing is retried inside the window, and the window ends with one `excluded` event, reason `displays_disabled`. The grid reads **"Screens switched off"** instead of "No capture". It does not count as a failed window. |

## Rollout

* Deploy the **backend first, and run the migration before the new code takes traffic.** `GET
  /api/v1/screenshot/privacy-config` reads `member_displays`; on a database without the table it answers 503,
  and a desktop that cannot load its privacy configuration holds capture (`blocked` /
  `privacy_config_unavailable`) for every member. This is deliberately not swallowed: answering "no screens
  excluded" would capture a screen an administrator had switched off.
* Then the frontend. The Multi-screen tab is empty ("No member's computer has reported its screens yet") until
  desktops that contain this change sign in.
* Only desktops that contain this change report screens or honour a choice. Staff run tagged releases, not
  `main`: check `GET /desktop/client-versions` before relying on a screen choice for a particular person. An
  older desktop keeps capturing every screen, which is also what the page shows for it (nothing reported).

## Verification

`backend/tests/test_member_displays.py` (report, choice, scope, permissions, validation, the whole flow over
HTTP), `desktop/tests/test_screenshot_displays.py` (a fake `mss` that records every read, so the tests prove an
excluded screen is never grabbed), and `frontend/src/features/admin/__tests__/screenshotScreens.test.tsx`.
Not covered by any automated run: a real multi-monitor machine and a real browser.

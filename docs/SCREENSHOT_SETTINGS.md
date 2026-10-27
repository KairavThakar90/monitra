# Screenshot Management

**Authoritative** for what an administrator can control about the desktop's
screenshots, and how that control reaches the desktop. Read with
[SCREENSHOT_PERSISTENCE.md](SCREENSHOT_PERSISTENCE.md) (what happens to a capture
after it is taken) and [SCREENSHOT_PRIVACY.md](SCREENSHOT_PRIVACY.md) (which
applications and sites are never captured).

Web page: **Settings → Screenshot Management** (`/admin/settings/screenshot-management`,
administrators only), one page with three accordion sections: **Who is captured**
(the per-member switch), **Size and quality**, and **Privacy rules** (the application and
website exclusions, which used to be their own page).

## What can be set

| Control | Scope | Stored in | Default |
|---|---|---|---|
| **Capture on / off** | per member | `users.screenshots_enabled` | on |
| **Image size** (width × height, px) | the whole deployment | `system_settings` key `screenshot_settings` | 1500 × 1500 |
| **Image quality** | the whole deployment | the same row | Automatic |

Size and quality are deployment-wide, like the desktop notification schedule:
one setting for every desktop. Only the on/off switch is per member.

An untouched deployment behaves exactly as it did before this existed: nobody is
switched off, the size is 1500 × 1500, the quality is Automatic. The stored row
holds only what differs from the default, so choosing the defaults again puts the
row back as if nobody had touched it.

### Quality: Automatic or fixed

* **Automatic** (`image_quality: null`) is the behaviour before this feature: the
  desktop encodes WebP starting at 79, steps down on a busy screen to reach a byte
  target, and holds every image under a hard limit (135 KB per display at
  1500 × 1500, scaled with the canvas area for any other size). The page shows
  "79%" beside it, but that is where it *starts*, not a promise about the file.
* **Fixed** (`image_quality: 10..100`) is encoded at exactly that WebP quality. None
  of the byte-budget machinery runs: if it did, "90%" would silently become
  "whatever fits in 135 KB" and the setting would change nothing. A higher quality
  is a sharper, larger file, and more Drive storage.
  * The one ceiling is `FIXED_QUALITY_MAX_BYTES` (4 MB), below the server's 5 MB
    upload limit (`SCREENSHOT_MAX_UPLOAD_BYTES`). A file over the upload limit is
    answered 413, which the uploader treats as a refusal and parks for ever, so above
    4 MB the desktop lowers the quality step by step until it fits and logs
    `SCREENSHOT_FIXED_QUALITY_LOWERED`.

### Size

Width and height are each `250..4000` pixels and need not be equal. The screenshot
is the screen scaled to **fit** that canvas and centred, padded with the neutral
grey; it is never stretched. On a multi-display machine the size is the box the
*primary* display is fitted into, and the merged image is as wide as the desk needs
(see `compositor.output_size`), capped at 4000 on the long edge.

The limits are the upload check's own (`backend/app/core/screenshot_geometry.py`):
an administrator can never choose a size the server would refuse to store, which
would park every capture at that size as permanently failed. The widest shape the
limits allow is 16:1, inside the 20:1 the upload check accepts; a test pins that.

## How it reaches the desktop

The desktop does not call the admin API. It reads the result from
`GET /screenshots/config`, the endpoint it already polled for the member's capture
interval, and the on/off switch also rides on the sign-in profile:

```
GET /screenshots/config  ->  {
    "capture_frequency": 10,
    "screenshots_enabled": true,
    "image_width": 1500, "image_height": 1500,
    "image_quality": null            // null = Automatic; present, never omitted
}
GET /auth/me             ->  { ..., "screenshots_enabled": true }
```

* The switch is seeded from the profile at sign-in and session verification, so a
  member who is off never captures even the first window of a session; the periodic
  refresh (`ScreenshotService.CONFIG_REFRESH_SECONDS`, 3 minutes) then follows an
  administrator's change. **A change is therefore not instant: it reaches a running
  desktop within about three minutes.**
* **Only an explicit `false` turns capture off.** A missing key (a backend older than
  this feature), a `null` or any non-boolean leaves the switch where it was. A
  profile that carries no switch means *on*, so one member's "off" is never inherited
  by the next person to sign in on the machine.
* Size and quality are applied from the next capture; a capture already encoded keeps
  the size it was taken at, and so does every screenshot already stored.
* A key the answer does not carry changes nothing; a value outside the limits is
  ignored, never clamped into something nobody chose.

## What the desktop does while a member is off

* No screen is read, nothing is queued and **no "unresolved window" event is sent**:
  no capture was expected, so there is nothing to explain to the backend.
* The schedule stays armed, so switching back on needs no restart; the rest of the
  current window is planned at once.
* Screenshots taken before the switch-off are kept and still upload.
* The line beside ACTIVITY says "Screenshots are turned off" (information, not a
  failure), and nobody is prompted to grant macOS Screen Recording for a capture that
  will not be taken.

## The admin API

All four are administrators only (`administrator`, `org_admin`, `super_admin`), by
role name and never a service credential; every other caller is answered 403.

| Request | Body | Notes |
|---|---|---|
| `GET /api/v1/screenshot-settings` | | Current values, the defaults, `automatic_image_quality` and the limits the page validates against. |
| `PUT /api/v1/screenshot-settings` | `{image_width, image_height, image_quality}` | A **replacement**: all three keys are required; `image_quality: null` is Automatic, and leaving the key out is a 422 (it must not read as "Automatic" and undo a fixed quality). Idempotent: a request that changes nothing records nothing. |
| `PUT /api/v1/screenshot-settings/members/{id}` | `{enabled: bool}` | `changed` is false, and nothing is audited, if the member already held that position. 404 for another organization's member, a client account or the release account. |
| `POST /api/v1/screenshot-settings/members` | `{enabled: bool, apply_to: {scope: "all"} \| {scope: "members", user_ids: [...]}}` | `all` is every *active* member of the caller's organization today, never a client or the release account. A listed member that cannot be found is a 400 and nothing is changed. |

`GET /members` also returns `screenshots_enabled` on every member, which is what
the page's member table reads.

Integers follow the shared `INTEGER` rule ([VALIDATION.md](VALIDATION.md)): a whole
number, or a string holding one; text, decimals, booleans and `null` (where a number
is required) are 422.

## Audit

| Module | Action | When |
|---|---|---|
| `member` | `screenshots_enabled` / `screenshots_disabled` | A member's switch actually moved (one row for a bulk change, with no single member). |
| `system` | `screenshot_settings_updated` | The size or quality actually changed. |

Log lines: `SCREENSHOT_SETTINGS_CHANGED`, `SCREENSHOTS_SWITCHED`,
`SCREENSHOTS_SWITCHED_BULK` (backend); `SCREENSHOTS_SWITCH`, `SCREENSHOT_SETTINGS`,
`SCREENSHOT_FIXED_QUALITY_LOWERED` (desktop).

## Rollout notes

* Deploy the **backend first** (it adds the `screenshots_enabled` column — migration
  `c9e2a7d41b36`, additive and idempotent), then the frontend. An older desktop
  ignores every new key.
* **The setting only reaches desktops that contain this change.** Staff run tagged
  releases, not `main`: until a member is on a build with this change, switching their
  screenshots off, or changing the size or quality, has no effect on that machine. The
  backend deliberately does not refuse uploads from a member who is switched off — an
  older desktop would park every refused capture on disk and retry it hourly — so the
  switch is enforced by the desktop alone. Check `GET /desktop/client-versions` before
  relying on it for a particular person.
* Tested at the unit and HTTP layers; the desktop path has not been exercised on a real
  display (all automated runs are headless), nor against a real Google Drive.

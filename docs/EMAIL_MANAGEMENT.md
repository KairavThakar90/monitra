# Email Management -- who is copied on which email

**Authoritative** for the CC policy: the page (Settings -> Email Management), the API, how the outbox applies it
at send time, and what is never copied. Read with [Email_Notifications.md](Email_Notifications.md) (the outbox and
the emails themselves).

## What it does

Monitra used to copy one fixed list of addresses (`EMAIL_CC_ADDRESSES`) on every email it sent. Now an
administrator decides, **per address and per email**: an address is copied on an email only when that email is
ticked for it, and otherwise it is not. Addresses can be added and removed. The page is a grid -- one row per
email (every email registered in `backend/app/core/email_catalogue.py`, fifteen today), one column per CC address.

The people an email is *addressed to* are never affected; this only governs the `Cc` header.

## The fifteen emails

The catalogue is one **registry**, shared by the page, the API and the outbox, keyed by the outbox's
`notification_type`. An email is registered **where its builder is** -- one `_register(...)` call beside each
builder in `backend/app/services/email/messages.py` carries the key, the builder and the words the page shows.
There is no second list: the number is the registration order, the page reads the registry through the API, the
schema validates against it, and `check_registry()` runs when the backend starts and refuses to start on a builder
that is not registered, a copyable email that is never queued, or a configuration flag that does not exist.

| # | Key | Email | `cc_allowed` |
|---|---|---|---|
| 1 | `welcome` | Welcome | yes |
| 2 | `feedback` | Feedback received | yes |
| 3 | `feedback_status` | Feedback status (Working / Resolved) | yes |
| 4 | `release` | New version available (off by default, `RELEASE_EMAIL_ENABLED`) | yes |
| 5 | `weekly_report` | Weekly productivity report | yes |
| 6 | `monthly_report` | Monthly productivity report | yes |
| 7 | `manual_time_request` | Manual time request, for review | yes |
| 8 | `manual_time_receipt` | Manual time request, receipt | yes |
| 9 | `manual_time_decision` | Manual time decision (Approved / Rejected) | yes |
| 10 | `monthly_project_summary` | Monthly project summary | yes |
| 11 | `project_budget_alert` | Project hours alert | yes |
| 12 | `client_invitation` | Client invitation | **no** |
| 13 | `client_login_link` | Client sign-in link | **no** |
| 14 | `screenshot_notice` | Screenshot notice, with the picture attached | yes |
| 15 | `member_access` | Access update (Excluded / Allowed, for Login or Add Task) | yes |

**12 and 13 are never copied.** They carry a bearer secret (an invitation's Set-password / Reject links, a one-time
sign-in link); copying anyone would hand that person the client's access. They are sent immediately rather than
through the outbox and are `copy_exempt`. The page shows them locked, the schema refuses to store them in a
policy (422), and `cc_for` returns nothing for them whatever is stored. They are registered with no builder, and
the registry refuses a copyable email that has none (the outbox is the only place the policy is applied).

An email switched off in configuration (`WELCOME_EMAIL_ENABLED`, `RELEASE_EMAIL_ENABLED`,
`ACCESS_CHANGE_EMAIL_ENABLED`) is still listed and still configurable, with a "Switched off in configuration"
badge, so the choice is already made the day it is switched on.

## Where the policy lives

One row in `system_settings` (key `email_cc`) -- deployment-wide, read on every send, changed with no redeploy, **no
migration**:

```json
{"version": 3, "addresses": [{"email": "hr@storetransform.com", "triggers": ["welcome", "weekly_report"]}]}
```

* **Default.** With no row, the policy is the configured standing CC (`EMAIL_CC_ADDRESSES`, shipped as
  bharat@storetransform.com, projectmanager663@gmail.com, hr@storetransform.com, piyush@storetransform.com), each
  ticked on **every email that may be copied** -- exactly what was sent before this existed. Deploying it changes
  no email.
* **From the first save the row is the truth** and the environment variable is no longer consulted for the policy
  (it stays the starting list for a deployment that has never saved, and the fallback for a message built outside
  the outbox). Saving an empty list means *nobody* is copied; it does not fall back to the default.
* Addresses are stored lower-cased (the sender lower-cases the whole address), listed once, up to 50.
* A damaged row never stops an email: unreadable entries are skipped and logged (`EMAIL_CC_INVALID_ENTRY`), and a
  row with no readable list is the default.

## How a send applies it

`EmailOutboxService._send_claimed` reads `EmailCcService.cc_for(db, notification_type)` -- one row -- **at send
time**, before it ends the transaction (so the SMTP wait still holds no connection), and puts the result on the
message (`OutgoingEmail.cc`). `build_mime_message` copies exactly those addresses, never repeats a recipient, and
skips a malformed entry rather than failing the email. So **a change applies to every email that has not gone
out yet**, including ones already queued.

| `OutgoingEmail.cc` | Result |
|---|---|
| `None` (a message built outside the outbox) | the standing CC, as before |
| a tuple | exactly those addresses; `()` means no `Cc` header |
| any value, with `copy_exempt` | no copy |

**If the policy cannot be read** (the database is down) the send is a normal *delivery failure*: the attempt is
counted and retried with the outbox's jittered backoff. It is deliberately not sent without the chosen people, nor
to the environment's list -- either would copy the wrong people on a real email.

## API (administrators only: `administrator`, `org_admin`, `super_admin`)

| Call | |
|---|---|
| `GET /api/v1/email-cc` | The fifteen emails (key, number, name, who it goes to, when, `cc_allowed`, `note`, `active`), the addresses with their emails, `is_default`, `version`, who last changed it and when. |
| `PUT /api/v1/email-cc` | `{"addresses": [{"email": "...", "triggers": ["welcome", ...]}], "version": 3}` -- a **replacement**. 422 for an invalid or repeated address, an unknown email key, or a private-link email. `version` is optional: when it is sent (the page always sends the version it was built from) and is no longer the stored version, the save is **409** and nothing changes, so two administrators cannot silently overwrite each other; the page then reloads what is in force. Idempotent: saving what is already in force writes and audits nothing. |

Everyone else, including HR and a service credential, is 403 on both.

A real change is audited (`activity_logs`, module `system`, action `email_cc_updated`) with the addresses added,
removed and the emails switched on or off, and logged as `EMAIL_CC_CHANGED`.

## Rollout

* No migration. Deploy the backend, then the frontend; the page is empty-handed until then (404 on the API).
* Nothing changes for any email until an administrator saves.
* **Adding a new email to the product** is one `_register(key, builder, name, goes_to, trigger, ...)` call beside
  its builder in `messages.py` (plus the builder, the template and the `dedupe_key` the outbox already needs). It
  then appears on the page, in the API and in the schema with no further change anywhere -- there is no frontend
  list, no second backend list and no seed row. Forgetting the registration (`BUILDERS[key] = ...` by hand) stops
  the backend starting and fails `tests/test_email_management.py::RegistryTests`. Once a policy has been saved,
  existing addresses do **not** automatically get the new email: it appears unticked, so it is never copied to anyone
  by accident. (An untouched deployment still copies its standing list on every copyable email, the new one
  included -- that is what the environment list always did.)
* `backend/scripts/smoke_email.py` copies nobody: a message built outside the outbox would otherwise carry the
  environment's standing list, which administrators may have changed.

## Verification

`backend/tests/test_email_management.py` (catalogue, the registry -- a temporary email registered the way a
developer would, manageable end to end and removed cleanly -- defaults, change, the version check, validation,
permissions, damaged rows, the provider, the real send path, the whole flow over HTTP) and
`frontend/src/features/admin/__tests__/emailManagement.test.tsx`. Not covered by an automated run: a real SMTP
server and a real browser -- no email is sent by any test.

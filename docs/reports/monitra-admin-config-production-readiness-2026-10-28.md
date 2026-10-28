# Monitra -- administrator configuration: production-readiness audit, fixes and verification (2026-10-28)

Branch `smit`, starting commit `b042593` (clean tree), work in commits `aadb316` .. `f6ec8eb`
plus the E2E module committed after this report. Nothing pushed, deployed, merged or tagged.
Written for the owner and whoever deploys the next backend and desktop release.

## A. Executive summary

**PASS WITH LIMITATIONS -- implementation verified locally and on a throwaway integration
rig; production behaviour not verified.** Every one of the five features works end to end
against a real backend process, a real PostgreSQL, two real desktop runtimes, real screen
captures decoded from disk, and a real SMTP socket. Nothing was run against the production
backend, database or a real multi-monitor machine; the desktop paths were not exercised on
macOS; no real browser session was driven (the web pages are covered by their component tests
and by the API they call).

The headline change: an administrator's change now reaches a connected desktop in **well under
a second** (measured 0.01-0.33 s on the rig) instead of up to three minutes, through the change
stream the desktop already held open -- no new thread, timer, table or synchronisation
architecture. The polls remain the guarantee for a desktop that cannot hold a stream.

## B. Feature-by-feature results

| Feature | Before | Changes made | Automated tests | Real E2E (rig) | Live production | Final |
|---|---|---|---|---|---|---|
| 1. User Management switches (screenshot / notification / activity) | Implemented and correct per member; reached a desktop in up to 3 min; a switch learnt from a poll was lost on an offline restart | Configuration revision on the stream (seconds); poll-learnt switches written into the persisted profile; privacy config forgotten at sign-out | backend 3384 passed; desktop 165/166 modules (see F) | PASS: off/on for A in 0.02-0.33 s, B untouched, capture refused while off, DB row agrees, five rapid changes end in the last, restart offline keeps the switch | NOT VERIFIED | PASS (limitations) |
| 2. Screenshot Management (size, quality) | Implemented; applied on the next capture but only after the first 3-min refresh; nothing persisted across a restart | Revision on the stream; size/quality persisted and read at start; a restored session reads its configuration at start | as above + `test_config_change_signal.py` | PASS: 800x600 -> decoded 800x600 WebP in 0.05 s; q30 26 770 B vs q95 76 224 B of the same screen; two changes before a capture -> the latest; invalid values 422 | NOT VERIFIED | PASS (limitations) |
| 3. Multi-screen capture + per-screen choice | Implemented (report, choice, exclusion before any read, all-off refused) | Revision on the stream; a client/release account cannot report screens | as above | PASS with one physical screen: report, last-screen refusal (422). Two screens **pretended** (enumeration only; grabs, compositor, encoder, queue, report, choice, stream real): screen 2 off -> `display_count` 1, image 1500 px wide vs wider for both; screen 1 off/2 on; both back; monitor removed -> row kept disconnected | NOT VERIFIED; no real second monitor on this machine | PASS WITH LIMITATIONS |
| 4. Real-time announcements | Delivered in ~1 s via the stream, but shown as an ordinary toast; a push made while the desktop was closed was dropped on restart although inside its lifetime | Announcement card (Monitra mark, ANNOUNCEMENT heading, title + message as plain text, own accent/surface, close button); same-version pushes adopted; markup never rendered | as above | PASS: B shown in 0.00-0.28 s as `announcement`, once; A switched off is shown nothing; A switched back on is shown a push still inside its lifetime | NOT VERIFIED | PASS (limitations) |
| 5. Email Management (CC) | Implemented; a hand-typed catalogue beside the builder table (test-only guard); no concurrency guard on save; smoke script copied the env list | One registry populated beside each builder, checked at start-up; 409 on a stale version; page sends the version and reloads on 409; smoke copies nobody | backend 66 (management) + 7 real-SMTP; frontend 22 | PASS over a real SMTP socket and a real PostgreSQL: default 4 copied; two removed -> envelope and `Cc` header have two; a new address for the welcome email only; queued email uses send-time policy; empty list -> nobody; welcome once | NOT VERIFIED (no production email sent, by design) | PASS (limitations) |

## C. Defects found and fixed

1. **Two Alembic heads** (`e3b7c1a9d5f2`, `f1a7c3e9d2b4`) after merging `main` into `smit`:
   `alembic upgrade head` refused. Fix: merge revision `a1c4e8b2d6f0` (no-op). Test:
   `test_member_desktop_switches::MigrationTests` (was failing at baseline, now passes).
2. **Configuration reached a running desktop only at the next three-minute poll.** Fix:
   `system_settings.desktop_config` revision raised by every writer
   (`MemberService.update` for the six desktop fields, `MemberDisplayService.set_choices`,
   `ScreenshotSettingsService.update_settings`, every privacy-rule route), carried on
   `GET /desktop-notifications/stream?since_config=` with `concerns` per desktop; the desktop
   emits `NotificationScheduleService.config_changed` and the runtime re-reads the three polls.
   Older desktops never receive a configuration-only event. Tests:
   `backend/tests/test_desktop_config_revision.py` (22), `desktop/tests/test_config_change_signal.py` (29).
3. **A pushed message was dropped when it arrived on a same-version answer** (restart inside
   its ten-minute lifetime; a member switched back on while a push was live): `_apply` treated
   an equal version as "the level". Fix: adopt an answer whose pushes carry an unseen id and
   wake the scheduler. Tests in `test_config_change_signal.py`; DO_NOT_DO entry added.
4. **Every desktop start captured at the default size/quality until its first refresh**, and
   an offline restart lost an administrator's size; a switch learnt from a poll was lost on an
   offline restart. Fix: `screenshot.capture_settings` persisted in `app_state` and loaded at
   start; a restored session reads its configuration at start; `SessionManager.update_user_info`
   writes a poll-learnt switch into the persisted profile. Tests: `TestTheScreenshotServiceRemembers`,
   `TestTheSessionRemembersWhatThePollSaid`, activity refresh tests; E2E restart test.
5. **Privacy configuration (incl. excluded screens) survived a sign-out**, so the next member
   could capture under the previous member's exclusions for up to a refresh. Fix:
   `ScreenshotService.reset_session()` called from `on_logout`; capture then waits for the new
   member's own answer. Test: `test_sign_out_forgets_the_previous_members_privacy_configuration`.
6. **Announcements indistinguishable from reminders; card labels used Qt AutoText.** Fix:
   `NotificationLevel.ANNOUNCEMENT` (informational, suppressed for a member who is off),
   `native=False`, the Announcement card variant, `PlainText` on every card label. Tests:
   `TestTheAnnouncementCard`; the native-push test updated.
7. **Email catalogue was a second hand-maintained list**: a builder without its row was
   delivered but never manageable or copied. Fix: `_register(key, builder, name, ...)` beside
   each builder populates `BUILDERS` and the registry; `check_registry()` stops the backend at
   boot on an unregistered builder, a copyable email that is never queued, or a missing flag.
   Tests: `RegistryTests` (a temporary email registered the way a developer would, saveable,
   applied by `cc_for`, removed cleanly).
8. **Lost update between two administrators on the CC policy.** Fix: optional `version` on
   `PUT /api/v1/email-cc`, 409 when it has moved; the page sends it and reloads on 409.
9. **`scripts/smoke_email.py` copied the environment's standing list**, not the policy. Fix:
   a smoke message copies nobody.
10. **`PUT /screenshots/displays` accepted a client or the release account.** Fix: 403, the
    roles the overview and the choice already exclude.
11. **Configuration-only stream events made every desktop refetch an unchanged schedule.**
    Fix: refetch only when the version moved (one read per desktop per change avoided).
12. **E2E helpers provisioned members with `capture_frequency = 0`**, which
    `GET /screenshots/config` refuses with 500, so no E2E desktop had ever received its
    screenshot configuration. Fix: 10 (the documented default).
13. **Admin pages said "within a few minutes"**; now "within a few seconds while connected,
    within three minutes otherwise".

Recorded, not changed (owner decisions): the 60-second spacing between notifications also
spaces two announcements (`WellbeingService.MIN_SPACING_SECONDS`); screen numbers are the OS
ordering and a replugged monitor may be renumbered (the page shows size and primary flag so an
administrator can recognise it); showing announcements on Monitra's own card accepts the
card's thirty seconds, which DO_NOT_DO asks to be a deliberate choice -- it is the only surface
that can carry the heading and the whole message.

## D. Configuration propagation (rig: real backend, two real desktops, same machine)

| Change | Measured (admin API 200 -> desktop state) |
|---|---|
| Screenshots off / on for A | 0.02-0.03 s / 0.22-0.23 s |
| Five activity changes in a row, then on | 0.01-0.03 s / 0.03 s |
| Notifications off / on for A | 0.20-0.28 s / 0.33 s |
| Size 800x600 to A and B | 0.05-0.06 s / 0.02 s |
| Quality 30, quality 95, two changes before a capture | 0.05 s each |
| Screen 2 off / screen 1 off / both on (pretended screens) | see E2E log; all under the 5 s budget |
| Announcement to a connected desktop | 0.00-0.28 s (first push); a second push within 60 s of the first waits out the spacing rule (measured 51-60 s) |

Method: `_measure` in `desktop/tests/e2e/test_admin_config_e2e.py` pumps the Qt event loop
until the desktop's own service reports the new state. Budget 5 s; worst case under 0.4 s.
Load: 100 streams held open against a two-worker backend; all 50 of the changed member's
streams told within 0.078-0.094 s, the other member's 50 told `concerns: false`; config poll
latency under that load p50 < 1 ms, p95 16 ms (`scratchpad/rig/stream_load_probe.py`).
The backend watcher reads one row per worker every 2 s only while a stream is open (existing
`WatcherCostTests` prove reads do not grow with desktops).

## E. Email service inventory

Fifteen emails, all registered (`backend/app/services/email/messages.py`), all listed by
`GET /api/v1/email-cc`, all covered by the policy except the two private-link emails:
welcome, feedback, feedback_status, release, weekly_report, monthly_report,
manual_time_request, manual_time_receipt, manual_time_decision, monthly_project_summary,
project_budget_alert, client_invitation (locked), client_login_link (locked),
screenshot_notice, member_access. Operator tooling (`smoke_email.py`) now copies nobody;
preview endpoints send nothing; no other producer exists (grep of `backend/`, `api/`, `deploy/`).

**Adding an email in future:** write the builder and template, then one
`_register(TYPE_X, build_x, "Name", "Who it goes to", "When", enabled_setting=...)` beside it.
It appears on the page, in the API and in the schema with no other change; forgetting it stops
the backend at boot and fails `RegistryTests`. An untouched deployment copies its standing list
on the new email (the environment list always did); once a policy has been saved, a new email
is unticked for everyone.

## F. Test evidence (commands run from `backend/`, `desktop/`, `frontend/`)

* `python -m pytest tests/ -q` (backend): **3384 passed, 75 skipped** (skips are the opt-in PG,
  real-SMTP and E2E modules).
* Desktop, one interpreter per module (`run_modules_parallel.py`, 166 modules): **165 OK, 1
  failing**: `tests/test_changelog.py::test_the_repository_changelog_is_releasable_as_it_stands`
  -- pre-existing at baseline (notes under `[Unreleased]` while `version.py` is 1.3.3; a
  version decision for the owner, not code). `test_timer_lifecycle_reliability.py` crashed once
  at baseline when run after other modules (known native segfault, memory note) and passes
  alone (46 passed).
* `python tools/check_architecture.py`: Architecture boundaries OK (152 files).
* `python tests/soak/run_launch_cycles.py --cycles 10`: 10/10 clean (6.6 s each).
* `python tests/soak/run_soak.py --duration 60`: PASS -- no thread growth, no duplicates, queue
  drained, clean shutdown.
* Frontend: `npm run build` OK, `npm run lint` clean (3 pre-existing warnings), vitest admin
  folder: all passing (final count in the run log).
* Real E2E, local rig (PostgreSQL 18 on 127.0.0.1:58432, backend spawned per module,
  `MONITRA_E2E=1`): `desktop/tests/e2e/test_admin_config_e2e.py` -- **8 passed, 1 skipped**
  (the real-second-monitor leg; this machine has one panel) in 164 s, final run 13:49 IST;
  `backend/tests/test_email_cc_smtp_e2e.py` -- **7 passed**, envelopes printed.
  Earlier E2E runs exposed three harness facts worth knowing, none a product defect: a push
  from a previous run is still live for ten minutes and is shown at sign-in (the module now
  clears pushes and uses per-run titles); a desktop started inside the 13:30 lunch reminder's
  grace window shows it first and the 60-second spacing then delays an announcement (the
  module switches the built-in reminders off through the real admin route and restores the
  row); and a "refresh now" that lands while a refresh is in flight was de-duplicated by the
  pool and left to the three-minute timer -- that one was a real gap in the new path and is
  fixed (the in-flight refresh re-issues it on completion; unit-tested for all three owners).
* Screenshot evidence: decoded WebP 800x600 (21 504 B), 1280x720 q30 26 770 B vs q95 76 224 B,
  1500x1500 after two changes; pretended two screens: `display_count` 2 vs 1 and width 1500 vs wider.
* Not covered by any automated run: a real second monitor, a real browser click-through, macOS,
  production.

## G. Database and deployment

* Migrations: `a1c4e8b2d6f0` (merge, no-op). Everything else lives in `system_settings`
  rows written on first use (`desktop_config`) -- no schema change.
* Order: backend first (`alembic upgrade head`, then restart), then frontend, then a desktop
  release. Older desktops keep working at the three-minute poll; older backends leave new
  desktops on the poll (no `config` key, no event).
* No environment variable changes. The boot-time `check_registry()` stops a backend whose
  email builders are inconsistent -- a developer error, not an operational one.
* Rollback: revert the backend commits; the `desktop_config` row is ignored by older code.

## H. Git

Branch `smit`; start `b042593`; commits `aadb316`, `401266b`, `523c43f`, `18139ba`,
`7df7365`, `397f3e1`, `f6ec8eb`, plus the E2E module commit. Nothing pushed or deployed.

## I. Remaining blockers before production

1. No live production verification (owner's call to run the E2E against staging/production
   with a test account; the backend must be deployed first).
2. A real multi-monitor machine has not run the per-screen choice; monitor renumbering on
   replug is a documented limitation.
3. The desktop release must be built, signed (still unsigned, see earlier reports) and
   installed; until then every desktop is on the three-minute poll and the old toast.
4. The changelog check fails on the 1.3.3 `[Unreleased]` notes -- bump the version or move
   the notes before tagging.
5. Announcements are spaced 60 s apart from any other notification -- acceptable by the
   documented policy, worth a line on the admin page if the owner wants it.

## J. Decision

**IMPLEMENTATION VERIFIED, PRODUCTION BEHAVIOR NOT YET VERIFIED.**

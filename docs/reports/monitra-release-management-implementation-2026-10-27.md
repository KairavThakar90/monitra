# Monitra Desktop Release Management: Implementation Report

**Date:** 2026-10-27 · **Branch:** `smit` · **Commits:** `9bce76a` (backend), `0e604f5` (frontend), `8613129` (desktop + docs), plus this report
**Follows:** [monitra-release-management-audit-2026-10-27.md](monitra-release-management-audit-2026-10-27.md) (the mandatory first phase) and [monitra-auto-update-implementation-2026-10-27.md](monitra-auto-update-implementation-2026-10-27.md).

## Executive summary

**Verdict: IMPLEMENTED BUT NOT FULLY PRODUCTION VERIFIED.** The release-management system is built across backend, desktop and admin panel, tested at every layer with the clock injected, and the migration was applied, rolled back and re-applied on a real PostgreSQL. Nothing was pushed, published, deployed, emailed, migrated in production or forced on a user. What remains is exactly what no repository can supply: signed installers, the IIS `.dmg` MIME type on the website, a run on real Windows and Mac machines, and the production credential. The 1.3.2 fleet in the field is unaffected until an administrator publishes a release.

## A. Implementation

### What a release now is

- **`desktop_release_versions`** (new): one record per version with title, notes, target platforms (Windows and/or macOS), mandatory or optional, grace hours (default 24), required minimum version (the floor), effective time, lifecycle (`draft` → `validated` → `published` → `withdrawn`; `superseded` derived) and who did what. The artifact rows (`desktop_releases`, unchanged) stay the unit a client downloads; a CI registration creates the record as an optional draft.
- **`desktop_update_requirements`** (new): the grace period. One row per user × platform × floor, filed by the server the first time it sees that user's desktop below an active floor, stamped `first_seen_at` / `deadline_at` on the server clock. Unique; never extended; `satisfied_at` written only when the server observes the client at or above the floor.
- **`desktop_update_reports`** (new): what each desktop reported about an update attempt, idempotent on a client event id.
- **Migration `f1a7c3e9d2b4`** adds the three tables; downgrade drops them (a running grace period is lost on rollback, documented in the migration).

### Backend (`backend/`)

| Change | File |
|---|---|
| Policy engine: active policy per platform (strictest floor; only while an installable published release exists, which is what makes withdrawal safe), grace-row creation, verdicts, the 426 gate with its exempt-path allow-list, a 30 s policy cache | `app/services/desktop_update_policy.py` |
| Enforcement in the one dependency every protected route uses | `app/core/security.py::get_current_user` |
| Platform/arch headers in the request context | `app/core/request_context.py` |
| Version records: create/update/validate/publish/withdraw, derived `superseded`, detail with stats and audit history; the update check answers `mandatory`, `required_version`, `deadline_at`, `server_time`, `grace_active`, `blocked`; `force_update` for legacy clients only once expired; update-policy view; install reports | `app/services/desktop_release.py` |
| Readiness narrowed to the version's targets | `app/services/desktop_release_policy.py` |
| Routes: `GET /desktop/update-policy`, `POST /desktop/update-reports`, `GET/POST /desktop/releases/versions`, `GET/PATCH /desktop/releases/versions/{v}`, `/validate`, `/withdraw`; publish and create now audited with the actor | `app/api/desktop_release.py` |
| Audit actions `release_created/updated/validated/published/withdrawn` in `activity_logs` (module `system`) | `app/models/activity_log.py` |
| Announcements targeted by last-seen platform; mandatory/grace wording in the email (HTML block and text) | `app/repositories/user.py`, `app/services/email/{workflows,messages}.py`, `app/templates/emails/release.html` |
| Schemas for all of the above | `app/schemas/desktop_release.py` |

Response for a refused request: **HTTP 426** with `detail = {code: "desktop_update_required", message, update_required, required_version, latest_version, installed_version, platform, grace_active, deadline_at, first_seen_at, server_time, policy_url}`.

Paths that stay open past the deadline (recovery and finishing only): `/auth/*`, `/health`, `/desktop/latest-version`, `/desktop/update-policy`, `/desktop/update-reports`, the public download endpoints, `/system/maintenance-status`, `/time-entries/active`, `/time-entries/{id}/{stop,heartbeat,screenshots,unwanted-activity,adjustments,activity/batch,app-usage*}`, `/time-entry-activities/batch`, `/url-usage/batch`, `/time-entry-screenshots/capture-events`, `/idle-periods*`, `/activity-logs/client-events`. Everything else a desktop calls (`/time-entries/start`, projects, tasks, sync revision, manual entries, feedback, notification schedule…) is refused.

### Desktop (`desktop/`)

| Change | File |
|---|---|
| `UpdateRequirement`: the server's instants, remaining time from `server_time` + monotonic elapsed (wall clock only after a restart, clamped at zero), persistence record | `background_services/update/state.py` |
| Requirement kept in `app_state`, loaded at start and dropped only when the running version satisfies it; `blocked_changed` / `requirement_changed` edges; the loop wakes at the deadline (offline or signed out too); "Later" refused once blocked; manual check re-offers a postponed release; durable, bounded install-report queue sent on the task pool and retried on the next check | `background_services/update/update_service.py` |
| `POST /desktop/update-reports` client | `app/updates/service.py` |
| 426 → `updates.check_now()` at most once a minute | `app/api/client.py`, `core/runtime.py` |
| New start / switch / break-out refused while blocked; stop, heartbeat and uploads untouched | `background_services/timer/timer_service.py` |
| Dialog: mandatory-with-grace mode (Later + countdown from the service, deadline in local time), `set_blocked()` withdraws Later live | `ui/update_dialog.py` |
| The update-required page: branding, installed/required/platform/size, notes, progress, verification, errors, Try Again, Download manually; the page replaces the dashboard in the main window's stack | `ui/mandatory_update_screen.py`, `main.py` |
| `update_blocked()`, `update_requirement()`, `update_remaining_seconds()`, `check_for_updates_now(manual=)` | `background_services/public_api.py`, `ui/dashboard_window.py` |

Preserved unchanged: "Checking for updates…", "Monitra X is the latest version.", the "could not check" and "no release information" outcomes, the badge, the reminder policy for optional releases, the download/verify/install path, the restart that keeps the session.

### Frontend (`frontend/`)

Admin → Settings → **Desktop Releases** (`features/admin/AdminDesktopReleases.tsx`, `store/api/desktopReleasesApi.ts`, route and sidebar link): the history table (version, platforms, policy, published, ready, status), a detail with targeted users, grace periods (pending/expired/satisfied), installs by outcome, adoption, the announcement's outbox counts, the installers, the audit history; Validate, Publish (confirmation states the 24-hour rule), Withdraw (confirmation states what happens to running grace periods), Edit; a New release dialog with optional installer entry. `validateVersion` / `validateSha256` added to the validation catalogue in step with the backend's rules.

### Documentation

`docs/Desktop_Release_Runbook.md` §8 (authoritative), `desktop/ARCHITECTURE.md`, `desktop/DO_NOT_DO.md` (five new entries), `CLAUDE.md` §5.8, `docs/postman/collection.json` (Desktop Releases folder), `desktop/CHANGELOG.md` `[Unreleased]`.

### Configuration changes required

None new. The existing ones from the auto-update report still apply (`DESKTOP_DOWNLOAD_URL_PREFIXES`, `DESKTOP_DOWNLOAD_ALLOWED_HOSTS`, `RELEASE_EMAIL_ENABLED`, `RELEASE_EMAIL_TEST_RECIPIENTS`, `MONITRA_RELEASE_CREDENTIAL`). Nothing was changed on any server.

## B. Commit information

| Item | Value |
|---|---|
| Branch | `smit` |
| Commits | `9bce76a` backend, `0e604f5` frontend, `8613129` desktop + docs, then this report |
| Working tree | clean after the report commit |
| Pushed | **No.** Not authorised. |

## C. Test results

All on this machine (Windows 11, repo-root `.venv`, Python 3.12.10), against the committed code.

| Suite | Command | Result |
|---|---|---|
| Backend unit + integration (whole suite, incl. 90 new tests in `test_desktop_release_management.py`: lifecycle, policy engine, grace creation/expiry/idempotency, server-clock authority, platform divergence, withdrawal and corrective releases, the gate's allow-list and 426 body, bypass over real HTTP with a real token, legacy client, email targeting and wording, reports, routes and authorization) | `cd backend && python -m pytest tests/ -q` | **3195 passed, 68 skipped, 1921 subtests passed** in 72.8 s (final run; the one extra skip is the opt-in PostgreSQL race test without `MONITRA_PG_TEST_URL`) |
| Database integration on real PostgreSQL | `migrate_check.py` (scratchpad) on a throwaway cluster | `alembic upgrade head` → 3 tables, indexes, uniques, checks as declared; duplicate grace row refused (`IntegrityError`); unknown outcome refused; `downgrade e9f5a3b7c2d4` → tables gone; `upgrade head` again → present; `alembic current` = `f1a7c3e9d2b4 (head)` |
| Concurrent grace-period creation on real PostgreSQL | `MONITRA_PG_TEST_URL=… pytest tests/test_desktop_release_management_pg.py` | **1 passed**: 8 racing first encounters → 1 row, 1 deadline, 1 `created` |
| Desktop (whole suite, one process per module; incl. 35 new tests in `test_update_requirement.py` and 2 updated in the hardening/installer modules) | `cd desktop && python tools/run_tests_per_module.py` | **180 modules in 994 s: 3951 passed, 0 failed, 0 errors, 108 skipped** |
| Architecture boundaries | `python tools/check_architecture.py` | `Architecture boundaries OK (150 files scanned).` |
| Launch cycles | `python tests/soak/run_launch_cycles.py --cycles 10` | `10/10 cycles clean; launch+quit 6.6-6.7s` |
| Soak | `python tests/soak/run_soak.py --duration 60` | `PASS: no thread growth, no duplicates, queue drained, clean shutdown.` |
| Frontend (whole suite, incl. 9 new in `desktopReleases.test.tsx`) | `cd frontend && npx vitest run` | **86 files, 1370 passed** |
| Frontend build + lint | `npm run build && npm run lint` | build OK (`tsc -b`, vite); lint exit 0, 30 pre-existing warnings, none in the new files |
| Email | the backend suite above (targeting by platform, both-platform includes unknown, dedupe per user per version, draft never announced, outbox counts per version, mandatory wording, download link is the web page) | PASS; no email sent (`RELEASE_EMAIL_ENABLED` off in tests) |

### End-to-end scenarios (automated; the test that proves each)

| # | Scenario | Evidence |
|---|---|---|
| 1 | No update: current client told nothing | `UpdateCheckAnswerTests::test_a_current_client_is_told_nothing_is_pending…` |
| 2 | Optional update: offered, blocks nobody | `test_an_optional_release_blocks_nobody_however_old_the_client`, dialog tests |
| 3 | Mandatory both-platform publish → grace row + email | `test_first_encounter_files_the_grace_row…`, `AnnouncementTargetingTests` |
| 4 | Later keeps working; deadline survives restart | desktop `test_a_restart_inside_the_grace_keeps_the_deadline_without_a_backend`, backend `test_the_deadline_never_moves…` |
| 5 | Grace expires → page + backend refusal | `test_after_the_deadline_a_protected_request_is_refused_with_426`, `TestMainWindowGate` |
| 6 | App stays open → blocks at the deadline | `test_the_block_happens_at_the_deadline_while_the_app_stays_open`, `test_the_loop_wakes_for_the_deadline` |
| 7 / 8 | Windows-only / macOS-only targeting | `test_a_windows_only_mandatory_release_leaves_macs_alone`, `…macos_only…`, email targeting tests |
| 9 | Offline during publication | grace starts at the first request after reconnection (`first_seen_at`), dedupe per user per version |
| 10 / 11 | Interrupted download, bad checksum/signature | existing hardening tests (unchanged) + report queued (`test_a_failed_attempt_is_reported_with_its_category`) |
| 12 | Install succeeds → requirement cleared by the running version | `test_installing_the_required_version_clears_the_requirement`, server `_settle` |
| 13 | Install fails → requirement stands | `test_a_failed_installer_is_reported_as_failed_never_success` |
| 14 | Active timer | `test_a_running_timer_keeps_running_through_the_block`, stop/heartbeat/uploads exempt |
| 15 | Bypass attempt | `GateOverHttpTests` (real token, skipped UI) |
| 16 | Platform version divergence | `test_a_higher_windows_version_does_not_satisfy_or_invalidate_a_macos_policy` |
| 17 | Legacy client | `test_a_legacy_client_that_cannot_show_the_screen_is_still_refused`, `force_update` only once expired |
| 18 | No active restriction | `test_no_active_policy_lets_everything_through` |

## D. Real-world verification (each stated independently)

| Item | Status |
|---|---|
| Windows installer download from the website | NOT RE-RUN today; PASS on 2026-10-27 earlier (see the auto-update report) |
| macOS installer download | **FAIL** (IIS 404 for `.dmg`, unchanged) |
| Windows signature verification | NOT VERIFIED (no certificate; code path unit-tested against real signed/unsigned binaries) |
| macOS signature verification | NOT VERIFIED (no certificate, no Mac) |
| Actual Windows installation of an update | NOT VERIFIED on this build (the 1.3.1→1.3.2 hidden-desktop run predates this work) |
| Actual macOS installation | NOT VERIFIED |
| Version verification after installation | unit-tested (`_report_previous_install`, `_load_requirement`); NOT VERIFIED on a real install |
| Real email-provider integration | NOT VERIFIED (no email sent; outbox tested with a recording provider) |
| Live backend policy enforcement | NOT VERIFIED on production; verified over real HTTP against the local app with a real JWT |
| Real active-timer safety | NOT VERIFIED on a real display; verified against the live runtime in tests |
| Offline-to-online recovery | verified in tests (clock injected); NOT VERIFIED on a real network outage |
| Real-device matrix (Win 10, Win 11, Intel Mac, Apple Silicon) | **BLOCKED** (no devices, no signing) |

## E. Remaining blockers

1. **Unsigned installers.** A production client refuses an unsigned installer by design, so no automatic update can complete. Needs certificates and the CI secrets (`WINDOWS_CERTIFICATE*`, `MACOS_*`) and the client pins. Blocks activation.
2. **IIS serves no `.dmg`.** Every Mac download fails. Needs the MIME type on the web server (`Desktop_Artifact_Hosting.md` §7.7), then `verify_download_urls.py` → 3 × OK. Blocks activation for macOS.
3. **Production migration not applied.** `alembic upgrade head` on the production database, with a backup first. Needs the owner on the server. Blocks activation.
4. **Release credential refused by production.** Needs rotation and the repository secret. Blocks CI registration, not the admin panel.
5. **No real-device run.** Needs one Windows machine with an installed 1.3.2 and both Mac architectures with standard accounts. Blocks calling the update path verified.
6. **Clients ≤ 1.3.2 cannot show the grace UI.** They get an ordinary offer, then the existing mandatory dialog at the deadline, and the 426 refusal; the way out is the download page. Does not block activation; the first mandatory release should carry a long grace period.
7. **Known limitation, by design:** the platform and version a desktop claims are self-reported. A claim of the *other* supported platform is subject to that platform's own policy (a both-platform mandatory release closes it); unsupported or missing claims are held to every policy. A modified client can lie about its version; the gate moves the honest fleet, it is not a defence against a hostile user.

## F. Production activation checklist

1. Owner: back up the production database; `cd backend && alembic upgrade head` (one revision, `f1a7c3e9d2b4`); confirm `alembic current`.
2. Owner: deploy the backend and the frontend from `smit` after merge; confirm `GET /desktop/releases/versions` answers an administrator and `GET /desktop/update-policy` answers a desktop.
3. Owner: IIS MIME type for `.dmg`; `verify_download_urls.py` → three OK lines.
4. Owner: signing certificates in CI; set `policy.WINDOWS_SIGNER_PINS` / `MACOS_TEAM_ID_PINS`; keep `DESKTOP_REQUIRE_SIGNED_RELEASES=true`.
5. Build 1.3.4 (first version with the grace UI) from `main`; register; copy the installers to the website; bump `DOWNLOAD_VERSION`.
6. Rehearse: `RELEASE_EMAIL_TEST_RECIPIENTS` set, publish as **optional** first; on a pilot machine run "Check for Updates", Update Now, relaunch; check the detail page's install reports.
7. Only then, with the owner's explicit decision: edit the version record to mandatory, grace 24 h (or longer for the first one), publish; watch grace periods and adoption on the detail page; withdraw and publish a corrective release with the same floor if anything goes wrong (runbook §8.4).
8. Never set a mandatory policy while step 3 or 4 is outstanding: a client that cannot install the update would be blocked with only the manual path.

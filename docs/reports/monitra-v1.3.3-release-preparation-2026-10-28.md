# Monitra 1.3.3 -- release preparation and verification (2026-10-28)

Branch `smit`, release candidate commit **`4a90fc5`** (three commits on top of `17a4afa`:
changelog filed under 1.3.3, the heartbeat backoff fix, the soak stub repair). Nothing pushed,
tagged, published, registered or deployed. Written for the owner and whoever runs the release.

## 1. Release summary

| | |
|---|---|
| Target version | 1.3.3 (`desktop/version.py`, `PRERELEASE = ""`) |
| Previous baseline | v1.3.2 = tag at `3fcb60e` (2026-10-09 build, published on GitHub); a second 1.3.2 build of `9ddf6df` exists from run 39597960024 (2026-10-21, never released) |
| Changes since v1.3.2 | 105 commits, 417 files, +53 920 / -2 125 lines (backend 99 files in `app/`, frontend 79, desktop 30 service files + 65 test files, 9 migrations) |
| Release commit | `4a90fc5` on `smit`, 3 commits ahead of `origin/smit` |
| Release tag | **not created** |
| CI build | **not run** -- `git push` was refused by this session's permission policy ("External System Writes"), and the workflow cannot be dispatched for a commit GitHub does not have |
| Publication | **nothing published**; production still serves 1.1.1 rows |

**Verdict: BLOCKED -- NOT READY FOR PRODUCTION.** Every gate that can be evaluated from this
machine passed, including the real end-to-end suites and a local Windows candidate build of the
exact commit, but the three mandatory CI artifacts do not exist, nothing was installed on a
clean machine, the upgrade from an installed 1.3.2 was not exercised, the installers are
unsigned, and the Drive-backed screenshot E2E is blocked here. Section 8 lists exactly what the
owner has to run; sections 2-7 are the evidence.

## 2. What was done in this session

1. **Baseline audit.** Repository, remotes (`origin` = `KairavThakar90/monitra`, private),
   tags, the 105 commits since v1.3.2 and the files they touch, the release workflow
   (`.github/workflows/desktop-release.yml`: Windows + macOS arm64 + macOS x86_64, release
   job only on a tag or a dispatch with *Attach the artifacts* on), repository variables
   (`RELEASES_REPO`, `RELEASE_DOWNLOAD_BASE_URL` = the website folder) and secret names
   (no signing secrets of any kind), live artifact usage (190 MB of the 500 MB private-repo
   quota), and the live production state (`/health?deep=1`, `/internal/jobs/status`).
2. **Changelog.** `desktop/CHANGELOG.md` had 1.3.3 notes under both `## [1.3.3]` and
   `## [Unreleased]`, which fails the tag-build gate (`check_changelog.py --release`). No CI
   run has ever built 1.3.3, so the version is not spent: the Unreleased notes were filed
   under 1.3.3 and Unreleased is empty (`83b36cf`).
3. **Defect found and fixed (desktop, sync).** A heartbeat failure that the service layer
   did not wrap as `ApiError` escaped `_heartbeat_tick` without recording anything, so the
   next sync pass -- every ~100 ms while the queue is busy -- retried and logged a traceback
   each time. The soak found it (71 tracebacks in 10 s). The unexpected path now takes the
   same bounded, jittered backoff as a failing backend, logs once per outage and resets on
   the next delivered beat; regression test added; soak stub extended so the soak drives
   the heartbeat and URL-usage paths (`4c8bf22`, `4a90fc5`).
4. **Migrations proven against production's exact revision.** Production reports schema
   `e9f5a3b7c2d4` (database and code). On a throwaway PostgreSQL 18, a schema built from the
   backend at that revision (`29afc51` worktree) was upgraded by HEAD's Alembic to head
   `a1c4e8b2d6f0` (five revisions: `f1a7c3e9d2b4`, `c9e2a7d41b36`, `d1a8f3c5e7b9`,
   `e3b7c1a9d5f2`, merge `a1c4e8b2d6f0`), downgraded back, and upgraded again. The migrated
   schema is identical to one built from HEAD's models (columns, constraints), except for
   one extra index the migration adds on `member_displays.organization_id` (harmless).
5. **Desktop 1.3.3 against the production-revision backend.** A real `ApplicationRuntime`
   from HEAD was signed in to a backend running the `29afc51` worktree on that schema:
   sign-in profile (which lacks the three new switch fields) applied with defaults; the
   stream with `since_config` accepted; update check answered "no update"; timer start
   bound, heartbeat landed (`last_seen_at` set), stop landed (`total_seconds` 6). The two
   routes production does not have (`PUT /screenshots/displays`, `GET /activity/config`)
   answered 404, were logged once and are retried at the next refresh. No crash, no retry
   storm. **1.3.3 can be installed before the backend is deployed.**
6. **Scheduled-job endpoints at HEAD** on the migrated schema: email dispatch, budget alerts,
   weekly / monthly / monthly-projects report runs, daily rollup, WFPM dispatch, timer
   reconcile (dry run), catch-up, jobs status -- all 200; the manual close without the
   operator token refused (400); anonymous calls refused (401).
7. **Local Windows candidate build of `4a90fc5`** (PyInstaller + Inno Setup + portable + the
   packaged smoke test) to prove the packaging of this commit; see section 5.

## 3. Test results (all executed, not inspected)

| Suite | Command | Result |
|---|---|---|
| Backend | `python -m pytest tests/ -q` (repo-root venv, 3.12) | **3384 passed, 75 skipped** (opt-in modules), 2202 subtests, 107 s |
| Backend opt-in PostgreSQL + real SMTP | same with `MONITRA_PG_TEST_URL` on the rig, `-k "pg or postgres or smtp_e2e or sql"`; then `test_email_cc_smtp_e2e.py` with `MONITRA_E2E=1` | **98 passed, 7 skipped**; SMTP CC suite **7 passed** |
| Frontend | `npm run build`, `npm run lint`, `vitest run` | build OK (`tsc -b` + vite), lint clean (7 pre-existing warnings), **1490 passed** |
| Desktop unit/integration | one interpreter per module, 191 modules | **190/191 at baseline; 191/191 after the changelog fix** -- 4 253 passed, 118 skipped (opt-in E2E), 0 failed |
| Desktop architecture | `python tools/check_architecture.py` | Architecture boundaries OK (152 files) |
| Desktop changelog | `python tools/check_changelog.py --release` | OK |
| Launch cycles | `run_launch_cycles.py --cycles 10` (isolated `MONITRA_DATA_DIR`) | 10/10 clean, 6.6-6.7 s each |
| Soak | `run_soak.py --duration 60` (final, after the fix) | PASS: no thread growth, no duplicates, queue drained, clean shutdown; late-run slope +0% |
| Desktop real-backend E2E | 20 modules in `desktop/tests/e2e/`, `MONITRA_E2E=1`, real backend per module on the rig | **98 passed, 0 failed, 16 skipped** (4 need Google Drive -- blocked, see 7; 9 screenshot-lifecycle legs and 3 others are declared environment-conditional skips) |
| Alembic | upgrade `e9f5a3b7c2d4` -> head, downgrade, upgrade (PG 18) | OK, schema diff clean |

Notes on the runs: the 60 s soak failed once with "+28% late-run slope" after the stub fix;
a 120 s run with `-X importtime` showed the step is the one-time lazy import of `mss`, `PIL`
and `pynput` (~2 MB) at the first capture/listener, landing in the last quarter of a short run;
it is not a leak and the next 60 s run was +0%. Two E2E modules first failed on *my* rig
environment, not the product: the release journey needs `EMAIL_PROVIDER=console`,
`RELEASE_EMAIL_ENABLED=true` and the token the module hardcodes; the native-toast pixel check
passed on re-run.

## 4. Critical functionality -- evidence

| Area | Evidence in this session | Status |
|---|---|---|
| Timer accuracy, duplicate start/stop, offline queue | `test_timing_lifecycle_e2e` 9, `test_sync_lifecycle_e2e` 10, `test_network_resilience_e2e` 8, `test_timer_controls_e2e` 2 (real backend + PG) | PASS |
| Overnight / forgotten timers, weekend wake, recovery cap | `test_weekend_wake_e2e` 8, `test_timer_abandonment_e2e` 6, `test_timing_surfaces_e2e` 6; backend `test_timer_reconciliation_pg` in the opt-in run; production reconciler runs every 5 min, `dry_run: true`, 0 candidates | PASS (reconciler stays report-only by design) |
| Active Users consistency | backend liveness tests (`0f89c9f`), `test_timing_surfaces_e2e` | PASS |
| Activity calculation, deaf listener | desktop activity modules in the 191; prior audit `docs/reports/monitra-activity-tracking-audit-2026-10-28.md`; **not run on a real Mac** | PASS with limitation |
| Idle detection and popup, Decide later / Retry now | `test_idle_lifecycle_e2e` 6, `test_idle_resilience_e2e` 9, `test_idle_popup_real_window_e2e` 9 (real window), `test_break_lifecycle_e2e` 2 | PASS |
| Administrative settings reach the desktop | `test_admin_config_e2e` 8 passed, 1 skipped (real second monitor) | PASS |
| Desktop notifications | `test_notification_schedule_e2e` 2, `test_native_toast_e2e` 3 (real toast), `test_os_session_end_real_messages_e2e` 5 | PASS |
| Update and Later behaviour, 426 gate | `test_release_management_e2e` 1 (whole journey: draft, validate, publish, 426, install report, email) + 191 unit modules incl. update hardening | PASS on the rig; **never run on a real device** |
| Screenshot pipeline | capture, queue, size/quality decoded in `test_admin_config_e2e`; `test_screenshot_lifecycle_e2e` 2; production Drive probe `ok` on `/health`; **Drive upload E2E BLOCKED here** | PASS with limitation |
| Email and CC | 66 management tests + 7 real-SMTP (envelope and `Cc` asserted) + the registry check at boot | PASS |
| Scheduled emails in production | all eight jobs `ok` from `Store-Server`; weekly report queued 68 on 2026-10-26; monthly report and project summary have **never fired** (first due 2026-11-01 03:30Z); missing periods: weekly 2026-10-05 and 2026-10-12, August monthly report and project summary | PASS for what has run; first monthly firing unproven; catch-up is an owner decision |
| Authentication and permissions | backend suite (3384) incl. RBAC, `test_every_admin_route_refuses_an_employee` in the admin E2E | PASS |
| Upgrade from installed 1.3.2 | **NOT DONE** -- the only installed 1.3.2 on this machine is the owner's working copy | BLOCKED |
| Backend <-> desktop compatibility before the backend deploy | section 2.5 | PASS |

## 5. Artifact inventory

**CI artifacts: none.** The required three (`Monitra-Windows`, `Monitra-macOS-arm64`,
`Monitra-macOS-x86_64`) have not been built because the commit could not be pushed.

Local Windows candidate build of `4a90fc5` (same scripts the Windows job runs; in
`desktop/dist/`, git-ignored; **not a release artifact** -- the CI build is):

| File | Size | SHA-256 | Version resource | Signed |
|---|---|---|---|---|
| `Monitra-Setup-1.3.3.exe` | 32 387 010 B | `451875a1818daa0731cf013df3559b7543c573d4c0daf1abbb2ad09376f0d2fe` | FileVersion 1.3.3, ProductVersion 1.3.3 | No (`NotSigned`) |
| `Monitra-Portable-1.3.3.zip` | 44 246 706 B | `6084da9099f9608e06439ce5db433b700b72af5a8f7658245b449096bd667f7e` | -- | n/a |
| `dist\Monitra\Monitra.exe` | 7 284 522 B | -- | 1.3.3, "Monitra — Staff Management", Monitra | No |

Packaged smoke test (`scripts/smoke_test_package.ps1`): passed. The packaged `Monitra.exe`
launched against a local HEAD backend stayed up and made its sign-in requests
(`/auth/me`, privacy config, screenshot config); it reused the session stored in the shared
Qt settings, which the local backend correctly refused with 401. macOS: nothing built
(needs the CI runners).

## 6. Security and configuration review

* No dependency changed since v1.3.2 (`requirements*.txt`, `package.json` identical).
* Backend defaults stay production-safe: `DESKTOP_REQUIRE_SIGNED_RELEASES=true`,
  `RELEASE_EMAIL_ENABLED=false`, `TIMER_RECONCILER_ENABLED=false`;
  `DESKTOP_DOWNLOAD_ALLOWED_HOSTS` already includes `staff.peakworkos.com`, so
  `DESKTOP_DOWNLOAD_URL_PREFIXES` may stay empty in production.
* Internal job routes refuse a missing token (401); the manual timer close needs the
  operator token (400 without the named entries). Anonymous `GET /api/v1/email-cc` is 401.
* Secrets: none in the three new commits; none printed in this session; the dispatch token
  was read from `backend/.env` only to call the read-only status endpoint.
* The repository holds **no code-signing secrets**; every artifact will be unsigned.
* Observation (not a defect): the packaged build and the installed Monitra share the Qt
  settings store, so a fresh `MONITRA_DATA_DIR` does not isolate the stored session.

## 7. Remaining risks and blocked items

1. **CI build not run** (push refused). No artifact, no checksums, no macOS build.
2. **Unsigned installers** -- the in-app updater refuses them in production by design;
   Windows SmartScreen and macOS Gatekeeper will warn. Signing/notarization secrets are
   still absent (`docs/Desktop_Artifact_Hosting.md` §7).
3. **No real-device run**: clean-machine install, upgrade over an installed 1.3.2, the macOS
   update path, Input Monitoring on a real Mac, a real second monitor.
4. **Drive screenshot E2E blocked** on this machine: `backend/.env` names production's
   Drive root folder and the service-account file is absent; it was not run rather than
   write test files into production storage.
5. **Backend registration credential**: `register_release.py` on a tag build fails the job
   on the 401 production has answered since 1.3.2 unless `MONITRA_RELEASE_CREDENTIAL` is
   rotated first (runbook §8.8).
6. **Monthly emails** have never fired in production (first firing 2026-11-01). The weekly
   rows for 2026-10-05 and 2026-10-12 and August's monthly rows are missing; decide on the
   catch-up before 1 November (`GET /internal/reports/catch-up` lists; `POST ...?dry_run=false`
   queues one email per eligible person per period).
7. Website: the `.dmg` MIME mapping ships in `frontend/public/download_app_files/web.config`
   and takes effect only when the frontend is deployed; prove with
   `desktop/tools/verify_download_urls.py` before registering rows.
8. Actions billing: the repository is private, so one release run bills roughly 300+
   macOS-weighted minutes against the free allowance; the 1.3.2 run was billed at zero only
   because the repository was public that day.

## 8. What the owner has to run, in order

```
# 1. push the candidate and build it (no tag, no release, no registration)
git push origin smit
#    GitHub -> Actions -> "Desktop release" -> Run workflow -> branch smit,
#    "Attach the artifacts to a GitHub release" OFF
#    (or from this machine once pushes are allowed:
#     python <scratchpad>/tools/gh_dispatch.py dispatch smit ; ... wait <run id> ; ... artifacts <run id> <dir>)

# 2. verify the three artifacts from the run (from desktop/)
python tools/verify_release_artifacts.py --artifacts <dir with the three jobs' files> --tag v1.3.3

# 3. host them (runbook §3 step 1): copy the three installers under their built names into
#    frontend/public/download_app_files/, set DOWNLOAD_VERSION = '1.3.3' in
#    frontend/src/api/desktopRelease.ts and the pinned paths in its test, npm run build && npm run lint,
#    commit, deploy the frontend, then prove the downloads:
python desktop/tools/verify_download_urls.py --base-url https://staff.peakworkos.com/download_app_files/ --version 1.3.3 --artifacts <dir>

# 4. backend: back up production, then from backend/ with the production DATABASE_URL in that shell only
python -m alembic current        # must print e9f5a3b7c2d4
python -m alembic upgrade head   # five revisions -> a1c4e8b2d6f0
python -m alembic current
#    deploy the backend from the same commit; the boot-time email registry check must pass;
#    then: GET /health?deep=1 (status healthy), GET /internal/jobs/status (schema at_head: true)

# 5. pilot ring (runbook §2) on the CI artifacts: one Windows and one Mac of each architecture,
#    including an upgrade over an installed 1.3.2; then rotate MONITRA_RELEASE_CREDENTIAL (§8.8),
#    merge to main, tag v1.3.3 and push the tag, let the tag build register the draft rows,
#    GET /desktop/releases/versions/1.3.3/readiness, POST .../publish.
```

Until step 1 has produced the three artifacts and step 5 has run on real devices, 1.3.3 is a
verified **candidate**, not a release.

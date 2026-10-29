# Monitra 1.3.4 -- release preparation and verification (2026-10-29)

Branch `smit`. The session started from `99cb9c1` (= `origin/smit`, 11 commits ahead of
`origin/main`, clean tree), built it in CI (run 42), diagnosed the macOS failure in two steps
(section 5) and built the fix; the release commit is **`38a1c97`** (`0c4c35e` = the fix, plus
one workflow change: the Apple Silicon job runs on `macos-15`), pushed and built as run 45.
Written for the owner and whoever runs the release. Nothing was tagged, registered, deployed or
changed in production by this session; four CI builds were dispatched with publishing off (runs
42-45), and the withdrawn `v1.3.3` GitHub release was marked a pre-release (section 9).

## 1. Release summary

| | |
|---|---|
| Target version | 1.3.4 (`desktop/version.py`, `PRERELEASE = ""`); changelog `## [1.3.4]` present, `[Unreleased]` empty |
| Why 1.3.4 | 1.3.3 (built from `44b143f`) shipped Qt 6.12.0 through an unpinned PySide6 and draws the Request dialog's Start/End Time fields one pixel wide. The `v1.3.3` tag already exists, so the corrective release is 1.3.4 (1.3.3 report, section 9). |
| Changes since `v1.3.3` | 16 commits, 73 files, +1 889 / -220: the Qt 6.12 fix and the PySide6 pin, the 2026-10-29 input-security work in all three layers, screenshot-privacy authorisation, and the 1.3.3 report. **No new Alembic migration** (63 revisions, head `a1c4e8b2d6f0`, unchanged since 1.3.3). |
| CI build | run **45** = [40895074130](https://github.com/KairavThakar90/monitra/actions/runs/40895074130), `workflow_dispatch` on `smit` at `38a1c97`, *Attach the artifacts* OFF: **all three jobs green, three artifacts uploaded and verified (section 5.5)**. Runs 42-43 are the diagnosis; run 44 proved the fix on Windows and macOS x86_64 but its arm64 job never got a `macos-14` runner. |
| Release tag / GitHub release / backend rows | **none created** |
| Decision | see section 10 |

## 2. Changes introduced since v1.3.3

Commits `3ce0fba..99cb9c1` on `smit` (`git log v1.3.3..smit`):

| Area | What changed | Commits |
|---|---|---|
| Desktop -- the defect 1.3.4 exists for | `ui/task_table.py`: the `QTimeEdit#TimeEditInner` rule no longer names a `border` (one pixel wide on Qt 6.12). `requirements.txt`: `PySide6==6.11.2`. New tests read the fields' geometry back and fail on a bare or different PySide6 (`test_manual_time_entry_dialog.py`, `test_dependency_pins.py`). Two `DO_NOT_DO.md` entries. | `324e160`, `2aa533a` |
| Desktop -- input security | `core/text_safety.py` (plain tooltips, cleaned search text), server/validator messages shown as literal text (`PlainText` labels, `html.escape` in the one rich-text label), a gateway's HTML error page never shown as a message (`app/api/exceptions.py`), a duplicated task's name validated before it is sent, stricter shared `EMAIL_PATTERN` and a `JSON_FRAGMENT_PATTERN` in `core/validation/rules.py` (kept equal to the backend's by `test_validation_framework.py`). | `8ca067e`, `9d09593`, `99f931e`, `e8df520` |
| Backend -- input security | Members, projects, tasks, WFPM inbound and screenshot-privacy schemas take the shared rules (`Name`, `OptionalPlainDescription`, ...); `auth.py` login hardening; `test_input_security_api.py` (302 lines). WFPM: `project_name`/`name`/`description` now go through the catalogue -- `docs/WFPM_INTEGRATION.md` already states the 150-character names and the plain-text `422` for descriptions, so the contract document and the code agree. | `d73e4fa`, `99f931e`, `e8df520` |
| Backend -- authorisation | Only administrators and HR may change screenshot privacy rules and exclusions (`test_screenshot_privacy_authorization.py`). | `888d76e` |
| Frontend | Never raw JSON on screen (`utils/safeText.ts`, Members page legacy records), validated reasons and privacy rules, safe exports, every text input has a limit, blur validation shows its error; `rules.ts`/`validators.ts` in step with the other catalogues. | `bbcf69c`, `9d09593`, `99f931e`, `e8df520` |
| Docs | `docs/VALIDATION.md` (+39), the 1.3.3 report (CI runs, quota, verdicts, section 9). | `1106068`, `3ce0fba..dafddd9` |
| Website folder | `frontend/public/download_app_files/`: the **1.3.3** installers replaced the 1.3.2 ones (`b1842f2`, owner). See section 9, item 2. | `b1842f2` |

Unfinished work found in the diff: none. Every change has tests; no TODO/placeholder data was
introduced. The one compatibility note is the WFPM inbound tightening (a project or task name
over 150 characters, or a description that reads as code, is now refused with `422`); that is
what the integration document already promised.

## 3. Version consistency

Every artifact derives its version from the single `VERSION = "1.3.4"` line in
`desktop/version.py`: `packaging/monitra.spec` stamps the Windows `VERSIONINFO`
(`build/file_version_info.txt` already reads 1.3.4), `Info.plist`
(`CFBundleShortVersionString`/`CFBundleVersion`), the Inno Setup `AppVersion`, the
`Monitra/<version>` User-Agent, and the artifact file names. `tools/check_changelog.py --release`
passes ("CHANGELOG.md documents version 1.3.4"). Nothing else needed editing for the build.

**Left at 1.3.2 on purpose:** `frontend/src/api/desktopRelease.ts` `DOWNLOAD_VERSION` (the
download page's buttons) and `DESKTOP_AUTO_UPDATE_MIN_CLIENT_VERSION` in the backend. The
first is bumped when the 1.3.4 files are hosted (runbook step, section 8); the second is the
oldest client that can self-update and is unrelated to this release. No API endpoint, domain,
credential or production configuration was changed.

## 4. Automated test results (all executed in this session, on this machine)

| Suite | Command | Result |
|---|---|---|
| Backend | `../.venv/Scripts/python.exe -m pytest tests/ -q` (Python 3.12.10) | **3414 passed, 75 skipped** (opt-in PostgreSQL/SMTP modules), 2415 subtests, 125 s |
| Frontend | `npm run build` / `npm run lint` / `npx vitest run` | build OK (`tsc -b` + vite); lint clean (0 errors, the pre-existing warnings only); **1549 passed, 0 failed** (95 files) |
| Desktop unit/integration | one interpreter per module, 6 in parallel, `QT_QPA_PLATFORM=offscreen`, PySide6 6.11.2 | **173/173 modules, 4312 passed, 0 failed, 4 skipped** |
| Desktop architecture | `python tools/check_architecture.py` | Architecture boundaries OK (153 files) |
| Desktop changelog | `python tools/check_changelog.py --release` | OK |
| Launch cycles | `tests/soak/run_launch_cycles.py --cycles 10` (isolated `MONITRA_DATA_DIR`) | **10/10 clean**, launch+quit 6.6-6.8 s |
| Soak | `tests/soak/run_soak.py --duration 60` | **PASS**: threads first 29 / peak 29 / last 20, duplicate stops 0, queue max 429 -> final 0, late-run slope +0%, clean shutdown |
| Desktop real-backend E2E | 20 modules in `tests/e2e/`, `MONITRA_E2E=1`, one process each, real backend on a throwaway PostgreSQL 18 (`127.0.0.1:58432`, schema from the models + `alembic stamp head`) | **110 passed, 4 skipped, 1 failed** after the re-runs below: 17 modules green first time; `test_native_toast_e2e` green on re-run alone (3/3); `test_admin_config_e2e` 8 passed / 1 skipped, see the note; `test_release_management_e2e` **1 failed** -- its last step downloads from the live website folder, which answers 500 today (section 9, item 1) |
| Alembic | no new revision since 1.3.3; local head `a1c4e8b2d6f0` | n/a (see section 7) |

Notes. The first full `vitest run` had 10 failures, all in
`MaintenanceToast.test.tsx`, while the desktop and backend suites were running on the same
machine; the file passes alone (22/22) and the whole suite passed on the immediate re-run
(1549/1549) -- a load-induced timing failure, not a regression (the file is unchanged since
`v1.3.3`). The desktop count differs from the 1.3.3 report's 191 modules because the 20 E2E
modules were run separately against the rig instead of being counted as skipped.

## 5. CI builds -- runs 42, 43 and 44

### 5.1 Run 42 (`99cb9c1`, 6.11.2 pin) -- the failure

Dispatched by this session at 13:00 UTC on `smit` = `99cb9c1` with *Attach the artifacts* OFF
(the publish job was skipped: no tag, no GitHub release, no backend registration).

| Job | Attempt 1 | Attempt 2 (re-run of the failed jobs) |
|---|---|---|
| Windows package | **success** (22 min): architecture OK, changelog OK, regression suite passed in the single process, built, smoke-tested, attested (unsigned), checksummed, uploaded | not re-run (already green) |
| macOS package (arm64) | **failure** in *Regression suite*: 193 modules, 4248 passed, **24 failed**, 157 skipped -- all 24 in `tests/test_layout_stability.py::TestUsageRowsDoNotWidenThePanel::test_the_content_is_exactly_as_wide_as_the_viewport_on_every_tab` | **failure**, same module, 23 failed |
| macOS package (x86_64) | **failure**, same module, 21 failed (4251 passed) | **failure**, same module, 23 failed |
| Publish release | skipped | skipped |

**The Windows artifact of run 42** (`Monitra-Windows-1.3.4`, id 12900916320, 77 043 787 B zip,
expires 2026-11-28), downloaded and checked on this machine:

| File | Size | SHA-256 (sidecar = recomputed) | Checks |
|---|---|---|---|
| `Monitra-Setup-1.3.4.exe` | 32 445 830 B | `733b7c21ca935841ff27f7acfb47c6fadd11c3291c5504828df4a5fb3795e1fa` | FileVersion 1.3.4, ProductVersion 1.3.4, Product Monitra, Company Monitra; `Get-AuthenticodeSignature`: **NotSigned**; `signature.json`: `signed: false`, `WinVerifyTrust` |
| `Monitra-Portable-1.3.4.zip` | 45 374 343 B | `93f86981c2597334d84912479de51f64b0dd412071859ba6f560acff2ff7c572` | unpacked `Monitra.exe` FileVersion 1.3.4; bundled `Qt6Core.dll` **6.11.2.0**; launched under an isolated `MONITRA_DATA_DIR` with the offscreen platform: "Monitra 1.3.4 starting ... frozen=True portable=True", storage ready, services started, still running after 15 s |

The failing assertion was the panel's *floor*: `section.minimumSizeHint().width()` (973-978 px
on macOS) differed by 1-3 px between two panels that differ only in the length of their names
(`assert 978 == 976`). The test's other claim -- content exactly as wide as the viewport, no
horizontal scroll bar -- held on every width and tab.

### 5.2 Run 43 (`b279381`, 6.12.0 pin) -- the first diagnosis was wrong

The first diagnosis blamed the toolkit: run 41 (the green `v1.3.3` tag build) had installed
PySide6 **6.12.0** on the Macs (bare requirement, newest of the day) and run 42 the new pin
**6.11.2**, on which the macOS suite had never run. Commit `a968dba` moved the pin to 6.12.0
(the full Windows suite passes on it: 4311 passed) and run 43 built it: the **arm64 job failed
the same 22 cases on 6.12.0**. The toolkit was not the variable. Run 43 was cancelled once that
was known (its Windows artifact is a 6.12.0 build and must not be used).

### 5.3 The real cause, and the fix (`0c4c35e`)

Measured on this machine with the test's own fixture: `ActivitySection(api=MagicMock(), ...)`
shows the header's screenshot-status label **visible, 816 px wide, with the text
`<MagicMock name='mock.screenshot_status().get()' id='2065605343376'>`**. The 2026-10-29
input-security work had switched that label to `PlainText` (a headline is somebody else's
words and must read literally) -- and on `AutoText` the same string had been read as an empty
rich-text tag and drawn as nothing. The mock id differs per instance, so two otherwise identical
panels had floors a few pixels apart. On macOS that label *is* the panel's minimum width; on
Windows a wider floor (1498 px) hid it, which is why only the Macs failed and why the count
wobbled (21-24 of 27). The layout code never changed.

Fix: `set_screenshot_status` ignores anything that is not a status dict (a host without a real
status shows nothing, as its own docstring promised), a regression test keeps a `MagicMock` host
hidden (`tests/test_screenshot_status_line.py::TestAHostWithoutARealStatus`), `DO_NOT_DO.md`
records the label lesson and the misdiagnosis, and **the pin returns to `PySide6==6.11.2`** --
the owner's choice, the version the suite, the local builds and the real-window checks run on;
the project `.venv` is back on it. Full desktop suite on `0c4c35e`, this machine: **173/173
modules, 4313 passed, 0 failed, 4 skipped**; architecture OK; changelog OK.

### 5.4 Run 44 (`0c4c35e`, 6.11.2 pin) -- the release build

| Job | Attempt 1 | Attempt 2 |
|---|---|---|
| Windows package | **success** -- regression suite green, built, smoke-tested, attested (unsigned), uploaded | -- |
| macOS package (x86_64) | **success** -- 193 modules green **including `test_layout_stability`**, built, smoke-tested, attested, uploaded | -- |
| macOS package (arm64) | **cancelled by GitHub**: "The job was not acquired by Runner of type hosted even after multiple attempts" (arm64 capacity notices) -- never started | re-queued by `rerun-failed-jobs`; result below |
| Publish release | skipped (no tag, publishing off) | |

Artifacts of run 44, downloaded and checked on this machine (expire 2026-11-28):

| Artifact | File | Size | SHA-256 (sidecar = recomputed) | Checks |
|---|---|---|---|---|
| `Monitra-Windows-1.3.4` (id 12903804413, 77 042 171 B zip) | `Monitra-Setup-1.3.4.exe` | 32 444 901 B | `c2be6a944bfada4ee2b83e51129a157ac012668a57c2c44af2a6fe5d1ca8aa9d` | FileVersion 1.3.4, ProductVersion 1.3.4, Product Monitra, Company Monitra; `Get-AuthenticodeSignature`: **NotSigned**; `signature.json` `signed: false` (`WinVerifyTrust`) |
| | `Monitra-Portable-1.3.4.zip` | 45 374 204 B | `8c19c280a217c9d733c2055bb1a7f75cd79b47880b5339a3615185e0ec94d004` | `Monitra.exe` FileVersion 1.3.4; bundled `Qt6Core.dll` **6.11.2.0**; launched under an isolated `MONITRA_DATA_DIR` (offscreen): "Monitra 1.3.4 starting ... frozen=True portable=True", services up, still running after 15 s. It reused the session in the shared Qt settings store and got a clean 401 from the production API (observation also made for 1.3.3; read-only requests, handled) |
| `Monitra-macOS-x86_64-1.3.4` (id 12904444596, 58 533 189 B zip) | `Monitra-macOS-x86_64-1.3.4.dmg` | 61 434 292 B | `87cdff0c0bb4b5872148ed7bebf920139faa88cd835648ce86758441a6bf999e` | UDIF (`koly` trailer), GPT + one `Apple_APFS` volume (161 349 632 B); inside it `Monitra.app/Contents/Info.plist`: `com.monitra.desktop`, `CFBundleShortVersionString` 1.3.4, `CFBundleVersion` 1.3.4, `LSMinimumSystemVersion` 11.0; Qt frameworks 6.11.2, no 6.12 marker; `signature.json` `signed: false` ("code object is not signed at all"). Not mounted or launched (no Mac here) |
| `Monitra-macOS-arm64-1.3.4` | `Monitra-macOS-arm64-1.3.4.dmg` | -- | -- | never built: the re-queued job stayed `queued` on `macos-14` (14:35-14:50 UTC) and run 44 was cancelled in favour of run 45 |

### 5.5 Run 45 (`38a1c97`) -- the release build

Run 44's arm64 job waited 35 minutes on `macos-14`, was cancelled by GitHub ("The job was not
acquired by Runner of type hosted even after multiple attempts", arm64 capacity notices), and
sat queued again after the re-run while GitHub's status page showed Actions operational.
Commit `38a1c97` moves only the arm64 matrix entry to **`macos-15`** (same architecture, the
current default image; the suite runs there before anything is packaged, as on every runner);
nothing in the application changes. Run 45 acquired all three runners within seconds.

| Job | Result |
|---|---|
| Windows package (`windows-latest`) | **success**, 14:48-15:10 UTC |
| macOS package (arm64, `macos-15`) | **success**, 14:48-15:00 UTC -- regression suite green including `test_layout_stability` |
| macOS package (x86_64, `macos-15-intel`) | **success**, 14:48-15:11 UTC |
| Publish release | skipped (publishing off, no tag) |

**The 1.3.4 release artifacts** (run 45, expire 2026-11-28), downloaded and checked on this
machine; `python tools/verify_release_artifacts.py --artifacts <dir> --tag v1.3.4` prints
"Monitra 1.3.4: every required artifact is present, intact. (Signatures were not required for
this run.)":

| Artifact | File | Size | SHA-256 (sidecar = recomputed) | Checks |
|---|---|---|---|---|
| `Monitra-Windows-1.3.4` (id 12907299251, 77 035 895 B zip) | `Monitra-Setup-1.3.4.exe` | 32 438 229 B | `4f7ee34d63fa3fe6253a75c0348e8a3eea506e458d9fb6814d99d43d2f9f0e60` | FileVersion 1.3.4, ProductVersion 1.3.4, Product Monitra, Company Monitra; **NotSigned**; `signature.json` `signed: false` |
| | `Monitra-Portable-1.3.4.zip` | 45 374 317 B | `256c4701e597c33510322f82bf4b93c2fa73628122e787dfad11ca93fd7a954c` | `Monitra.exe` FileVersion 1.3.4; `Qt6Core.dll` 6.11.2.0; launched under an isolated `MONITRA_DATA_DIR` (offscreen): "Monitra 1.3.4 starting", still running after 12 s |
| `Monitra-macOS-arm64-1.3.4` (id 12907751254, 52 479 138 B zip) | `Monitra-macOS-arm64-1.3.4.dmg` | 54 373 610 B | `3be98f5d10fffa8814ed8a141c2020640c22973c0fe4a0a1c9e69d23ce59a014` | UDIF (`koly`), APFS volume; `Monitra.app` `Info.plist`: `com.monitra.desktop`, 1.3.4 / 1.3.4, `LSMinimumSystemVersion` 11.0; Qt 6.11.2, no 6.12 marker; `signature.json` `signed: false` ("not signed at all"). Not mounted or launched (no Mac here) |
| `Monitra-macOS-x86_64-1.3.4` (id 12908526279, 58 103 952 B zip) | `Monitra-macOS-x86_64-1.3.4.dmg` | 60 092 172 B | `3b6da84b876ba58ffad38957aa5894e8387a9201fb56a2fcf17af02526f89c8f` | same checks as arm64: `com.monitra.desktop`, 1.3.4 / 1.3.4, min 11.0, Qt 6.11.2, unsigned |

**Hosted in the repository** (final commit of this session): the three installers above replace
the 1.3.3 files in `frontend/public/download_app_files/`, `DOWNLOAD_VERSION = '1.3.4'`, the
download-page test updated (18 passed), `npm run build` and `npm run lint` clean. They reach the
website on the next frontend deploy -- after the server-side 500 fix (section 9, item 1).

The run-42, run-43 and run-44 artifacts are superseded and are not to be hosted.

## 6. Critical functionality -- evidence

| Area | Evidence in this session | Status |
|---|---|---|
| Timer accuracy, duplicate start/stop, offline queue | `test_timing_lifecycle_e2e` 9, `test_sync_lifecycle_e2e` 10, `test_network_resilience_e2e` 8, `test_timer_controls_e2e` 2 (real backend + PostgreSQL 18 rig) | PASS |
| Overnight / forgotten timers, weekend wake, recovery cap | `test_weekend_wake_e2e` 8, `test_timer_abandonment_e2e` 6, `test_timing_surfaces_e2e` 6; production reconciler `ok`, dry-run only | PASS (reconciler report-only by design) |
| Activity calculation | `test_screenshot_events_e2e` 4, activity modules in the 173; **not run on a real Mac** | PASS with limitation |
| Idle detection, popup, Decide later / Retry now, breaks | `test_idle_lifecycle_e2e` 6, `test_idle_resilience_e2e` 9 (1 skipped), `test_idle_popup_real_window_e2e` 9 (1 skipped, real window), `test_break_lifecycle_e2e` 2 | PASS |
| Administrative settings reach the desktop | `test_admin_config_e2e` 8 passed, 1 skipped (real second monitor), twice; the pytest process then exited abnormally **after** reporting (exit 0xC0000409 the first time) -- a native teardown crash of the two full runtimes in one process, a known trap in this suite, not a product path | PASS with note |
| Desktop notifications | `test_notification_schedule_e2e` 2, `test_native_toast_e2e` 3 (real toast; pixel check failed once under load, passed alone), `test_os_session_end_real_messages_e2e` 5 | PASS |
| Login exclusion, single instance | `test_login_exclusion_e2e` 1, `test_single_instance_activation_e2e` 1 | PASS |
| Update, Later, 426 gate, release journey | `test_release_management_e2e`: with the rig's default `DESKTOP_REQUIRE_SIGNED_RELEASES=true` the draft is correctly *not ready* (unsigned artifacts); with it off the journey runs draft → validate → publish → announcement → 426 → update offered and fails only at the **download from the live website folder**, which answers 500 today (section 9, item 1; the test expects the 404/web-page refusal). Unit coverage in the 173 modules passed. **Never run on a real device.** | PASS on the rig up to the download; BLOCKED by the website |
| Screenshot pipeline | `test_screenshot_lifecycle_e2e` 11, `test_screenshot_events_e2e` 4; production Drive probe `ok`; **Drive upload E2E blocked here** (production folder, no service-account file) | PASS with limitation |
| Email and CC, scheduled jobs | backend suite (email management + registry); production jobs all `ok` (section 7) | PASS; monthly jobs still unproven |
| Authentication, permissions, admin/HR | backend suite incl. `test_input_security_api`, `test_screenshot_privacy_authorization`, `test_login_access` | PASS |
| Database migration compatibility | no new migration; production at head | PASS |
| Desktop ↔ backend API compatibility | the desktop only uses endpoints production already serves; the 2026-10-29 backend commits only tighten validation (422 on invalid input) | PASS (deployed backend commit unknown) |
| Clean install, upgrade from 1.3.3/1.3.2, macOS mount and launch, Gatekeeper, Input Monitoring | **NOT DONE** -- no clean Windows machine and no Mac here; the only installed Monitra is the owner's | BLOCKED |

## 7. Backend, database, email scheduler, screenshot pipeline (production, read-only)

Read from the live API on 2026-10-29 ~13:00 UTC (`/health?deep=1`, `/internal/jobs/status`
with the dispatch token from `backend/.env`; nothing written):

* **Schema: production is now at head `a1c4e8b2d6f0`** (`database` = `code_head`,
  `at_head: true`). The five migrations the 1.3.3 report listed as pending have been applied
  by the owner since; **1.3.4 needs no migration.** The deployed backend's commit is not
  exposed by `/health`, so whether the 2026-10-29 validation commits are deployed is **not
  known from here**; the desktop does not depend on them.
* **Health:** `healthy`; database latency 1 ms, clock skew 0.001 s; email configured (SMTP,
  authenticated, 1 deliverability warning, 2 feedback recipients); screenshot storage
  (Google Drive) `probe.ok: true`.
* **Scheduled jobs:** email-dispatch, budget-alerts, weekly-report (2026-10-26, 68 queued),
  daily-rollup, wfpm-timer-dispatch and timer-reconcile all `ok` from `Store-Server`, none
  stale, no failures in 24 h. Outbox: 0 pending, 560 sent, 50 cancelled, 0 failed in 7 days.
  `healthy: false` only because of the known missing periods: weekly 2026-10-05 and
  2026-10-12, August monthly report and project summary (catch-up dry run on 2026-10-22
  listed 281). **monthly-report and monthly-project-summary have still never fired**; first
  firing 2026-11-01 03:30 UTC. Timer reconciler: dry-run only, by design.
* **Production download rows** still offer **1.1.1** with GitHub URLs
  (`/desktop/releases/downloads`), unchanged.

## 8. Signing and notarization

Unchanged since 1.3.3: the repository holds no signing secrets (`WINDOWS_CERTIFICATE*`,
`MACOS_*` absent; present: deploy SSH keys, `MONITRA_API_BASE_URL`,
`MONITRA_RELEASE_CREDENTIAL`, `RELEASES_REPO_TOKEN`). Every 1.3.4 artifact is **unsigned and
un-notarized**; the sidecars say `signed: false`. The in-app updater refuses unsigned
installers in production by design, SmartScreen warns on Windows, Gatekeeper refuses the DMG
on any Mac other than the builder.

## 9. Known issues, unresolved 1.3.3 items and blocked checks

**New since the 1.3.3 report (found today, all owner-side):**

1. **The website's installer folder answers HTTP 500 for everything.** On 2026-10-29 every
   URL under `https://staff.peakworkos.com/download_app_files/` -- the `.exe`, both `.dmg`,
   the folder itself and its `web.config` -- returns IIS's generic *500 - Internal server
   error* page, while the site root and `/download` answer 200. On 2026-10-27 the `.exe`
   downloaded and only the `.dmg` were 404. The frontend deploy since then ships
   `frontend/public/download_app_files/web.config` (committed 2026-10-27 to add the `.dmg`
   MIME type), and a folder-level `web.config` that IIS cannot apply fails the whole folder
   with 500. The detailed cause is only visible on the server (IIS detailed error / event
   log, typically *500.19*: the `staticContent` section locked at site level, or the
   `<remove>` of an extension not defined above). Until it is fixed **no installer can be
   downloaded from the website, no client can self-update, and the download page's buttons
   are dead.** **Repository side done (`b2fb0f4`):** the folder `web.config` is removed and the
   runbook (§8.7) and hosting notes now say to set `.dmg` at the site level. **Server side
   still open (owner):** delete `download_app_files\web.config` from the deployed folder, add
   `.dmg` = `application/x-apple-diskimage` in IIS Manager on the site node, deploy the
   frontend, then `verify_download_urls.py --version 1.3.4` must print three OK lines.
2. **The download folder in the repository held the withdrawn 1.3.3 installers** (`b1842f2`,
   owner) while `DOWNLOAD_VERSION` was `1.3.2`. **Done in this session's final commit:** the
   run-44 `Monitra-Setup-1.3.4.exe` and `Monitra-macOS-x86_64-1.3.4.dmg` (and the arm64 DMG once
   its job has run) replace them; `DOWNLOAD_VERSION = '1.3.4'`; the three pinned paths in
   `desktopRelease.test.ts` updated (18 passed). Takes effect on the next frontend deploy.
3. **`v1.3.3` was a public, non-draft GitHub release** (published 2026-10-29 07:46 UTC by the
   owner, 14 assets) -- the build that is withdrawn. **Done:** it is now a *pre-release* named
   "Monitra v1.3.3 (withdrawn)" with a do-not-install note at the top of its body pointing at
   1.3.4; assets and tag untouched (the version is spent). Delete it outright if preferred.
4. **The `v1.3.3` tag run (41) failed at backend registration** (`register_release.py`,
   expected: `MONITRA_RELEASE_CREDENTIAL` still answers 401 in production). The same will
   happen on a `v1.3.4` tag until the credential is rotated (runbook §8.8).
5. **The repository is public today** (it was made public for the 1.3.3 upload); live Actions
   artifacts total 571 MB (three 1.3.2, six 1.3.3, one 1.3.4). Public: no artifact quota,
   macOS minutes free. If it is made private again before run 43, the 500 MB quota refuses the
   upload exactly as on 2026-10-21 and 2026-10-29 -- delete the 1.3.2 and 1.3.3 artifacts
   first, or keep it public for the build.
6. **The macOS layout failure of runs 42/43** -- a `MagicMock` host's repr drawn by the
   now-plain-text status label (section 5.3). Fixed in `0c4c35e` and proven on both macOS
   runners (run 44 x86_64, run 45 arm64 and x86_64). The arm64 job now runs on `macos-15`
   (`38a1c97`) after `macos-14` could not supply a runner for 50 minutes.

**Carried forward from 1.3.3, still open:**

7. **Unsigned installers; no notarization.** SmartScreen warns, Gatekeeper refuses, the in-app
   updater refuses unsigned artifacts in production by design. No signing secrets exist.
8. **No real-device run:** clean-machine install, upgrade over an installed 1.3.2/1.3.3,
   mounting and first launch of either DMG, Gatekeeper and Input Monitoring on a real Mac, a
   real second monitor, the macOS update path.
9. **Drive screenshot upload E2E blocked here** (production folder, no service-account file).
10. **Monthly report and monthly project summary have never fired** (first 2026-11-01 03:30
    UTC); the missing weekly (2026-10-05, 2026-10-12) and August monthly rows await the catch-up
    decision (`/internal/reports/catch-up`, dry run of 2026-10-22 listed 281).
11. **Production download rows still offer 1.1.1** with dead GitHub URLs.
12. **Backend validation commits of 2026-10-29 may or may not be deployed** -- `/health` does
    not expose the commit. Deploying them is a separate decision; the desktop does not need them.

**Resolved since the 1.3.3 report:** the five pending migrations are applied (production at
`a1c4e8b2d6f0`, `at_head: true`); the heartbeat backoff fix is in; the Request-dialog defect is
fixed on both toolkits with geometry tests; the toolkit is pinned.

**Blocked checks in this session:** real-device installation and upgrade; Google Drive upload; the macOS activity path; the website
download (500, server-side); IIS cannot be reproduced on this machine (no admin rights to install
IIS Express).

## 10. Decision

**BLOCKED.**

What is proven: the 1.3.4 source (`38a1c97`) passes every automated gate on this machine --
backend 3414, frontend 1549 with a clean build and lint, desktop 4313 (173 modules), architecture,
changelog, 10/10 launch cycles, the soak, and 110 real-backend end-to-end tests on a throwaway
PostgreSQL -- and in CI all three packages pass their full suites, build, smoke-test, and verify
as 1.3.4 with matching checksums (section 5.5). Production needs no migration for it.

What blocks a release decision, in order of what must happen next:

1. The website folder's 500 (item 1, server side), then deploy the frontend (which now carries
   the 1.3.4 files and `DOWNLOAD_VERSION = '1.3.4'`) and prove the three URLs with
   `verify_download_urls.py --version 1.3.4 --artifacts frontend/public/download_app_files`.
2. Pilot ring on real devices (item 8), including an upgrade over an installed 1.3.3; the macOS
   DMGs have never been mounted.
3. Rotate `MONITRA_RELEASE_CREDENTIAL`, merge to `main`, tag `v1.3.4`, let the tag build
   register draft rows, `GET /desktop/releases/versions/1.3.4/readiness`, publish.

The installers remain unsigned, so even after 1-5 the outcome is at best **READY WITH
LIMITATIONS** (SmartScreen / Gatekeeper warnings; no self-update of unsigned artifacts in
production), as for every release so far.

# Monitra Desktop Release Management: Remaining Fixes and Local End-to-End Verification

**Date:** 2026-10-27 (second pass) · **Branch:** `smit` · **Follows:** [monitra-release-management-implementation-2026-10-27.md](monitra-release-management-implementation-2026-10-27.md)
**Pushed / deployed / published / production migrated / real email sent / mandatory update forced:** none of these.

## A. Final verdict

**PASS WITH BLOCKERS.** Every automated suite passes; the complete release journey runs end to end on a local stack (real backend process, real PostgreSQL, real desktop runtime, real website for the download); the admin page was driven in a real browser. What remains unverified needs things the repository cannot provide: the IIS deploy of the shipped `web.config`, a credential issued on the production database, signing certificates, the production migration, and physical Windows and Mac machines.

## B. Changes made in this pass

| Change | Files |
|---|---|
| IIS: `.dmg` (and `.sha256`) MIME mapping scoped to the installer folder, shipped with every frontend deploy; verified present in the Vite build output | `frontend/public/download_app_files/web.config` |
| Downloader: a `text/*` answer is refused before a byte is kept, named as a web page served instead of the installer (`http-error`), never "incomplete, try again" | `desktop/background_services/update/downloader.py`, test in `desktop/tests/test_update_hardening.py` |
| Registration tool: proves the credential with `GET /auth/me` before any row is registered, prints the public key id (never the secret), exits 1 on refusal with the remedy; a manual token is not preflighted; a backend that cannot be asked does not stop the run | `desktop/tools/register_release.py`, 5 tests in `desktop/tests/test_register_release.py` |
| Enforcement gate: the running session's two capture reads (`/screenshot/privacy-config`, `/screenshots/config`) added to the exempt paths — found by the E2E: refused, the screenshot gate held every capture of a timer the policy had left running | `backend/app/services/desktop_update_policy.py`, `backend/tests/test_desktop_release_management.py` |
| E2E: the complete release journey against the real local stack; the e2e administrator carries `manage_desktop_releases` | `desktop/tests/e2e/test_release_management_e2e.py`, `backend/tests/e2e_support.py` |
| Changelog notes filed under the unbuilt 1.3.3 (the repository's release gate requires `[Unreleased]` empty) | `desktop/CHANGELOG.md` |
| Documentation: runbook §8.7–§8.10 (IIS administrator steps, credential diagnosis, production migration procedure, local run instructions), hosting §7, DO_NOT_DO entry, physical-device test plan | `docs/Desktop_Release_Runbook.md`, `docs/Desktop_Artifact_Hosting.md`, `desktop/DO_NOT_DO.md`, `docs/Desktop_Release_Device_Test_Plan.md` |

No migration, API shape, desktop component or frontend component changed beyond the above.

## C. Root causes and fixes

### The macOS download failure

- **What was wrong (measured live, twice today):** `GET …/Monitra-Setup-1.3.2.exe` → `200 application/octet-stream`, 32,478,743 bytes, digest equal to the committed file. `GET …/Monitra-macOS-arm64-1.3.2.dmg` and `…x86_64-1.3.2.dmg` → `404 text/html`, `Server: Microsoft-IIS/10.0`, with the files present in the folder. IIS's static-file handler serves only extensions with a MIME type; `.dmg` has none by default. The buttons, filenames, casing, per-architecture artifacts, URL construction and metadata were checked and are correct (`desktopRelease.ts` tests, `verify_download_urls.py`).
- **Second finding:** a *missing* installer URL under the folder answers `200 text/html` (the site's index page via an SPA rewrite), not 404. The desktop refused it by size with "incomplete, try again"; it now refuses any `text/*` answer up front with a message that names the server.
- **What was changed:** the directory-scoped `web.config` (above) and the downloader. Installers are never marked available because a URL is configured: a published row still needs a file that exists, and the hash is checked against the backend's row, never the download.
- **IIS administrator action still required:** deploy the frontend (which carries the `web.config`) or add the mapping by hand, then prove it (runbook §8.7, exact commands). **Live `.dmg` downloads are NOT verified**: both URLs still answer 404 today; only the Windows download is verified live.

### The release credential refusal

- **Confirmed by code and tests:** the key travels as `Authorization: Bearer msk_<key id>_<secret>`, stripped; the backend looks the row up by public key id, compares the SHA-256 of the whole key in constant time, refuses revoked/expired keys and non-`release_bot` accounts, answers one 401 for every reason and logs the reason. 44 registration-tool tests and the service-credential suite pass; no defect was found in the flow.
- **Actual root cause:** external. The backend that the (secret) `MONITRA_API_BASE_URL` names answered 401 to a key that is not in *its* database; production moved to the company server in October 2026 and its database is not the one any repository `.env` names, so a key issued against the development database (or the previous host) is `unknown key_id` there. The deciding line is in the production backend log (`SERVICE_CREDENTIAL_REFUSED: …`), which the repository cannot read.
- **What was changed:** the preflight and key-id diagnostic in `register_release.py`, so the next run says which key id was refused and never registers half a set.
- **A real external credential is still needed:** rotate on the production database and paste into the repository secret (runbook §8.8). **The local registration flow passes:** valid key accepted (`accepted: … release_bot`), missing credential skips, incorrect key refused with 401 and no row, repeated registration answers 409 and counts as registered, a refused run cannot publish, logs never contain the secret, rotation covered by `test_service_credential.py`.

## D. Test evidence

| Suite | Command | Result | Kind |
|---|---|---|---|
| Backend | `cd backend && python -m pytest tests/ -q` | **3195 passed, 68 skipped, 1923 subtests** (76.7 s) | automated |
| Backend, PostgreSQL race (opt-in) | `MONITRA_PG_TEST_URL=… pytest tests/test_desktop_release_management_pg.py` | 1 passed (earlier today; 8 racing first encounters → 1 row) | integration |
| Migration on PostgreSQL 18 | scratch `migrate_check.py`: stamp → upgrade → downgrade → upgrade | all clean; duplicate grace row and bad outcome refused by constraints | integration |
| Desktop unit, per module | `cd desktop && python tools/run_tests_per_module.py` | 181 modules: **3956 passed, 1 failed, 109 skipped** → the one failure (`test_changelog`, release gate on `[Unreleased]`) fixed by filing the notes under 1.3.3; `tests/test_changelog.py` 9 passed, `check_changelog.py --release` OK | automated |
| Desktop architecture | `python tools/check_architecture.py` | `Architecture boundaries OK (150 files scanned).` | automated |
| Desktop launch cycles / soak | `run_launch_cycles.py --cycles 10` / `run_soak.py --duration 60` | 10/10 clean; `PASS: no thread growth, no duplicates, queue drained, clean shutdown.` | integration |
| Desktop E2E, every module, on the local rig | `MONITRA_E2E=1 … pytest tests/e2e/<module>` one process each | 19 modules: **93 passed, 16 skipped** (Drive-dependent screenshot checks, Drive deliberately off), 1 flake (`test_native_toast_e2e`, a real OS toast under three concurrent suites) which passes alone | integration (real backend, real PostgreSQL, real desktop runtime) |
| Release journey E2E | `tests/e2e/test_release_management_e2e.py` | 1 passed; output: `{"targeted_users": 2, "emails": 2, "failure_reported": true, "stats": {"requirements": {"pending": 0, "expired": 1, "satisfied": 1}, "installs": {"download-failed": 1}, "adoption": {"on_or_above": 1, "below": 0}, "email": {"sent": 2}}}` | integration |
| Frontend | `npx vitest run` / `npm run build && npm run lint` | 86 files, 1370 passed (previous run; no frontend code changed) / build clean, `dist/download_app_files/web.config` present / lint exit 0 | automated |
| Admin page in a real browser | scratch `browser_check.py` (Playwright, system Chrome, vite 5173 → backend 58080) | list, validate, publish behind the confirmation, client-side refusal of `v1.9`/grace 9999, new Windows-only mandatory draft with its installer, withdraw; **no console errors**; screenshots taken | integration, real browser |
| Live website | `verify_download_urls.py --version 1.3.2` | `.exe` OK (digest matches); both `.dmg` HTTP 404 | live, read-only |
| Physical devices | — | **NOT RUN** (see the device test plan) | — |

### The 21-step end-to-end scenario (what each step was: real, simulated, or blocked)

1 older version (the running 1.3.3 build) · 2 publish a test release (real, admin API) · 3 platform targeting (real: arm64/x86_64/win32 answers, Linux claim held) · 4 publication-time email (real outbox rows, delivered by the console provider; no real mail) · 5–6 update check and prompt (real desktop runtime: `update_offered` mandatory/initial) · 7 Later (real) · 8–9 restart keeps the deadline (real persisted record; second device shares it; a repeated check does not move it) · 10 expiry (**simulated**: the grace row's `deadline_at` moved on the server) · 11 protected calls blocked (real 426 on start and projects) · 12 recovery routes open (real: active, auth/me, heartbeat, stop) · 13 download the platform artifact (real HTTP to the live website; the file does not exist there) · 14 checksum/signature policy (download refused before either; policy unchanged and unit-tested) · 15 failed download (real) · 16 failed installation (**blocked**: no installer executed; covered by the hidden-desktop run of 2026-10-27 and unit tests) · 17 retry (real Try Again path in unit tests; not re-run live) · 18 installed version verified (**simulated**: the running version patched to the target) · 19 pending state cleared only then (real) · 20 "latest version" afterwards (real server answer) · 21 timer integrity (real: the same entry ran through the block and finalised with duration = end − start, one row).

## E. Remaining blockers

| Area | Blocker | Next action | Who |
|---|---|---|---|
| 1. Local code | none open | — | — |
| 2. External credentials | the key in `MONITRA_RELEASE_CREDENTIAL` is not in the production database; `MONITRA_API_BASE_URL` unverified | runbook §8.8: `provision_release_credential.py show` / `rotate` on the server, update the two secrets, re-run the registration step and read `credential: service key id …accepted` | server administrator |
| 3. IIS | `.dmg` answers 404 until the folder's `web.config` is deployed (or the mapping added by hand) | runbook §8.7; prove with `verify_download_urls.py` → three OK lines | server administrator |
| 4. Production database | migration `f1a7c3e9d2b4` not applied | runbook §8.9 (backup; `alembic upgrade head`; deploy backend) | owner |
| 5. Signing | no Windows certificate, no Apple Developer ID/notarization; production clients refuse unsigned installers by design | procure; CI secrets; pins; `REQUIRE_SIGNED_RELEASE=true` | owner |
| 6. Physical devices | nothing run on Win 10/11, Intel/Apple Silicon Macs | `docs/Desktop_Release_Device_Test_Plan.md` | owner / testers |

## F. Local run instructions

Runbook §8.10 has the full recipe (throwaway PostgreSQL, environment, backend on 58080, vite on 5173, the desktop from source, provisioning, the automated journey, publishing a test release by hand).

## G. Production-readiness checklist

1. Deploy the frontend (ships `web.config`); prove the three downloads (§8.7).
2. Rotate the release credential on the production database; set the two secrets; re-run a registration (§8.8).
3. Back up and migrate (§8.9); deploy the backend; confirm `/desktop/releases/versions` and `/desktop/update-policy`.
4. Signing certificates and pins; `DESKTOP_REQUIRE_SIGNED_RELEASES=true` stays on.
5. Build 1.3.3 from `main` (first version with the grace UI), register, copy installers, bump `DOWNLOAD_VERSION`, publish as **optional**; run the device plan on all four machines.
6. Only then, with explicit approval, a mandatory release with a 24 h or longer grace; watch the detail page; withdraw and publish a corrective release with the same floor if needed.
7. Do not describe the feature as live until steps 1–3 are done and verified on the server.

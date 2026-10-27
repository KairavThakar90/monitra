# Monitra Desktop Auto-Update: Implementation Report and Production-Readiness Verdict

**Date:** 2026-10-27 · **Branch:** `smit` (commits `0a34f58` … see §A) · **Version in `desktop/version.py`:** 1.3.3 (bumped from the spent 1.3.2)
**Follows:** [monitra-auto-update-audit.md](monitra-auto-update-audit.md) (same day). Every audit finding was re-checked against the current code and the live production API before anything was changed.

**Verdict: NOT PRODUCTION READY** — the code side is complete and tested; four external actions (one of them a one-line server setting that currently breaks every Mac download) and the real-device runs remain. §D lists each with the exact next action. Nothing was pushed, published, deployed, emailed or installed on anyone's machine.

---

## A. Implementation

### What was changed, and why

| Commit | Change | Bug it fixes |
|---|---|---|
| `0a34f58` | Client (`policy.ALLOWED_DOWNLOAD_HOSTS`) and backend (`DESKTOP_DOWNLOAD_ALLOWED_HOSTS` default) trust `staff.peakworkos.com`, the website folder that already serves the download page's installers. `register_release.py --download-base-url` / `MONITRA_RELEASE_DOWNLOAD_BASE_URL` derives the rows' URLs from that folder; the workflow passes the repository variable `RELEASE_DOWNLOAD_BASE_URL`. | **Audit AU-02 / AU-11.** The source repository is private, so every GitHub release-asset URL answers 404 to an installed client (re-verified live). The website is the one public place the installers live and was never a host the updater accepted. |
| `0a34f58` | A refused registration exits 1 and the workflow step is no longer `continue-on-error`. "Not configured" (a fork) still skips with a warning. | **AU-01 / AU-16.** 1.3.2's three registrations answered 401 under a green tick; production still offers 1.1.1. |
| `134ae4e` | `VERSION = "1.3.3"`; the 78 commits' notes moved from `[Unreleased]` to `## [1.3.3]`; `check_changelog.py --release` (run on tag builds) refuses a release while `[Unreleased]` has notes. | **AU-05.** 1.3.2 was built twice from different commits. |
| `134ae4e` | Windows helper passes `/CURRENTUSER`; `can_install` refuses a Program Files (machine-wide) install with the manual path. | **AU-09 / AU-10.** Reproduced with a real Inno Setup installer carrying `monitra.iss`'s privilege directives: `/SILENT` without the switch stopped on "Select Setup Install Mode" (nothing installed in 20 s, killed); with it, installed in under a second. |
| `134ae4e` | macOS: the bundle replacement is a separate script run directly when the bundle's folder is writable, else through `osascript … with administrator privileges` (the system dialog). New exit code 17 = authorization declined, bundle untouched. Translocated bundles / bundles on the mounted image are refused up front. Mount point under the data directory, not `/tmp`. | **AU-08.** A standard user cannot write `/Applications`; the old helper failed with code 15 and no way forward. |
| `c044696` | Reminder policy: "Later" persisted (`app_state["updates.postponed"]`), prompt hidden 24 h across restarts, returns at the first due check, **final reminder** after 3 postponements or 7 days on offer — words only; "Later" stays. `update_offered(release, mandatory, stage)`. Dialog wired to `BackgroundApi.postpone_update`. | **AU-06.** No reminder policy existed; the prompt returned at every login and never otherwise. |
| `c044696` | One `UPDATE_EVENT stage=… category=…` log line per lifecycle step; every `DownloadError` carries a fixed category (`url-refused`, `disk-space`, `no-connection`, `timeout`, `http-error`, `download-incomplete`, `checksum-failed`, `signature-refused`, `permission`, `install-unavailable`, `installer-launch`, `cancelled`). | **AU-14** (local half). Support can tell a dead link from a full disk from a tampered file. |
| `0217d22` | `tools/verify_download_urls.py` fetches a version's three installers as a client would and refuses an HTML page, wrong size or wrong digest; documentation (hosting, runbook, architecture, DO_NOT_DO, CLAUDE.md §5.6). | **New finding:** the live website answers an IIS 404 for both `.dmg` files. |

### Files and components

- `desktop/background_services/update/{policy,installer,update_service,downloader,signature}.py`, `desktop/ui/update_dialog.py`, `desktop/ui/dashboard_window.py`, `desktop/background_services/public_api.py`, `desktop/version.py`, `desktop/CHANGELOG.md`
- `desktop/tools/{register_release,check_changelog,verify_download_urls}.py`
- `desktop/tests/{test_update_hardening,test_register_release,test_release_pipeline,test_changelog,test_verify_download_urls}.py`
- `backend/app/core/config.py`, `backend/.env.example`
- `.github/workflows/desktop-release.yml`
- `docs/Desktop_Artifact_Hosting.md`, `docs/Desktop_Release_Runbook.md`, `desktop/ARCHITECTURE.md`, `desktop/DO_NOT_DO.md`, `CLAUDE.md`
- Not changed: the frontend (the download page's three buttons and `DOWNLOAD_VERSION = '1.3.2'` are correct for the files that exist today; bumping them is a release step), the email package (audited, works as designed, see §C), authentication, tracking, screenshots, sync.

### How the update architecture works now

```
developer: fix → VERSION → CHANGELOG [x.y.z] → tag vX.Y.Z
CI (desktop-release.yml): tests + packaging per platform → attest → checksum →
     verify the set → DRAFT GitHub release (archive) → register DRAFT rows
     pointing at https://staff.peakworkos.com/download_app_files/<built name>
     (fails the job if the backend refuses)
operator: copy the 3 installers into frontend/public/download_app_files/,
     bump DOWNLOAD_VERSION, deploy; verify_download_urls.py → 3 × OK;
     readiness → publish the version (administrator, not the CI key)
client (1.3.3+): every 10 h / at login: GET /desktop/latest-version →
     strictly newer? this platform+arch? https on an approved host? sha256?
     → dialog (initial | reminder | final) → Update Now → stream + SHA-256 +
     size + Authenticode → detached helper (/CURRENTUSER | osascript admin)
     → restart (timer kept) → next launch reads update-result.txt → toast
```

## B. Release and hosting

- **How private CI artifacts reach the download system.** They do not reach it automatically. CI produces the artifacts (30-day retention) and files a draft GitHub release that clients cannot read. An operator copies the three installers into the website folder (committed to the frontend, deployed with it), as the owner set up on 2026-10-09. The backend rows then point at that folder. **Manual upload is still required**, and the runbook §3.1 now says so step by step. Automating the copy needs a deploy credential for the web server that CI does not hold (optional future improvement).
- **Google Drive.** No Drive URL exists anywhere in the repository, backend configuration or frontend, so none could be tested. Drive is unsuitable as an *updater* source for three concrete reasons: a share link returns HTML (preview / virus-scan / sign-in) rather than bytes, the URL does not carry the filename the backend's filename rule and the client's installer-type check require, and download quotas fail a popular link. It remains fine for handing an installer to a person. Nothing about that workflow was changed.
- **Were the website links tested?** Yes, for real, through the updater's own downloader under production policy: `Monitra-Setup-1.3.2.exe` → 32,478,743 bytes, SHA-256 `9620c66c…` equal to the committed file and to the CI 1.3.2 build; a deliberately wrong digest was refused and left no file behind; a missing file was a clean `HTTP 404` failure. **Both `.dmg` URLs return an IIS 404 HTML page** (`Server: Microsoft-IIS/10.0`, no MIME type for `.dmg`), so the Mac buttons on the live download page are broken today, independent of the updater.
- **Release metadata.** One authoritative source: the `desktop_releases` table (one row per artifact: version, platform, architecture, URL, SHA-256, size, notes, status, signed, force/min-version). Validation at registration (URL host/prefix/filename, checksum shape), at publish (complete set of `win32`, `darwin:arm64`, `darwin:x86_64`, size, signed-when-required), and again when served (URL re-checked). Pre-releases are refused before registration. Nothing is advertised until the whole version is published; a partial set is a 422 with every problem listed.
- **Version consistency.** `desktop/version.py` is the only source for the executable, installer, bundle, DMG name, User-Agent and tag check; `test_packaging.py` forbids a second literal. New gate: a tag build fails while `[Unreleased]` has notes. The website's `DOWNLOAD_VERSION` is the one value that must be bumped by hand at release time; `verify_download_urls.py` prints a note when it lags.

## C. Test results

| Area | Result | Evidence |
|---|---|---|
| Desktop unit/integration tests (full suite, per module) | PASS | `python tools/run_tests_per_module.py` → **179 modules in 947 s: 3914 passed, 0 failed, 0 errors, 108 skipped** |
| Desktop targeted update modules | PASS | `test_update_hardening/installer/service/badge`: 268 passed, 1 skipped (platform-conditional); `test_register_release`, `test_release_pipeline`, `test_packaging`, `test_changelog`: all passed; `test_verify_download_urls`: 9 passed |
| Architecture boundaries | PASS | `python tools/check_architecture.py` → `Architecture boundaries OK (149 files scanned).` |
| Backend tests (full suite) | PASS | `python -m pytest tests/ -q` → **3105 passed, 67 skipped, 1877 subtests passed** in 72.9 s |
| Frontend tests/build | NOT RUN | No frontend file was changed. `npm run build && npm run lint` must be run when `DOWNLOAD_VERSION` is bumped at release time. |
| Launch cycles (10) | PASS | `run_launch_cycles.py --cycles 10` → `10/10 cycles clean; launch+quit 6.7-6.8s` (isolated `MONITRA_DATA_DIR`, beside the owner's running Monitra) |
| Soak (60 s) | PASS | `run_soak.py --duration 60` → `PASS: no thread growth, no duplicates, queue drained, clean shutdown.` |
| Windows real-device update | NOT VERIFIED | No end-to-end install was run against a real installation (the owner's Monitra was running on this machine and must not be touched). **Verified for real:** the install-mode dialog reproduction and its fix with a throwaway Inno installer; the real `cmd.exe` helper against stand-in executables (`TestWindowsHelper`, incl. the new `/CURRENTUSER`). |
| macOS Apple Silicon real-device update | NOT VERIFIED | No Mac available. Generated helper and replace scripts are unit-tested and `bash -n` clean. |
| macOS Intel real-device update | NOT VERIFIED | Same. |
| Actual installer download | PASS (Windows) / **FAIL (both macOS)** | Live website through the updater's downloader, production policy: `.exe` 200 + digest match; `.dmg` ×2 → IIS 404 (`verify_download_urls.py` output in §D-2) |
| Release registration | BLOCKED | Code path unit-tested (idempotent 409, 401 named without echoing the body, exit 1 on refusal). Not exercised against production: the stored `MONITRA_RELEASE_CREDENTIAL` was refused by production on the 1.3.2 run and no credential is available to this session. |
| Update notification emails | PASS (safe tests) | Existing `queue_release_announcements`: off by default, test-recipient mode replaces the audience, per-user-per-version dedupe key in the outbox, queued only when the whole version is published, points at `MONITRA_APP_URL/download`, never a GitHub or artifact URL. Covered in the backend suite above. No email was sent. |
| Failure recovery | PASS (unit) | Tampered/truncated/HTML/interrupted/404/5xx/timeout/full-disk/cancelled downloads leave nothing behind; failed installer → old version relaunched and reported; declined macOS authorization → bundle untouched (script text). |
| Existing Monitra regression tests | PASS | the full desktop and backend suites above (login, timer, idle, sync, screenshots, notifications, email outbox all included); no regression found |

All suites were run on this machine (Windows 11, repo-root `.venv`, Python 3.12.10) against the final commits.

## D. Remaining blockers

1. **Production still offers 1.1.1 with dead URLs.** *Why:* a client on 1.1.0 is sent to a 404; everyone else is told nothing. *Needs:* a credential + a published 1.3.3. *Next action (owner):* `cd backend && DATABASE_URL=<prod> python scripts/provision_release_credential.py show`, rotate if refused, paste the new key into the repository secret `MONITRA_RELEASE_CREDENTIAL`; after 1.3.3 is published, `POST /desktop/releases/{id}/rollback` for the three 1.1.1 rows.
2. **IIS serves no `.dmg`.** *Why:* every macOS download, button or updater, fails. *Needs:* a server setting. *Next action (owner, on the web server):* IIS Manager → the site → MIME Types → add `.dmg` = `application/x-apple-diskimage` (or the `<mimeMap>` in `Desktop_Artifact_Hosting.md` §7.7); then `python desktop/tools/verify_download_urls.py --base-url https://staff.peakworkos.com/download_app_files/ --version 1.3.2 --artifacts dist-release-1.3.2` must print three `OK` lines. Today it prints:
   ```
   OK   Monitra-Setup-1.3.2.exe: 32478743 bytes, digest matches the build
   - darwin/arm64: Monitra-macOS-arm64-1.3.2.dmg: HTTP 404
   - darwin/arm64: a 404 HTML page for a .dmg usually means the web server has no MIME type for .dmg ...
   - darwin/x86_64: Monitra-macOS-x86_64-1.3.2.dmg: HTTP 404
   ```
3. **Hosting variables not set.** *Needs:* repository variable `RELEASE_DOWNLOAD_BASE_URL = https://staff.peakworkos.com/download_app_files/`; production env `DESKTOP_DOWNLOAD_URL_PREFIXES=https://staff.peakworkos.com/download_app_files/` and `DESKTOP_DOWNLOAD_ALLOWED_HOSTS=github.com,staff.peakworkos.com` (the latter is the new default, the former pins the folder). *Why:* without the variable the rows point at GitHub (404 for clients).
4. **No signing certificate.** *Why:* a production client deletes an unsigned installer by design; **zero auto-updates can complete until this exists.** *Needs:* procurement + secrets (`WINDOWS_CERTIFICATE`, `WINDOWS_CERTIFICATE_PASSWORD`, the seven `MACOS_*`), then `policy.WINDOWS_SIGNER_PINS` / `MACOS_TEAM_ID_PINS` set in the client and `REQUIRE_SIGNED_RELEASE=true`. Nothing was invented here: pins stay empty. For a pilot ring only, `DESKTOP_REQUIRE_SIGNED_RELEASES=false` and a *development/staging* client with `MONITRA_UPDATE_ALLOW_UNSIGNED=1`; a production client ignores that variable.
5. **Real-device runs.** *Needs:* one Windows 10/11 machine with an installed 1.3.2 (not the owner's working copy), one Intel Mac, one Apple Silicon Mac, each with a standard (non-admin) account for the authorization path. Matrix in the audit §13; the macOS replace-with-authorization step has never executed anywhere.
6. **Clients older than 1.3.3 cannot self-update** (backend floor 1.3.2 + `github.com`-only host list). *Next action:* the 1.3.3 announcement says "install this one by hand from the download page"; watch `GET /desktop/client-versions`.
7. **Server-side update telemetry** is not implemented (local `UPDATE_EVENT` lines only). Optional before rollout; recommended.

## E. Release instructions (next version, from this repository)

```bash
# 1. Change, version, notes (desktop/)
#    edit VERSION in desktop/version.py (1.3.3 is now the next unbuilt number; after any build, bump again)
#    move the notes under ## [Unreleased] to ## [<version>] in desktop/CHANGELOG.md
cd desktop
python -m pytest tests/ -q                      # or: python tools/run_tests_per_module.py (this machine)
python tools/check_architecture.py
python tools/check_changelog.py --release
python tests/soak/run_launch_cycles.py --cycles 10
python tests/soak/run_soak.py --duration 60

# 2. Tag from main (never re-tag a built version)
git tag v<version> && git push origin v<version>
#    CI: three packages, attest, checksum, verify set, DRAFT GitHub release, DRAFT backend rows
#    (the job FAILS if registration is refused -- fix the credential, re-run)

# 3. Operator: put the installers on the website
#    download the run's artifacts, check .sha256, copy into frontend/public/download_app_files/
#    bump DOWNLOAD_VERSION (frontend/src/api/desktopRelease.ts) and the pinned test paths
cd frontend && npm run build && npm run lint && cd ..
#    commit, deploy the frontend
python desktop/tools/verify_download_urls.py --base-url https://staff.peakworkos.com/download_app_files/ \
    --version <version> --artifacts <folder with the CI outputs>      # must print 3 x OK

# 4. Publish (administrator, signed-in; the CI key cannot)
GET  /desktop/releases/versions/<version>/readiness                   # ready: true
POST /desktop/releases/versions/<version>/publish                     # all three rows, one commit
GET  /desktop/releases/downloads                                      # latest_version == <version>
#    optional: RELEASE_EMAIL_ENABLED + RELEASE_EMAIL_TEST_RECIPIENTS rehearsal first

# 5. Verify on a pilot machine (installed <previous>): account menu -> Check for Updates
#    -> dialog -> Update Now -> relaunch -> "Monitra was updated to version <version>."
#    ~/.monitra/logs: UPDATE_EVENT stage=install-result ... outcome=success
GET  /desktop/client-versions                                         # adoption
```

Fully automated: build, test, attest, checksum, set verification, draft release, draft registration, publish gate, email queueing. Operator steps: copying the three files to the website, bumping `DOWNLOAD_VERSION`, deploying the frontend, publishing the version, enabling the email. Blocked by missing access: signing, the production credential, the IIS MIME type, real-device runs.

## F. Final verdict

**NOT PRODUCTION READY.** The implementation that could be completed from the repository is complete and tested (see §C). It cannot deliver one update to one real user until the owner: fixes the IIS `.dmg` MIME type, sets the hosting variable and prefix, rotates the release credential and publishes 1.3.3, and obtains signing certificates (without which every production client refuses the installer by design). The real-device runs on Windows and both Mac architectures remain NOT VERIFIED.

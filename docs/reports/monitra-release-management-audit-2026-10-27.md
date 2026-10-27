# Monitra Desktop Release Management: Audit of the Existing System

**Date:** 2026-10-27 · **Branch:** `smit` · **Installed desktop version in the field:** 1.3.2 · **`desktop/version.py`:** 1.3.3 (unbuilt)
**Follows:** [monitra-auto-update-implementation-2026-10-27.md](monitra-auto-update-implementation-2026-10-27.md) and [monitra-auto-update-audit.md](monitra-auto-update-audit.md).

This is the mandatory first phase of the release-management work: what exists, what it does, and what is missing against the specification (centralised release records, platform targeting, mandatory releases with a server-authoritative 24-hour grace period, backend enforcement, admin panel, targeted email, installation reporting). Nothing was changed while writing it.

## 1. What already works (and is reused)

| Area | Where | Verdict |
|---|---|---|
| Desktop startup, session restore, window stack (login / dashboard) | `desktop/main.py` (`MainWindow`, a `QStackedWidget`) | Works. The stack is the one place a third page ("update required") can be enforced centrally. |
| Version source | `desktop/version.py` (`VERSION`, `user_agent()` = `Monitra/<version>`) | Works. One constant; `test_packaging.py` forbids a second literal. The User-Agent is what the backend parses. |
| Update check | `background_services/update/update_service.py` (`UpdateService`, a `LoopService`, every 10 h from a persisted stamp, `GET /desktop/latest-version` with `X-Monitra-Platform` / `X-Monitra-Arch`) | Works. Edge-triggered; validates the answer (strictly newer, this platform/arch); badge; reminder policy ("Later" persisted, 24 h, final after 3 postponements / 7 days). |
| Existing notifications | `ui/dashboard_window.py` (`_check_for_updates`, `manual_check_outcome`) | Works. "Checking for updates…", "Monitra X is the latest version.", "could not check", "no release information published". Must be preserved. |
| Update dialog | `ui/update_dialog.py` | Works. Optional (Later/Update Now) and mandatory (Update Now only, undismissable) modes; progress, verifying, error + Try Again + Download manually. |
| Download / verify / install | `update/downloader.py`, `signature.py`, `installer.py` | Works (unit-tested, Windows helper run for real). SHA-256 + size streamed, Authenticode via `WinVerifyTrust`, macOS `codesign`/`spctl` in the helper (never run on real hardware). Nothing unverified runs. |
| Install result reporting | `installer.read_and_clear_result` → `UpdateService._report_previous_install` | Works locally: compares the helper's recorded target with the version actually running. **Not reported to the server.** |
| Installer builds | `.github/workflows/desktop-release.yml` | Works: Windows + macOS arm64/x86_64, attest, checksum, verify set, draft release, draft backend registration (fails the job if refused). **Unsigned** (no certificates). |
| Backend release records | `backend/app/models/desktop_release.py` (`desktop_releases`, one row per artifact: version, platform, arch, URL, SHA-256, size, notes, status, `force_update`, `min_supported_version`, `signed`) | Works. Hosting policy (`desktop_release_policy.py`), readiness (complete set), publish whole version in one commit, rollback keeps rows. |
| Backend update check | `services/desktop_release.py::latest_version` | Works. Records the client's version (`desktop_client_versions`), answers newest published artifact for the platform/arch, `force_update` only when a newer build exists, floors a client below `DESKTOP_AUTO_UPDATE_MIN_CLIENT_VERSION` to announce-only. |
| Admin authorization | `manage_desktop_releases` (three administrator roles; the CI service credential may register drafts but `require_release_publisher` refuses it for publish/withdraw) | Works. |
| Email | `services/email/` outbox (`(type, dedupe_key)` unique, retry with jittered backoff, `EMAIL_MAX_ATTEMPTS`, statuses pending/sent/failed/cancelled), `queue_release_announcements` (off by default, test-recipient mode replaces the audience, keyed `release:<version>:user:<id>`, sent only when the whole version is published) | Works. **Not platform-targeted** and the template has no mandatory/grace wording. |
| Audit trail | `activity_logs` + `ActivityLogService.record` (module/action vocabulary) | Works. **No release actions are recorded.** |
| Admin panel | `frontend/src/features/admin/*` (Settings → Maintenance / User Management / Screenshot Privacy / Desktop Notifications) | Works as a pattern. **No release-management page exists**; the frontend only has the public download page. |
| Download hosting | `https://staff.peakworkos.com/download_app_files/` (IIS); `RELEASE_DOWNLOAD_BASE_URL` set as a repository variable | `.exe` serves; **both `.dmg` answer an IIS 404** (no MIME type). |
| Tests | backend 3105 passed; desktop 3914 passed (per-module runner); frontend vitest | Green on the previous verification. |

## 2. What is incomplete or missing against the specification

1. **No server-authoritative grace period.** `force_update` locks the client out at once on the next check; nothing records *when a user first met* a mandatory release, nothing persists a deadline, and the desktop deliberately does not persist `force_update_pending` across restarts.
2. **No backend enforcement.** Every protected endpoint answers an outdated client exactly as a current one. A client that skips the dialog is never refused anything.
3. **No per-platform targeting of a release.** A version is only publishable as the complete set (`DESKTOP_REQUIRED_ARTIFACTS`: Windows + both macOS); there is no Windows-only or macOS-only release. The mandatory policy and notes are duplicated on every artifact row, so the three rows of one version can disagree.
4. **No release entity.** Title, type (mandatory/optional), grace period, required minimum version, effective time, lifecycle (draft → validated → published → withdrawn/superseded) and "who did what" do not exist as a record. Rows have four statuses (`draft`/`published`/`disabled`/`rolled_back`).
5. **No release audit history.** Publish/withdraw/edit leave only a log line.
6. **No installation reporting to the server.** Success/failure counts per release are unknowable from the backend; the fleet view only shows last-seen versions.
7. **Email is not targeted.** Every active desktop user is announced to, whatever platform they use; nothing says a release is mandatory or explains the 24-hour rule; no per-release queued/sent/failed view.
8. **No mandatory-update screen with grace.** The mandatory dialog has no "Later" and no countdown; nothing blocks the application centrally after a deadline; shortcuts, the tray restore and background loads are not gated.
9. **Withdrawal semantics for an active mandatory policy are undefined.**
10. **Legacy clients** (≤ 1.3.2 in the field) understand `force_update` only as an immediate lockout; they cannot show a deadline. They need a documented, backward-compatible answer.

## 3. What is broken (pre-existing, outside this task's code scope)

- Both macOS download URLs on the website answer an IIS 404 (no `.dmg` MIME type) — every Mac download and update fails today.
- Production still offers 1.1.1 with dead GitHub URLs; the release credential was refused on the 1.3.2 run.
- Installers are unsigned; a production client refuses an unsigned installer by design, so **zero automatic updates can complete until certificates exist**.

## 4. Design decisions taken for the implementation

- **One source of version truth stays `desktop_releases` rows** (artifacts). A new `desktop_release_versions` record holds the version-level policy the administrator manages (title, targets, mandatory, grace hours, required minimum version, effective time, lifecycle, who). Rows registered by CI without a version record get one created (defaults: every configured platform, optional).
- **Grace periods are rows** (`desktop_update_requirements`): one per user × platform × required floor, created on the *first authenticated request* that meets an active mandatory policy, with `first_seen_at` and `deadline_at` on the server clock. Unique, never extended, never reset by a restart, a clock change or a repeated check. "Satisfied" is set server-side when the server itself observes the client at or above the floor.
- **Enforcement lives in `get_current_user`** (the one dependency every protected route goes through), reading the request context the middleware already fills. After the deadline a desktop client below the floor gets **HTTP 426 Upgrade Required** with a structured `detail` naming the required version, platform, deadline and the policy endpoint. An explicit allow-list of recovery paths stays open: auth, the update endpoints, stop/heartbeat/active, idle periods, every upload of already-captured data, client events, maintenance status. Nothing else.
- **Withdrawal of an active mandatory release suspends enforcement but never erases the deadline.** Enforcement requires an installable published release for the platform at or above the floor; a corrective release with the same floor re-arms the existing deadline. Documented and tested.
- **Legacy clients:** `force_update` in the update-check answer becomes true only once the grace has expired, so a 1.3.2 client shows its existing mandatory dialog at the deadline (and an ordinary offer before it); the backend refuses its protected operations with the same 426 whether or not it understood the answer.
- **Desktop:** the service persists the server's deadline (`updates.requirement`), wakes at the deadline, and raises `blocked_changed(True)` on the edge; `MainWindow` switches its stack to a mandatory-update page; `TimerService.start_tracking` refuses a new start while blocked; a running timer is never stopped by enforcement (uploads stay open; the update restart preserves the session). Install outcomes are reported to `POST /desktop/update-reports` with a client event id.

## 5. What cannot be verified from this repository

Real-device installation on Windows 10/11 and on Intel/Apple Silicon Macs; Windows Authenticode and macOS notarization (no certificates); live email provider delivery; the production backend's policy answers; the IIS `.dmg` fix. Each is listed as NOT VERIFIED or BLOCKED in the implementation report.

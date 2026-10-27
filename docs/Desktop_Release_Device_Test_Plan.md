# Desktop Release Management: Physical-Device Test Plan

**Scope:** what must be run on real machines before a mandatory release is ever published to staff, and how to record the result. Everything here is **NOT VERIFIED** until a person has run it on the named hardware; nothing in the automated suites stands in for it.

Automated and local-integration evidence lives in `docs/reports/`. This plan covers only what those cannot prove: a real installer, really executed, on the operating systems staff use.

## 0. Prerequisites (owner / server administrator)

| # | Prerequisite | Why |
|---|---|---|
| 1 | The IIS `.dmg` MIME type is live: `python desktop/tools/verify_download_urls.py --base-url https://staff.peakworkos.com/download_app_files/ --version <v> --artifacts <ci outputs>` prints three `OK` lines | Without it every macOS download is an HTML page |
| 2 | Signed installers, or an explicit pilot-ring decision | A production client refuses an unsigned installer by design; see §4 |
| 3 | The backend migration `f1a7c3e9d2b4` applied and the backend deployed (runbook §8.8) | The policy endpoints must exist |
| 4 | A **pilot** version registered and published as **optional** first (runbook §8) | The first real run must not be able to lock anyone out |
| 5 | A test account per tester (an ordinary `employee`), never a real member's account | Grace rows and reports are per user |

## 1. Machines

| Machine | OS | Account type | Notes |
|---|---|---|---|
| W10 | Windows 10 x64 22H2 | standard (non-admin) user | per-user install (`/CURRENTUSER`); also one run as an admin-installed machine-wide copy, expected to be refused with the manual path |
| W11 | Windows 11 x64 | standard user | same matrix |
| MAS | macOS 14+ on Apple Silicon | standard user, no write access to `/Applications` | exercises the `osascript … with administrator privileges` path; **never run anywhere yet** |
| MIN | macOS 12+ on Intel | standard user | same |

Each machine starts with the **previous** version installed from the download page, signed in as its test account, with a timer running for at least five minutes.

## 2. Checklist per machine

Record `PASS`, `FAIL` (with the log lines) or `BLOCKED` (with the reason) for each row. The log is `~/.monitra/logs` (Windows: `%USERPROFILE%\.monitra\logs`); every update step writes an `UPDATE_EVENT stage=… category=…` line.

| # | Step | Expected | Evidence to capture |
|---|---|---|---|
| 1 | Open the account menu → **Check for Updates** while nothing newer is published | "Checking for updates…" then "Monitra <installed> is the latest version." with the real installed version | screenshot |
| 2 | Administrator publishes the pilot version (optional, both platforms) | within a few seconds of the next check (or Check for Updates): the "Update available" prompt with Later and Update Now, installed and available versions, size, notes | screenshot |
| 3 | Click **Later**; restart Monitra | no prompt for 24 h; the account menu shows "Updates (1)" | log: `UPDATE_EVENT stage=postponed` |
| 4 | Administrator edits the version to **mandatory**, grace 1 h (pilot), then Check for Updates | "Update required" prompt with **Later**, a countdown near 1 h, the deadline in local time | screenshot; `GET /desktop/update-policy` for the account shows `grace.deadline_at` |
| 5 | Click Later; change the machine's clock forward by a day; restart | the countdown is unchanged (it is the server's); the app is **not** blocked early | screenshot |
| 6 | Set the clock back; disconnect the network; wait past the deadline | the "Update required" **page** replaces the dashboard while offline; the running timer keeps running | screenshot; log `UPDATE_BLOCKED` |
| 7 | Reconnect | still blocked; the backend answers 426 to a new start (observe in the log: `API_UPGRADE_REQUIRED`); Stop works | log |
| 8 | **Update Now** | download with progress, "Verifying…", then on Windows the installer runs with `/CURRENTUSER` and Monitra relaunches; on macOS the authorization dialog appears for a standard user, then relaunch | log `UPDATE_EVENT stage=install-started`, next launch `UPDATE_RESULT … exit_code=0` |
| 9 | After relaunch | toast "Monitra was updated to version <v>"; the same timer still running with the same start time; the dashboard is back; Check for Updates says "<v> is the latest version" | screenshot; the entry on the web Time Tracking page is one entry, not two |
| 10 | Administrator detail page | install reports show `success: 1` for this machine; grace periods `satisfied` | screenshot |
| 11 | Failure path: administrator publishes a version whose installer file is **not** on the website | Update Now fails with "The download server answered with a web page instead of the installer…", Try Again and Download manually offered; the old version keeps working; the detail page shows `download-failed` | screenshot, log `category=http-error` |
| 12 | Failure path (Windows only): copy an installer with a changed byte to the website under the published name | "could not be verified and has been discarded"; nothing installed | log `category=checksum-failed` |
| 13 | Unsigned installer on a production build (while §4 is outstanding) | refused: `category=signature-refused`, manual path offered | log |
| 14 | Withdraw the pilot version while a tester is still blocked | the next check releases the block (nothing installable); publishing a corrective version with the same floor re-blocks with the **original** deadline | logs on the tester's machine |

## 3. Platform-specific points

- **Windows machine-wide install** (installed by an administrator into Program Files): Update Now must be refused up front with the manual path, never attempted.
- **macOS**: run both the writable-`/Applications` case and the standard-user case; the authorization dialog declined (exit code 17) must leave the bundle untouched and the failure reported. Translocated bundles (run from the mounted `.dmg`) must be refused.
- **Both**: an OS sign-out during the download must not leave a partial file in `~/.monitra/updates`.

## 4. What stays blocked without signing certificates

| Path | Status without certificates |
|---|---|
| Windows: download, SHA-256, size, HTML-page refusal, 426 handling, the page, the timer | can be tested with the committed unsigned pilot build **only on a development/staging client** (`MONITRA_UPDATE_ALLOW_UNSIGNED=1`); a production build refuses the installer by design |
| Windows: Authenticode verification and signer pin | BLOCKED until `WINDOWS_CERTIFICATE*` exist and `policy.WINDOWS_SIGNER_PINS` is set |
| macOS: `codesign` / `spctl` / Team ID check in the helper | BLOCKED until the Apple Developer ID and notarization secrets exist |
| Any real auto-update on a staff machine | BLOCKED; do not publish a mandatory release to real users before this table is empty |

Do not record a row as PASS from a unit test, a simulated install or a screenshot of the dialog alone.

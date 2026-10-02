# Desktop auto-update — end-to-end verification (1.3.1 → 1.3.2)

**Status:** PARTIAL. The Windows upgrade path was exercised for real on an
isolated build, on a hidden desktop, with test-only trust hooks. It was not run
on a signed build, on a normal interactive desktop, on macOS, or against GitHub.
Read "What this does not prove" before relying on any line below.

## Method

Everything ran outside the repository and outside the installed Monitra.

- **Isolated builds.** Test-only installers built from the repository with a
  different Inno `AppId`, single-instance mutex, organisation name and data
  directory, so they could not touch the installed application or its data.
  "1.3.1" and "1.3.2" are builds of the same source stamped with those versions.
- **Local release host.** A local backend (dev database only) plus a local TLS
  server presenting a throwaway CA, serving `/dl/…` as a 302 to `/cdn/…`, so the
  download crossed a real redirect hop over TLS.
- **Hidden desktop.** The application ran on a separate Windows desktop
  (`CreateDesktopW`), driven only by UI Automation and posted window messages.
  No input was injected into the real session. Installer prompts that only
  exist on that desktop ("Select Setup Install Mode") were answered by the
  harness; that is a harness step, not user-facing behaviour.
- **Real pieces:** the update dialog, the download, SHA-256 verification, the
  detached helper, the Inno installer, the relaunch, session recovery, the
  backend release endpoints (register → readiness → publish), and a running timer.
- **Test hooks:** `MONITRA_UPDATE_EXTRA_HOSTS` and `MONITRA_UPDATE_ALLOW_UNSIGNED`
  were set on the test builds so the local host and an unsigned installer were
  accepted. Production builds ignore both.

## Results

| Scenario | What it did | Result |
|---|---|---|
| **S1** good upgrade | Timer running; newer version published; Later, then Update Now; download, verify, install, relaunch | **37/37** — re-run on the final helper |
| **S2** installer fails | Same, with an installer that exits non-zero | **37/37** — previous version intact, relaunched, failure reported honestly on next launch. Run on the *earlier* helper (see below) |
| **S3a** shipped 1.3.1 client, floor on | Backend floor 1.3.2 | **5/5** — update announced, not installable, no dialog |
| **S3b** shipped 1.3.1 client, floor off | The old client's own Update Now | **8/8** — installed 1.3.2 and relaunched, *because the app had already exited* |

S1 also shows: a single dialog, no duplicate entry, the **same backend time
entry** still running after the update, `duration == end − start` (the time the
installer ran is included, by design), login/session preserved, and the local
database intact. The next launch logged `UPDATE_RESULT … stage=started exit_code=0`.

### The helper bug this found

While validating S1/S2 the helper turned out to contain a stray TAB
(`System32<TAB>asklist.exe`), so its wait never ran and it fell through to a
3-second grace. S1/S2 passed anyway because Monitra exits quickly. It was fixed
(`c68f1c4`), the test that should have caught it was strengthened, and the fixed
helper was then run from a windowless parent: it waited for a 25 s process to
exit, started the installer 3.6 s later, and relaunched. S1 was re-run on the
fixed helper; S2 and S3 were **not**.

The shipped 1.3.1 helper, measured the same way from a windowless parent,
did not start the installer in the 27 s after a process that was alive at its
first check exited. It does work when the app has already exited (S3b). It
therefore depends on timing, which is why 1.3.1-and-earlier clients are
announce-only until they have moved to 1.3.2 by hand.

## What this does not prove

- **Signature enforcement.** No trusted certificate exists. The verifier is
  tested against real signed and unsigned binaries, but an end-to-end update of
  a *signed* build is NOT VERIFIED.
- **A normal interactive desktop.** A hidden desktop is not a user's session.
  Behaviour of the Inno dialogs, UAC and the relaunch there is NOT VERIFIED.
- **macOS (arm64 and Intel).** Never run on real hardware. NOT VERIFIED.
- **The release workflow on GitHub.** Never executed. NOT VERIFIED.
- **Hosting.** Artifacts were served from a local TLS host, not from the real
  one.
- **Soak and launch cycles** passed on the final code (soak PASS; 10/10 launch
  cycles via an isolated-mutex variant, because a running installed Monitra
  holds the stock script's mutex).

## Incident disclosed

Early in the harness work, two calls (`click_input` and a screen-region
screenshot) acted on the real screen before the hidden desktop was in place. The
image was deleted and the test app killed; everything afterwards used the hidden
desktop and no input injection. The installed Monitra was still present and
unmodified afterwards, but a click on the real screen cannot be fully accounted
for after the fact, so the owner should know it happened.

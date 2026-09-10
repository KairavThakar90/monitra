# Desktop Release Runbook

**Scope:** cutting, piloting, publishing, announcing and — when it goes wrong —
withdrawing a Monitra desktop release.

This is the *process* layer. The mechanics live elsewhere and are not repeated
here:

- `desktop/BUILD.md` — how each artifact is built, prerequisites,
  troubleshooting, and the clean-machine checklist.
- `desktop/version.py` — the single source of truth for the version number.
- `desktop/CHANGELOG.md` — the hand-written release note, one entry per version.
- `.github/workflows/desktop-release.yml` — build, test, package, checksum,
  draft release.
- `.github/workflows/desktop-stability.yml` — the correctness gate on every push.
- `docs/Desktop_Update_Distribution_Decisions.md` — the approvals this
  procedure implements.

---

## 1. Cutting a release

1. **Decide the version bump.** `major.minor.patch`, semver as `version.py`
   describes. Edit `VERSION` in `desktop/version.py`. Work on a branch.
2. **Write the changelog entry.** Add a `## [<version>]` section to
   `desktop/CHANGELOG.md` describing what changed *for the person installing
   it*. This is enforced: `tools/check_changelog.py` runs in CI and fails the
   build if the version in `version.py` has no entry. Auto-generated commit
   titles are not a substitute — they answer a different question.
3. **Run the full local gate**, from `desktop/`:

   ```bash
   python -m pytest tests/ -q
   python tools/check_architecture.py
   python tools/check_changelog.py
   python tests/soak/run_launch_cycles.py --cycles 10
   python tests/soak/run_soak.py --duration 60
   ```

   All five must pass before tagging.
4. **Merge to `main`**, then tag: `git tag v<version> && git push origin v<version>`.
5. **CI builds it.** Windows and both macOS architectures, each re-running the
   architecture check, the changelog check and the test suite *on that
   platform*, then packaging, smoke-testing the real binary, and computing a
   SHA-256 sidecar per artifact.
6. **A draft release opens automatically** with every artifact and checksum
   attached. Nothing is public yet, and this gate stays — a human publishes.

## 2. The pilot ring — mandatory before publishing

**No release goes to the whole team without a pilot.** This is the cheapest
defence available and it is pure process: no code, no infrastructure.

- Install **the CI-built artifacts**, not a local rebuild. The thing being
  tested is the thing being shipped.
- Pilot group: the engineering team, **plus at least one Windows and one macOS
  user who did not build the release**. A build only ever tested by its author
  has not been tested by a user.
- Run the clean-machine checklist in `BUILD.md` §14 — installing without admin
  rights, version metadata, login, tracking, offline and reconnect, uninstall,
  reinstall over the top, sleep/wake, force-kill recovery, and on macOS the
  permission grant/deny paths.
- Soak for **at least one full working day**, covering a real start-of-day
  login, a normal working session and a normal end-of-day shutdown. Most of
  what a pilot catches is not visible in the first ten minutes.
- Verify the checksum of at least one downloaded artifact, so the published
  sidecars are known to be correct rather than assumed to be.

Only when the pilot signs off does the draft get published.

## 3. Publishing and announcing

Publishing is **two** acts, and both are deliberate: the GitHub release
carries the bytes, the `desktop_releases` row is what makes clients and the
website offer them. CI has already registered a **draft** row per artifact
(`desktop/tools/register_release.py`), with each artifact's own SHA-256, size
and derived download URL.

1. **Publish the draft GitHub release.** Until this happens the asset URLs in
   the draft rows are not reachable, so do this first.
2. **Publish each release row.** For every artifact registered by CI:

   ```
   POST /desktop/releases/{id}/publish
   ```

   Requires the `manage_desktop_releases` permission. `GET /desktop/releases`
   lists the drafts with their ids. From the moment a row is published, the
   update check offers it to matching clients and the website's download page
   serves it — so publish the rows only after the pilot has signed off.

   Publish **every** artifact for the version, not just one. A version with
   only the Windows row published leaves every Mac user's download button
   saying "Not available yet".
3. **Nothing else needs changing.** The public download page
   (`/download` in the frontend) asks the backend for "the latest published
   release per platform" and renders whatever comes back. There is no
   frontend edit, redeploy, or URL to update when a version ships. The three
   `DESKTOP_LATEST_VERSION` / `DESKTOP_DOWNLOAD_URL` /
   `DESKTOP_RELEASE_NOTES_URL` settings still exist, but only as a **fallback**
   for a deployment that has registered no releases at all; as soon as one
   published row exists for a platform, the table wins and they are ignored.
   Do not use them to announce a release that has rows.
4. **Announce it**, naming three distinct downloads — Windows, macOS Apple
   Silicon, macOS Intel. macOS ships one build per architecture, deliberately
   (see `BUILD.md` §6), so an announcement that says "the Mac build" will
   generate support traffic.
5. Until code signing is in place, say plainly in the announcement that the
   installer is unsigned and what warning to expect. Training staff to click
   past a security warning without explanation is its own risk.

### Never publish a row with a placeholder URL

A published row is what the download button and the auto-updater both read. A
row whose `download_url` does not point at the real artifact turns the public
download page into a broken download, and there is no client-side check that
will save you — the desktop verifies the SHA-256 it was given, so a row that
is internally consistent but points somewhere fake still fails at the worst
moment. Register rows with `register_release.py`, which derives the URL and
computes the digest from the actual file, rather than by hand.

## 4. Retention policy

**A published GitHub release is never deleted.** Not after it is superseded,
not to tidy the list. Published release assets are the rollback inventory —
they are the only way to put a previous version back on someone's machine.

This is distinct from the 30-day `actions/upload-artifact` retention in CI,
which only covers unpublished runs. Publishing is what makes an artifact
permanent.

## 5. Withdrawing a bad release

1. **Stop the in-app prompt first.** Clear `DESKTOP_LATEST_VERSION` on the
   backend (or set it to the previous good version). The update notice stops
   recommending the bad build immediately, on every client, without shipping
   anything. This is the fastest lever available and it is why the endpoint
   reads configuration rather than the newest tag.
2. **Un-publish or clearly mark the GitHub release** so nobody downloads it.
3. **Tell affected users to install the previous version.** Both installers
   accept installing an older version over a newer one — there is no downgrade
   guard — so this works with no new code. On macOS, drag the older `Monitra.app`
   over the current one.
4. **User data is safe in both directions.** `~/.monitra` (the local database,
   the durable sync queue, the logs) lives outside the installation directory
   and is untouched by an install, an upgrade or a downgrade.
5. **Check who actually moved.** `GET /desktop/client-versions` shows the
   last-seen desktop version per user, so "did everyone come off the bad
   build" is answerable rather than assumed.
6. **Fix forward.** Cut a new patch version. Never re-publish a different build
   under a version number that has already shipped — a version must identify
   exactly one build, or every support report becomes untrustworthy.

## 6. Withdrawing a release, in the table

§5 describes the configuration-era lever. With release rows, the fastest and
most precise lever is the row itself:

```
POST /desktop/releases/{id}/rollback
```

The row is kept — it is the rollback inventory and the only thing that makes a
support report naming that build resolvable. It simply stops being offered, and
the previous published version becomes the newest again for both the update
check and the download page. Roll back **every** artifact of the bad version,
or a Windows user stops being offered it while a Mac user is still handed it.

## 7. What is still missing

Honest list, so nobody assumes otherwise:

- **Code signing.** Nothing is signed. **Both** CI jobs are now wired for it and
  need only the credentials — the Windows job signs `Monitra.exe` and the
  installer with `signtool` (RFC 3161 timestamped) before the checksums are
  computed, and the macOS job imports a certificate and passes an identity and
  notary profile to `build_macos.sh`. Each step is gated on its secret being
  present, so with the secrets absent the build produces unsigned artifacts and
  says so. Approved as a production-release requirement, and it is the stated
  prerequisite in `docs/Desktop_Update_Distribution_Decisions.md` §2 for
  publishing a release to real users. Registering drafts and piloting them is
  fine meanwhile.
- **Auto-update (Phase 1).** **Built** (2026-09-08) as the approved *prompted*
  update — see `background_services/update/` and the decision record. It is not
  yet safe to switch on for real users, for the signing reason above, and no
  build has yet been installed, superseded and updated on a real machine.
- **macOS on real hardware.** Every macOS build so far has been produced and
  smoke-tested in CI only, which now also covers the macOS *update* path.
  The first real-world macOS install will also be the first real test — the
  pilot ring matters more, not less, for that platform.
- **CI secrets for release registration.** `MONITRA_API_BASE_URL` plus the
  release account's own credentials, `MONITRA_RELEASE_EMAIL` and
  `MONITRA_RELEASE_PASSWORD` — an account holding the `release_bot` role, whose
  entire authority is `manage_desktop_releases`. Credentials rather than a
  token because an access token expires after thirty minutes and one stored in
  a repository secret would be dead long before the next release;
  `register_release.py` mints a token per run and never stores it.
  `MONITRA_RELEASE_TOKEN` is still honoured for registering a build by hand
  from an existing session, and is unset in CI. Without these the release job
  skips registration and says so; the artifacts and the GitHub release are
  unaffected, and the rows can be registered by re-running that step later.

- **The registration sign-in path depends on the deployment not declaring
  itself production.** `register_release.py` authenticates through
  `POST /auth/dev-login`, and `backend/app/api/auth.py` returns 404 for that
  route whenever `settings.ENV == "production"`. The deployment at
  `monitra-lvzq.vercel.app` currently runs with `ENV=development`, which is why
  the path works today. Setting `ENV=production` there — correct in itself —
  would break CI release registration until the release account is given a
  sign-in route that survives it. Noted here so the two are changed together.

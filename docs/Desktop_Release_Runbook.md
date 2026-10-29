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
   describes. Edit `VERSION` in `desktop/version.py`. Work on a branch. **A
   version that has been built is spent**: 1.3.2 was built twice from different
   commits, so the next change after a build is always a new number.
2. **Write the changelog entry.** Move everything under `## [Unreleased]` in
   `desktop/CHANGELOG.md` to a `## [<version>]` section describing what changed
   *for the person installing it*, leaving `[Unreleased]` empty. This is
   enforced twice: `tools/check_changelog.py` fails the build if the version in
   `version.py` has no entry, and on a tag build (`--release`) it also fails if
   anything is still filed under `[Unreleased]`. Auto-generated commit titles are
   not a substitute — they answer a different question.
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

## 1a. Internal test builds

A build for the pilot group **before** a version is cut sets `PRERELEASE` in
`desktop/version.py` (`"beta.1"`, `"beta.2"`, …) next to a numeric `VERSION`
that has never shipped. Mechanics are in `desktop/BUILD.md` §10; the process
rules are:

1. **It is never registered and never published.** `register_release.py`
   refuses a pre-release outright, so no `desktop_releases` row exists for it,
   the update check never offers it, and the download page never shows it.
   Testers get the installer by hand, with its `.sha256`.
2. **Do not push a `v*` tag for it.** Build it locally (Windows) or with
   `workflow_dispatch` on the branch with *Attach the artifacts to a GitHub
   release* left **off** — that produces workflow artifacts only, no GitHub
   release and no registration attempt.
3. **The changelog entry is `## [<version>-<prerelease>]`**, written for the
   testers; the gate requires it.
4. **The production release that follows uses a different `VERSION`** with
   `PRERELEASE = ""`. A version identifies exactly one build, and a tester on
   `1.2.0-beta.1` is only offered the production build by the updater if that
   build's number is strictly greater than `1.2.0`.
5. Everything else in this runbook — the local gate, the pilot checklist, one
   full working day of use — applies unchanged.

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
carries the bytes, the `desktop_releases` rows are what make clients and the
website offer them. CI has already registered a **draft** row per artifact
(`desktop/tools/register_release.py`), with each artifact's own SHA-256, size,
derived download URL and — read back from the finished file — whether its code
signature was verified.

The release job refuses to start publishing at all unless the whole set is
sound (`desktop/tools/verify_release_artifacts.py`): all three installers
present once and named for this version, each matching its checksum, the tag
agreeing with `version.py`, and — when the repository variable
`REQUIRE_SIGNED_RELEASE` is `true` — every one validly signed.

1. **Put the installers where clients download them: the website.** The
   source repository is **private** (since October 2026), so a GitHub release's
   assets answer `404` to every installed client, which carries no GitHub
   credential and must never be given one. The clients (1.3.3+) and the backend
   fetch from `https://staff.peakworkos.com/download_app_files/<built filename>`,
   the same folder the download page's three buttons use, and that is what the
   registered rows point at when the repository variable
   `RELEASE_DOWNLOAD_BASE_URL` names it ([Desktop_Artifact_Hosting.md](Desktop_Artifact_Hosting.md) §1, §4).

   *Operator steps, in this order — none of them is automated yet:*

   1. Download the three installers from the run's artifacts (or the draft
      GitHub release) and check each against its `.sha256` sidecar.
   2. Copy them, **under their built names**, into
      `frontend/public/download_app_files/`, bump `DOWNLOAD_VERSION` in
      `frontend/src/api/desktopRelease.ts` to this version (and the pinned
      paths in `frontend/src/api/__tests__/desktopRelease.test.ts`), run
      `npm run build && npm run lint`, commit, and deploy the frontend.
      Remove the previous version's files only after the new rows are
      published and `GET /desktop/releases/downloads` names this version.
   3. **Prove the downloads** before touching the backend:

      ```
      python desktop/tools/verify_download_urls.py \
          --base-url https://staff.peakworkos.com/download_app_files/ \
          --version <version> --artifacts <folder with the CI outputs>
      ```

      All three must print `OK` with matching digests. On 2026-10-27 the
      website served the `.exe` and an HTML `404` for both `.dmg` files: IIS
      had no MIME type for `.dmg`. That is a one-time server fix
      ([Desktop_Artifact_Hosting.md](Desktop_Artifact_Hosting.md) §7.7) and
      the reason this check exists.
   4. Publishing the draft GitHub release is now optional (archive and
      rollback inventory for people with repository access); the clients never
      read it. Google Drive, if used to hand installers to someone, is not a
      release host and is never registered.

   If the release job's **Register the release with the backend** step failed
   (it fails the job on a refused credential since 1.3.3), fix the cause —
   usually `MONITRA_RELEASE_CREDENTIAL`, see §8 — and re-run the job: rows
   already registered answer `409` and count as done.
2. **Ask whether the version is ready**, as a signed-in administrator:

   ```
   GET /desktop/releases/versions/{version}/readiness
   ```

   It lists *every* problem at once — a platform with no row, a row whose URL is
   not on an approved host or whose file name is not this version's installer, a
   missing size or checksum, a row registered as unsigned — so a release is fixed
   in one pass rather than one refusal at a time. `ready: true` is the same
   judgement publishing applies.
3. **Publish the whole version together:**

   ```
   POST /desktop/releases/versions/{version}/publish
   ```

   All or nothing: Windows and both macOS architectures go live in one commit, or
   none does. (Publishing a single row, `POST /desktop/releases/{id}/publish`, is
   still possible and applies the same gate — it is refused while the rest of the
   version is missing or invalid.) From the moment a row is published, the update
   check offers it to matching clients and the website's download page serves it
   — so publish only after the pilot has signed off.

   **Who can:** a signed-in administrator holding `manage_desktop_releases`. The
   CI release credential holds the same permission so it can *register* drafts,
   but publishing, withdrawing and changing a row's status are refused for it
   outright — a leaked pipeline secret costs a stray draft, never a release.
4. **The announcement email follows by itself — and only when you have turned it
   on.** `RELEASE_EMAIL_ENABLED` is **off by default**. When on, the email is
   queued once **every required artifact is published** (not at the first), once
   per user per version (a unique constraint in the outbox), to active users —
   the release pipeline's own account and invited external clients are excluded.
   Before the first real send, rehearse it:

   ```
   RELEASE_EMAIL_TEST_RECIPIENTS=you@example.com
   ```

   While that is set the announcement goes **only** to those addresses, tagged
   `[TEST]`, and to no user — and uses its own dedupe keys, so rehearsing never
   consumes the real announcement. Remove it to go live. Delivery is paced by the
   outbox sweeper (20 per 5-minute sweep by default), so a large audience takes
   a while; raise `EMAIL_DISPATCH_BATCH_SIZE` if that matters.
5. **Nothing else needs changing.** The public download page
   (`/download` in the frontend) asks the backend for "the latest published
   release per platform" and renders whatever comes back. The three
   `DESKTOP_LATEST_VERSION` / `DESKTOP_DOWNLOAD_URL` /
   `DESKTOP_RELEASE_NOTES_URL` settings still exist, but only as a **fallback**
   for a deployment that has registered no releases at all; as soon as one
   published row exists for a platform, the table wins and they are ignored.
   Do not use them to announce a release that has rows.
6. **Announce it**, naming three distinct downloads — Windows, macOS Apple
   Silicon, macOS Intel. macOS ships one build per architecture, deliberately
   (see `BUILD.md` §6), so an announcement that says "the Mac build" will
   generate support traffic.
7. Until code signing is in place, say plainly in the announcement that the
   installer is unsigned and what warning to expect. **An unsigned installer is
   also refused by the in-app updater in production** — people on it must
   download the new version by hand (the update dialog offers a "Download
   manually" button when an automatic install is refused or fails).

### Never publish a row with a placeholder URL

A published row is what the download button and the auto-updater both read. The
backend now refuses to register or publish a row whose URL is not on an approved
host, is not https, or does not name the installer this version's build scripts
produce — but it cannot tell that a well-formed URL points at the *right file*.
Register rows with `register_release.py`, which derives the URL and computes the
digest from the actual file, rather than by hand.

## 4. Retention policy

**A published GitHub release is never deleted.** Not after it is superseded,
not to tidy the list. Published release assets are the rollback inventory —
they are the only way to put a previous version back on someone's machine.

This is distinct from the 30-day `actions/upload-artifact` retention in CI,
which only covers unpublished runs. Publishing is what makes an artifact
permanent.

## 5. Withdrawing a bad release

**Use the rows (§6); the configuration lever below only works for a deployment
that has registered none.** Once any published row exists for a platform the
table wins and `DESKTOP_LATEST_VERSION` is ignored, so clearing it does *not*
stop the in-app prompt — an earlier version of this section said it did, and was
wrong for every deployment that has used the release table.

1. **Stop the offer first:** `POST /desktop/releases/{id}/rollback` for **every**
   artifact of the bad version (§6). Clients stop being offered it on their next
   check.
2. **Un-publish or clearly mark the GitHub release** so nobody downloads it by
   hand.
3. **Tell affected users to install the previous version.** Both installers
   accept installing an older version over a newer one — there is no downgrade
   guard — so this works with no new code. On macOS, drag the older `Monitra.app`
   over the current one. **The updater cannot do this for them:** it only ever
   moves forward (an equal or older version is never an update, on the server or
   the client), so a client already on the bad build is not told to go back.
   Fix forward with a new patch version, which *is* an update.
4. **User data is safe in both directions.** `~/.monitra` (the local database,
   the durable sync queue, the logs) lives outside the installation directory
   and is untouched by an install, an upgrade or a downgrade.
5. **Check who actually moved.** `GET /desktop/client-versions` shows the
   last-seen desktop version per user, so "did everyone come off the bad
   build" is answerable rather than assumed.
6. **Fix forward.** Cut a new patch version. Never re-publish a different build
   under a version number that has already shipped — a version must identify
   exactly one build, or every support report becomes untrustworthy.
7. **An announcement email cannot be unsent.** That is the strongest reason the
   email is off by default, rehearsed in test mode first, and sent only when the
   whole version is live.

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

- **Code signing.** Nothing is signed yet. Both CI jobs are wired for it and
  need only the credentials: the Windows job signs `Monitra.exe` **before** the
  installer is built (so the installer contains a signed program), then signs the
  installer, then reads the signature back with the same verifier the desktop
  uses; the macOS job imports a certificate and passes an identity and notary
  profile to `build_macos.sh`, then verifies. Each step is gated on a step
  *output* decided once from the secrets. (The earlier `if: env.X != ''`
  conditions could never be true and left signing silently skipped even with the
  secrets set; `tests/test_release_pipeline.py` now fails on that pattern.)
  **The desktop updater refuses an unsigned installer in a production build**, so
  until a certificate exists no auto-update can complete — by design. Approved as
  a production-release requirement; see `Desktop_Update_Distribution_Decisions.md`
  §2 and `Desktop_Artifact_Hosting.md` §7.
- **Auto-update.** Built, hardened and tested (see `desktop/ARCHITECTURE.md`,
  "Update notice and installer"), but **not yet safe to switch on for real
  users**: no certificate, the publisher is not yet pinned, the macOS path has
  never run on a real Mac, and a first real end-to-end update on a signed build
  has not happened.
- **A first real end-to-end update on real hardware.** The Windows helper has been
  run for real against stand-in executables (and a defect that would have left a
  user with Monitra closed and no update was found and fixed that way) but not
  against a real signed Inno Setup upgrade of a real installation.
- **macOS on real hardware.** Every macOS build so far has been produced and
  smoke-tested in CI only, which also covers the macOS *update* path.
- **The source repository is private and the website is the release host**
  (2026-10-27). Still to do, all owner-side: set the repository variable
  `RELEASE_DOWNLOAD_BASE_URL=https://staff.peakworkos.com/download_app_files/`;
  set `DESKTOP_DOWNLOAD_URL_PREFIXES` to the same value in production; give
  IIS a MIME type for `.dmg` (the Mac buttons answer 404 today). Copying the
  artifacts to the website is a manual step (§3.1).
- **The production release table is stale.** On 2026-10-27 it offered 1.1.1
  on every platform, with GitHub URLs that answer 404. Nothing newer was ever
  registered: the 1.3.2 run's registration answered 401 and the step was
  non-fatal. The 1.1.1 rows should be rolled back once 1.3.3 is published, so
  no client is pointed at a dead link.
- **CI secrets for release registration.** `MONITRA_API_BASE_URL` plus
  `MONITRA_RELEASE_CREDENTIAL`. Without these the release job skips
  registration and says so; **with them set and refused, the job now fails**
  (1.3.3), and is re-run once the credential is fixed. Rotate it against
  production with `provision_release_credential.py` (§8) — the key stored today
  was refused by production on the 1.3.2 run.
  `MONITRA_RELEASE_TOKEN` is still honoured for registering a build by hand
  from an existing session, and is unset in CI.
- **Clients older than 1.3.3 cannot self-update at all**: the backend floor
  (`DESKTOP_AUTO_UPDATE_MIN_CLIENT_VERSION`) makes them announce-only, and they
  trust only `github.com` for downloads. The 1.3.3 announcement must say
  "install this one by hand". Adoption is visible in `GET /desktop/client-versions`.
- **The reminder policy** (1.3.3): "Later" hides the prompt for 24 h, it returns
  at the first check after that, and after three postponements or seven days it
  is a *final reminder* — still optional. Only `force_update` /
  `min_supported_version` on the row make an update mandatory, and neither has
  been used for real yet.
- **The release account still has to be provisioned.** Nothing creates it
  automatically. See §8.
- **Rollback cannot downgrade a client already on the bad build** (§5.3).

## 8. The release credential

CI authenticates with a **service credential** — a long-lived API key belonging
to a machine, presented as an ordinary bearer token. The account behind it holds
the `release_bot` role, whose entire authority is `manage_desktop_releases`, and
it has **no password at all**: the key is the only way in, and revoking it
closes that door completely.

Why not the two obvious alternatives:

- **Not a password.** The password path is `POST /auth/dev-login`, which returns
  404 whenever `ENV=production`. Registration used to go through it, which meant
  the deployment had to stay in development mode to keep one build step working.
  That is why this changed (2026-09-10); production can now run as production.
- **Not an access token.** Those expire after thirty minutes, so one stored in a
  repository secret is dead long before the next release.

### Provisioning, rotating and revoking

All of it goes through one script, run by a person against a named database:

```bash
cd backend
python scripts/provision_release_credential.py show      # read-only
python scripts/provision_release_credential.py issue --email <address>
python scripts/provision_release_credential.py rotate --name github-actions-release
python scripts/provision_release_credential.py revoke --key-id <id>
```

Every run prints which database it resolved before doing anything, and any run
that writes refuses a target other than the development database unless
`--i-am-sure` is passed. To act on production, export `DATABASE_URL` for that
one command.

The key is printed **once** and stored only as a SHA-256; nothing can read it
back. Paste it straight into the repository secret `MONITRA_RELEASE_CREDENTIAL`.
If it is lost, rotate — mint the replacement, update the secret, then revoke the
old key, in that order, so no window exists in which a release cannot be
registered.

Each key has a non-secret `key_id` carried inside it. That is what logs, this
runbook and `revoke` name; the secret half is never written down anywhere.

### Verifying it end to end

With a server running, `backend/scripts/smoke_release_credential.py` mints a
key, registers and amends a release through the real API, checks that the same
key is refused on the member directory, the employee list, the fleet view and
the web-session handoff, checks that ordinary admin and employee sign-in still
work, and revokes the key. Run it against a deployment in production mode to
confirm the whole arrangement, including that `/auth/dev-login` is gone.

---

## 8. Release management: targets, mandatory updates and the 24-hour grace period

**Since 2026-10-27.** A release is a *version record* an administrator manages
(Admin → Settings → Desktop Releases, or `/desktop/releases/versions`), on top of
the artifact rows CI registers. The record carries what was decided about the
version; the artifact rows carry what a client downloads. The version record is
the single place a policy is written, so the three artifacts of one version
cannot disagree.

### 8.1 The record

| Field | Meaning |
|---|---|
| `target_platforms` | `win32`, `darwin`, or both. A platform not named is never offered the version and never held to its policy. Readiness and publication require only the artifacts for the named platforms (Windows installer; both Mac builds). |
| `mandatory` | Every targeted client below the floor must take it after the grace period. Optional (the default) blocks nobody. |
| `grace_hours` | Default 24. Counted per user, per platform, from the first authenticated request the server sees from a desktop below the floor. |
| `required_min_version` | The floor. Mandatory: this, or the version itself when empty. Optional: a set floor still requires only the clients below it. Never newer than the version. |
| `effective_at` | The policy takes effect no earlier than this; empty means at publication. |
| `status` | `draft` → `validated` → `published` → `withdrawn`; `superseded` is derived (a newer published version covers every platform this one targets). |

A CI registration (`register_release.py`) creates the record as an optional draft
for every configured platform; edit it before publishing.

### 8.2 What publishing does

`POST /desktop/releases/versions/<v>/publish` (a signed-in administrator; the CI
credential is refused) moves the required artifact rows and the record to
published in one commit with an audit row (`activity_logs`, module `system`,
`release_published`), drops the policy cache, and queues the announcement to the
users last seen on the targeted platforms (a user with no known platform is
included only by a both-platform release). The email states whether the update
is mandatory and the grace rule.

### 8.3 The grace period, on the server's clock

The first time the server sees a user's desktop below an active floor -- on
*any* authenticated request, not only the update check -- it files one
`desktop_update_requirements` row for that user, platform and floor:
`first_seen_at`, `deadline_at = first_seen_at + grace_hours`. The row is unique;
nothing extends it: another device, a restart, a cleared local cache, a changed
system clock or a thousand repeated checks read the same row. `satisfied_at` is
written by the server when it observes the client at or above the floor. A
desktop that names no supported platform is held to every platform's policy
under the key `unknown`, and an existing deadline for the same floor under any
platform is the deadline, so changing the claim cannot buy time.

Before the deadline the desktop shows the prompt with **Later** and a countdown
read from the server's clock. After it, the backend refuses the client's
protected routes with **HTTP 426** (`detail.code = desktop_update_required`,
the required version, platform, deadline and `policy_url`), and the desktop
shows the update-required page instead of the dashboard. The routes that stay
open are the recovery and finishing paths only: `/auth/*`, the update
endpoints, `/time-entries/active`, stop, heartbeat, the uploads of captured
activity, app usage, URLs, screenshots and events, idle periods, the client
event trail, maintenance status. A running timer keeps running; a new start is
refused (by the backend, and by the desktop's timer service itself).

### 8.4 Withdrawing, and a corrective release

`POST /desktop/releases/versions/<v>/withdraw` moves every artifact row to
`rolled_back` and the record to `withdrawn`: the version stops being offered at
once and the previous published version is newest again. **Grace periods
already running are kept, never erased.** The policy engine enforces a floor
only while an installable published release at or above it exists for the
platform, so with the release gone nothing is refused -- a lockout with nothing
to install would be a dead end. Publish the corrective release **with the same
floor** (`required_min_version = <the withdrawn version>`) and every deadline
resumes exactly where it was; publish it with a new floor and each user gets a
fresh grace period. The withdraw answer says how many grace periods are
waiting.

### 8.5 Legacy clients (1.3.2 and earlier)

They read `force_update` only, as "lock out now". The update check answers
`force_update: true` only once the grace period has expired, so they see their
existing mandatory dialog at the deadline and an ordinary offer before it. The
426 gate applies to them exactly as to a new client; what they render for it is
a connection error, and the dialog's link is the way out.

### 8.6 Watching a rollout

`GET /desktop/releases/versions/<v>` (or the panel's detail): targeted users,
grace periods pending/expired/satisfied, install reports by outcome
(`POST /desktop/update-reports`, idempotent on the desktop's event id),
adoption on or above vs below by last-seen version, the announcement's outbox
counts (`sent` is provider acceptance, not delivery), and the audit history.

### 8.7 The macOS downloads and IIS

**Found 2026-10-27, measured on the live website:** `Monitra-Setup-1.3.2.exe`
answers `200 application/octet-stream` (32,478,743 bytes, digest equal to the
committed file); both `.dmg` files answer an IIS `404 text/html` page although
the files are in the folder. IIS serves a static file only when its extension
has a MIME type, and `.dmg` has none by default.

**Repository fix that was tried and withdrawn (2026-10-27 to 2026-10-29):** a
directory-scoped `frontend/public/download_app_files/web.config` mapping `.dmg`
(and `.sha256`) for that folder only. After the frontend deploy that carried it,
**every** URL under `/download_app_files/` -- the `.exe`, both `.dmg`, the
sidecars, a missing file, the folder itself -- answered IIS's generic `500` page
while the site root and `/download` answered `200` (measured 2026-10-29). A
folder `web.config` that IIS cannot apply fails the whole folder, and the folder
had served the `.exe` without one two days earlier, so the file was removed from
the repository (commit after `b279381`). The exact IIS complaint is only visible
on the server (IIS detailed error, usually `500.19`); the two usual causes are
the `staticContent` section being locked for override below the site, and a
`<remove>` of an extension the parent does not define. **The MIME type is set at
the site level instead, by hand (step 1 below), and the deployed folder must not
contain a `web.config`.**

**Second finding:** a *missing* installer URL under that folder answers
`200 text/html` with the site's index page (a single-page-application rewrite),
not `404`. The desktop's downloader now refuses any `text/*` answer before
reading it ("The download server answered with a web page instead of the
installer", category `http-error`), and `verify_download_urls.py` already
refuses an HTML body. A published row must still point at a file that exists:
copy the installers **before** publishing, and prove them (below).

**Administrator steps on the server, after the next frontend deploy** (or by
hand before it):

1. On the server, **delete `download_app_files\web.config`** from the deployed
   site if it is there (a deploy made between 2026-10-27 and 2026-10-29 carried
   it), then IIS Manager → Sites → the `staff.peakworkos.com` site (the site
   node, not the folder) → **MIME Types** → add `.dmg` =
   `application/x-apple-diskimage` if it is not listed (optionally `.sha256` =
   `text/plain`). Re-check `curl -sI .../download_app_files/Monitra-Setup-<v>.exe`
   answers `200`; a `500` means a folder `web.config` is still present or the
   site-level change did not take.
2. From any machine outside the server:
   ```
   curl -sI https://staff.peakworkos.com/download_app_files/Monitra-macOS-arm64-1.3.2.dmg
   curl -sI https://staff.peakworkos.com/download_app_files/Monitra-macOS-x86_64-1.3.2.dmg
   ```
   Both must answer `HTTP/1.1 200 OK` with `Content-Type: application/x-apple-diskimage`
   and the file's `Content-Length` (55,273,984 and 62,060,924 bytes for 1.3.2). A
   `text/html` content type, whatever the status, means the server is still
   answering with a page.
3. From the repository, the authoritative check (it downloads the files the
   way an installed client does and compares digests):
   ```
   python desktop/tools/verify_download_urls.py --base-url https://staff.peakworkos.com/download_app_files/ --version 1.3.2 --artifacts frontend/public/download_app_files
   ```
   must print three `OK` lines and exit 0. Keep the output with the release record.
4. If the folder answers `500`, a `web.config` is present in the deployed
   `download_app_files` folder: delete it (step 1). The repository no longer
   ships one; do not add one back without first proving it on the server with
   `curl -sI` against the `.exe`.

### 8.8 The release credential (`MONITRA_RELEASE_CREDENTIAL`)

**What the code does (verified by `desktop/tests/test_register_release.py`,
`backend/tests/test_service_credential.py`):** the pipeline presents the key as
`Authorization: Bearer msk_<key id>_<secret>`, whitespace-stripped; the backend
finds the row by the public key id, compares the SHA-256 of the whole key in
constant time, refuses revoked and expired keys and any account that is not
`release_bot`, and answers one 401 for every reason while logging the reason:
`SERVICE_CREDENTIAL_REFUSED: unknown key_id=… | bad secret | revoked | expired |
account unavailable`. Nothing in that path was found wrong.

**Why the 1.3.2 run was refused:** the backend that `MONITRA_API_BASE_URL`
named answered 401 to a key that is not in *its* database. Production moved to
the company server in October 2026 and its database is not the one any
repository `.env` names; a key issued against the development database (or
the previous host) is `unknown key_id` on the current one. The exact reason is
one line in the production backend log; the repository cannot read it, and
neither `MONITRA_API_BASE_URL` nor the secret can be inspected from here.

**What changed (2026-10-27):** `register_release.py` now proves the credential
with one `GET /auth/me` **before** registering anything, prints the credential's
public key id (never the secret) and, on a refusal, names the remedy and exits 1
with no row created. A run can no longer register half a set under a refused
key, and an operator can match the printed key id against
`provision_release_credential.py show` on the production database.

**Administrator steps (on the production server, never from a laptop):**
1. `cd backend && python scripts/provision_release_credential.py show` --
   prints the host and database it resolved and the key ids it holds. If the
   key id the failed run printed is not there, the secret in GitHub was issued
   elsewhere.
2. `python scripts/provision_release_credential.py rotate --name github-actions-release --i-am-sure`
   -- mints a new key (shown once) and revokes the old.
3. Paste it into the repository secret `MONITRA_RELEASE_CREDENTIAL`; confirm the
   secret `MONITRA_API_BASE_URL` is the production API's `https://…/api/v1`.
4. Re-run the "Register the release with the backend" step of the last tag
   build: the log must show `credential: service key id <id>` and
   `accepted: the backend takes it for release_bot (role release_bot)`, then
   `registered (201)` or `already registered` per artifact.

### 8.9 The production database migration (owner, later)

Exactly one revision: `f1a7c3e9d2b4` (adds `desktop_release_versions`,
`desktop_update_requirements`, `desktop_update_reports`; touches no existing
table). Verified locally: `upgrade head`, `downgrade e9f5a3b7c2d4`, `upgrade
head` again on PostgreSQL 18, constraints enforced.

1. Take a backup of the production database first.
2. On the server, from `backend/`, with the production `DATABASE_URL` exported
   for that shell only (never written into a repository file):
   ```
   python -m alembic current          # must print e9f5a3b7c2d4 (head)
   python -m alembic upgrade head     # one revision, transactional DDL
   python -m alembic current          # must print f1a7c3e9d2b4 (head)
   ```
3. Rollback, if ever needed: `python -m alembic downgrade e9f5a3b7c2d4`. It
   drops the three tables; grace periods in progress are lost; the update
   check falls back to the artifact rows' own flags.
4. Deploy the backend built from this branch in the same window; until the
   backend is deployed the tables are unused, and until the migration is
   applied the new endpoints answer 500.

### 8.10 Running everything locally

```
# a throwaway PostgreSQL (never the development or production database)
#   initdb -D <dir> -U postgres --auth=trust ; pg_ctl -D <dir> -o "-p 58432" start
#   psql -h 127.0.0.1 -p 58432 -U postgres -c "create database monitra_e2e"
# schema from the models + stub organisation + status catalogues + alembic stamp:
#   see backend/tests/pg_support.py (the same recipe), then
export DATABASE_URL=postgresql+psycopg2://postgres@127.0.0.1:58432/monitra_e2e
export DATABASE_URL_DEV=$DATABASE_URL ENV=development
export EMAIL_PROVIDER=console RELEASE_EMAIL_ENABLED=true EMAIL_DISPATCH_TOKEN=local-token
export GOOGLE_DRIVE_ROOT_FOLDER_ID= WFPM_TIMER_START_URL= WFPM_TIMER_STOP_URL=
export DESKTOP_DOWNLOAD_ALLOWED_HOSTS=staff.peakworkos.com
export DESKTOP_DOWNLOAD_URL_PREFIXES=https://staff.peakworkos.com/download_app_files/
export DESKTOP_REQUIRE_SIGNED_RELEASES=false      # pilot setting, local only

cd backend  && python -m uvicorn app.main:app --host 127.0.0.1 --port 58080   # backend
cd frontend && VITE_API_BASE=http://127.0.0.1:58080/api/v1 npx vite --host 127.0.0.1 --port 5173 --strictPort
cd desktop  && SMS_API_BASE_URL=http://127.0.0.1:58080 MONITRA_ENV=development python main.py   # desktop (from source)

# an administrator and a member for the rig (printed as JSON with a 2 h token):
cd backend && python tests/e2e_support.py provision administrator
cd backend && python tests/e2e_support.py provision

# the whole release journey, automated, against that rig:
cd desktop && MONITRA_E2E=1 python -m pytest tests/e2e/test_release_management_e2e.py -q -s
```

To publish a local test release by hand: Admin → Settings → Desktop Releases →
**+ New release** (version newer than `desktop/version.py`, both platforms,
optional, "Enter the installers now" with URLs under the website folder and
any 64-hex digest), **Validate**, **Publish**; then in the desktop, account menu
→ Check for Updates. Update Now will end at the website's answer for a file that
is not there -- the honest outcome, reported on the detail page as
`download-failed`. To exercise the mandatory path, edit the version to
mandatory with a short grace and move the grace row's `deadline_at` on the
throwaway database; the backend answers 426 and the desktop shows the page.

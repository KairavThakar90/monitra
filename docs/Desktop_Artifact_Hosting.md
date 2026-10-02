# Desktop Artifact Hosting, Signing and the Update Trust Model

**Status:** written 2026-10-02 with the auto-update hardening. It records what
is true *today*, what must change to keep the source repository private, and the
order to do it in. Nothing in it has been applied to GitHub or to production —
repository visibility, repository variables/secrets, backend environment and
production data are the owner's to change.

Read with [Desktop_Release_Runbook.md](Desktop_Release_Runbook.md) (the process),
[Desktop_Update_Distribution_Decisions.md](Desktop_Update_Distribution_Decisions.md)
(the approvals) and `desktop/ARCHITECTURE.md` ("Update notice and installer").

---

## 1. The question this answers

> Can an ordinary installed user download an update without any GitHub
> credential — and can the source repository stay private?

**Today: yes to the first, and only because the source repository is public.**

Checked on 2026-10-02 with unauthenticated requests:

| Request | Result |
|---|---|
| `GET https://api.github.com/repos/KairavThakar90/monitra` | `200`, `"private": false` |
| `GET …/releases/download/v1.3.1/Monitra-Setup-1.3.1.exe` | `302` → `release-assets.githubusercontent.com/…` → `200` |
| The updater's own downloader, production policy, against that URL | downloaded 31,921,460 bytes, SHA-256 matched the published digest |

So releases published from this repository are anonymously downloadable. That
property is **inherited from the repository's visibility**: GitHub has no
per-asset public switch, and a private repository's release assets answer `404`
to anyone without access. The desktop client carries no GitHub credential, and
must never be given one (any value shipped in an installer can be read out of
it by every user).

**Consequence: making this repository private, by itself, breaks every
download and every auto-update.** Do not change its visibility until §5 is done.

## 2. What a user's machine does

```
Monitra (installed)                               Monitra backend           Release host
  │  every ~10 h:  GET /desktop/latest-version ──▶ policy + table
  │      headers: User-Agent Monitra/1.3.1,        ◀── {latest, download_url,
  │               X-Monitra-Platform/-Arch              sha256, size, force…}
  │  client re-checks: strictly newer? this platform/arch? host approved?
  │  dialog → [Later] / [Update Now]
  │  Update Now:  GET download_url  ───────────────────────────────▶ github.com
  │               ◀── 302 (https, approved CDN host only) ───────────  CDN
  │               hash while streaming; size; free space
  │  signature verified (WinVerifyTrust) — on the task pool
  │  helper script written, detached;  Monitra exits as a *restart*
  ▼
helper: wait for exit → run installer → record result → relaunch
  Monitra relaunches, recovers the running session, reads the result file,
  tells the user whether the update happened.
```

No GitHub account, token or login is involved at any step.

## 3. Trust model — what is checked, where

| Property | Checked by | Where it is enforced |
|---|---|---|
| The URL is https, on an approved host, no credentials, default port | backend | `desktop_release_policy.check_download_url` — at registration, at publish, and again when the update check would serve the row |
| …and again, by the client, regardless of what the backend says | desktop | `background_services/update/policy.py` — `ALLOWED_DOWNLOAD_HOSTS` is a constant compiled into the build |
| Every redirect hop stays https and on an approved host / the GitHub CDN | desktop | `downloader.py` follows redirects by hand; an https→http hop, or a host outside `ALLOWED_REDIRECT_HOST_SUFFIXES`, is refused *before it is requested* |
| The file is the one the backend named | desktop | SHA-256 computed while streaming, size compared; a mismatch deletes the file |
| The file is signed, and by whom | desktop | `signature.py` — `WinVerifyTrust` on Windows; `codesign`/`spctl` in the macOS helper. Unsigned ⇒ refused in production |
| Only the named publisher | desktop | `policy.WINDOWS_SIGNER_PINS` / `MACOS_TEAM_ID_PINS` (**empty until the certificate exists — see §7**) |
| The version really is newer, for this platform and architecture | backend **and** desktop | the server compares; the client compares again and refuses an equal/older/other-machine answer |
| A version is complete (Windows + both Macs), sized, checksummed and signed before any row is served | backend | `desktop_release_policy.readiness` at publish; CI repeats it earlier (`tools/verify_release_artifacts.py`) |
| Who may publish | backend | `POST …/publish`, rollback and status changes refuse a service credential: the pipeline key registers drafts, a signed-in administrator decides they ship |
| Nobody is emailed early | backend | the announcement waits until every required artifact is published; off by default; test mode available |

The SHA-256 and the signature protect against *different* failures. The digest
comes from the same backend that names the URL, so it cannot tell you the
backend is wrong; the signature is verified against the machine's own trust
store, so it holds even if the release channel is compromised end to end.

## 4. Where artifacts may live

The **client** fetches only from `github.com` (and, for redirects, from
`*.githubusercontent.com`). This is deliberate and has one consequence worth
stating plainly: **moving to a different host (object storage, a CDN, another
domain) needs a new client first.** An installed client will refuse an address
outside its list whatever the backend says, so the order is always: ship a
client that allows the new host → wait for the fleet to take it → then start
publishing there. Doing it in the other order strands every installed client.

The **backend** adds a second, configurable layer:

```
DESKTOP_DOWNLOAD_ALLOWED_HOSTS=github.com
DESKTOP_DOWNLOAD_URL_PREFIXES=https://github.com/<owner>/<releases-repo>/releases/download/
```

The prefix is what pins downloads to *one repository*. Without it any
`github.com/<anyone>/<anything>/releases/download/…` URL is accepted by the host
check, which is why production should set it.

## 5. Keeping the source repository private

### Target

```
KairavThakar90/monitra            PRIVATE   source, CI, issues — nothing downloadable
KairavThakar90/monitra-releases   PUBLIC    installers, .sha256, .signature.json — no source
```

CI already supports this (`.github/workflows/desktop-release.yml`, release job):
it publishes to the repository named by the `RELEASES_REPO` variable using
`RELEASES_REPO_TOKEN`, and `register_release.py --repo` derives the download URLs
from that same variable, so the rows the backend serves point at the public
repository. The `monitra-releases` repository must contain **only** release
assets: public means everything in it, forever, including old assets.

### Migration — in this order

Steps 1–6 change nothing for users. Step 7 is the only irreversible-feeling one,
and is deferred until the new path has carried a real release.

1. **Create `monitra-releases`** (public, empty; a README is fine). *Owner.*
2. **Create a fine-grained token** scoped to that one repository with
   *Contents: read and write* and nothing else, and store it as the secret
   `RELEASES_REPO_TOKEN` on the source repository. *Owner — never paste it into
   a chat or a file in the repository.*
3. **Set the repository variable** `RELEASES_REPO` = `KairavThakar90/monitra-releases`.
4. **Set the backend environment** (production, then restart):
   `DESKTOP_DOWNLOAD_URL_PREFIXES=https://github.com/KairavThakar90/monitra-releases/releases/download/`
   **Do this only after the first release from the new repository is registered**,
   or the existing 1.x rows (which point at the old repository) stop being served.
   Until then leave it empty, or list *both* prefixes.
5. **Dry run:** trigger the workflow by hand (`workflow_dispatch`, *Attach the
   artifacts* on). It files a **draft** release in `monitra-releases` and registers
   **draft** rows. Nothing is offered to anyone. Confirm the three installers and
   their sidecars are there, and that rows were registered.
6. **Release 1.3.2 (or later) the normal way** from that path, through the pilot
   ring, then `POST /desktop/releases/versions/<v>/publish`. Verify an anonymous
   download works:

   ```
   curl -sIL -o /dev/null -w "%{http_code}\n" \
     https://github.com/KairavThakar90/monitra-releases/releases/download/v1.3.2/Monitra-Setup-1.3.2.exe
   ```
7. **Only now make the source repository private.** *Owner.* After this, the
   assets of every release *in the source repository* (1.1.1 … 1.3.1) stop being
   downloadable by anyone but collaborators. That is acceptable **only because**
   a newer version is already the one the backend serves; confirm with
   `GET /desktop/releases` that the newest published row for every platform
   points at `monitra-releases`. If a row for an old version must stay
   reachable, copy that version's assets into `monitra-releases` under the same
   tag **before** step 7 and register a new row (rows are not editable —
   identity is part of what a support report cites).
8. Set `DESKTOP_DOWNLOAD_URL_PREFIXES` to the single new prefix (drop the old one).

### Rollback

| If… | Do |
|---|---|
| a release from the new repository will not download | `POST /desktop/releases/{id}/rollback` for each artifact of that version. The previous published row becomes the newest again. **That row's URL is in the source repository, so this only works while the source repository is still public — which is why step 7 comes last.** |
| step 7 has been done and the new path then breaks | make the source repository public again (instant, *owner*), roll the bad rows back, fix forward with a new patch version |
| the new repository was set up wrongly | clear `RELEASES_REPO`; the workflow falls back to publishing into the source repository |

Never delete a published release in either repository: it is the rollback
inventory.

## 6. Alternatives considered

| Option | Verdict |
|---|---|
| **Public installer-only GitHub repository** (above) | **Chosen.** Zero new infrastructure; the pipeline already speaks it; the client's host list already covers it; assets inherit GitHub's CDN and availability. |
| Object storage + CDN (S3/R2/GCS behind your own domain) | Better control over bandwidth, retention and signed URLs. Costs a new service to run and secure, and — per §4 — a *new client release first*. A reasonable later step, not a prerequisite for privacy. |
| Embed a GitHub token in the client to read a private repo | **Rejected outright.** Extractable from the binary by any user; grants read on the repository. |
| Backend streams the bytes from a private repo with its own token | Rejected: puts a multi-hundred-MB download behind the API's availability, and makes the backend hold a repository credential. |

## 7. Before the first real auto-update (all outstanding)

1. **Obtain a Windows code-signing certificate** and add `WINDOWS_CERTIFICATE` /
   `WINDOWS_CERTIFICATE_PASSWORD` (secrets). Until then **every** Windows
   auto-update is refused in production by design.
2. **Pin the publisher:** set `policy.WINDOWS_SIGNER_PINS` to the certificate's
   display name (and `MACOS_TEAM_ID_PINS` to the Apple Team ID) and ship that in
   the client. A valid signature from *anyone's* certificate is not enough.
3. **Apple Developer ID + notarization** secrets
   (`MACOS_CERTIFICATE`, `MACOS_CERTIFICATE_PASSWORD`, `MACOS_KEYCHAIN_PASSWORD`,
   `MACOS_CODESIGN_IDENTITY`, `MACOS_NOTARY_APPLE_ID`, `MACOS_NOTARY_TEAM_ID`,
   `MACOS_NOTARY_PASSWORD`). **The macOS update path has never run on a real Mac.**
4. Set the repository variable **`REQUIRE_SIGNED_RELEASE=true`** once 1–3 are in
   place, so CI refuses an unsigned set instead of merely labelling it.
5. Keep the backend defaults: `DESKTOP_REQUIRE_SIGNED_RELEASES=true`,
   `RELEASE_EMAIL_ENABLED=false`. Turn the email on only after a verified release
   (§ the runbook), and rehearse it first with `RELEASE_EMAIL_TEST_RECIPIENTS`.
6. Complete §5 if the source repository is to become private.
7. Run the first real update on a **signed** build, from a pilot account, on a
   real Windows machine and a real Mac of each architecture.

## 8. Environment and configuration reference

**Backend** (`backend/.env.example` has the same list with comments)

| Variable | Default | Purpose |
|---|---|---|
| `RELEASE_EMAIL_ENABLED` | `false` | Whether publishing announces by email at all |
| `RELEASE_EMAIL_TEST_RECIPIENTS` | empty | While set, the announcement goes **only** to these addresses, tagged `[TEST]`, and to no user |
| `DESKTOP_DOWNLOAD_ALLOWED_HOSTS` | `github.com` | Exact hostnames a download URL may name |
| `DESKTOP_DOWNLOAD_URL_PREFIXES` | empty | https prefixes pinning downloads to one releases repository (set in production) |
| `DESKTOP_REQUIRED_ARTIFACTS` | `win32,darwin:arm64,darwin:x86_64` | What a version must contain before it is published or announced |
| `DESKTOP_REQUIRE_SIGNED_RELEASES` | `true` | Refuse to publish a row registered as unsigned |
| `EMAIL_PROVIDER`, `SMTP_*`, `EMAIL_FROM_*`, `MONITRA_APP_URL` | — | Unchanged; see `docs/Email_Production_Runbook.md` |

**GitHub** (repository settings)

| Name | Kind | Purpose |
|---|---|---|
| `RELEASES_REPO` | variable | `owner/name` the release is filed in (default: the source repository) |
| `RELEASES_REPO_TOKEN` | secret | fine-grained token, contents:write on that repository only |
| `REQUIRE_SIGNED_RELEASE` | variable | `true` makes CI fail on an unsigned set |
| `MONITRA_API_BASE_URL`, `MONITRA_RELEASE_CREDENTIAL` | secrets | registration (service credential holding only `manage_desktop_releases`) |
| `WINDOWS_CERTIFICATE`, `WINDOWS_CERTIFICATE_PASSWORD` | secrets | Authenticode signing |
| `MACOS_*` (above) | secrets | Developer ID signing and notarization |

**Desktop** (test hooks — honoured **only when the build is not a production
build**, i.e. `MONITRA_ENV` is `development` or `staging`)

| Variable | Effect |
|---|---|
| `MONITRA_UPDATE_EXTRA_HOSTS` | adds hostnames to the download allowlist (for a local test server) |
| `MONITRA_UPDATE_ALLOW_UNSIGNED=1` | lets an unsigned installer run (for exercising the update path before a certificate exists) |

A production build ignores both. Neither exists to be set on a staff machine.

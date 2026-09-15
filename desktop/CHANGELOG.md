# Changelog

Every released version of the Monitra desktop client, newest first, in the
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) style.

This file is **hand-written, not generated.** GitHub's auto-generated release
notes list commit titles, which answer "what did we change" and not "what
changed for you" — the question a member of staff actually has when they are
asked to install something. Both go out with a release; only this one is
written for the person installing it.

`tools/check_changelog.py` fails CI if the version in `version.py` has no
entry here, so a release cannot ship without one.

The version numbers follow semver as `version.py` describes: **patch** for
fixes with no behaviour change worth knowing about, **minor** for new features
and additive backend contract changes, **major** for a breaking desktop↔backend
contract change, a data-directory migration, or anything that makes every user
re-grant a permission.

## [Unreleased]

## [1.1.2]

### Fixed

- **Downloading Monitra no longer requires a GitHub account.** The download
  page handed every visitor a link that only worked if their browser happened
  to be signed in to GitHub with access to a private repository. For everyone
  else the download failed with a "not found" page, which looked like a problem
  with their browser — the same link would work in one browser and fail in
  another on the same machine. The installers are now published somewhere
  genuinely public, so the download works for anyone with the link, signed in
  or not.

- **You can no longer open a future date, and a past date is now genuinely
  read-only.** Today is the latest date the header will show: the forward
  chevron stops there and the calendar will not select past it. Choosing an
  earlier day shows that day's tracked time, applications and websites exactly
  as before, but Start and Stop are hidden while you are looking at it —
  previously a future date kept the live controls, so a timer could be started
  from a day that had not happened. A timer that is already running is not
  affected by browsing dates: it keeps running, and returning to today brings
  its controls back.

## [1.1.1]

The same Monitra as 1.1.0, rebuilt and published from the project's new home.
Nothing about the application has changed: if 1.1.0 is working for you, there
is no reason to hurry.

### Changed

- **Monitra is now built and distributed from a new repository.** The download
  page, the in-app update check and the installers all point at the new
  location. You do not have to do anything — updating to 1.1.1 moves you across
  and every future update arrives from there.

  1.1.0 remains downloadable from the old location and keeps working. It will
  not, however, be offered any further updates, so installing 1.1.1 is what
  keeps you on the update path.

### Fixed

- A build-server test incorrectly required a Windows-only input hook to start
  on Linux, where Monitra does not run and no such hook exists. This failed the
  correctness gate on every push without indicating any real fault. No change
  to how Monitra behaves on Windows or macOS.

## [1.1.0]

Monitra updates itself from this version onwards. Install it once; you will be
offered every future version inside the app.

### Added

- **Automatic updates.** Monitra checks about every ten hours whether a newer
  version has been released, and offers it to you in a dialog showing what
  changed. Choose **Update Now** and it downloads the new version, checks it is
  genuine, installs it and restarts itself. Choose **Later** and nothing
  happens — you will be offered it again next time.

  The check is deliberately quiet. It never interrupts tracking, never delays
  startup or sign-in, and if it cannot reach the backend it simply tries again
  later. Nothing is downloaded until you ask for it.

  The schedule survives closing the app: if you were away longer than ten
  hours, Monitra checks shortly after you open it rather than waiting again.

- **Required updates.** A release can be marked as required — for a security
  fix, or a change the backend depends on. You are then asked to update before
  continuing. This is rare, and it is always the server's decision at the time
  you ask: an unreachable backend can never lock you out.

- **A download page.** New machines install Monitra from the website, which
  always offers the current version for your operating system. There is no
  versioned link to go stale.

- **An "Updates" entry in the account menu**, beside Profile and Feedback &
  Help, showing how many newer releases are waiting — "Updates (1)". A
  notification disappears; this does not, so an update you were away for, or
  dismissed without reading, is still there when you come back. It disappears
  by itself once you have installed the update, and if a release is withdrawn
  it goes with it. With nothing waiting it reads **Check for Updates** and asks
  the backend directly.

- **Version visibility for support.** The desktop identifies its own version
  on every request, so support can see which build you are running when you
  report a problem, and can tell whether a fix has actually reached everyone.
  Nothing else about your machine is collected.

### Notes

- **Your data is never at risk from an update.** Tracked time, the pending
  sync queue and your logs live in `~/.monitra`, outside the installation, and
  an update replaces only the installed application. If an update fails at any
  point — no connection, an interrupted download, a file that does not verify —
  the version you already have keeps working and nothing is left half-installed.

- Every download is checked against a SHA-256 published with the release
  before it is run. A file that does not match is discarded, not installed.

- Automatic updating applies to the installed build. The portable build is a
  folder you unzip yourself, so it is still updated by downloading the new
  one.

## [1.0.1]

This file was introduced after 1.0.1 was already tagged, so this entry is a
placeholder rather than a reconstruction. What shipped in 1.0.1 was not
recorded at the time and is not being guessed at here; `git log v1.0.0..v1.0.1`
is the only accurate account of it. Every version from the next release
onwards has a written entry.

## [1.0.0]

First packaged release: Windows installer and portable build, macOS DMGs for
Apple Silicon and Intel.

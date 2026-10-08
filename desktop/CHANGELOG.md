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

### Added

- **A second Add button: Add Non Billable Task.** If an administrator allows it for
  you (Members page), a teal **Add Non Billable Task** button appears right after
  Add Task. It opens the same dialog, with a fixed "Non billable" tag beside the
  name, and the task is created with " - Non billable" on the end of its name.
  You can rename such a task in Edit Task, but the marker stays. Members who are
  not allowed see the top bar exactly as before, and the button works even if
  Add Task itself has been switched off for you. The Add Task, Add Non Billable Task
  and Request buttons now share a light highlighted border.
- **Attach a screenshot to your feedback.** The Feedback & Help form now has an
  optional **Attach file** button: add up to three images (PNG, JPG or WEBP, 10 MB
  in all) to show us what you are seeing. Each file is listed with a preview, its
  size and a **Remove** button, and a file that is not a supported image is
  refused with a clear message. Sending feedback without an attachment works
  exactly as before, and if a send fails your message and files are kept so you
  can try again without creating a duplicate.

- **Monitra tells you what is happening to your screenshots.** A quiet line
  beside "ACTIVITY" says **Uploading screenshot** while one is on its way,
  **Screenshot uploaded 10:34 AM** once it is confirmed in the cloud (never
  before), and turns amber or red only if an upload has been failing for a
  couple of minutes or a capture failed outright. Hover it for what to expect.
  It does not pop up, and one retry is not a warning. The "Screenshot captured"
  toast is unchanged: it still means the picture was *taken*, not uploaded.
- **Your manager can see why a screenshot is missing.** When a screenshot could
  not be taken or uploaded, the web screenshots page now says so — "Capture
  failed", "Upload pending", "Capture blocked" (e.g. Screen Recording not
  allowed) or "Held back by privacy rule" — with the reason, instead of only
  "No capture". Your own timer and tracked time are unaffected.

### Changed

- **Updating Monitra is safer.** An update is only downloaded from an
  approved address over a secure connection, is checked against its published
  checksum, and — on Windows — is only installed if it carries a valid digital
  signature. If any of that fails, nothing is installed, your current version
  carries on working, and the update window offers **Download manually**.
- **Monitra tells you how an update went.** After it restarts you are told
  whether the update was installed, and if it was not (the installer failed, or
  did not finish) you are told that you are still on your previous version
  rather than being left to find out.
- **Your timer survives an update**, as before — it is not stopped, and
  Monitra picks it up again after it restarts.

### Fixed

- **Monitra uses far less memory, and it stays there.** The desktop could be
  seen in Task Manager at 500 MB to over 1 GB after an hour on the dashboard.
  Nearly all of it was screenshot thumbnails: the Screenshots tab refreshes
  every minute, each refresh rebuilt its cards, and every replaced card kept
  its full-size decoded picture (4 MB each) until Python's garbage collector
  happened to run -- so memory climbed by a page of cards a minute and then
  dropped suddenly, over and over. A card now releases its picture the moment
  it is replaced, and keeps only the small version it actually draws. Nothing
  about what is captured, uploaded or shown has changed; the thumbnails look
  the same.

- **A screenshot that fails to capture is retried, not skipped.** If the screen
  could not be read at the planned moment (a locked screen, a monitor asleep, a
  remote-desktop reconnect), Monitra used to give up on that ten-minute slot and
  say nothing. It now tries again within the same slot, with growing pauses,
  and if the slot ends without a picture it records why.
- **Screenshots no longer stop for the rest of the session after one problem.**
  A single error — or one capture that never finished — could silently end all
  later screenshots while your timer kept running. The schedule now always
  re-arms itself, a watchdog restarts it if it ever stops, a capture that hangs
  is abandoned after 90 seconds and retried, and after the computer wakes from
  sleep Monitra checks the schedule straight away.
- **Screenshots are no longer held back for ever when privacy settings fail to
  load.** If the request for your organisation's privacy settings was dropped
  while you signed in, every screenshot was held until Monitra was restarted.
  Monitra now asks again, and tells the web page if it is still waiting.
- **A session that expires no longer throws away screenshots waiting to
  upload.** They are kept (with their files) and upload once you sign back in;
  only another user signing in on the same computer discards them.
- **A screenshot is dated when the screen was read**, not when the picture
  finished processing, so one taken in the last second of a slot is no longer
  filed under the next one.
- **Pasting into a task description no longer brings its formatting.** Text
  copied from a web page or a dark editor arrived as white-on-black blocks; the
  description boxes (Add Task, Edit Task, Request) now take plain text only.
- **A task's description lines up with its name.** In the task list the
  description used to start under the small circle at the left of the row; it
  now starts directly under the task name.
- **macOS: screenshots are never wallpaper-only any more.** Without macOS
  Screen Recording permission, macOS hands Monitra a picture of just the
  desktop wallpaper (and Monitra's own window) instead of an error, and Monitra
  used to upload that as your work. Now, if Screen Recording is off — or was
  switched on but Monitra has not been reopened since — Monitra takes **no**
  screenshot, tells you once when you press Start, and links to the System
  Settings switch. Screenshots resume by themselves once it is in effect. Your
  timer is not affected either way.
- **macOS: a notification no longer pulls Monitra to the front.** A
  notification appearing while you type in another app no longer moves your
  cursor or activates Monitra, and closing it with its × only closes it.
- **macOS: Minimize** now minimises Monitra to the Dock, so clicking the Dock
  icon brings it back, and Cmd+Q still asks before quitting. Pressing Cmd+Q or
  Cmd+W while the quit question is already showing no longer stacks a second
  one.
- **A username with an accent, a space or a symbol** (for example
  `C:\Users\José`) no longer breaks the update handoff.
- **An update that was started but never finished installing** no longer leaves
  Monitra closed: the updater now reliably waits for Monitra to exit, runs the
  installer, and starts Monitra again.
- **A malformed answer from the update service** can no longer leave the
  "Check for updates" menu entry saying it is already checking.

## [1.3.1]

The installer and the macOS bundles are still unsigned: Windows SmartScreen
will warn on first run ("More info" → "Run anyway"), and macOS will refuse
the app until you allow it under System Settings → Privacy & Security.

### Changed

- **The project list shows 20 projects per page**, up from 10.
- **The project you are tracking is at the top of the project list.** While a
  timer is running, its project is the first row on the first page, so you do
  not have to search for it. When you stop, or switch to another project, it
  goes back to its usual place.
- **Notifications stay on screen for 30 seconds**, down from a minute.
- **A new notification no longer replaces the one already on screen.** It
  appears as its own card above it, so you can see that something new came
  in. Each card closes by itself after 30 seconds, or when you click its ×.
- **A manual time entry (Request) now asks for a reason.** Choose one of
  "Forgot to start/stop timer", "Used wrong task/project" or "Other"; your
  approver sees it with the request. The Billable box is gone: whether the
  time is billable is taken from the project itself, so it is always right.
- **You can search inside the project and task drop-downs.** When you add a
  manual time entry (Request) or reassign idle time, open the list and start
  typing to find a project or task by name.
- The Activity search box is shown on the Apps and URLs tabs only. It no
  longer takes up a row on the Screenshots tab, where it could not be used.

### Fixed

- **The project list no longer jumps back to page 1 by itself.** If you were
  on page 2 or 3, every background sync sent you back to the first page.
  The list now stays where you left it.
- **Health reminders no longer arrive in a burst.** After an hour of work,
  several reminders (water, posture, blink, 20-20-20) used to appear within a
  minute or two of each other, each replacing the last. They are now spread
  out: no two are ever less than five minutes apart, and each one still
  repeats as often as before.
- **Reminders arrive on time.** A reminder is shown when it is due rather than
  up to half a minute later, a late one no longer delays the ones after it,
  and the break reminders (10:30, 13:30, 14:15, 16:30) appear at that time.
- On a Mac, a reminder no longer appears moments after the laptop wakes from
  sleep.
- **Tooltips are readable.** The "Previous page" / "Next page" tips under the
  task list, the tips on the top bar buttons, on your name in the sidebar and
  on the Play button were white text on a white box.
- The Activity search box has a visible border and no longer sits flush
  against the list below it.
- The scrollbar beside the project list no longer shows a checkered strip.
- **Drop-downs look right.** The arrow on the Project, Task, Work Date, Status
  and Feedback category fields was a boxed button with a broken border, and
  on the idle Reassign dialog it was missing altogether. Every drop-down now
  has a clean arrow and the same list.
- A long notification title wraps onto a second line instead of being cut
  off.
- **The last application you were using is no longer recorded twice** when
  Monitra closes while a timer is still running — a Windows shutdown, or a
  restart to install an update. The final stretch of application time was
  stored two times.

## [1.3.0]

The installer and the macOS bundles are still unsigned: Windows SmartScreen
will warn on first run ("More info" → "Run anyway"), and macOS will refuse
the app until you allow it under System Settings → Privacy & Security.

### Added

- **Changes made on the web show up in the app within seconds.** While the
  Monitra window is on screen, new or edited projects and tasks, and your
  admin's settings for you, appear without a restart or a manual refresh.
- **Your admin can let you add tasks, or not.** When adding tasks is turned
  off for you, the Add Task button is greyed out, and clicking it tells you
  why.
- **If your admin removes your sign-in access, Monitra signs you out within
  seconds** and tells you why, rather than failing quietly on the next
  request.
- A small time-of-day icon beside the greeting in the sidebar.

### Fixed

- **Screenshots are never lost when an upload fails.** A failed upload is
  retried until it succeeds, and the local copy is only deleted once the
  server confirms it has stored the image.
- **Break In right after Start reaches the server straight away.** Before,
  the stop could take several seconds to arrive, and in a rare timing case
  the entry kept running on the server until the next launch.
- The manual time entry dialog only shows the Billable option for projects
  that are billed that way.
- The screenshot card fits its content instead of using a fixed height.

## [1.2.7]

### Fixed

- **Default backend URL corrected.** The production default pointed at
  `monitra-lvzq.vercel.app`, a retired deployment with its own separate
  database. A fresh install or an update with no local override now reaches
  the current backend at `api.peakworkos.com` instead. If you installed a
  previous build and configured `SMS_API_BASE_URL` yourself to work around
  this, that override still takes precedence and needs no change.

## [1.2.6]

1.2.3, 1.2.4 and 1.2.5 never shipped — each macOS release build failed a
test that only breaks off Windows, so no artifacts were published for any
of them. This is the same set of changes under the version that actually
built.

The installer and the macOS bundles are still unsigned: Windows SmartScreen
will warn on first run ("More info" → "Run anyway"), and macOS will refuse
the app until you allow it under System Settings → Privacy & Security.

### Added

- **Screenshot privacy controls.** An admin can now exclude specific
  applications, websites, or individual users from screenshot capture.
  Your desktop picks up the current rules at sign-in and keeps them
  refreshed while you work, so a change an admin makes reaches you without
  a restart.
- **A redesigned sign-out confirmation dialog**, matching Monitra's current
  look, with a clear Yes/No choice before your dashboard state is reset.

### Fixed

- **The dashboard no longer shows "Not tracking" for the first moment of a
  new session.** A just-started session is now detected by its running
  state rather than by elapsed time, so today's project time updates the
  instant tracking begins.
- **Screenshot exclusions now match Windows applications correctly.**
  Process names are normalized before matching, so an app excluded by an
  admin is actually skipped.
- **Screenshot capture no longer starts before your privacy settings have
  loaded.** The client waits for the initial privacy configuration to
  resolve (and still captures normally if that request fails), instead of
  capturing before it knows what to exclude.
- Idle and screenshot configuration changes made by an admin now reach a
  running desktop within minutes instead of up to fifteen.
- The maintenance notice ("Monitra is under maintenance") no longer shows
  itself more than once for the same event.
- Session recovery after an unexpected exit is capped at 15 minutes,
  narrowing the window in which recovered time could overstate what was
  actually tracked.
- Minor layout fixes: toast notification positioning, stat card column
  widths, and project loading performance.

## [1.2.2]

The installer and the macOS bundles are still unsigned: Windows SmartScreen
will warn on first run ("More info" → "Run anyway"), and macOS will refuse
the app until you allow it under System Settings → Privacy & Security.

### Added

- **A TODAY'S ACTIVITY card** on the dashboard, beside Project Status,
  Project Hours and Active Task -- one number for how active your keyboard
  and mouse have been today, weighted by how long each stretch actually
  lasted. It updates live while you track, and reads an honest "Not
  tracking yet" rather than a fabricated 0% before anything has been
  measured.
- **Screenshot capture frequency now follows what an admin sets for you**,
  the same way your idle threshold already did. It is picked up at sign-in
  and re-checked periodically, so a change an admin makes reaches a running
  desktop without needing a restart.
- **A crash-recovered session more than an hour old is capped, not resumed
  forever.** If Monitra was closed uncleanly and reopened over an hour
  later, the session is stopped at the one-hour mark instead of quietly
  running for however long the app was shut.
- **A session left running across midnight now splits automatically** into
  one entry per calendar day, so your hours land on the day you actually
  worked them.
- **The idle-time popup opens immediately** after a crash recovery, showing
  the gap as it is being measured, instead of waiting on a round trip to
  the server before you can see or answer it.
- Admin pages (Project Management, Time Tracking, Screenshots, Feedback) on
  the web dashboard gained project and member filters, matching the
  checkbox picker already used on Reports.

### Changed

- **The idle-time popup now defaults to "No, discard idle time"** rather
  than "Yes, keep idle time" -- you still choose either way, only the
  starting selection changed.
- Dashboard stat cards and project-specific hour tracking were reworked as
  part of this release; task actions are now restricted based on a
  project's own status (Active / Paused / Completed).

### Fixed

- **The project list occasionally failed to appear on launch** and needed
  the app reopened before it would load. A failed first load now retries
  itself on a short backoff instead of waiting on the next scheduled
  refresh.

## [1.2.1]

The production release of everything the pilot group tested as 1.2.0-beta.1,
plus what was finished after the beta. If you are on 1.1.1, Monitra offers
this version through its own update check; installing it over 1.1.1 or over
1.2.0-beta.1 is an ordinary upgrade, and your tracked time, sync queue and
settings are untouched. (1.2.0 was the number of the internal test build; the
production release is 1.2.1 so that testers are offered it too.)

The installer and the macOS bundles are unsigned, like every Monitra build so
far: Windows SmartScreen will warn on first run ("More info" → "Run anyway"),
and macOS will refuse the app until you allow it under System Settings →
Privacy & Security.

### Added

- **Project rows show the day's time.** Each project in the sidebar list now
  carries its tracked time for the day on screen -- the sum of its tasks'
  HOURS column, ticking while a task in it is being tracked.
- **The task list turns pages.** A project with more than ten tasks shows
  ten at a time with the same previous / next control the project list has,
  and says which rows of how many you are looking at.
- **The idle alert can be moved.** It still has no close button and still
  has to be answered, but you can drag it by its body anywhere on the
  screen instead of it sitting on top of what you were about to read.
- **Notifications carry the Monitra mark**, so a card in the corner reads
  as Monitra's at a glance.

- **A Play / Pause button under the day's total.** The sidebar now has one
  circular control beneath TOTAL TIME TODAY. Press Play to start the task
  you have selected in the list (click a task's row to select it, or press
  its Start), and Pause to stop whatever is running -- the same Start and
  Stop the task rows have always done, so it is the same timer, the same
  time entry and the same screenshots and activity, whichever control you
  press. After a Pause, Play resumes the same task even if you have browsed
  to another project meanwhile. With nothing selected, Play is disabled and
  says so; it never picks a task for you. During a break it is disabled as
  well: Break Out is the way back to your task.
- **Break In / Break Out moved into the ACTIVE TASK card,** on the right of
  the task it pauses or resumes. It works exactly as before. While you are
  on break the card shows the task Break Out will resume, marked "On break"
  rather than as running, and a short message at the top of the page
  confirms that the break started, and that your task resumed when it ends.
- **Active / Idle now sits beside your name** in the account card at the
  bottom of the sidebar, instead of under the day's total.
- **Break In / Break Out.** **Break In** stops the task you are tracking and remembers it;
  **Break Out** starts that same task again, so you never have to find it in
  the list after a break. The break itself is not counted as work: it is
  simply the gap between the two time entries, exactly as if you had pressed
  Stop and then Start yourself. You can browse other projects while on break
  without changing what Break Out will resume. If the task was archived,
  completed or taken away from you meanwhile, Break Out tells you so and
  starts nothing -- it never picks a different task for you. Closing Monitra
  during a break just leaves the timer stopped; nothing starts by itself when
  you open it again.
- **Screenshots capture every display.** A desk with two or three monitors is
  captured as one merged image at its real layout, instead of only the primary
  screen. It is still one screenshot every ten minutes, not one per monitor. A
  single-display machine is unchanged.
- **Private and incognito browsing is recorded as such.** A page visited in a
  private window shows up in your browsing time marked private. Where Monitra
  genuinely cannot tell (an unsupported browser, or macOS), it records that it
  could not tell rather than guessing.
- **The sidebar greets you by name**, centres the day's total, and draws the
  idle state in red so "not tracking" no longer looks like decoration.
- **The wellbeing reminders are back on the schedule the catalogue documents.**

### Fixed

- **"Yes, keep idle time" keeps it, whichever button you press next.** It
  used to keep the time only if you also pressed Resume; pressing Stop timer
  after choosing to keep it quietly deducted the idle minutes anyway. Now the
  two radio buttons decide what happens to the time and the two action
  buttons decide only whether the timer goes on running. A Stop pressed
  while the alert is still unanswered still discards, as before.
- **Totals no longer jump when you press Stop.** For a task tracked for the
  first time that day, TOTAL TIME TODAY and the task's hours briefly showed
  roughly double the session the instant Stop was pressed -- and stayed
  there offline -- because the just-stopped session was added on top of a
  figure the server had already counted it in. Most visible right after
  "No, discard idle time" + Stop, where the total was supposed to drop.
- **Notifications are fully on screen.** The card in the bottom-right corner
  was placed as if it were narrower than it is, so its right third -- close
  button included -- hung off the edge of the screen. It now sits inside the
  working area at a fixed margin whatever the message length.
- **Compact task rows.** Every task row carried about 22px of empty height
  from layout margins nothing asked for, and its name sat 11px to the right
  of the TASK header. Both are gone; the buttons and the columns are unchanged.

- **Discarded idle time now leaves the running clock at once.** When you
  answer the idle alert with "No, discard idle time" and Resume, the timer on
  the task row, the day total in the sidebar and the summary card drop by the
  idle minutes straight away -- the same figure the web dashboard and the
  reports show. Before, only the web side dropped; the desktop kept counting
  the whole interval until you stopped the timer. Stopping from the alert
  (with either answer) banks the netted figure, "Yes, keep idle time" and
  Resume leaves the clock exactly as it was, and a deduction survives a
  restart of Monitra.
- **Queued work can no longer stall for the rest of the session.** After a
  stretch offline with several task switches, every queued Stop was waiting
  for its own Start to reach the server, and the Starts never got a turn --
  so nothing else queued behind them (task edits, later stops) was sent
  until Monitra was restarted. The queue now works through such a backlog in
  order and the rest of the day's changes follow it as they should.
- **Quitting Monitra now stops your timer.** Quit from the close dialog,
  from the tray menu, or by closing the window with "Remember my choice"
  set to Quit, all stop a running timer at that moment and send the stop to
  the server before Monitra exits (the window stays up for a few seconds
  with "Stopping your timer…" if the server is slow). Before, quitting left
  the timer running on the server and the next launch picked it up again
  with all the time in between counted. "Remember my choice" only decides
  whether you are asked; it never leaves a timer running. Installing an
  update, and Windows shutting down or signing you out, are not quits: the
  session carries on and is recovered when Monitra next starts.
- **A stop can no longer be lost.** Stops are written to the durable queue
  before anything else and are never given up on, so a crash or power cut
  right after you press Stop, or a long server outage, cannot leave a timer
  running on the server for the next launch to resurrect.
- **A timer interrupted by a crash or power cut is recovered, and the
  time Monitra was not running is treated as idle time.** The same entry
  carries on, and if the gap reaches your idle threshold you get the usual
  idle prompt to keep it, discard it, or stop — the gap is never counted as
  work on its own. The recovery notice says how long Monitra was not
  running. A timer stopped from the web or another machine while Monitra
  was away is ended here too, instead of counting on.
- **Double-clicking Start or Stop counts as one click.** It used to start
  and immediately stop (or stop and restart) the timer.
- **Working with several browser tabs no longer triggers the unwanted-activity
  warning — or the ten-minute deduction that came with it.** Holding CTRL, or
  CTRL+T / CTRL+TAB / CTRL+W / CTRL+click, was being counted as a key mashed
  fifteen times. A key is now counted once per press, and only a key pressed
  on its own counts toward that rule.
- **Today's Activity counts what you actually typed and clicked.** A second,
  dead measurement path had been reporting scrolling and reading as typing.
- **A session tracked while offline keeps its whole day of activity**, minute
  by minute, instead of arriving as one lump — or, past an hour, not arriving
  at all.
- **Screenshots taken while Start was still being confirmed are uploaded.** If
  the network was slow or dropped at the moment you pressed Start, every
  screenshot of that session used to sit on disk for ever and never reach the
  server, while the tracked time itself was fine.
- **Tracked time no longer jumps after a lost reply to Start.** A Start whose
  answer was lost is retried as the same start, so the server cannot end up
  with an entry running for hours that the desktop had already stopped.
- **The day's total on the desktop now matches the reports**: idle deductions
  and other adjustments are applied to it the same way.
- **Projects and tasks now stay in step with the server on their own — the
  Refresh button is no longer part of normal use.** Monitra asks the server
  every half minute whether anything you can see has changed and re-reads
  only when it has, so a project or task created on the web, a task
  reassigned, or a project you were removed from shows up within that time.
  Waking your machine from sleep re-synchronises immediately instead of
  waiting out the old timers. If you were viewing a project that is no
  longer yours, Monitra now moves you to one that is rather than keeping the
  old task list on screen.
- **All of your projects are listed.** Anyone with more than twenty projects
  only ever saw the first twenty, and the project they were last in could
  appear to vanish.
- **A task you have just created cannot disappear again.** A background
  re-read that was already in flight when you pressed Add could overwrite
  the list and hide the new task until the next refresh.
- **A refresh that failed part-way no longer silently blocks every later
  one.** After one such failure the dashboard could stay stale for the rest
  of the session with nothing to show for it in the log; it now recovers on
  its own and says so.
- **Retrying a task creation after a lost reply no longer creates the task
  twice.**
- **A brief problem renewing your sign-in no longer signs you out.** Only a
  definitive refusal from the server ends the session; a server that could
  not be reached for the renewal is retried.

- **You can no longer open a future date, and a past date is now genuinely
  read-only.** Today is the latest date the header will show: the forward
  chevron stops there and the calendar will not select past it. Choosing an
  earlier day shows that day's tracked time, applications and websites exactly
  as before, but Start and Stop are hidden while you are looking at it —
  previously a future date kept the live controls, so a timer could be started
  from a day that had not happened. A timer that is already running is not
  affected by browsing dates: it keeps running, and returning to today brings
  its controls back.
- **The calendar's month button has a real chevron.**
- **A sign-in that fails on the server now leaves a diagnosable record in the
  log**, instead of only "server error".

## [1.2.0-beta.1]

**An internal test build, for the pilot group only.** It is installed by hand
from a link you were sent, it is not offered by the in-app update check or the
download page, and it will not be — a later production release will be. If
you were not asked to test it, stay on 1.1.1. Everything in it is also what
the next production release will contain, so please report anything that
looks wrong, however small.

The installer and the macOS bundles are unsigned, like every Monitra build so
far: Windows SmartScreen will warn on first run ("More info" → "Run anyway"),
and macOS will refuse the app until you allow it under System Settings →
Privacy & Security. Installing over 1.1.1 is an ordinary upgrade; your tracked
time, sync queue and settings are untouched, and 1.1.1 can be reinstalled over
this build at any time.

### Added

- **Screenshots capture every display.** A desk with two or three monitors is
  captured as one merged image at its real layout, instead of only the primary
  screen. It is still one screenshot every ten minutes, not one per monitor. A
  single-display machine is unchanged.
- **Private and incognito browsing is recorded as such.** A page visited in a
  private window shows up in your browsing time marked private. Where Monitra
  genuinely cannot tell (an unsupported browser, or macOS), it records that it
  could not tell rather than guessing.
- **The sidebar greets you by name**, centres the day's total, and draws the
  idle state in red so "not tracking" no longer looks like decoration.
- **The wellbeing reminders are back on the schedule the catalogue documents.**

### Fixed

- **Quitting Monitra now stops your timer.** Quit from the close dialog,
  from the tray menu, or by closing the window with "Remember my choice"
  set to Quit, all stop a running timer at that moment and send the stop to
  the server before Monitra exits (the window stays up for a few seconds
  with "Stopping your timer…" if the server is slow). Before, quitting left
  the timer running on the server and the next launch picked it up again
  with all the time in between counted. "Remember my choice" only decides
  whether you are asked; it never leaves a timer running. Installing an
  update, and Windows shutting down or signing you out, are not quits: the
  session carries on and is recovered when Monitra next starts.
- **A stop can no longer be lost.** Stops are written to the durable queue
  before anything else and are never given up on, so a crash or power cut
  right after you press Stop, or a long server outage, cannot leave a timer
  running on the server for the next launch to resurrect.
- **A timer interrupted by a crash or power cut is recovered, and the
  time Monitra was not running is treated as idle time.** The same entry
  carries on, and if the gap reaches your idle threshold you get the usual
  idle prompt to keep it, discard it, or stop — the gap is never counted as
  work on its own. The recovery notice says how long Monitra was not
  running. A timer stopped from the web or another machine while Monitra
  was away is ended here too, instead of counting on.
- **Double-clicking Start or Stop counts as one click.** It used to start
  and immediately stop (or stop and restart) the timer.
- **Working with several browser tabs no longer triggers the unwanted-activity
  warning — or the ten-minute deduction that came with it.** Holding CTRL, or
  CTRL+T / CTRL+TAB / CTRL+W / CTRL+click, was being counted as a key mashed
  fifteen times. A key is now counted once per press, and only a key pressed
  on its own counts toward that rule.
- **Today's Activity counts what you actually typed and clicked.** A second,
  dead measurement path had been reporting scrolling and reading as typing.
- **A session tracked while offline keeps its whole day of activity**, minute
  by minute, instead of arriving as one lump — or, past an hour, not arriving
  at all.
- **Screenshots taken while Start was still being confirmed are uploaded.** If
  the network was slow or dropped at the moment you pressed Start, every
  screenshot of that session used to sit on disk for ever and never reach the
  server, while the tracked time itself was fine.
- **Tracked time no longer jumps after a lost reply to Start.** A Start whose
  answer was lost is retried as the same start, so the server cannot end up
  with an entry running for hours that the desktop had already stopped.
- **The day's total on the desktop now matches the reports**: idle deductions
  and other adjustments are applied to it the same way.
- **Projects and tasks now stay in step with the server on their own — the
  Refresh button is no longer part of normal use.** Monitra asks the server
  every half minute whether anything you can see has changed and re-reads
  only when it has, so a project or task created on the web, a task
  reassigned, or a project you were removed from shows up within that time.
  Waking your machine from sleep re-synchronises immediately instead of
  waiting out the old timers. If you were viewing a project that is no
  longer yours, Monitra now moves you to one that is rather than keeping the
  old task list on screen.
- **All of your projects are listed.** Anyone with more than twenty projects
  only ever saw the first twenty, and the project they were last in could
  appear to vanish.
- **A task you have just created cannot disappear again.** A background
  re-read that was already in flight when you pressed Add could overwrite
  the list and hide the new task until the next refresh.
- **A refresh that failed part-way no longer silently blocks every later
  one.** After one such failure the dashboard could stay stale for the rest
  of the session with nothing to show for it in the log; it now recovers on
  its own and says so.
- **Retrying a task creation after a lost reply no longer creates the task
  twice.**
- **A brief problem renewing your sign-in no longer signs you out.** Only a
  definitive refusal from the server ends the session; a server that could
  not be reached for the renewal is retried.

- **You can no longer open a future date, and a past date is now genuinely
  read-only.** Today is the latest date the header will show: the forward
  chevron stops there and the calendar will not select past it. Choosing an
  earlier day shows that day's tracked time, applications and websites exactly
  as before, but Start and Stop are hidden while you are looking at it —
  previously a future date kept the live controls, so a timer could be started
  from a day that had not happened. A timer that is already running is not
  affected by browsing dates: it keeps running, and returning to today brings
  its controls back.
- **The calendar's month button has a real chevron.**
- **A sign-in that fails on the server now leaves a diagnosable record in the
  log**, instead of only "server error".

### For the pilot

Please cover, over at least one full working day: sign in and stay signed in
across a restart; select a project and a task; start, switch and stop the
timer; add a manual time entry; work normally in a browser, an editor and one
other application; let the machine go idle long enough for the idle prompt
and answer it; check the Activity tab shows your applications, sites and
screenshots for the day; disconnect the network for a while and reconnect;
quit from the tray and reopen. Report what you saw against what the web
dashboard shows for the same day.

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

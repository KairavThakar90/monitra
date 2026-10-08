# Monitra timing model

This is the one description of how tracked time is measured, recorded and
shown. Every layer -- the desktop client, the API, the database and the web
dashboard -- implements this document. Where anything in code disagrees with
it, the code is the bug.

It exists because the product once had several answers to "how long did that
session last": the desktop counted from its own clock, the backend stamped
requests on arrival, the web rebuilt seconds from two-decimal hours, and a
refresh could change the number on screen. The rules below are what make all
of them agree.

---

## 1. One source of truth

A time entry is two instants in Postgres, `time_entries.start_time` and
`time_entries.end_time`, both `timestamptz`, both UTC. Its duration is
derived from them and from nothing else:

```
total_seconds = round(end_time - start_time)          -- completed entry
elapsed       = server_now - start_time               -- running entry
```

`total_seconds` is written once, by `TimeEntryService.stop_timer`, and never
edited afterwards. Deductions (discarded idle time, reassigned idle time,
unwanted-activity penalties) are signed rows in `time_entry_adjustments`; every
report nets them on top. No client counter, tick, or estimate is ever
persisted as a duration.

## 2. One clock

**The server clock is the reference for every persisted instant.** A client
never asserts *what time* an event happened; it tells the server *how long
ago* it happened, by sending the event instant together with its own clock at
the moment of sending:

```
age        = client_time - started_at        (both on the client's clock)
start_time = server_now  - age               (both on the server's clock)
```

The two clocks are only ever subtracted from themselves, so a client whose
clock is minutes wrong records exactly the same entry as one whose clock is
right. The same rule places `stopped_at`.

Why "age" and not "trust the client's instant": the desktop queues starts
and stops durably and replays them minutes later when it was offline. The
instant the user pressed the button is the one that must be recorded, and
the age of that press is the only thing the client knows that survives a
skewed clock.

Guards, all of which fall back to the server's own `now` rather than reject:

| Situation | Result |
|---|---|
| No `client_time` (older client) | The client instant is used when plausible: naive is read as UTC, a future value is clamped to now, a value older than 7 days is refused. |
| Negative age (event "after" its request) | now |
| Age greater than 7 days | now |
| Stop earlier than the entry's start | the later of now and the start |

## 3. What the user sees

Each client shows elapsed time as `now - anchor` on **its own** clock,
where the anchor is the start instant expressed on that clock:

* A session the desktop started itself keeps its local anchor
  (`started_at_utc`) for the whole session. The backend's `start_time` is
  the same instant on the server's clock; replacing the local anchor with it
  is exactly how the display used to jump by the machines' skew when the
  reply arrived. The difference is recorded as `clock_offset_seconds` for
  diagnostics.
* A session adopted *from* the backend (after a restart with no local record,
  or after a 409) is anchored to `start_time + (client_now - server_time)`,
  using the `server_time` every time-entry response carries. A session the
  desktop is already tracking is only re-anchored when the backend's record
  differs from the local one by more than `REANCHOR_TOLERANCE_SECONDS`; a
  smaller difference is the network round trip, not a disagreement.
* The web client keeps no timer. Every figure it renders is a server
  aggregate whose running entries are measured with the database's `now()`.
* **The figure shown for a running entry is net of its adjustments**, on
  both clients. `TimeEntryRead.net_seconds` is `elapsed + adjustment_seconds`
  floored at zero, the web aggregates net the same rows, and the desktop
  displays `measured − deductions` where the deduction is the backend's own
  figure (`time_entry_adjustment_seconds` on an idle-period response,
  `adjustment_seconds` on the entry). So "No, discard idle time" + Resume
  drops the running clock by the idle minutes the instant the server has
  written them, and "Yes, keep idle time" + Resume leaves it unchanged. The
  desktop stores the deduction beside its anchor -- never inside it -- and
  never derives it from an idle duration of its own.

## 4. Tolerance

For an online session, the recorded duration differs from the interval the
user saw by at most **two seconds**: one second of rounding on each end, and
the difference in network latency between the start request and the stop
request. A session that was started or stopped offline records the interval
between the two presses exactly, because both ages are known.

This is the tolerance `desktop/tests/e2e/test_timing_lifecycle_e2e.py`
asserts against the live database.

## 5. Start and stop are idempotent

* **Start** carries the desktop's session key, `client_op`, stored on the
  entry. A retried start with the same key is answered with the entry the
  first attempt created (`200`, not `201`), whether or not it is still
  running. A start without a key, or with a new key while another entry is
  running, is refused with `409`; the refusal carries the running entry in
  `detail.active_entry` so the client can adopt it instead of guessing.
* **Stop** of an entry that is already stopped returns it unchanged. Two
  stops racing for one entry are settled by a compare-and-set
  (`UPDATE ... WHERE end_time IS NULL`); the loser returns the winner's row.
* Two starts racing for one user are settled by the partial unique index
  `uq_active_time_entry (user_id) WHERE end_time IS NULL`; the loser is
  answered as a `409` (or as a replay, if it carried the winner's key), never
  as a 500.

## 6. Convergence without a refresh

* **Desktop.** `timer_stopped` fires when the local clock stops; the day's
  cached total already holds the session's estimate. `timer_finalized` fires
  when the backend has committed the stop (directly, or through the durable
  queue) and carries the finalized entry; only then does the dashboard re-read
  the day, so the figure it lands on is the one the reports show. Re-reading
  on the local stop raced the stop request and overwrote the estimate with a
  still-running entry. While a stop is still queued, the backend's running
  row is overlaid with the queued instant, so a refresh in that window shows
  the entry as it will be recorded.
* **Web.** The RTK Query cache revalidates on focus and on reconnect
  (`setupListeners` is installed), anything older than 60 seconds is
  revalidated on mount, and every mutation that changes tracked time
  invalidates the bare `TimeTracking` tag that every report query provides.
  Durations are rendered from exact `total_seconds`, never rebuilt from
  two-decimal hours.

  A machine that sleeps and wakes with the tab still in front fires neither a
  focus nor a visibility event, so three more rules apply to the pages that show
  a *running* entry (Admin Active Users and Admin Time Tracking):

  * **Poll while a figure can still be moving.** Active Users re-reads every 30
    seconds; Time Tracking does the same for any range reaching back to
    yesterday or later (a machine that sleeps through midnight keeps the earlier
    day's entry open until it wakes). Both pause while the tab is hidden.
  * **Re-read on resume.** A clock that jumps between two ticks of a
    five-second timer is the one signal a suspend always leaves
    (`hooks/useResume.ts`); the pages refetch at once. Resume is reported only
    while the page is visible -- coming back to a hidden tab is a visibility
    event, which the slice already handles.
  * **Live readings are never restored.** The persisted API cache paints old
    rows on the first frame and corrects them a moment later; for a running
    entry that shows hours that no longer exist, because the desktop ends a
    session retroactively once it wakes (the idle answer, the interruption cap
    in section 7, or the IST-midnight split). `getActiveTimeTracking`,
    `getTimeTracking` and `getTimeTrackingDetails` are therefore neither
    written to nor read from that cache (`store/persist.ts`). While one is
    being replaced after a long gap its figures are dimmed, and a failed
    re-read keeps the rows but says they are old.

  "Today", and every other relative range preset, is resolved from the IST day
  *now* -- never captured when the bundle loads -- and a page showing one moves
  to the new day when IST midnight passes, with no reload.

  What the web cannot do is know a desktop is asleep. While it sleeps, its entry
  stays open and the server's `now - start_time` keeps growing; the correction
  arrives only when the desktop wakes and the server records it. Showing it
  sooner needs a signal from the desktop, which no endpoint carries today.

  **Which day an entry's time belongs to.** The desktop splits a session at IST
  midnight, but only while it is awake, so an entry can be open across a
  midnight for as long as the machine is asleep, off or offline. Time Tracking
  (the day list and the per-member drill-down) therefore apportions an entry to
  every IST calendar day it overlaps, clipped at the boundary
  (`TimeTrackingRepository.day_segments`), instead of reporting all of it under
  the day it started. For a member who began on the 5th at 19:00 and is still
  running on the 6th, the 5th shows 19:00-24:00 and the 6th shows 00:00-now with
  the entry still running -- the same figures the desktop's own split produces
  when it wakes, so nothing moves when it does. An entry that lies wholly inside
  one day keeps its persisted `total_seconds`, so every figure that did not
  cross a midnight is unchanged. An entry's adjustments are one signed figure
  with no instant of their own, so they belong to the day the entry started on.
  Active Users is not day-scoped: it reads every running entry and shows the
  whole of its `now - start_time`, `start_time` included. The Reports pages are
  not changed here and still key an entry on its `start_time`.

## 7. Restart, update, crash

The desktop persists its session record (`app_state.timer_state`) once, when
the timer starts, and removes it once, when the timer stops -- *after* the
stop has been written to the durable queue, so at no instant can a kill lose
a stop or turn an interruption into one. Elapsed time is derived from
`started_at_utc`, so an interruption -- a crash, a kill, a power cut, an OS
shutdown, or a restart through the updater -- is recovered as the same
session with the exact value, the gap included, and recovery adopts the
record rather than starting a new entry. A record whose stop is already
queued is not recovered. `GET /time-entries/active` is the reconciliation
read: it is scoped to the caller by the backend and carries `server_time`;
when it answers that nothing is running, a local session bound against an
entry the backend has since finalized elsewhere ends here too.

**Sleep and hibernate are inactivity, not interruptions.** The process
survives a suspend, so the session record is untouched and the timer keeps
its anchor. The existing idle rule applies: one idle period is reported from
the last moment the idle monitor was running before the machine went down, and
the backend's keep/discard/stop rule decides whether it counts. Nothing stops
the timer silently and nothing counts the sleep silently. The desktop notices
the suspend from its own monitor's missed ticks rather than from the
inactivity reading, which cannot be relied on: the key or lid that wakes a
machine often counts as input, so the first reading afterwards is only a few
seconds. A sleep shorter than the user's threshold is not reported.

**An idle report carries the client's clock.** `POST /idle-periods` takes
`client_time` -- the desktop's clock as the request leaves -- and the server
places `idle_started_at` and `idle_detected_at` by age
(`now - (client_time - instant)`), exactly as start and stop do, with no cap on
how old: an interruption gap may be hours. A client that sends none (an older
desktop) is allowed a 5-minute lead over the server's clock, clamped to now,
instead of a permanent 400. Every refusal is logged (`IDLE_REPORT_REJECTED`).
`POST /idle-periods/{id}/resolve` is idempotent on the answer: the same
`keep_idle_time` and `action` repeated returns the stored result and applies
nothing, which is what lets a client whose reply was lost simply ask again.

**The gap of an interruption is idle time, and the user decides it.** A
session recovered after a power cut, a crash, a kill or a hang continues from
its original start -- the record is adopted, no second entry is created --
but a powered-off machine is not evidence of work. The gap runs from the
dead process's last durable heartbeat to the recovery instant. When it
reaches the user's own `idle_minutes` threshold, `IdleService` reports it
through the same `POST /idle-periods` an ordinary idle stretch uses, the
same popup asks, and the same backend rule accounts for it: the gap counts
only for keep + resume, discard + resume deducts it as a signed adjustment,
and stop discards it and stops the entry at the answer. Below the threshold
nothing is reported, exactly as for any shorter pause. No new threshold and
no maximum session length exist. The report is idempotent three ways: a
client event id keyed on the session and the interruption instant, the
backend's one-pending-period-per-entry rule, and the pending lookup every
entry id gets at recovery. Nothing fabricates activity or screenshots for
the gap; capture restarts at recovery.

**A forgotten timer while Monitra keeps running is the ordinary idle rule**
-- the user's threshold, the popup, the same four answers -- and nothing
more. **Still undefined, deliberately:** what happens to an idle period that
is never answered. Today the entry keeps running with the period pending
until the user returns or the timer is stopped (a stop discards it). No
automatic finalization and no maximum unattended duration are implemented,
because no business rule names one; that decision is recorded here as
pending rather than invented.

**An explicit quit is not an interruption.** Quit, X with Quit chosen, a
remembered Quit and the tray's Quit all stop the timer first, at the instant
of the quit, and wait a bounded time for the stop to reach the backend before
the process exits. Offline, the stop stays queued with that instant and is
placed by age when it is finally delivered. The remembered close choice
decides only whether the confirmation is shown.

## 8. Diagnosability

Every start and stop outcome writes one `app.timing` line on the backend and
one `timing ...` line on the desktop, each carrying the identifiers needed to
match the two: `client_op`, entry id, user id, the request id, the client's
instants, the server's `now`, and the persisted `start_time` / `end_time` /
`total_seconds`. Nothing a user typed is ever logged.

Events: `start.created`, `start.replayed`, `start.conflict`, `stop.finalized`,
`stop.replayed` (backend); `start.local`, `start.bound`, `start.refused`,
`stop.local`, `stop.finalized`, `adopt.remote`, `reconcile.reanchored`,
`recover` (desktop).

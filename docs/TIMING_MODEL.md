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

# Entry 5958: a plan to close it, for the owner to review

**Nothing in this document has been run against production.** The agent that wrote
it could not read the production database. Every production fact below is taken
from the two screenshots in the incident report (the `time_entries` row and the
Active Users row); every figure labelled *worked example* comes from a test
fixture, not from production. Closing the entry is a production data change and is
the owner's decision (`docs/TIMING_MODEL.md` 9.5, `CLAUDE.md` section 2).

## 1. What is known, and what is not

| | |
|---|---|
| Known (screenshots) | `time_entries.id = 5958`, `user_id = 260`, `status = running`, `end_time` NULL, `total_seconds = 0`, `start_time = 2026-10-09 17:23:21.328824+05`. Active Users on Monday 2026-10-12 10:55 IST read **65:32:33** and 25 members active. |
| Inferred, **verify** | `last_seen_at IS NULL`. The row predates the migration, and nothing writes the column for it until its desktop uploads something. |
| Unknown | Whether user 260's desktop was alive at any moment after Friday evening; whether it uploaded anything for this entry after 17:23; which path left the entry open (a closed lid, a crash, an uninstall, an expired session, a lost stop). No log or database row that could say was available. |

Because the row has never reported liveness, it is **`unverified`/`unknown`** to
the policy, and the policy deliberately never closes such a row on its own:
silence from a client that never spoke proves nothing. Deploying the backend does
**not** fix this row: it is still measured to *now*, as before, and keeps growing
by an hour every hour, until either its desktop uploads something (it then becomes
`live`/`stale` and is handled like any other) or an operator names it.

## 2. Read first, in pgAdmin (read-only)

1. `backend/scripts/stale_running_entries_report.sql` and find id 5958. Note
   `category`, `last_seen_at`, `upload_evidence_at`, `signal_source`,
   `can_report_liveness`, `desktop_version`, and `proposed_end`.
   * `upload_evidence_at` set: something (an activity window, a screenshot, app
     usage) named the entry after it started. The proposed end is that instant plus
     15 minutes.
   * Both empty: nothing ever named it. The proposed end is the start plus 15
     minutes, which is a **floor, not evidence**: the entry is 15 minutes long
     because that is the shortest the policy will write, not because 15 minutes
     were observed.
2. `backend/scripts/entry_reconciliation_preview.sql` (the id is already 5958;
   `as_of` is `now()`, or pin it to `TIMESTAMPTZ '2026-10-12 10:55:00+05:30'` to
   replay the screenshot). It prints, for this entry, what each report shows now
   and what it would show after the closure.

Do not proceed to step 3 until both have been read by a person.

## 3. Ask the application for the same answer (still no write)

```
POST /internal/time-entries/reconcile?dry_run=true&include_unreported=true&entry_id=5958
X-Email-Dispatch-Token: <EMAIL_DISPATCH_TOKEN>
```

`dry_run` must be stated: with the job enabled an omitted one means "apply". The
item for 5958 carries `reason: operator`, `would_end_at`, `would_total_seconds`
and `currently_measured_seconds`. `would_end_at` must equal `proposed_end` from
the SQL (a test pins that the two agree).

## 4. The decision

| Option | Closing instant | Consequence |
|---|---|---|
| **A. Close on the evidence (proposed)** | newest upload that names the entry + 15 minutes; else the start + 15 minutes. Never before the start, never in the future | Auditable and the same rule every other closure follows. Recorded as `decided_by = operator` with the evidence in `time_entry_reconciliations`. |
| B. A different instant chosen by hand | not supported by the endpoint, and not recommended | Do it as a manual-time request instead: close on A, and have the member file a manual-time request for any time they really worked after it. That route is approved by a person and audited. It is also *not blocked* any more: the overlap check now uses the measured end of a running entry, so a closed 5958 occupies only its own span. |
| C. Leave it | none | It keeps growing in Active Users, the Reports page, and every weekly and monthly report, on Friday 9 October. |

Whichever is chosen, record it here before running anything:

* Approved by: ______________________   Date/time: ______________________
* Option: ____   Closing instant (from step 3): ______________________

## 5. The write (only after approval)

```
POST /internal/time-entries/reconcile?dry_run=false&include_unreported=true&entry_id=5958
```

The same request as step 3 with `dry_run=false`. It writes, in one transaction
under a row lock that is skipped rather than waited for:

* `time_entries`: `end_time`, `total_seconds = round(end - start)`, `status = stopped`.
  `start_time` and every other column are preserved.
* One `time_entry_reconciliations` row: reason `operator`, what evidence, the
  instants written, the thresholds in force. It is unique per entry, so a repeated
  request changes nothing.
* The idle sweep, the task's `time_tracked_seconds` rollup, the activity-trail line
  ("Timer stopped automatically ... an administrator corrected a timer that was
  never closed"), and, if the task is linked to WFPM, a timer stop for WFPM
  (idempotent on the entry).

It touches no other entry, deletes nothing and reopens nothing.

## 6. What changes in the reports

Two bucketings exist, and both are shown by the preview script. Reports are keyed
on the **IST date of `start_time`** (the Reports page and its daily trend, project
and task views, the weekly email and the monthly summary); Time Tracking
**apportions** an entry to each IST day it overlaps.

| View | Bucket | Before closing | After closing |
|---|---|---|---|
| Reports page, project/task reports, daily trend | Fri 9 Oct (the start date) | the whole running figure, growing: **65:32:33** at Mon 10:55, more every hour | start to closing instant |
| Weekly report | week of Mon 5 to Sun 11 Oct | same figure, in that week | start to closing instant |
| Monthly summary | October 2026 | same figure | start to closing instant |
| Time Tracking, day list and drill-down | Fri 9, Sat 10, Sun 11, Mon 12 Oct | Fri 6:36:39 (17:23:21 to midnight), Sat 24:00:00, Sun 24:00:00, Mon up to now | Fri: start to the closing instant. Sat, Sun, Mon: nothing |
| Active Users | row | listed, growing | gone |
| Task `time_tracked_seconds` | the task | unchanged (a running entry has `total_seconds` 0) | gains the closed duration |

**Worked example (synthetic, not production):** with the newest evidence at
17:54:21 IST (30 minutes after the start, as the test fixture assumes) the closing
instant is 18:09:21 and every row above reads **0:46:00** on Friday and zero on
the days after. The preview test reproduces the screenshot's 65:32:33 exactly from
the start and the Monday 10:55:54 instant, and checks that the four Time Tracking
days add up to the same figure (23799 + 86400 + 86400 + 39354 = 235953 s).

**Already-sent reports are not recalled.** If the weekly report for 5 to 11 October
was emailed on Monday after the entry had grown, it contains the inflated figure
and closing the entry does not resend it. Whether to send a corrected one is a
separate decision.

## 7. After

1. Re-run the report script: 5958 is no longer listed; re-run the preview: before
   and after now agree.
2. In Active Users the row is gone within its 30 second poll.
3. If user 260's desktop is alive and still believes its timer is running, its next
   heartbeat is answered with the ended entry and the reason; it ends its own
   session and tells the person. If it is dead or uninstalled, nothing more happens
   and nothing more is needed.
4. Tell the member what was done and that any time actually worked after the
   closing instant can be requested as manual time.

## 8. Undo

There is no automated undo, deliberately: reopening a finalized entry is what lets a
late request resurrect a closed day, and the unique running-entry index makes it race
the member's next Start. The audit row keeps `entry_start_time`, `last_seen_at`,
`ended_at` and `total_seconds`, and the original row was `end_time` NULL,
`total_seconds` 0, `status` running. Restoring that by hand is possible only while
the member has no other running entry, and re-creates the problem this closes.

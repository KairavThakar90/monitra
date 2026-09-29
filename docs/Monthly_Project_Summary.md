# Monthly Project Summary email

A company-level summary of every project worked on in the previous calendar
month. It is sent automatically on the 1st of each month.

## Purpose and reporting period

- **Sent:** on the 1st of every month at 09:00 in the reporting timezone.
- **Covers:** the previous completed calendar month in that timezone, as the
  half-open window `[1st 00:00, next 1st 00:00)`, converted to UTC for the
  query. A run on 1 October 2026 reports 1–30 September 2026. A run on
  1 March 2027 reports 1–28 February 2027.
- **Timezone:** `WEEKLY_REPORT_TIMEZONE` (default `Asia/Kolkata`). It is the
  same calendar the dashboard, the weekly report and the personal monthly
  report use.
- **Never includes the current month.** The period always ends the day
  before the month the job runs in.

## Recipients and scope

The recipient list is resolved from the database on every run. Nothing is
hard-coded.

| Recipient | Rule | Receives |
|---|---|---|
| Administrator | `role_name` is `administrator`, `org_admin` or `super_admin` | Every project in the organisation |
| Owner | `users.can_own_projects` is true, whatever the role | Every project in the organisation |
| Leader | `role_name` is `leader` or `project_leader` | Only projects inside the leader's existing scope: projects they lead or are staffed on (`project_scope.visible_project_ids`) |

- Recipients must be active (`is_active` true and `status` = `active`) and
  have a usable email address.
- Employees, HR, managers and clients do not receive the summary unless they
  hold the owner flag.
- An administrator or owner who is also a leader receives the company-wide
  summary once.
- A leader's scope is enforced on the server when the payload is built.
  Totals are recomputed over the leader's projects only.
- Each recipient gets their own individually addressed email. There is no
  CC or BCC.

## The numbers

Every hour figure comes from `app/services/project_hours.py`. That is the
single calculation behind the Project Management table, the Dashboard billing
card and the client Billing page. The email has no calculation of its own.

| Figure | Definition |
|---|---|
| Internal Hours | Time in the month on the project's four default tasks: "Project Setup / Understanding", "Review Client Update", "Send Client Update" and "Internal Discussion". Tasks are matched by name within their own project. A renamed default task counts as ordinary work. |
| Billable Hours | Time in the month on every other task. This is Project Management's "Used Hours", restricted to the month. |
| Total Hours Used | Internal + Billable for the month. |
| Remaining (Fixed Hours only) | Allocation minus Billable hours used from the project's start through the last day of the reported month. Internal time does not use the allocation. Later activity is excluded, so a report regenerated later gives the same answer. A negative value is shown as "Over by X" and is never clamped to zero. |
| Remaining (Flexible Time) | None. The section states there is no fixed allocation. |

Seconds combine timer sessions, approved manual entries (unmirrored ones by
work date) and signed time adjustments. A session belongs to the month it
**started** in.

**Running timers.** A session that is still running when the report is
generated contributes its elapsed time so far, in the month it started. This
is the existing rule for every Monitra report, and the Reports page shows the
same figure. A timer left running for weeks therefore inflates its project's
figures on every surface.

**Project types.** A project is *Fixed Hours* when `billing_type` is `fixed`
and it has a non-zero `fixed_hours`. Otherwise it is *Flexible Time*. This is
the same test Project Management uses to show Remaining or "no fixed limit".

**Inclusion.** A project is listed when its net tracked time in the month is
positive, regardless of its current status. A project archived after the
work was done is still reported. A fixed project with no activity in the
month is not listed: this is a report of the month's activity, and its
budget is unchanged from the previous month's report.

**Totals.** The summary shows these totals:

- Projects worked
- Total, Internal and Billable Hours
- Fixed and Flexible project counts
- Fixed hours allocated, used to date and remaining
- Projects over their allocation
- Contributors (distinct members with positive time)
- Average hours per project
- The project with the most hours and the project with the most billable hours

## The email

The email uses the shared Monitra frame: both logos, the footer and the
button style. From top to bottom it contains:

1. The title, the month and the reporting period.
2. A note saying whether it is company-wide or scoped.
3. Four summary cards.
4. The **View Detailed Project Report** button.
5. Highlights.
6. The **Fixed Hours Projects** table: Internal, Billable, Total and
   Remaining, with the allocation and used-to-date under each name.
7. The **Flexible Time Projects** table: Internal, Billable and Total.
8. The button again.

Every project is listed, however many there are. The tables are compact, and
the button appears before them. Some mail clients shorten very long messages,
but the complete report is always one click away. The plain-text alternative
carries every row. Every value is HTML-escaped.

An empty month still sends. It shows zeroes and says "No project activity
was recorded during this reporting period."

**Button destination.** The existing Reports page's Projects report, filtered
to the month: `/dashboard/reports/projects?start=YYYY-MM-01&end=YYYY-MM-DD`.
Readers without `time_entries:view_all` go to
`/member/reports/projects?start=…&end=…` instead, which is the screen their
account can open. The base URL is `MONITRA_APP_URL`. There is no button
unless that is an `https://` address.

## Scheduling, idempotency and delivery

- **Trigger:** a Vercel cron entry in `vercel.json` calls
  `GET /internal/reports/monthly-projects/run` on schedule `30 3 1 * *`
  (09:00 IST on the 1st). The endpoint needs `EMAIL_DISPATCH_TOKEN`, sent as
  `X-Email-Dispatch-Token` or as a bearer token. Vercel Cron sends
  `CRON_SECRET`, so the two must be equal. Without the token the endpoint
  answers 401. With no token configured it answers 503.
- **Computation:** each organisation's summary is computed once, with a fixed
  number of grouped queries. Each recipient's copy is a filter over that
  result.
- **Idempotency:** one outbox row per recipient per month, of type
  `monthly_project_summary`, keyed `month:<YYYY-MM-01>:user:<id>`. The unique
  constraint on the outbox makes cron retries, replays and manual re-runs
  collapse onto the existing row. A sent summary is never sent again.
- **Delivery and retry:** the run only queues. The `/internal/email/dispatch`
  sweeper, which runs every five minutes, delivers with the standard retry,
  exponential backoff and attempt budget. One recipient's failure affects
  only their own row.
- **Logging:** these log lines are written:
  - `MONTHLY_PROJECT_SUMMARY_RUN_STARTED` and `..._RUN_COMPLETE`, with an
    execution id, the period, organisations, projects, recipients, queued,
    already queued, skipped, failed and duration.
  - `..._ORGANIZATION` for each tenant.
  - `..._QUEUED`, `..._ALREADY_QUEUED` and `..._QUEUE_FAILED` for each
    recipient.

  Logs never contain credentials or report figures.

## Configuration

| Variable | Purpose |
|---|---|
| `MONTHLY_PROJECT_SUMMARY_ENABLED` | Kill switch. Default `true`. |
| `WEEKLY_REPORT_TIMEZONE` | The reporting calendar, shared. |
| `MONTHLY_REPORT_HOUR`, `MONTHLY_REPORT_MINUTE` | The local send time on the 1st, shared with the personal monthly report. A test asserts that the cron line matches. |
| `EMAIL_*`, `SMTP_*`, `MONITRA_APP_URL`, `EMAIL_DISPATCH_TOKEN` | Existing email configuration. Nothing is duplicated. |

No database migration is needed. The notification type is a string column,
and the owner flag already exists.

## Manual test procedure

These endpoints need the dispatch token. None of them sends email to
anybody.

```bash
# What would a run do? Queues nothing.
curl -H "X-Email-Dispatch-Token: $EMAIL_DISPATCH_TOKEN" \
  "$API/internal/reports/monthly-projects/run?dry_run=true&month_start=2026-09-15"

# The exact HTML one recipient would get. 404 for a non-recipient.
curl -H "X-Email-Dispatch-Token: $EMAIL_DISPATCH_TOKEN" \
  "$API/internal/reports/monthly-projects/preview?user_id=<id>&month_start=2026-09-15"

# Queue for ONE recipient only, for example yourself, then let the sweeper deliver it.
curl -H "X-Email-Dispatch-Token: $EMAIL_DISPATCH_TOKEN" \
  "$API/internal/reports/monthly-projects/run?user_id=<your id>&month_start=2026-09-15"
```

Never run it without `user_id` or `dry_run` outside the scheduled run. That
queues the real summary for every admin, owner and leader.

## Production verification

1. Deploy, and confirm that `vercel.json` lists `/internal/reports/monthly-projects/run`
   with `30 3 1 * *`.
2. Confirm that `CRON_SECRET` equals `EMAIL_DISPATCH_TOKEN` and that
   `MONITRA_APP_URL` is the public `https://` address.
3. Run a dry run for the previous month. Check that `eligible_recipients`
   matches the expected admins, owners and leaders, and that `projects` looks
   right.
4. Preview one admin's and one leader's summary. Compare a fixed project's
   Remaining with the Project Management page, bearing in mind the page shows
   the live value and the email the month-end value.
5. After the 1st, check the logs for `MONTHLY_PROJECT_SUMMARY_RUN_COMPLETE`
   with `failed=0`, and check that the outbox rows for the month reach
   `sent`.

## Tests

- `backend/tests/test_monthly_project_summary.py`: periods (January, February,
  leap year, December to January), timezone and cross-month boundaries,
  fixed, flexible, internal and billable figures, mixed projects, recipients,
  scope, rendering and escaping, the button route, idempotency, retry, the
  kill switch, query cost and endpoint security.
- `backend/tests/test_fixed_hours_consistency.py`: Project Management, the
  Dashboard billing card, client Billing and this summary all report the same
  Used, Internal, Total and Remaining from the same rows.

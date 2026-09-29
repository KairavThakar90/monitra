# Fixed-hours budget alerts

Automatic emails for **Fixed Hours** projects. An email is sent when a
project's remaining allocation reaches **50%**, **20%** and **10%**, and when
its allocation is fully used: **100% Hours Consumed**, or **OVER BUDGET**.
Flexible Time projects never alert.

## The numbers

Every figure comes from `app/services/project_hours.py`. That is the same
calculation behind the Project Management table, the Dashboard billing card,
client Billing and the Monthly Project Summary.
`tests/test_fixed_hours_consistency.py` asserts that all five surfaces report
the same values.

| Figure | Definition |
|---|---|
| Used | All-time time on the project's work tasks. Timer sessions, approved manual entries and time adjustments are included. |
| Internal | Time on the four default tasks. It **never** consumes the allocation. |
| Remaining | Allocation minus Used. |

For example, with a 100h allocation, 20h internal and 50h used, Remaining is
50h, not 30h.

A **running timer** counts its elapsed time so far, the same rule every
report uses. Usage is all-time. No month or date filter applies.

## When an alert fires

Comparisons use whole seconds and never rounded percentages. An allocation of
`NUMERIC(8,2)` hours is an exact number of seconds.

| Event | Condition | Email |
|---|---|---|
| `remaining_50` | `remaining × 100 ≤ 50 × allocation` | "50% Hours Remaining" (amber) |
| `remaining_20` | `remaining × 100 ≤ 20 × allocation` | "20% Hours Remaining" (orange) |
| `remaining_10` | `remaining × 100 ≤ 10 × allocation` | "10% Hours Remaining" (red) |
| `exhausted` | `used ≥ allocation` | "100% Hours Consumed" when used equals the allocation, or "OVER BUDGET" with the overspend when used exceeds it (dark red) |

- **Boundaries are inclusive.** With a 120h allocation and 108h used, 12h is
  exactly 10% remaining, so the 10% alert fires. At 49.999h used of 100h,
  nothing fires, because remaining is above 50%.
- **Each event fires once per budget.** Continuing to use hours adds nothing.
  Reaching 100% and then 101% sends a single "exhausted" email.
- **Large jumps are handled.** Going from 40h to 91h of 100h claims 50%, 20%
  and 10% in that order, each once. Going straight past the allocation also
  claims `exhausted`.
- **The state is always shown in words.** Colour is never the only signal.

## Durable state and idempotency

The `project_budget_alerts` table holds one row per project, budget version
and event, with a unique constraint on those three columns. A claim is a
single `INSERT … ON CONFLICT DO NOTHING RETURNING id`. Only the evaluation that
creates the row queues email, so concurrent cron runs, a timer stop and a
reconciliation racing each other can never send twice.

Each recipient's email is an outbox row keyed
`project:<id>:v<version>:<event>:user:<id>`, of type `project_budget_alert`.
The standard sweeper delivers it with retry and exponential backoff.

A claim whose emails were not all queued, for example after a crash, keeps a
NULL `emails_queued_at`. The next reconciliation finishes queueing it without
claiming again.

## Budget versions

The version is stored in `projects.budget_version`. A model event bumps it
whenever `fixed_hours` or `billing_type` changes; re-saving the same value is
not a change. Every alert row belongs to one version, so each allocation has
its own independent 50/20/10/exhausted state.

The first time a version is evaluated, a `start` row is written. Thresholds
already behind the project at that moment are handled like this:

| Version | Meaning | Thresholds already passed at first evaluation |
|---|---|---|
| 0 | The budget a project already had when alerts went live | 50/20/10 are recorded silently. An already-exhausted budget is notified once, because an overspend is a current condition. |
| 1 | The first budget of a project created after go-live | All are notified. Nothing can be historical. |
| 2+ | A budget an administrator changed | All are recorded silently, including exhausted. Only crossings after the change are notified. |

A budget change is evaluated immediately inside the Project Management
update, so the new version's baseline is taken at the moment of the change.

**Example.** A project uses 90h of 100h, and 50%, 20% and 10% have been sent.
The budget is raised to 120h, which is version 2. At 25% remaining, the 50%
threshold is recorded silently. At 96h, which is exactly 20% remaining, the
20% alert is sent. At 108h, the 10% alert is sent. Lowering a budget follows
the same rule. A later over-budget condition under a newer version is
notified again.

**First deployment.** The migration sets every existing project to version
0. Existing projects past 50/20/10 get no historical emails. An active fixed
project already over budget gets exactly one over-budget email. This
initialisation is idempotent: it is keyed on the `start` row.

## Which projects are monitored

Projects that are fixed billing, have `fixed_hours > 0` and are in one of
these statuses: planning, active, todo, or pending (shown as Paused).

Completed, cancelled and archived projects are not evaluated. Their alert
rows stay as history. A project created before go-live and reactivated later
gets the version 0 rules, so it doesn't receive a burst of old thresholds.

## Recipients

The rules are the same as for the Monthly Project Summary, resolved on every
run:

- Active administrators.
- Every active user with the owner flag (`can_own_projects`).
- Leaders whose existing scope includes the project: they lead it or are
  staffed on it (`project_scope.visible_project_ids`).

Employees who only worked on the project, HR, managers and clients receive
nothing. Recipients are deduplicated by user and by case-insensitive email,
so an administrator who is also the owner and the leader gets one email.

**Button:** "View Project" opens `/admin/project-management` for readers who
can open the directory, which shows Used, Internal and Remaining for each
project. Everyone else goes to `/member/projects`. There is no per-project
page in the app, so no deep link exists.

## Detection and delivery

| Trigger | Detection |
|---|---|
| A timer stop (`POST /time-entries/{id}/stop`) | Evaluates that project right after the response, within seconds |
| A manual-time approval | Evaluates that project right after the response, within seconds |
| A budget change in Project Management | Evaluated synchronously, but only records the silent baseline |
| A **running timer**, idle resolutions, synced deductions, time transfers | Caught by the reconciliation cron |

The reconciliation cron runs as `vercel.json`
`/internal/project-budget-alerts/run` on `*/5 * * * *`. It is the safety net
for everything else, so it is detected within about five minutes.

After a claim, the run attempts delivery straight away for up to 25 emails.
The dispatch sweeper, which also runs every five minutes, delivers the rest
and retries failures. Delivery is prompt, not instantaneous.

**Logs:**
- `PROJECT_BUDGET_ALERT_CLAIMED` for each project.
- `PROJECT_BUDGET_ALERT_QUEUED` for each recipient.
- `PROJECT_BUDGET_ALERT_RUN_COMPLETE`, with an execution id, source, projects
  scanned, detected, claimed, baselined, queued, requeued, sent, send failed,
  skipped, failed and duration.

No credentials or figures are logged beyond project ids and seconds.

## Configuration and endpoints

| Setting | Purpose |
|---|---|
| `PROJECT_BUDGET_ALERTS_ENABLED` | Kill switch. Default `true`. |
| `EMAIL_DISPATCH_TOKEN` | Authenticates the cron and the manual endpoints. `CRON_SECRET` must equal it. |
| `MONITRA_APP_URL` | Base URL for the button. There is no button unless it is `https://`. |

These endpoints need the token. None of them emails anybody unless stated.

```bash
# What would happen, for everything or for one project. Claims and queues nothing.
curl -H "X-Email-Dispatch-Token: $EMAIL_DISPATCH_TOKEN" "$API/internal/project-budget-alerts/run?dry_run=true"
curl -H "X-Email-Dispatch-Token: $EMAIL_DISPATCH_TOKEN" "$API/internal/project-budget-alerts/run?dry_run=true&project_id=<id>"

# Render one alert for one recipient. 404 unless that user receives this project's alerts.
curl -H "X-Email-Dispatch-Token: $EMAIL_DISPATCH_TOKEN" \
  "$API/internal/project-budget-alerts/preview?project_id=<id>&user_id=<id>&event=remaining_20"

# Evaluate one project for real. This DOES claim events and email the recipients.
curl -H "X-Email-Dispatch-Token: $EMAIL_DISPATCH_TOKEN" "$API/internal/project-budget-alerts/run?project_id=<id>"
```

## Production deployment

1. Apply the migration `b3d5f7a9c1e2` (`alembic upgrade head`). It sets every
   existing project to version 0 and creates `project_budget_alerts`.
2. Deploy, and confirm that `vercel.json` has the `*/5` budget-alert cron and
   that `CRON_SECRET` equals `EMAIL_DISPATCH_TOKEN`.
3. Before the first cron run, do a dry run. `would_notify` should equal the
   number of active fixed projects currently over budget, and nothing else.
4. After the first run, confirm that the `PROJECT_BUDGET_ALERT_RUN_COMPLETE`
   line shows `failed=0`, and that `baselined` accounts for the thresholds
   existing projects had already passed.

## Tests

- `backend/tests/test_project_budget_alerts.py`: exact boundaries, each
  threshold once, 100% versus 101%, large jumps, internal hours, all-time
  usage, flexible and inactive projects, first deployment (70%, 95% and 101%,
  plus idempotency), budget increases and decreases, version independence,
  recipients and scope, deduplication, atomic claims, losing the start claim,
  crash recovery, dry run, the kill switch, containment, outbox retry, email
  content and escaping, the button route, the cron schedule and endpoint
  authentication.
- `backend/tests/test_fixed_hours_consistency.py`: the alert figures equal
  Project Management, the Dashboard, client Billing and the monthly summary.
- The running-timer and true two-runs-at-once cases were exercised against
  Postgres in the live test. SQLite can execute neither.

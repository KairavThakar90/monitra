# Email in production — runbook

Production runs on a GCP VM (`api.peakworkos.com`, `monitra-backend.service`).
**The `crons` in `vercel.json` do nothing there.** Every scheduled email job
is fired by the systemd timers in `deploy/backend/scheduled-jobs/`.
`backend/tests/test_scheduled_jobs.py` keeps those timers and `vercel.json`
identical.

## The twelve emails and what sends each

| # | Email | Trigger | Needs the scheduler? |
|---|---|---|---|
| 1 | Welcome | First provisioning of an account | For retries only |
| 2 | Feedback received (Admin/HR) | Feedback submitted | For retries only |
| 3 | Feedback status | Admin marks feedback Working or Resolved | For retries only |
| 4 | New version available | A desktop release is published | For retries only |
| 5 | Weekly report | `weekly-report` timer, Monday 09:00 IST | **Yes** |
| 6 | Monthly report | `monthly-report` timer, 1st at 09:00 IST | **Yes** |
| 7 | Client invitation | Admin invites a client (sent immediately) | No |
| 8 | Client login link | Client requests a sign-in link (sent immediately) | No |
| 9 | Manual time request (approvers) | Request submitted from the desktop or web | For retries only |
| 10 | Manual time receipt (requester) | Request submitted | For retries only |
| 11 | Manual time decision | Request approved or rejected | For retries only |
| 12a | Monthly project summary | `monthly-project-summary` timer, 1st at 09:00 IST | **Yes** |
| 12b | Fixed-hours budget alerts | Timer stop or approval, plus the `budget-alerts` timer every 5 minutes | **Yes**, for running timers and missed events |

Every outbox email gets one immediate delivery attempt. The `email-dispatch`
timer runs every 5 minutes and delivers everything else: retries, and the
scheduled reports, which only queue. **Without that timer, an email whose
first attempt fails is never retried, and the scheduled reports are never
delivered.**

## Deploying

1. **Deploy the backend** as usual (`deploy/backend/deploy.sh`). It runs
   `alembic upgrade head` before switching the release, and a failed
   migration never goes live. The owner and share-billing migrations are
   re-runnable, so a database restored from a dump whose `alembic_version`
   is behind its schema no longer blocks the upgrade.
2. **Install the scheduled jobs**, once, and again whenever anything in
   `deploy/backend/scheduled-jobs/` changes:
   ```bash
   # copy deploy/backend/scheduled-jobs/ to the VM, then:
   sudo bash scheduled-jobs/install.sh
   systemctl list-timers 'monitra-job-*'
   ```
   Each timer calls its internal endpoint on `127.0.0.1:8000`, with
   `EMAIL_DISPATCH_TOKEN` from `/etc/monitra/backend.env` sent as a header.
   Daily, weekly and monthly jobs are `Persistent`: if the VM was down at the
   scheduled time, the job runs as soon as it is back. Every job is
   idempotent, so a late or repeated run never sends twice.
3. **Run the preflight.** It is read-only and sends nothing.
   ```bash
   sudo -u monitra bash -c 'set -a; . /etc/monitra/backend.env; set +a; \
     cd /opt/monitra/backend && /opt/monitra/venv/bin/python scripts/email_preflight.py --smtp-login'
   ```
   Every line should be `OK`. It fails the run when:
   - email isn't configured
   - the SMTP login is refused
   - the database isn't at head
   - outbox rows are more than 15 minutes overdue, which means the dispatch
     timer isn't running
   - any `monitra-job-*` timer is missing or inactive
4. **Check a real send** to your own inbox with `scripts/smoke_email.py`.

## Checking that it keeps working

```bash
journalctl -u 'monitra-job@*' --since today      # one line per job run, with its tally
systemctl list-timers 'monitra-job-*'            # next and last run of each job
```

A job that fails exits non-zero, and `systemctl --failed` lists it.

## Required configuration (`/etc/monitra/backend.env`)

`EMAIL_PROVIDER=smtp`, `EMAIL_FROM_ADDRESS`, `SMTP_HOST`, `SMTP_PORT`,
`SMTP_USERNAME`, `SMTP_PASSWORD`, `FEEDBACK_ADMIN_EMAIL`, `FEEDBACK_HR_EMAIL`,
`MONITRA_APP_URL=https://staff.peakworkos.com` and `EMAIL_DISPATCH_TOKEN`. The
feature flags (`*_ENABLED`) all default to on.

Detail for each email lives in [Email_Notifications.md](Email_Notifications.md),
[Monthly_Project_Summary.md](Monthly_Project_Summary.md) and
[Project_Budget_Alerts.md](Project_Budget_Alerts.md).

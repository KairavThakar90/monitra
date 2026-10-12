# Email in production — runbook

Production now runs on the company's own **Windows server** (`api.peakworkos.com`,
IIS in front of uvicorn). It used to run on a GCP Linux VM (`monitra-backend.service`).
**The `crons` in `vercel.json` do nothing on either.** Every scheduled email job is
fired by a timer of the host's own kind, and a host without its timers sends the first
email of each action and nothing after that: no retries, no weekly or monthly report,
no budget alerts, no WFPM timer events, no daily roll-up.

| Host | Timers | Installed with |
|---|---|---|
| Windows server (current) | Task Scheduler tasks in `\Monitra\` — `deploy/backend/scheduled-jobs-windows/` | `Install-MonitraJobs.ps1` |
| Linux VM (former) | systemd timers — `deploy/backend/scheduled-jobs/` | `install.sh` |

`backend/tests/test_scheduled_jobs.py` and `test_scheduled_jobs_windows.py` keep both sets
identical to `vercel.json`, so a job added to one and forgotten in the other fails a test.

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

### On the Windows server (current production)

1. **Deploy the backend** the way the server already does, then bring the database
   to head from the backend folder: `python -m alembic upgrade head`.
   (`deploy/backend/deploy.sh` belongs to the old Linux VM and is not in the repository.)
2. **Put the email settings where the backend reads its others** — `backend\.env`
   or the service's environment (see "Required configuration"). Restart the
   backend, then open `https://api.peakworkos.com/health`: its `email` block must
   show `"configured": true` and `"dispatch_endpoint_enabled": true`.
3. **Install the scheduled jobs**, once, and again whenever anything in
   `deploy/backend/scheduled-jobs-windows/` changes. From an elevated PowerShell:
   ```powershell
   cd <repo>\deploy\backend\scheduled-jobs-windows
   .\Install-MonitraJobs.ps1 -BackendPath D:\monitra\backend -Port 8000 -RunNow
   ```
   It checks that the token is found and the backend answers, registers seven tasks
   under `\Monitra\`, and fires `email-dispatch` once to prove the whole path. It is safe
   to run again; `-WhatIf` and `-ShowXml` show what it would do, `-Status` shows how the
   jobs are doing, `-Uninstall` removes them. By default the tasks run as `SYSTEM`; for a
   dedicated account add `-RunAsUser DOMAIN\svc-monitra` (asked for its password once,
   handed to Task Scheduler and kept nowhere else). That account needs "Log on as a batch
   job" and read access to `<BackendPath>\.env`.

   How the jobs behave:
   - Each job calls the backend on `http://127.0.0.1:<port>`, **never through IIS**: IIS
     answers a body-less POST with HTTP 411, and its proxy timeout would cut a long job short.
   - The times are `vercel.json`'s, in UTC. Task Scheduler stores them with a fixed offset,
     which is how it marks a trigger as "synchronize across time zones", so the server's time
     zone setting should not move them. That was verified on a machine set to IST only: after
     installing, read the **Next run** column of `-Status` once and check it against the times
     in the table at the top.
   - Daily, weekly and monthly jobs catch up when the server was off at their time, and make
     up to three attempts five minutes apart when the backend is unreachable or answers 5xx
     (for example while it restarts). Task Scheduler's own "restart on failure" was tried
     and does not act on a non-zero exit code, so the retry lives in the runner. The
     every-minute and every-five-minute jobs just run again at their next firing.
   - A job never overlaps itself. Every job is idempotent, so a late or repeated run
     never sends twice.
   - The token is read from the process environment, then the machine environment, then
     `<BackendPath>\.env` — a real environment variable beats the file, as it does for the
     backend itself. If the backend service carries its *own* private `EMAIL_DISPATCH_TOKEN`
     that differs, every job gets HTTP 401 and the log says where the token was read
     from: put the token in the machine environment or `.env` instead.
   - One line per run goes to `C:\ProgramData\Monitra\logs\monitra-jobs.log`, and the
     task's **Last Run Result** is the exit code: `0` fine, `3` no token found, `4` backend
     unreachable, `5` backend answered non-2xx (`401` = wrong token).
4. **Keep `/internal/*` off the internet.** Nothing legitimate needs it from outside — the
   jobs use the loopback — yet today the dispatch token is the only lock on those addresses.
   In IIS, add a URL Rewrite rule that blocks requests whose path begins with `/internal/`.
5. **Run the preflight.** It is read-only and sends nothing.
   ```powershell
   cd D:\monitra\backend
   python scripts\email_preflight.py --smtp-login
   ```
   Every line should be `OK` (a free-mail sender is a `WARN`, see "Emails that land in
   spam"). It fails the run when:
   - email isn't configured
   - the SMTP login is refused
   - the database isn't at head
   - outbox rows are more than 15 minutes overdue, which means the dispatch job isn't running
   - any job's task is missing, disabled, or its last run failed (run it elevated: tasks
     owned by another account are invisible otherwise)
   - the sender's domain has two SPF records or two DMARC records
6. **Check a real send** to your own inbox with `scripts/smoke_email.py`, and judge the logos
   on a message that reached the Inbox.

### On the Linux VM (the former GCP host)

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
   The same checks apply, with systemd timers in place of the Task Scheduler tasks.
4. **Check a real send** to your own inbox with `scripts/smoke_email.py`.

## Checking that it keeps working

On the Windows server:

```powershell
.\Install-MonitraJobs.ps1 -Status                 # state, last result and next run of every job, and the last log lines
Get-Content C:\ProgramData\Monitra\logs\monitra-jobs.log -Tail 40
python scripts\email_preflight.py                 # from the backend folder: includes the overdue-outbox check
```

A job that fails exits non-zero, so Task Scheduler shows a non-zero **Last Run Result** for it
and the preflight fails. On the Linux VM:

```bash
journalctl -u 'monitra-job@*' --since today      # one line per job run, with its tally
systemctl list-timers 'monitra-job-*'            # next and last run of each job
```

A job that fails exits non-zero, and `systemctl --failed` lists it.

## Required configuration

Where the backend reads its settings: `backend\.env` (or the service's environment) on the
Windows server, `/etc/monitra/backend.env` on the Linux VM. A real environment variable beats
the file.

`EMAIL_PROVIDER=smtp`, `EMAIL_FROM_ADDRESS`, `SMTP_HOST`, `SMTP_PORT`,
`SMTP_USERNAME`, `SMTP_PASSWORD`, `FEEDBACK_ADMIN_EMAIL`, `FEEDBACK_HR_EMAIL`,
`MONITRA_APP_URL=https://staff.peakworkos.com` and `EMAIL_DISPATCH_TOKEN`. The
feature flags (`*_ENABLED`) all default to on. `SMTP_USERNAME` must be the same account as
`EMAIL_FROM_ADDRESS` (the preflight warns when they differ).

`EMAIL_CC_ADDRESSES` defaults to four fixed addresses that are copied on every email except
the few that carry a one-time link. Set it on purpose: an empty value switches the copy off.

**Do not change `ENV` without checking the database first.** In `development` the backend takes
its database from `DATABASE_URL_DEV`, in `production` from `DATABASE_URL` — unless `DATABASE_URL`
is set in the real environment, which always wins. Switching blindly can point Monitra at a
different database or stop it starting.

## Emails that land in spam, or show broken logos

Two problems that look random from the inbox and are not. Neither is a user-by-user fault: they
depend on the message and on the reader's mail client or filter.

**Broken logos.** A defect in how the message was built was found and fixed on 2026-10-12: the two
logos were attached with `Content-Disposition: attachment` although the HTML points at them by
Content-ID. A web mail client does not have to draw an attachment in the body (Gmail's web client
showed them as broken images plus two attachments; Outlook draws them), which would explain why only
some readers saw the problem. They are now `inline`, and `tests/test_email_notifications.py` fails if
that regresses. **Not yet confirmed in a Gmail inbox:** after deploying, send a test to a Gmail address
and look at it in the Inbox. Judge the logos on a message that reached the Inbox — a message already
filed as spam is not a fair test of the template.

**Spam placement** is decided by the sender's identity and reputation, which code cannot set:

1. **Find out what is actually failing.** In Gmail open one message that went to Spam and one that
   reached the Inbox, choose ⋮ > **Show original**, and compare the `From` address and the
   SPF / DKIM / DMARC lines at the top. `PASS` on all three with the sender's own domain is the goal.
2. **A free-mail sender is the first thing to remove.** `/health` reports it as
   `deliverability_warnings: 1` and the preflight as `Sender identity`. A brand name ("Monitra") on a
   `gmail.com` address, with links to a different domain, is scored as impersonation and cannot be
   authenticated for our domain. Send from an address on a domain you control (see the next section).
3. **Fix the DNS of that domain.** The preflight's `Sender SPF` / `Sender DMARC` / `Sender DKIM` lines
   say exactly what to change. As found on 2026-10-09, `storetransform.com` had two SPF records
   (a permanent error: SPF fails for everything), two DMARC records (receivers ignore DMARC) and no
   DKIM for Google.
4. **For staff on Google Workspace**, an administrator can also add the Monitra sender to the
   domain's approved-senders (spam override) list in the Admin console (Apps > Google Workspace >
   Gmail > Spam, Phishing and Malware; menu names move between console versions). That is immediate
   and covers every Workspace user at once, but only them: it does nothing for recipients outside
   the domain, which is what authentication is for.
5. **A user can retrain their own filter** by opening a spam-filed Monitra message and choosing
   **Report not spam**. Gmail's note "similar to messages that were identified as spam in the past"
   most likely means that earlier Monitra messages were marked as spam (during testing, say) and
   Gmail is filing look-alikes the same way. Step 1 is what tells you whether that is the case or
   whether authentication is failing.

No setting guarantees inbox placement for every recipient's mailbox. What can be done is to make the
sender authenticated and consistent, and to check it with a test to a Workspace address, a personal
Gmail address and an Outlook address, reading **Show original** on each.

## Deliverability — mail that lands in spam

Whether a recipient's server files a message as spam is decided mostly by the
**sender's domain** (SPF, DKIM, DMARC) and its reputation. Code cannot set
either, so this is largely configuration — and the client invitation and
sign-in emails are the most exposed, because they go to people who have never
heard from this sender before and carry a one-time link.

What the code controls, and does:

- **Every message carries a `Date`** (RFC 5322 requires it; its absence is a
  classic spam signal). `EmailMessage` does not add one.
- **The preheader is hidden one standard way** (`display:none`), not with the
  stack of background-coloured text, 1px font, `opacity:0` and zero-width
  padding that spam uses to hide text and that filters score as such.
- **A free-mail sender is reported.** `EMAIL_FROM_ADDRESS` on gmail.com,
  outlook.com, yahoo.com… produces a `Sender identity (spam placement)` WARN
  in `scripts/email_preflight.py` and a `deliverability_warnings` count in
  `GET /health` (`"email"`). Mail sent as "Monitra" from a consumer address can
  never be authenticated for our own domain, and a brand name on a consumer
  address with links to an unrelated domain reads as impersonation.

What only the owner of the domains can do — **send from an address on a domain
you control, authenticated for it.** State of the DNS when this was written
(2026-10-05; re-check with `nslookup -type=TXT <name>`):

| Domain | Finding |
|---|---|
| `peakworkos.com` (the app's own links) | One valid SPF (`include:_spf.google.com ~all`). **No DKIM** at any common selector, **no DMARC**, **no MX**. |
| `storetransform.com` | MX is Google Workspace. **Two SPF records** (`v=spf1 include:sender.zohobooks.com` and `v=spf1 a mx ~all`) — more than one SPF record is a permanent error (RFC 7208 §4.5), so SPF fails for everything sent as this domain. **Two DMARC records** (`p=quarantine …` and `p=none;`) — with more than one, receivers skip DMARC entirely (RFC 7489 §6.6.3), so the quarantine policy is not in force. DKIM exists only for selector `default`; Google's `google._domainkey` is absent. |

Two ways to get an authenticated sender; either needs `EMAIL_FROM_ADDRESS` (and
`SMTP_USERNAME`/`SMTP_PASSWORD`) changed in `/etc/monitra/backend.env`, then a
`sudo systemctl restart monitra-backend`:

1. **Google Workspace, on a domain it already hosts.** Create the sender (for
   example `monitra@storetransform.com`) and use its app password for
   `smtp.gmail.com:587`. In the Admin console turn on DKIM for the domain and
   publish the `google._domainkey` record it generates. Fix the domain's SPF to
   **one** record that lists every sender — for `storetransform.com` that is
   `v=spf1 a mx include:_spf.google.com include:sender.zohobooks.com ~all`
   (keep Zoho Books if it still sends invoices). Reduce DMARC to **one**
   record. Start it at `p=none` with the existing `rua=` address and tighten it
   only after the reports show every legitimate sender passing: enforcing
   `p=quarantine` the moment the duplicate is removed could quarantine mail
   from any sender (Zoho Books, for one) that is not aligned.
2. **A transactional provider** (Amazon SES, Postmark, SendGrid, Brevo, Resend)
   authenticating `peakworkos.com`: publish the DKIM CNAMEs and SPF include the
   provider gives, add a single DMARC record, and send from
   `noreply@peakworkos.com`. This also puts the sender on the same domain as
   the links in the email, which filters like to see.

Also set `EMAIL_REPLY_TO` to a mailbox somebody reads.

**Verify** by sending one real invitation to a Gmail address you own, opening
it, and choosing ⋮ → *Show original*: SPF, DKIM and DMARC must each say `PASS`,
and the `DKIM`/`SPF` domains must be the sender's own. Until they do, no change
to the message's content will reliably keep it out of spam.

Detail for each email lives in [Email_Notifications.md](Email_Notifications.md),
[Monthly_Project_Summary.md](Monthly_Project_Summary.md) and
[Project_Budget_Alerts.md](Project_Budget_Alerts.md).

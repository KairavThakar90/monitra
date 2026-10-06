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

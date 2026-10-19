"""Read-only production check for every Monitra email: will it actually go out?

Run it on the backend server before and after a deploy. It sends nothing, writes
nothing and prints no address, token or figure — only whether each piece the
twelve email types depend on is in place.

    # on the Linux VM
    sudo -u monitra bash -c 'set -a; . /etc/monitra/backend.env; set +a; \
        cd /opt/monitra/backend && /opt/monitra/venv/bin/python scripts/email_preflight.py'

    # on the Windows server (elevated PowerShell, from the backend folder)
    python scripts/email_preflight.py

    # add --smtp-login to also open an SMTP session and log in (no message is sent)

Checks:

1. Configuration — provider, sender, SMTP credential sanity, feedback
   recipients, a public https MONITRA_APP_URL, the dispatch token, and which
   email features are switched on.
2. Database — migrated to head (the budget-alert table and column exist).
3. Outbox — rows that should have been delivered but were not (the sure sign
   that the email-dispatch job is not running), and recent permanent failures.
4. Scheduler — the jobs that fire every scheduled email exist and are enabled: the
   systemd timers on Linux, the \\Monitra Task Scheduler tasks on Windows (skipped
   elsewhere). On Windows it also fails when a job's last run failed.
5. Recipients — how many admins, owners and leaders the reports would reach.
6. Sender DNS — SPF, DKIM and DMARC of the sender's domain. Receivers file mail in
   spam when these are missing or duplicated, whatever the message says.

Exit status is 1 when any check FAILs, so it can gate a deploy.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402

from app.core.config import settings  # noqa: E402

JOBS = ("email-dispatch", "budget-alerts", "weekly-report", "monthly-report",
        "monthly-project-summary", "daily-rollup", "wfpm-timer-dispatch", "timer-reconcile")
WINDOWS_TASK_FOLDER = "\\Monitra\\"
results: list[tuple[str, str, str]] = []


def report(status: str, check: str, detail: str = "") -> None:
    results.append((status, check, detail))
    print(f"[{status:4}] {check}" + (f" — {detail}" if detail else ""))


def check_configuration() -> None:
    from app.services.email import describe_configuration, describe_feedback_recipients, unconfigured_reason
    from app.services.email.provider import credential_warnings, deliverability_warnings

    config = describe_configuration()
    if config["configured"]:
        report("OK", "Email provider configured", f"provider={config['provider']} authenticated={config['smtp_authenticated']}")
    else:
        report("FAIL", "Email provider configured", unconfigured_reason() or "not configured")
    warnings = credential_warnings()
    report("FAIL" if warnings else "OK", "SMTP credential sanity", "; ".join(warnings))
    # WARN, not FAIL: the mail is delivered, it just lands in spam -- which is not
    # something a deploy should be blocked on, but is something someone must see.
    sender_warnings = deliverability_warnings()
    report("WARN" if sender_warnings else "OK", "Sender identity (spam placement)",
           "; ".join(sender_warnings) if sender_warnings else "sender is on its own domain")
    recipients = describe_feedback_recipients()
    report("OK" if recipients.get("configured") else "FAIL", "Feedback recipients (Admin/HR)",
           f"{recipients.get('recipient_count', 0)} configured")
    app_url = (settings.MONITRA_APP_URL or "").strip()
    report("OK" if app_url.startswith("https://") else "FAIL", "MONITRA_APP_URL is public https",
           "buttons in every email link here" if app_url.startswith("https://") else "emails will have no buttons")
    report("OK" if (settings.EMAIL_DISPATCH_TOKEN or "").strip() else "FAIL", "EMAIL_DISPATCH_TOKEN set",
           "required by every scheduled job")
    flags = {name: getattr(settings, name) for name in (
        "WELCOME_EMAIL_ENABLED", "RELEASE_EMAIL_ENABLED", "WEEKLY_REPORT_ENABLED", "MONTHLY_REPORT_ENABLED",
        "MONTHLY_PROJECT_SUMMARY_ENABLED", "PROJECT_BUDGET_ALERTS_ENABLED")}
    off = [name for name, value in flags.items() if not value]
    report("WARN" if off else "OK", "Email features enabled", ("switched off: " + ", ".join(off)) if off else "all on")


def check_smtp_login() -> None:
    import smtplib
    import ssl

    try:
        if settings.SMTP_USE_SSL:
            server = smtplib.SMTP_SSL(settings.SMTP_HOST, settings.SMTP_PORT, timeout=settings.SMTP_TIMEOUT_SECONDS,
                                      context=ssl.create_default_context())
        else:
            server = smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=settings.SMTP_TIMEOUT_SECONDS)
            if settings.SMTP_USE_TLS:
                server.starttls(context=ssl.create_default_context())
        if settings.SMTP_USERNAME:
            server.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD)
        server.quit()
        report("OK", "SMTP login", f"{settings.SMTP_HOST}:{settings.SMTP_PORT} accepted the credential (nothing sent)")
    except Exception as error:  # noqa: BLE001
        report("FAIL", "SMTP login", type(error).__name__)


def check_database() -> None:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    from app.core.database import get_session_local

    backend = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    heads = set(ScriptDirectory.from_config(Config(os.path.join(backend, "alembic.ini"))).get_heads())
    with get_session_local()() as db:
        current = {row[0] for row in db.execute(text("SELECT version_num FROM alembic_version")).all()}
        report("OK" if current == heads else "FAIL", "Database migrated to head",
               f"database={sorted(current)} code={sorted(heads)}")
        has_table = db.execute(text("SELECT to_regclass('public.project_budget_alerts') IS NOT NULL")).scalar()
        has_column = db.execute(text(
            "SELECT count(*) FROM information_schema.columns WHERE table_name='projects' AND column_name='budget_version'"
        )).scalar()
        report("OK" if has_table and has_column else "FAIL", "Budget-alert schema present")

        now = datetime.now(timezone.utc)
        overdue = db.execute(text(
            "SELECT notification_type, count(*) FROM email_notifications "
            "WHERE status='pending' AND next_attempt_at < :cutoff GROUP BY 1 ORDER BY 1"),
            {"cutoff": now - timedelta(minutes=15)}).all()
        report("FAIL" if overdue else "OK", "Outbox: nothing overdue by 15+ minutes",
               ("overdue: " + ", ".join(f"{t}={n}" for t, n in overdue)
                + " — the email-dispatch job is not running") if overdue else "")
        failed = db.execute(text(
            "SELECT notification_type, count(*), max(left(coalesce(last_error, ''), 90)) FROM email_notifications "
            "WHERE status='failed' AND updated_at > :since GROUP BY 1 ORDER BY 1"),
            {"since": now - timedelta(days=7)}).all()
        report("WARN" if failed else "OK", "Outbox: no permanent failures in 7 days",
               "; ".join(f"{t}={n} ({err})" for t, n, err in failed))
        last_sent = db.execute(text(
            "SELECT notification_type, max(sent_at) FROM email_notifications WHERE status='sent' GROUP BY 1 ORDER BY 1"
        )).all()
        for notification_type, sent_at in last_sent:
            print(f"       last sent {notification_type:24} {sent_at:%Y-%m-%d %H:%M} UTC" if sent_at else "")

        counts = db.execute(text(
            "SELECT "
            " count(*) FILTER (WHERE role_name IN ('administrator','org_admin','super_admin')),"
            " count(*) FILTER (WHERE can_own_projects),"
            " count(*) FILTER (WHERE role_name IN ('leader','project_leader')) "
            "FROM users WHERE is_active AND status='active' AND coalesce(email,'') <> ''")).one()
        report("OK" if counts[0] else "FAIL", "Report recipients exist",
               f"admins={counts[0]} owners={counts[1]} leaders={counts[2]}")


def check_job_ledger() -> None:
    """What the `scheduled_job_runs` ledger says: did each job actually call this backend?

    An installed task is not evidence that it runs (wrong port, wrong token, wrong
    server); a ledger row is. "Never ran" is a FAIL for the frequent jobs -- five
    minutes after installation they must exist -- and a WARN for the rare ones,
    which have no run until their day comes.
    """
    from app.core.database import get_session_local
    from app.services.scheduled_jobs import scheduler_status

    with get_session_local()() as db:
        verdict = scheduler_status(db)
    for name, info in verdict["jobs"].items():
        frequent = info["cron_utc"].startswith("*/")
        if info["never_ran"]:
            report("FAIL" if frequent else "WARN", f"Job ran: {name}",
                   "no run recorded on this backend yet -- the task is not calling it"
                   + ("" if frequent else f" (first firing expected {info['next_expected_at']})"))
        elif info["stale"]:
            report("FAIL", f"Job ran: {name}",
                   f"last ran {info['last_started_at']} on {info['last_host']} -- overdue for {info['cron_utc']} UTC")
        elif info["last_status"] == "failed":
            report("FAIL", f"Job ran: {name}", f"last run failed: {info['last_error']}")
        else:
            report("OK", f"Job ran: {name}",
                   f"{info['last_started_at']} on {info['last_host']} -> {info['last_summary']}")


def check_missing_report_periods() -> None:
    """Report periods in the recent window with no outbox rows at all -- weeks the job missed."""
    from app.core.database import get_session_local
    from app.services.report_catch_up import DEFAULT_MONTHS_BACK, DEFAULT_WEEKS_BACK, missing_report_periods

    with get_session_local()() as db:
        missing = missing_report_periods(db)
    gaps = [f"{kind}: {', '.join(periods)}" for kind, periods in missing.items() if periods]
    report("WARN" if gaps else "OK",
           f"Report periods queued (last {DEFAULT_WEEKS_BACK} weeks / {DEFAULT_MONTHS_BACK} months)",
           ("; ".join(gaps) + " -- review with `python scripts\\report_catch_up.py`, queue with `--apply`")
           if gaps else "every period has its rows")


#: Task Scheduler "last result" values that are not failures: success, running, never run yet.
_TASK_RESULT_FINE = (0, 267009, 267011)


def windows_task_json() -> str | None:
    """The \\Monitra tasks as JSON from PowerShell, or None when PowerShell is unavailable."""
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if not powershell:
        return None
    command = (
        f"Get-ScheduledTask -TaskPath '{WINDOWS_TASK_FOLDER}' -ErrorAction SilentlyContinue | ForEach-Object {{ "
        "$i = $_ | Get-ScheduledTaskInfo; [pscustomobject]@{ name = $_.TaskName; state = [string]$_.State; "
        "result = [int64]$i.LastTaskResult } } | ConvertTo-Json -Compress"
    )
    done = subprocess.run([powershell, "-NoProfile", "-NonInteractive", "-Command", command],
                          capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    return done.stdout


def summarise_windows_tasks(raw: str, jobs: tuple[str, ...] = JOBS) -> tuple[str, str]:
    """(status, detail) for the Task Scheduler answer: every job present, enabled, last run fine."""
    try:
        data = json.loads(raw) if (raw or "").strip() else []
    except ValueError:
        return "FAIL", "could not read the Task Scheduler answer"
    if isinstance(data, dict):
        data = [data]
    by_name = {item.get("name"): item for item in data}
    missing, disabled, failing = [], [], []
    for job in jobs:
        task = by_name.get(f"monitra-job-{job}")
        if task is None:
            missing.append(job)
        elif str(task.get("state", "")).lower() == "disabled":
            disabled.append(job)
        elif task.get("result") is not None and task.get("result") not in _TASK_RESULT_FINE:
            failing.append(f"{job}(result {task.get('result')})")
    problems = []
    if missing:
        hint = (" (none listed: run this from an elevated PowerShell, tasks owned by another account are invisible)"
                if len(missing) == len(jobs) else "")
        problems.append("not installed: " + ", ".join(missing) + hint
                        + " -- run deploy\\backend\\scheduled-jobs-windows\\Install-MonitraJobs.ps1")
    if disabled:
        problems.append("disabled: " + ", ".join(disabled))
    if failing:
        problems.append("last run failed: " + ", ".join(failing)
                        + " -- see C:\\ProgramData\\Monitra\\logs\\monitra-jobs.log "
                        "(3 = no token, 4 = backend unreachable, 5 = backend refused the call)")
    if problems:
        return "FAIL", "; ".join(problems)
    return "OK", f"all {len(jobs)} jobs installed, enabled, last run fine"


def check_scheduler() -> None:
    if sys.platform == "win32":
        raw = windows_task_json()
        if raw is None:
            report("SKIP", "Scheduled-job tasks", "PowerShell is not available on this machine")
            return
        status, detail = summarise_windows_tasks(raw)
        report(status, "Scheduled-job tasks installed, enabled and last run OK", detail)
        return
    if not shutil.which("systemctl"):
        report("SKIP", "Scheduled-job timers", "systemctl not available on this machine")
        return
    missing = []
    for job in JOBS:
        unit = f"monitra-job-{job}.timer"
        enabled = subprocess.run(["systemctl", "is-enabled", unit], capture_output=True, text=True).stdout.strip()
        active = subprocess.run(["systemctl", "is-active", unit], capture_output=True, text=True).stdout.strip()
        if enabled != "enabled" or active != "active":
            missing.append(f"{job}({enabled or 'missing'}/{active or 'inactive'})")
    report("FAIL" if missing else "OK", "Scheduled-job timers installed and active",
           ("not running: " + ", ".join(missing) + " — run deploy/backend/scheduled-jobs/install.sh") if missing else
           "email dispatch, budget alerts, weekly, monthly, monthly project summary, daily roll-up, WFPM timer events")


# ---------------------------------------------------------------- sender DNS --

def parse_nslookup_txt(output: str) -> list[str]:
    """The TXT records in `nslookup -type=TXT` output, one string per record.

    A long record is printed as several quoted strings; they are one record, so they are
    joined. Works for both the Windows and the BIND layout.
    """
    records = []
    for chunk in re.split(r"text\s*=", output or "")[1:]:
        parts = re.findall(r'"([^"]*)"', chunk)
        if parts:
            records.append("".join(parts))
    return records


def txt_records(name: str) -> list[str] | None:
    """TXT records of `name`; [] when there are none; None when the lookup itself failed."""
    nslookup = shutil.which("nslookup")
    if not nslookup:
        return None
    try:
        done = subprocess.run([nslookup, "-type=TXT", name], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    output = done.stdout or ""
    lowered = output.lower()
    if "non-existent domain" in lowered or "nxdomain" in lowered:
        return []
    if any(word in lowered for word in ("timed out", "server failed", "refused", "no response")):
        return None
    return parse_nslookup_txt(output)


def sender_dns_findings(domain: str, lookup, *, google_smtp: bool) -> list[tuple[str, str, str]]:
    """(status, check, detail) for the sender domain's SPF, DMARC and (Google) DKIM records.

    `lookup(name)` returns the TXT strings of a name, [] for none, None for "could not ask".
    """
    found = []
    top = lookup(domain)
    if top is None:
        return [("WARN", "Sender DNS (SPF, DKIM, DMARC)", f"could not look up {domain} from this machine")]

    spf = [record for record in top if record.lower().startswith("v=spf1")]
    if len(spf) > 1:
        found.append(("FAIL", "Sender SPF", f"{len(spf)} SPF records on {domain}: more than one is a permanent "
                      "error, so SPF fails for every message. Merge them into one record."))
    elif not spf:
        found.append(("WARN", "Sender SPF", f"no SPF record on {domain}"))
    else:
        found.append(("OK", "Sender SPF", "one SPF record"))

    dmarc_all = lookup(f"_dmarc.{domain}")
    dmarc = [record for record in (dmarc_all or []) if record.lower().startswith("v=dmarc1")]
    if len(dmarc) > 1:
        found.append(("FAIL", "Sender DMARC", f"{len(dmarc)} DMARC records on _dmarc.{domain}: receivers ignore "
                      "DMARC entirely when there is more than one. Keep one."))
    elif not dmarc:
        found.append(("WARN", "Sender DMARC", f"no DMARC record on _dmarc.{domain}"))
    else:
        policy = re.search(r"\bp=(\w+)", dmarc[0], re.I)
        found.append(("OK", "Sender DMARC", f"one record, policy {policy.group(1) if policy else '?'}"))

    if google_smtp:
        dkim = lookup(f"google._domainkey.{domain}") or []
        if any("p=" in record for record in dkim):
            found.append(("OK", "Sender DKIM", "google._domainkey is published"))
        else:
            found.append(("WARN", "Sender DKIM", f"no google._domainkey.{domain} record: turn on DKIM for the "
                          "domain in the Google Admin console (Gmail > Authenticate email) and publish the record"))
    return found


def check_sender_dns() -> None:
    from app.services.email.provider import _FREE_MAIL_DOMAINS

    sender = (settings.EMAIL_FROM_ADDRESS or "").strip().lower()
    domain = sender.rsplit("@", 1)[-1] if "@" in sender else ""
    if not domain:
        report("SKIP", "Sender DNS (SPF, DKIM, DMARC)", "EMAIL_FROM_ADDRESS is not set")
        return
    if domain in _FREE_MAIL_DOMAINS:
        report("SKIP", "Sender DNS (SPF, DKIM, DMARC)",
               f"{domain} is a free-mail domain: there is nothing of ours to authenticate (see 'Sender identity' above)")
        return
    google = (settings.SMTP_HOST or "").strip().lower().endswith("gmail.com")
    for status, check, detail in sender_dns_findings(domain, txt_records, google_smtp=google):
        report(status, check, detail)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--smtp-login", action="store_true", help="also log in to the SMTP server (sends nothing)")
    args = parser.parse_args()

    print(f"Monitra email preflight — environment={getattr(settings, 'ENV', '?')}\n")
    for step in (check_configuration, check_sender_dns, *( [check_smtp_login] if args.smtp_login else [] ),
                 check_database, check_job_ledger, check_missing_report_periods, check_scheduler):
        try:
            step()
        except Exception as error:  # noqa: BLE001 - one check must not hide the rest
            report("FAIL", step.__name__.replace("check_", ""), f"{type(error).__name__}: {str(error)[:120]}")

    failed = [r for r in results if r[0] == "FAIL"]
    print(f"\n{len(results) - len(failed)} passed/other, {len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

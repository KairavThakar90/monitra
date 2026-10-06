"""Read-only production check for every Monitra email: will it actually go out?

Run it on the backend VM before and after a deploy. It sends nothing, writes
nothing and prints no address, token or figure — only whether each piece the
twelve email types depend on is in place.

    # on the backend VM
    sudo -u monitra bash -c 'set -a; . /etc/monitra/backend.env; set +a; \
        cd /opt/monitra/backend && /opt/monitra/venv/bin/python scripts/email_preflight.py'

    # add --smtp-login to also open an SMTP session and log in (no message is sent)

Checks:

1. Configuration — provider, sender, SMTP credential sanity, feedback
   recipients, a public https MONITRA_APP_URL, the dispatch token, and which
   email features are switched on.
2. Database — migrated to head (the budget-alert table and column exist).
3. Outbox — rows that should have been delivered but were not (the sure sign
   that the email-dispatch job is not running), and recent permanent failures.
4. Scheduler — the systemd timers that fire every scheduled job exist and are
   enabled (skipped where systemctl is unavailable).
5. Recipients — how many admins, owners and leaders the reports would reach.

Exit status is 1 when any check FAILs, so it can gate a deploy.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402

from app.core.config import settings  # noqa: E402

JOBS = ("email-dispatch", "budget-alerts", "weekly-report", "monthly-report",
        "monthly-project-summary", "daily-rollup")
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


def check_scheduler() -> None:
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
           "email dispatch, budget alerts, weekly, monthly, monthly project summary, daily roll-up")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--smtp-login", action="store_true", help="also log in to the SMTP server (sends nothing)")
    args = parser.parse_args()

    print(f"Monitra email preflight — environment={getattr(settings, 'ENV', '?')}\n")
    for step in (check_configuration, *( [check_smtp_login] if args.smtp_login else [] ), check_database, check_scheduler):
        try:
            step()
        except Exception as error:  # noqa: BLE001 - one check must not hide the rest
            report("FAIL", step.__name__.replace("check_", ""), f"{type(error).__name__}: {str(error)[:120]}")

    failed = [r for r in results if r[0] == "FAIL"]
    print(f"\n{len(results) - len(failed)} passed/other, {len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

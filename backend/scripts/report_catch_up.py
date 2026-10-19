"""Find report periods the scheduler missed and, only when told to, queue them.

Run it on the backend server from the backend folder, with the backend's
environment (it reads the same .env the backend does):

    python scripts\\report_catch_up.py                # list the gaps; queues nothing
    python scripts\\report_catch_up.py --apply        # queue every missing report
    python scripts\\report_catch_up.py --weeks 8 --months 3

What it does is exactly POST /internal/reports/catch-up, without the HTTP: a
period counts as missing only when it has no outbox row at all, and each
missing period is queued through the ordinary weekly/monthly/summary run for
that period, so recipients, content and idempotency are the scheduler's own.
The next `email-dispatch` sweep delivers what was queued -- so install the
scheduled jobs first, or the reports will sit in the outbox.

Each invocation is recorded in the `scheduled_job_runs` ledger as `report-catch-up`.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--weeks", type=int, default=4, help="completed weeks to inspect (default 4, max 12)")
    parser.add_argument("--months", type=int, default=2, help="completed months to inspect (default 2, max 6)")
    parser.add_argument("--apply", action="store_true", help="queue the missing reports (default: dry run)")
    args = parser.parse_args()

    from app.core.config import settings
    from app.core.database import get_session_local
    from app.services.report_catch_up import MAX_MONTHS_BACK, MAX_WEEKS_BACK, catch_up
    from app.services.scheduled_jobs import record_job_run

    weeks = max(0, min(args.weeks, MAX_WEEKS_BACK))
    months = max(0, min(args.months, MAX_MONTHS_BACK))
    dry_run = not args.apply
    print(f"Monitra report catch-up — environment={settings.ENV} weeks={weeks} months={months} "
          f"{'DRY RUN (nothing queued)' if dry_run else 'APPLY'}\n")

    with get_session_local()() as db:
        with record_job_run("report-catch-up", dry_run=dry_run) as run:
            result = catch_up(db, weeks_back=weeks, months_back=months, dry_run=dry_run)
            run.record({"dry_run": dry_run, "queued_total": result["queued_total"]})

    for kind, periods in result["missing"].items():
        print(f"  {kind:24} missing: {', '.join(periods) if periods else 'none'}")
    for kind in ("weekly", "monthly", "monthly_project_summary"):
        for tally in result[kind]:
            start = tally.get("week_start") or tally.get("month_start")
            print(f"  {kind:24} {start}: eligible={tally.get('eligible_users', tally.get('eligible_recipients', 0))} "
                  f"queued={tally.get('queued', 0)} already={tally.get('already_queued', 0)} "
                  f"skipped={tally.get('skipped', 0)} failed={tally.get('failed', 0)}")
    print(f"\n{'would queue' if dry_run else 'queued'}: {result['queued_total']} email(s)")
    if dry_run and result["queued_total"]:
        print("Re-run with --apply to queue them. The email-dispatch job delivers them within five minutes.")
    print(json.dumps({"missing": result["missing"], "queued_total": result["queued_total"], "dry_run": dry_run}))
    return 0


if __name__ == "__main__":
    sys.exit(main())

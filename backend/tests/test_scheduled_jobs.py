"""Every scheduled email/report job fires in production, at the right time.

Production runs on a GCP VM, where vercel.json's "crons" do nothing; the
systemd timers in deploy/backend/scheduled-jobs/ are what actually fire. These
tests keep the two in lockstep and tie both to real routes, so adding a job to
one and forgetting the other -- the way every scheduled email silently stopped
when the backend moved off Vercel -- is a failing test.
"""
import json
import re
import unittest
from pathlib import Path

from app.core.config import settings

ROOT = Path(__file__).resolve().parents[2]
JOBS_DIR = ROOT / "deploy" / "backend" / "scheduled-jobs"

_WEEKDAYS = {"0": "Sun", "1": "Mon", "2": "Tue", "3": "Wed", "4": "Thu", "5": "Fri", "6": "Sat"}


def cron_to_oncalendar(cron: str) -> str:
    """The systemd OnCalendar (UTC) equivalent of the five-field crons used here."""
    minute, hour, day, month, weekday = cron.split()
    assert month == "*", cron
    if minute.startswith("*/") and hour == "*" and day == "*" and weekday == "*":
        return f"*-*-* *:00/{int(minute[2:])}:00 UTC"
    time = f"{int(hour):02d}:{int(minute):02d}:00"
    date = f"*-*-{int(day):02d}" if day != "*" else "*-*-*"
    prefix = f"{_WEEKDAYS[weekday]} " if weekday != "*" else ""
    return f"{prefix}{date} {time} UTC"


def runner_jobs() -> dict[str, str]:
    """{job name: endpoint} from the runner script's case statement."""
    script = (JOBS_DIR / "monitra-job.sh").read_text(encoding="utf-8")
    return dict(re.findall(r"^\s+([a-z-]+)\)\s+ENDPOINT=(\S+) ;;", script, re.M))


def timers() -> dict[str, dict[str, str]]:
    result = {}
    for path in JOBS_DIR.glob("monitra-job-*.timer"):
        fields = dict(re.findall(r"^(\w+)=(.+)$", path.read_text(encoding="utf-8"), re.M))
        result[path.stem.removeprefix("monitra-job-")] = fields
    return result


class TestScheduledJobs(unittest.TestCase):

    def test_every_vercel_cron_has_an_identical_vm_timer(self):
        crons = json.loads((ROOT / "vercel.json").read_text(encoding="utf-8"))["crons"]
        jobs, units = runner_jobs(), timers()
        by_endpoint = {endpoint: job for job, endpoint in jobs.items()}
        for cron in crons:
            with self.subTest(path=cron["path"]):
                self.assertIn(cron["path"], by_endpoint, "no VM job calls this endpoint")
                job = by_endpoint[cron["path"]]
                self.assertIn(job, units, f"no timer for job {job}")
                self.assertEqual(units[job]["OnCalendar"], cron_to_oncalendar(cron["schedule"]))
                self.assertEqual(units[job]["Unit"], f"monitra-job@{job}.service")
        self.assertEqual(len(jobs), len(crons), "a VM job with no vercel.json counterpart")
        self.assertEqual(set(units), set(jobs))

    def test_the_timers_match_the_configured_send_times(self):
        from app.services.monthly_report import monthly_cron_expression
        from app.services.weekly_report import weekly_cron_expression

        from unittest.mock import patch
        with patch.multiple(settings, WEEKLY_REPORT_TIMEZONE="Asia/Kolkata", WEEKLY_REPORT_DAY="monday",
                            WEEKLY_REPORT_HOUR=9, WEEKLY_REPORT_MINUTE=0,
                            MONTHLY_REPORT_HOUR=9, MONTHLY_REPORT_MINUTE=0):
            units = timers()
            self.assertEqual(units["weekly-report"]["OnCalendar"], cron_to_oncalendar(weekly_cron_expression()))
            self.assertEqual(units["monthly-report"]["OnCalendar"], cron_to_oncalendar(monthly_cron_expression()))
            self.assertEqual(units["monthly-project-summary"]["OnCalendar"], cron_to_oncalendar(monthly_cron_expression()))

    def test_every_job_calls_a_real_authenticated_post_route(self):
        """POST without the token: 401 proves the route exists, takes POST and
        is protected (a missing route would be 404, a wrong method 405)."""
        from unittest.mock import patch

        from fastapi.testclient import TestClient

        from app.main import app

        client = TestClient(app)
        with patch.object(settings, "EMAIL_DISPATCH_TOKEN", "scheduled-jobs-test-token"):
            for job, endpoint in runner_jobs().items():
                with self.subTest(job=job):
                    self.assertEqual(client.post(endpoint).status_code, 401)
                    self.assertEqual(client.post(endpoint, headers={"X-Email-Dispatch-Token": "wrong"}).status_code, 401)

    def test_daily_and_monthly_jobs_catch_up_after_downtime_and_frequent_ones_do_not(self):
        for job, fields in timers().items():
            with self.subTest(job=job):
                frequent = "/" in fields["OnCalendar"].split()[-2]
                self.assertEqual(fields["Persistent"], "false" if frequent else "true")

    def test_the_service_runs_as_the_backend_user_with_its_environment(self):
        unit = (JOBS_DIR / "monitra-job@.service").read_text(encoding="utf-8")
        self.assertIn("User=monitra", unit)
        self.assertIn("EnvironmentFile=/etc/monitra/backend.env", unit)
        self.assertIn("ExecStart=/opt/monitra/bin/monitra-job %i", unit)
        self.assertIn("Type=oneshot", unit)

    def test_the_runner_sends_the_token_as_a_header_never_in_the_url(self):
        script = (JOBS_DIR / "monitra-job.sh").read_text(encoding="utf-8")
        self.assertIn('-H "X-Email-Dispatch-Token: ${EMAIL_DISPATCH_TOKEN}"', script)
        self.assertNotRegex(script, r"\?token=|EMAIL_DISPATCH_TOKEN}\"?\s*\$\{BASE")
        self.assertIn("http://127.0.0.1:8000", script)

    def test_the_installer_installs_every_job(self):
        installer = (JOBS_DIR / "install.sh").read_text(encoding="utf-8")
        listed = re.search(r"JOBS=\(([^)]*)\)", installer).group(1).split()
        self.assertEqual(set(listed), set(runner_jobs()))

    def test_scripts_and_units_are_lf_only(self):
        for path in JOBS_DIR.iterdir():
            with self.subTest(file=path.name):
                self.assertNotIn(b"\r\n", path.read_bytes())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

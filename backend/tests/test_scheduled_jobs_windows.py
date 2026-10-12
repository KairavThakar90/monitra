"""The Windows scheduled jobs run the same jobs, at the same times, as everything else.

The company server runs Windows, where the Linux timers in deploy/backend/scheduled-jobs/
cannot run. deploy/backend/scheduled-jobs-windows/ is the Task Scheduler equivalent. These
tests keep it identical to vercel.json (the schedule of record) and to the Linux runner, so a
job added to one and forgotten in the other -- the way every scheduled email silently stopped
when the backend left Vercel, and again when it left the Linux VM -- is a failing test.

The checks on the generated Task Scheduler definitions need Windows PowerShell and are skipped
elsewhere; nothing here registers a task or touches the machine's scheduler.
"""
import json
import re
import shutil
import subprocess
import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WINDOWS_DIR = ROOT / "deploy" / "backend" / "scheduled-jobs-windows"
LINUX_DIR = ROOT / "deploy" / "backend" / "scheduled-jobs"
RUNNER = WINDOWS_DIR / "Monitra-Job.ps1"
INSTALLER = WINDOWS_DIR / "Install-MonitraJobs.ps1"
NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}

_DAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]


def runner_endpoints() -> dict[str, str]:
    """{job: endpoint} from the Windows runner's $Endpoints table."""
    text = RUNNER.read_text(encoding="utf-8")
    block = re.search(r"\$Endpoints = @\{(.*?)^\}", text, re.S | re.M).group(1)
    return dict(re.findall(r"^\s+'([a-z-]+)'\s+=\s+'(/internal/[^']+)'", block, re.M))


def installer_jobs() -> dict[str, str]:
    """{job: cron} from the Windows installer's $Jobs table."""
    text = INSTALLER.read_text(encoding="utf-8")
    return dict(re.findall(r"@\{ Name = '([a-z-]+)';\s+Cron = '([^']+)' \}", text))


def linux_endpoints() -> dict[str, str]:
    script = (LINUX_DIR / "monitra-job.sh").read_text(encoding="utf-8")
    return dict(re.findall(r"^\s+([a-z-]+)\)\s+ENDPOINT=(\S+) ;;", script, re.M))


def powershell() -> str | None:
    return shutil.which("powershell") or shutil.which("pwsh")


class TestWindowsJobsMatchTheSchedule(unittest.TestCase):

    def test_every_vercel_cron_has_the_same_windows_job(self):
        crons = json.loads((ROOT / "vercel.json").read_text(encoding="utf-8"))["crons"]
        endpoints, jobs = runner_endpoints(), installer_jobs()
        by_endpoint = {endpoint: job for job, endpoint in endpoints.items()}
        for cron in crons:
            with self.subTest(path=cron["path"]):
                self.assertIn(cron["path"], by_endpoint, "no Windows job calls this endpoint")
                job = by_endpoint[cron["path"]]
                self.assertIn(job, jobs, f"the installer registers no task for {job}")
                self.assertEqual(jobs[job], cron["schedule"])
        self.assertEqual(len(endpoints), len(crons), "a Windows job with no vercel.json counterpart")
        self.assertEqual(set(jobs), set(endpoints))

    def test_the_windows_runner_calls_the_same_endpoints_as_the_linux_runner(self):
        self.assertEqual(runner_endpoints(), linux_endpoints())

    def test_the_scripts_are_plain_ascii(self):
        """Windows PowerShell 5.1 reads a BOM-less file as ANSI; ASCII is the only safe subset."""
        for path in (RUNNER, INSTALLER):
            with self.subTest(file=path.name):
                path.read_bytes().decode("ascii")


class TestTheRunnerHandlesTheTokenCarefully(unittest.TestCase):

    def setUp(self):
        self.script = RUNNER.read_text(encoding="utf-8")

    def test_the_token_is_sent_as_a_header_and_nowhere_else(self):
        self.assertIn("'X-Email-Dispatch-Token' = $found.Token", self.script)
        self.assertNotRegex(self.script, r"\?token=")
        # Testing whether a token exists is fine; handing its value to the log or the console is not.
        for line in self.script.splitlines():
            self.assertNotRegex(line, r"(Write-JobLog|Write-Output)[^;{}]*\$found\.Token", line)

    def test_the_runner_talks_to_the_loopback_not_through_iis(self):
        """IIS answers a body-less POST with HTTP 411 and its proxy timeout would cut a long job."""
        self.assertIn("[string]$BaseUrl = 'http://127.0.0.1:8000'", self.script)
        for path in (RUNNER, INSTALLER):
            self.assertNotIn("peakworkos", path.read_text(encoding="utf-8"))

    def test_the_request_carries_an_empty_body(self):
        self.assertIn("-Body ''", self.script)

    def test_only_transient_failures_are_retried(self):
        """An unreachable backend or a 5xx is worth another try; a refused token or a 4xx is not."""
        self.assertIn("$transient = ($exitCode -eq 4) -or ($status -ge 500)", self.script)
        self.assertIn("[int]$Attempts = 1", self.script)

    def test_failures_have_distinct_exit_codes(self):
        for code in ("exit 2", "exit 3", "$exitCode = 4", "$exitCode = 5", "$exitCode = 0", "exit $exitCode"):
            self.assertIn(code, self.script)


@unittest.skipUnless(powershell(), "PowerShell is not installed")
class TestTheScriptsParse(unittest.TestCase):

    def test_both_scripts_parse_without_errors(self):
        for path in (RUNNER, INSTALLER):
            with self.subTest(file=path.name):
                command = (
                    "$e=$null;$t=$null;"
                    f"[void][System.Management.Automation.Language.Parser]::ParseFile('{path}',[ref]$t,[ref]$e);"
                    "if($e.Count){$e|ForEach-Object{$_.Message};exit 1}"
                )
                done = subprocess.run([powershell(), "-NoProfile", "-NonInteractive", "-Command", command],
                                      capture_output=True, text=True, timeout=120)
                self.assertEqual(done.returncode, 0, done.stdout + done.stderr)


@unittest.skipUnless(sys.platform == "win32" and powershell(), "needs Windows PowerShell")
class TestTheGeneratedTaskDefinitions(unittest.TestCase):
    """`-ShowXml` prints what would be registered; nothing is registered."""

    @classmethod
    def setUpClass(cls):
        done = subprocess.run(
            [powershell(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", str(INSTALLER), "-ShowXml", "-BackendPath", r"C:\monitra\backend"],
            capture_output=True, text=True, timeout=120,
        )
        if done.returncode != 0:
            raise AssertionError(done.stdout + done.stderr)
        cls.tasks = {}
        for chunk in re.split(r"^### ", done.stdout, flags=re.M)[1:]:
            name, _, xml = chunk.partition("\n")
            xml = re.sub(r"^<\?xml[^>]*\?>", "", xml.strip())
            cls.tasks[name.strip().removeprefix("monitra-job-")] = ET.fromstring(xml)

    def test_there_is_one_task_per_job(self):
        self.assertEqual(set(self.tasks), set(installer_jobs()))

    def test_each_trigger_is_the_cron_expressed_in_utc(self):
        for job, cron in installer_jobs().items():
            with self.subTest(job=job):
                minute, hour, dom, _, dow = cron.split()
                triggers = self.tasks[job].find("t:Triggers", NS)
                self.assertEqual(len(triggers), 1)
                trigger = triggers[0]
                start = trigger.findtext("t:StartBoundary", namespaces=NS)
                if minute.startswith("*/"):
                    self.assertTrue(trigger.tag.endswith("TimeTrigger"))
                    self.assertEqual(trigger.findtext("t:Repetition/t:Interval", namespaces=NS), f"PT{int(minute[2:])}M")
                    self.assertEqual(start, "2026-01-01T00:00:00Z")
                    continue
                self.assertTrue(trigger.tag.endswith("CalendarTrigger"))
                self.assertEqual(start, f"2026-01-01T{int(hour):02d}:{int(minute):02d}:00Z")
                if dow != "*":
                    days = [element.tag.split("}")[1] for element in trigger.find("t:ScheduleByWeek/t:DaysOfWeek", NS)]
                    self.assertEqual(days, [_DAYS[int(dow)]])
                elif dom != "*":
                    self.assertEqual(trigger.findtext("t:ScheduleByMonth/t:DaysOfMonth/t:Day", namespaces=NS), str(int(dom)))
                    self.assertEqual(len(trigger.find("t:ScheduleByMonth/t:Months", NS)), 12)
                else:
                    self.assertIsNotNone(trigger.find("t:ScheduleByDay", NS))

    def test_rare_jobs_catch_up_after_downtime_and_frequent_ones_do_not(self):
        for job, cron in installer_jobs().items():
            with self.subTest(job=job):
                frequent = cron.split()[0].startswith("*/")
                settings = self.tasks[job].find("t:Settings", NS)
                self.assertEqual(settings.findtext("t:StartWhenAvailable", namespaces=NS), "false" if frequent else "true")

    def test_a_job_never_overlaps_itself_and_is_given_time_to_finish_but_not_forever(self):
        for job, cron in installer_jobs().items():
            with self.subTest(job=job):
                frequent = cron.split()[0].startswith("*/")
                settings = self.tasks[job].find("t:Settings", NS)
                self.assertEqual(settings.findtext("t:MultipleInstancesPolicy", namespaces=NS), "IgnoreNew")
                # The rare jobs may spend three attempts, five minutes apart, waiting for a backend.
                self.assertEqual(settings.findtext("t:ExecutionTimeLimit", namespaces=NS), "PT15M" if frequent else "PT1H")
                self.assertEqual(settings.findtext("t:Enabled", namespaces=NS), "true")

    def test_rare_jobs_retry_inside_the_runner_because_task_scheduler_will_not(self):
        """Task Scheduler's "restart on failure" does not act on a non-zero exit code (tried: a
        failing task was never restarted, whether started by hand or by its trigger), so a setting
        that merely looks like a retry would leave a missed monthly report waiting a month."""
        for job, cron in installer_jobs().items():
            with self.subTest(job=job):
                frequent = cron.split()[0].startswith("*/")
                arguments = self.tasks[job].findtext("t:Actions/t:Exec/t:Arguments", namespaces=NS)
                if frequent:
                    self.assertNotIn("-Attempts", arguments)
                else:
                    self.assertIn("-Attempts 3 -RetryDelaySec 300", arguments)
                self.assertIsNone(self.tasks[job].find("t:Settings/t:RestartOnFailure", NS))

    def test_each_action_runs_the_runner_for_its_own_job_and_carries_no_secret(self):
        for job in installer_jobs():
            with self.subTest(job=job):
                exec_ = self.tasks[job].find("t:Actions/t:Exec", NS)
                self.assertTrue(exec_.findtext("t:Command", namespaces=NS).lower().endswith("powershell.exe"))
                arguments = exec_.findtext("t:Arguments", namespaces=NS)
                self.assertIn("Monitra-Job.ps1", arguments)
                self.assertIn(f"-Job {job} ", arguments)
                self.assertIn('-BaseUrl "http://127.0.0.1:8000"', arguments)
                self.assertNotRegex(arguments, r"(?i)token|password|secret")
                self.assertNotIn("<Password", ET.tostring(self.tasks[job], encoding="unicode"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

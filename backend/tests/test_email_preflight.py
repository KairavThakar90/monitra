"""The production email preflight reads the things that decide whether mail is delivered and trusted.

`scripts/email_preflight.py` is what an operator runs after a deploy. Two of its checks were added
when production moved from the Linux VM to a Windows server and mail began landing in spam:

* the scheduler check knows the Windows tasks, not only systemd timers, and fails on a job whose last
  run failed;
* the sender-DNS check finds the three faults that put mail in spam whatever the message says: two
  SPF records, two DMARC records, and no DKIM.

Nothing here queries DNS, PowerShell or a database: the parsing and the judgement are pure functions.
"""
import importlib.util
import json
import re
import unittest
from pathlib import Path

from tests.test_scheduled_jobs_windows import runner_endpoints

BACKEND = Path(__file__).resolve().parents[1]


def load_preflight():
    spec = importlib.util.spec_from_file_location("email_preflight_under_test", BACKEND / "scripts" / "email_preflight.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


preflight = load_preflight()

WINDOWS_NSLOOKUP = """Server:  dns.example
Address:  192.168.0.1

Non-authoritative answer:
storetransform.com\ttext =

\t"v=spf1 include:sender.zohobooks.com"
storetransform.com\ttext =

\t"v=spf1 a mx ~all"
storetransform.com\ttext =

\t"google-site-verification=abc123"
"""

BIND_NSLOOKUP = """Server:\t\t127.0.0.53
Address:\t127.0.0.53#53

Non-authoritative answer:
google._domainkey.example.com\ttext = "v=DKIM1; k=rsa; p=MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA" "second-half-of-a-long-key"

Authoritative answers can be found from:
"""


class TestNslookupParsing(unittest.TestCase):

    def test_windows_layout_gives_one_string_per_record(self):
        self.assertEqual(preflight.parse_nslookup_txt(WINDOWS_NSLOOKUP), [
            "v=spf1 include:sender.zohobooks.com", "v=spf1 a mx ~all", "google-site-verification=abc123",
        ])

    def test_a_long_record_split_into_quoted_strings_is_one_record(self):
        (record,) = preflight.parse_nslookup_txt(BIND_NSLOOKUP)
        self.assertTrue(record.startswith("v=DKIM1; k=rsa; p=MIIB"))
        self.assertTrue(record.endswith("second-half-of-a-long-key"))

    def test_no_records_is_an_empty_list(self):
        self.assertEqual(preflight.parse_nslookup_txt("Server: x\n*** can't find y: Non-existent domain\n"), [])


def fake_lookup(table):
    return lambda name: table.get(name, [])


class TestSenderDnsFindings(unittest.TestCase):

    def findings(self, table, *, google=True):
        return {check: (status, detail) for status, check, detail in
                preflight.sender_dns_findings("example.com", fake_lookup(table), google_smtp=google)}

    def test_the_faults_found_on_storetransform_com_are_all_failures_or_warnings(self):
        """Two SPF records, two DMARC records and no DKIM -- as published on 2026-10-09."""
        found = self.findings({
            "example.com": ["v=spf1 include:sender.zohobooks.com", "v=spf1 a mx ~all"],
            "_dmarc.example.com": ["v=DMARC1; p=quarantine; rua=mailto:r@example.com", "v=DMARC1; p=none;"],
        })
        self.assertEqual(found["Sender SPF"][0], "FAIL")
        self.assertEqual(found["Sender DMARC"][0], "FAIL")
        self.assertEqual(found["Sender DKIM"][0], "WARN")

    def test_a_correct_domain_passes_everything(self):
        found = self.findings({
            "example.com": ["v=spf1 include:_spf.google.com ~all", "google-site-verification=x"],
            "_dmarc.example.com": ["v=DMARC1; p=none; rua=mailto:r@example.com"],
            "google._domainkey.example.com": ["v=DKIM1; k=rsa; p=MIIBIjANBg"],
        })
        self.assertEqual({status for status, _ in found.values()}, {"OK"})
        self.assertIn("policy none", found["Sender DMARC"][1])

    def test_missing_records_are_warnings_not_failures(self):
        found = self.findings({})
        self.assertEqual(found["Sender SPF"][0], "WARN")
        self.assertEqual(found["Sender DMARC"][0], "WARN")

    def test_dkim_is_only_checked_for_a_google_sender(self):
        self.assertNotIn("Sender DKIM", self.findings({}, google=False))

    def test_a_failed_lookup_is_one_warning_not_a_false_failure(self):
        (finding,) = preflight.sender_dns_findings("example.com", lambda name: None, google_smtp=True)
        self.assertEqual(finding[0], "WARN")


class TestWindowsTaskSummary(unittest.TestCase):

    def tasks(self, **overrides):
        rows = [{"name": f"monitra-job-{job}", "state": "Ready", "result": 0} for job in preflight.JOBS]
        for row in rows:
            row.update(overrides.get(row["name"].removeprefix("monitra-job-"), {}))
        return rows

    def test_all_jobs_present_and_fine_is_ok(self):
        status, detail = preflight.summarise_windows_tasks(json.dumps(self.tasks()))
        self.assertEqual(status, "OK", detail)

    def test_never_run_and_running_are_not_failures(self):
        rows = self.tasks(**{"weekly-report": {"result": 267011}, "email-dispatch": {"result": 267009}})
        self.assertEqual(preflight.summarise_windows_tasks(json.dumps(rows))[0], "OK")

    def test_a_missing_job_fails_and_names_the_installer(self):
        rows = [row for row in self.tasks() if row["name"] != "monitra-job-daily-rollup"]
        status, detail = preflight.summarise_windows_tasks(json.dumps(rows))
        self.assertEqual(status, "FAIL")
        self.assertIn("daily-rollup", detail)
        self.assertIn("Install-MonitraJobs.ps1", detail)

    def test_a_disabled_job_fails(self):
        status, detail = preflight.summarise_windows_tasks(json.dumps(self.tasks(**{"budget-alerts": {"state": "Disabled"}})))
        self.assertEqual(status, "FAIL")
        self.assertIn("budget-alerts", detail)

    def test_a_job_whose_last_run_failed_fails_and_says_where_to_look(self):
        status, detail = preflight.summarise_windows_tasks(json.dumps(self.tasks(**{"email-dispatch": {"result": 5}})))
        self.assertEqual(status, "FAIL")
        self.assertIn("email-dispatch(result 5)", detail)
        self.assertIn("monitra-jobs.log", detail)

    def test_nothing_listed_suggests_running_elevated(self):
        status, detail = preflight.summarise_windows_tasks("")
        self.assertEqual(status, "FAIL")
        self.assertIn("elevated", detail)

    def test_a_single_task_comes_back_as_an_object_not_a_list(self):
        status, detail = preflight.summarise_windows_tasks(json.dumps({"name": "monitra-job-email-dispatch", "state": "Ready", "result": 0}))
        self.assertEqual(status, "FAIL")
        listed_missing = detail.split("not installed:")[1].split("(")[0].split("--")[0]
        self.assertNotIn("email-dispatch", listed_missing)   # the one task that exists is not reported missing
        self.assertIn("budget-alerts", listed_missing)

    def test_garbage_is_a_failure_not_a_crash(self):
        self.assertEqual(preflight.summarise_windows_tasks("not json")[0], "FAIL")


class TestThePreflightKnowsEveryJob(unittest.TestCase):

    def test_it_checks_every_job_the_runners_know(self):
        """A job added to the runners and forgotten here would be installed and never verified."""
        self.assertEqual(set(preflight.JOBS), set(runner_endpoints()))

    def test_the_task_folder_matches_the_installer(self):
        installer = (BACKEND.parent / "deploy" / "backend" / "scheduled-jobs-windows" / "Install-MonitraJobs.ps1").read_text(encoding="utf-8")
        default_folder = re.search(r"\[string\]\$TaskFolder = '([^']+)'", installer).group(1)
        self.assertEqual(preflight.WINDOWS_TASK_FOLDER.strip("\\"), default_folder.strip("\\"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

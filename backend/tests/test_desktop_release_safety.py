"""The rules that stand between a build and every staff machine.

A published release row names a file that every installed Monitra downloads and
then runs, and publishing it can email the whole company. Both are things you
cannot take back, so this file defends the gates in front of them:

* where an artifact may be hosted, and what it must be called;
* that a version is only ever published as a complete, signed set;
* that the announcement waits for the whole set, honours the off switch, and in
  test mode reaches only the addresses it was pointed at;
* that only a signed-in administrator can publish -- the pipeline's own key
  cannot;
* that registering the same artifact twice is refused, not overwritten.

Database access is stood in for (the suite's convention); the policy itself is
pure and runs for real.
"""

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app.core.config import Settings, settings
from app.core.database import get_db
from app.main import app
from app.models.desktop_release import ReleaseStatus
from app.schemas.desktop_release import DesktopReleaseCreate
from app.services import desktop_release_policy as policy
from app.services.desktop_release import (
    DesktopReleaseService, ReleaseConflict, ReleasePolicyViolation,
)
from app.services.email import workflows
from app.services.email.workflows import (
    queue_release_announcements, release_dedupe_key, release_test_dedupe_key,
)
from test_email_notifications import _notification, _release, email_settings
from test_service_credential import (
    _bot_user, _FakeSession, _mint, _person, _release_row,
)

SVC = "app.services.desktop_release"
WORKFLOWS = "app.services.email.workflows"
GH = "https://github.com/KairavThakar90/monitra/releases/download"
UTC = timezone.utc


def row(platform="win32", arch=None, version="1.3.2", status="draft", **overrides):
    """One registered artifact, valid unless a test breaks it."""
    names = {
        ("win32", None): f"Monitra-Setup-{version}.exe",
        ("darwin", "arm64"): f"Monitra-macOS-arm64-{version}.dmg",
        ("darwin", "x86_64"): f"Monitra-macOS-x86_64-{version}.dmg",
    }
    data = dict(
        id=hash((platform, arch, version)) % 10_000,
        version=version, platform=platform, architecture=arch,
        download_url=f"{GH}/v{version}/{names.get((platform, arch), 'x.bin')}",
        sha256="a" * 64, file_size=31_000_000, signed=True, signer="Monitra",
        status=status, published_at=None,
        force_update=False, min_supported_version=None,
        release_notes="Notes.", release_notes_url=None,
    )
    data.update(overrides)
    return SimpleNamespace(**data)


def full_set(version="1.3.2", status="draft", **overrides):
    return [
        row("win32", None, version, status, **overrides),
        row("darwin", "arm64", version, status, **overrides),
        row("darwin", "x86_64", version, status, **overrides),
    ]


# ─────────────────────────────────────────────────────────────────────────
# Where an artifact may be hosted
# ─────────────────────────────────────────────────────────────────────────


class DownloadUrlPolicyTests(unittest.TestCase):

    def test_an_approved_https_release_url_is_accepted(self):
        self.assertIsNone(policy.check_download_url(
            f"{GH}/v1.3.2/Monitra-Setup-1.3.2.exe"))

    def test_plain_http_is_refused(self):
        self.assertIn("https", policy.check_download_url(
            "http://github.com/o/r/releases/download/v1.3.2/Monitra-Setup-1.3.2.exe"))

    def test_an_unapproved_host_is_refused(self):
        problem = policy.check_download_url("https://evil.example.com/Monitra-Setup-1.3.2.exe")
        self.assertIn("not an approved release host", problem)

    def test_a_lookalike_host_is_refused(self):
        # Exact hostnames only: a suffix or prefix match would admit these.
        for host in ("github.com.evil.example", "evilgithub.com", "api.github.com"):
            with self.subTest(host=host):
                self.assertIsNotNone(policy.check_download_url(f"https://{host}/a/b.exe"))

    def test_credentials_in_the_url_are_refused(self):
        self.assertIn("credentials", policy.check_download_url(
            "https://user:pass@github.com/o/r/releases/download/v1/Monitra-Setup-1.exe"))

    def test_a_non_default_port_is_refused(self):
        self.assertIsNotNone(policy.check_download_url("https://github.com:8443/o/r/x.exe"))

    def test_dot_dot_segments_and_odd_characters_are_refused(self):
        for url in (
            "https://github.com/o/r/releases/download/../../x.exe",
            "https://github.com/o/r/a b.exe",
            "https://github.com/o\\r/a.exe",
            "https://github.com/o/r/a.exe#frag",
        ):
            with self.subTest(url=url):
                self.assertIsNotNone(policy.check_download_url(url))

    def test_missing_urls_are_refused(self):
        for value in (None, "", 5):
            with self.subTest(value=value):
                self.assertIsNotNone(policy.check_download_url(value))

    def test_a_prefix_pins_downloads_to_one_repository(self):
        with patch.object(settings, "DESKTOP_DOWNLOAD_URL_PREFIXES",
                          "https://github.com/KairavThakar90/monitra-releases/releases/download/"):
            ok = "https://github.com/KairavThakar90/monitra-releases/releases/download/v1/Monitra-Setup-1.exe"
            other = "https://github.com/someone-else/repo/releases/download/v1/Monitra-Setup-1.exe"
            self.assertIsNone(policy.check_download_url(ok))
            self.assertIn("approved release location", policy.check_download_url(other))

    def test_the_host_list_is_configurable(self):
        with patch.object(settings, "DESKTOP_DOWNLOAD_ALLOWED_HOSTS", "downloads.example.com"):
            self.assertIsNone(policy.check_download_url("https://downloads.example.com/x.exe"))
            self.assertIsNotNone(policy.check_download_url(f"{GH}/v1/x.exe"))


class FilenamePolicyTests(unittest.TestCase):

    def test_each_artifact_has_exactly_one_acceptable_name(self):
        self.assertEqual(policy.expected_filename("win32", None, "1.3.2"), "Monitra-Setup-1.3.2.exe")
        self.assertEqual(policy.expected_filename("darwin", "arm64", "1.3.2"),
                         "Monitra-macOS-arm64-1.3.2.dmg")
        self.assertEqual(policy.expected_filename("darwin", "x86_64", "1.3.2"),
                         "Monitra-macOS-x86_64-1.3.2.dmg")

    def test_the_wrong_versions_installer_is_refused(self):
        bad = row(download_url=f"{GH}/v1.3.1/Monitra-Setup-1.3.1.exe", version="1.3.2")
        self.assertIn("filename", [p.code for p in policy.check_row(bad)])

    def test_the_portable_zip_cannot_stand_in_for_the_installer(self):
        bad = row(download_url=f"{GH}/v1.3.2/Monitra-Portable-1.3.2.zip")
        self.assertIn("filename", [p.code for p in policy.check_row(bad)])

    def test_the_other_architectures_dmg_is_refused(self):
        bad = row("darwin", "arm64", download_url=f"{GH}/v1.3.2/Monitra-macOS-x86_64-1.3.2.dmg")
        self.assertIn("filename", [p.code for p in policy.check_row(bad)])


# ─────────────────────────────────────────────────────────────────────────
# A version is published as a complete set
# ─────────────────────────────────────────────────────────────────────────


class ReadinessTests(unittest.TestCase):

    def test_a_complete_valid_signed_set_is_ready(self):
        report = policy.readiness("1.3.2", full_set())
        self.assertTrue(report.ready, [p.message for p in report.problems])
        self.assertEqual(sorted(report.present), ["darwin/arm64", "darwin/x86_64", "win32"])

    def test_a_missing_platform_artifact_blocks_the_release(self):
        rows = [r for r in full_set() if r.architecture != "x86_64"]
        report = policy.readiness("1.3.2", rows)
        self.assertFalse(report.ready)
        self.assertEqual([(p.code, p.artifact) for p in report.problems],
                         [("missing", "darwin/x86_64")])

    def test_an_empty_version_reports_every_artifact_missing(self):
        report = policy.readiness("1.3.2", [])
        self.assertEqual(len(report.problems), 3)

    def test_an_unsigned_artifact_blocks_the_release(self):
        rows = full_set()
        rows[0].signed = False
        report = policy.readiness("1.3.2", rows)
        self.assertIn(("unsigned", "win32"), [(p.code, p.artifact) for p in report.problems])

    def test_unsigned_is_allowed_only_when_the_deployment_says_so(self):
        rows = full_set(signed=False)
        with patch.object(settings, "DESKTOP_REQUIRE_SIGNED_RELEASES", False):
            self.assertTrue(policy.readiness("1.3.2", rows).ready)

    def test_a_row_without_a_size_or_checksum_blocks_the_release(self):
        rows = full_set()
        rows[1].file_size = None
        rows[2].sha256 = "not-a-digest"
        codes = {(p.code, p.artifact) for p in policy.readiness("1.3.2", rows).problems}
        self.assertIn(("file_size", "darwin/arm64"), codes)
        self.assertIn(("sha256", "darwin/x86_64"), codes)

    def test_a_withdrawn_artifact_does_not_count_as_present(self):
        rows = full_set()
        rows[0].status = ReleaseStatus.ROLLED_BACK
        report = policy.readiness("1.3.2", rows)
        self.assertEqual([p.code for p in report.problems], ["withdrawn"])

    def test_rows_of_other_versions_are_ignored(self):
        rows = full_set("1.3.1") + [row("win32", None, "1.3.2")]
        report = policy.readiness("1.3.2", rows)
        self.assertEqual({p.artifact for p in report.problems}, {"darwin/arm64", "darwin/x86_64"})

    def test_complete_and_published_needs_every_required_row_live(self):
        rows = full_set(status="published")
        self.assertTrue(policy.is_complete_and_published("1.3.2", rows))
        rows[2].status = "draft"
        self.assertFalse(policy.is_complete_and_published("1.3.2", rows))

    def test_the_required_set_is_configurable(self):
        with patch.object(settings, "DESKTOP_REQUIRED_ARTIFACTS", "win32"):
            rows = [row("win32", None, "1.3.2")]
            self.assertTrue(policy.readiness("1.3.2", rows).ready)


# ─────────────────────────────────────────────────────────────────────────
# Registration
# ─────────────────────────────────────────────────────────────────────────


class RegistrationTests(unittest.TestCase):

    def payload(self, **overrides):
        data = dict(
            version="1.3.2", platform="win32", architecture=None,
            download_url=f"{GH}/v1.3.2/Monitra-Setup-1.3.2.exe",
            sha256="c" * 64, file_size=31_000_000, signed=True, signer="Monitra",
        )
        data.update(overrides)
        return DesktopReleaseCreate(**data)

    def register(self, payload):
        with patch(f"{SVC}.DesktopReleaseRepository") as repo:
            repo.get_artifact.return_value = None
            return DesktopReleaseService.create_release(MagicMock(), payload)

    def test_a_valid_artifact_is_registered_as_an_unpublished_draft(self):
        release = self.register(self.payload())
        self.assertEqual(release.status, ReleaseStatus.DRAFT)
        self.assertTrue(release.signed)
        self.assertEqual(release.signer, "Monitra")

    def test_registering_never_publishes(self):
        self.assertIsNone(self.register(self.payload()).published_at)

    def test_an_unapproved_host_is_refused_at_registration(self):
        with self.assertRaises(ReleasePolicyViolation) as caught:
            self.register(self.payload(download_url="https://evil.example.com/Monitra-Setup-1.3.2.exe"))
        self.assertEqual(caught.exception.problems[0].code, "url")

    def test_a_http_url_is_refused_at_registration(self):
        with self.assertRaises(ReleasePolicyViolation):
            self.register(self.payload(download_url="http://github.com/o/r/releases/download/v1.3.2/Monitra-Setup-1.3.2.exe"))

    def test_a_mismatched_file_name_is_refused_at_registration(self):
        with self.assertRaises(ReleasePolicyViolation) as caught:
            self.register(self.payload(download_url=f"{GH}/v1.3.2/Monitra-Setup-1.3.1.exe"))
        self.assertEqual(caught.exception.problems[0].code, "filename")

    def test_an_unsigned_draft_can_be_registered_for_a_pilot(self):
        release = self.register(self.payload(signed=False, signer=None))
        self.assertFalse(release.signed)

    def test_registration_is_idempotent_not_overwriting(self):
        with patch(f"{SVC}.DesktopReleaseRepository") as repo:
            repo.get_artifact.return_value = object()
            with self.assertRaises(ReleaseConflict):
                DesktopReleaseService.create_release(MagicMock(), self.payload())
            repo.add.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────
# Publishing
# ─────────────────────────────────────────────────────────────────────────


class PublishingTests(unittest.TestCase):

    def publish_one(self, rows, target):
        db = MagicMock()
        with patch(f"{SVC}.DesktopReleaseRepository") as repo, \
                patch("app.services.email.queue_release_announcements") as announce:
            repo.get.return_value = target
            repo.list_for_version.return_value = rows
            result = DesktopReleaseService.set_status(db, target.id, ReleaseStatus.PUBLISHED)
        return result, db, announce

    def test_a_row_cannot_be_published_while_a_required_artifact_is_missing(self):
        rows = [r for r in full_set() if r.platform == "win32"]
        with self.assertRaises(ReleasePolicyViolation) as caught:
            self.publish_one(rows, rows[0])
        codes = {p.code for p in caught.exception.problems}
        self.assertEqual(codes, {"missing"})
        self.assertEqual(rows[0].status, "draft", "a refused publish must change nothing")

    def test_a_refused_publish_commits_and_announces_nothing(self):
        rows = [r for r in full_set() if r.platform == "win32"]
        db = MagicMock()
        with patch(f"{SVC}.DesktopReleaseRepository") as repo, \
                patch("app.services.email.queue_release_announcements") as announce:
            repo.get.return_value = rows[0]
            repo.list_for_version.return_value = rows
            with self.assertRaises(ReleasePolicyViolation):
                DesktopReleaseService.set_status(db, 1, ReleaseStatus.PUBLISHED)
        db.commit.assert_not_called()
        announce.assert_not_called()

    def test_an_unsigned_release_cannot_be_published(self):
        rows = full_set(signed=False)
        with self.assertRaises(ReleasePolicyViolation) as caught:
            self.publish_one(rows, rows[0])
        self.assertIn("unsigned", {p.code for p in caught.exception.problems})

    def test_publishing_the_first_artifact_of_a_complete_set_does_not_announce(self):
        rows = full_set()
        _, db, announce = self.publish_one(rows, rows[0])
        db.commit.assert_called_once()
        announce.assert_not_called()

    def test_publishing_the_last_artifact_sends_the_announcement_once(self):
        rows = full_set()
        rows[0].status = rows[1].status = "published"
        _, _db, announce = self.publish_one(rows, rows[2])
        announce.assert_called_once()

    def test_republishing_a_published_row_is_not_new_news(self):
        rows = full_set(status="published")
        _, _db, announce = self.publish_one(rows, rows[0])
        announce.assert_not_called()

    def test_a_status_change_through_update_is_gated_the_same_way(self):
        from app.schemas.desktop_release import DesktopReleaseUpdate

        rows = [r for r in full_set() if r.platform == "win32"]
        with patch(f"{SVC}.DesktopReleaseRepository") as repo:
            repo.get.return_value = rows[0]
            repo.list_for_version.return_value = rows
            with self.assertRaises(ReleasePolicyViolation):
                DesktopReleaseService.update_release(
                    MagicMock(), 1, DesktopReleaseUpdate(status="published"))

    def test_a_complete_valid_set_publishes_together_and_announces_once(self):
        rows = full_set()
        db = MagicMock()
        with patch(f"{SVC}.DesktopReleaseRepository") as repo, \
                patch("app.services.email.queue_release_announcements", return_value=[1]) as announce:
            repo.list_for_version.return_value = rows
            published, queued = DesktopReleaseService.publish_version(db, "1.3.2")
        self.assertEqual({r.status for r in rows}, {"published"})
        self.assertEqual(len(published), 3)
        self.assertTrue(queued)
        announce.assert_called_once()
        db.commit.assert_called_once()

    def test_publishing_a_version_is_all_or_nothing(self):
        rows = full_set()
        rows[2].file_size = None
        db = MagicMock()
        with patch(f"{SVC}.DesktopReleaseRepository") as repo, \
                patch("app.services.email.queue_release_announcements") as announce:
            repo.list_for_version.return_value = rows
            with self.assertRaises(ReleasePolicyViolation):
                DesktopReleaseService.publish_version(db, "1.3.2")
        self.assertEqual({r.status for r in rows}, {"draft"}, "no row may move")
        db.commit.assert_not_called()
        announce.assert_not_called()

    def test_rolling_back_is_never_gated_and_never_announces(self):
        rows = full_set(status="published")
        db = MagicMock()
        with patch(f"{SVC}.DesktopReleaseRepository") as repo, \
                patch("app.services.email.queue_release_announcements") as announce:
            repo.get.return_value = rows[0]
            DesktopReleaseService.set_status(db, 1, ReleaseStatus.ROLLED_BACK)
        self.assertEqual(rows[0].status, "rolled_back")
        announce.assert_not_called()


class ServingTests(unittest.TestCase):
    """The update check refuses to hand a client what the policy would not accept."""

    def user(self):
        user = MagicMock()
        user.id = 7
        user.organization_id = 1
        return user

    def check(self, release):
        with patch(f"{SVC}.DesktopReleaseRepository") as repo, \
                patch(f"{SVC}.DesktopClientVersionRepository"):
            repo.latest_published.return_value = release
            return DesktopReleaseService.latest_version(
                MagicMock(), self.user(), "1.3.1", "win32", "AMD64")

    def test_a_valid_published_row_is_offered(self):
        answer = self.check(row(status="published"))
        self.assertTrue(answer.update_available)
        self.assertEqual(answer.latest_version, "1.3.2")

    def test_a_published_row_on_an_unapproved_host_is_not_served(self):
        answer = self.check(row(status="published",
                                download_url="https://evil.example.com/Monitra-Setup-1.3.2.exe"))
        self.assertFalse(answer.update_available)
        self.assertIsNone(answer.download_url)
        self.assertIsNone(answer.sha256)

    def test_an_equal_or_older_release_is_never_an_update(self):
        for latest in ("1.3.1", "1.3.0", "1.2.9"):
            with self.subTest(latest=latest):
                answer = self.check(row(version=latest, status="published",
                                        download_url=f"{GH}/v{latest}/Monitra-Setup-{latest}.exe"))
                self.assertFalse(answer.update_available)

    def test_a_newer_release_is_offered_across_the_semver_boundaries(self):
        for latest in ("1.3.2", "1.3.10", "1.4.0", "2.0.0"):
            with self.subTest(latest=latest):
                answer = self.check(row(version=latest, status="published",
                                        download_url=f"{GH}/v{latest}/Monitra-Setup-{latest}.exe"))
                self.assertTrue(answer.update_available)


# ─────────────────────────────────────────────────────────────────────────
# The announcement
# ─────────────────────────────────────────────────────────────────────────


class AnnouncementSafetyTests(unittest.TestCase):

    def users(self, count=2):
        users = []
        for n in range(count):
            user = MagicMock()
            user.id = 100 + n
            user.name = f"User {n}"
            user.email = f"user{n}@example.com"
            user.organization_id = 7
            users.append(user)
        return users

    def test_the_deployment_default_is_off(self):
        self.assertIs(Settings.model_fields["RELEASE_EMAIL_ENABLED"].default, False)

    def test_there_is_no_test_audience_by_default(self):
        self.assertEqual(Settings.model_fields["RELEASE_EMAIL_TEST_RECIPIENTS"].default, "")

    def test_when_disabled_nothing_is_queued_and_no_user_is_even_read(self):
        with email_settings(RELEASE_EMAIL_ENABLED=False), \
                patch(f"{WORKFLOWS}.UserRepository") as repo, \
                patch(f"{WORKFLOWS}.EmailOutboxService") as outbox:
            self.assertEqual(queue_release_announcements(MagicMock(), _release()), [])
        outbox.enqueue.assert_not_called()
        repo.list_release_recipients.assert_not_called()

    def test_disabled_wins_even_when_a_test_list_is_set(self):
        with email_settings(RELEASE_EMAIL_ENABLED=False,
                            RELEASE_EMAIL_TEST_RECIPIENTS="qa@example.com"), \
                patch(f"{WORKFLOWS}.EmailOutboxService") as outbox:
            self.assertEqual(queue_release_announcements(MagicMock(), _release()), [])
        outbox.enqueue.assert_not_called()

    def test_test_mode_reaches_only_the_listed_addresses_and_no_user(self):
        with email_settings(RELEASE_EMAIL_TEST_RECIPIENTS="qa@example.com, second@example.com"), \
                patch(f"{WORKFLOWS}.UserRepository") as repo, \
                patch(f"{WORKFLOWS}.EmailOutboxService") as outbox:
            outbox.enqueue.side_effect = [_notification(id=1), _notification(id=2)]
            queued = queue_release_announcements(MagicMock(), _release("2.1.0"))
        self.assertEqual(len(queued), 2)
        repo.list_release_recipients.assert_not_called()
        repo.list_announcement_recipients.assert_not_called()
        sent_to = [call.kwargs["recipients"] for call in outbox.enqueue.call_args_list]
        self.assertEqual(sent_to, [["qa@example.com"], ["second@example.com"]])

    def test_a_test_announcement_says_so_in_its_subject(self):
        with email_settings(RELEASE_EMAIL_TEST_RECIPIENTS="qa@example.com"), \
                patch(f"{WORKFLOWS}.EmailOutboxService") as outbox:
            outbox.enqueue.return_value = _notification(id=1)
            queue_release_announcements(MagicMock(), _release("2.1.0"))
        self.assertTrue(outbox.enqueue.call_args.kwargs["subject"].startswith("[TEST] "))

    def test_a_test_run_cannot_consume_the_real_announcement(self):
        self.assertNotEqual(release_test_dedupe_key("2.1.0", "qa@example.com"),
                            release_dedupe_key("2.1.0", 100))
        self.assertTrue(release_test_dedupe_key("2.1.0", "QA@Example.com")
                        == release_test_dedupe_key("2.1.0", "qa@example.com"))

    def test_a_test_list_with_nothing_usable_sends_to_nobody_rather_than_everybody(self):
        with email_settings(RELEASE_EMAIL_TEST_RECIPIENTS="not-an-address, ,"), \
                patch(f"{WORKFLOWS}.UserRepository") as repo, \
                patch(f"{WORKFLOWS}.EmailOutboxService") as outbox:
            self.assertEqual(queue_release_announcements(MagicMock(), _release()), [])
        outbox.enqueue.assert_not_called()
        repo.list_release_recipients.assert_not_called()

    def test_the_real_fan_out_uses_the_release_audience_and_a_per_user_key(self):
        with email_settings(), \
                patch(f"{WORKFLOWS}.UserRepository") as repo, \
                patch(f"{WORKFLOWS}.EmailOutboxService") as outbox:
            repo.list_release_recipients.return_value = self.users(2)
            outbox.enqueue.side_effect = [_notification(id=1), _notification(id=2)]
            queue_release_announcements(MagicMock(), _release("2.1.0"))
        keys = [c.kwargs["dedupe_key"] for c in outbox.enqueue.call_args_list]
        self.assertEqual(keys, ["release:2.1.0:user:100", "release:2.1.0:user:101"])

    def test_the_pipeline_account_and_external_clients_are_not_recipients(self):
        from app.repositories.user import UserRepository

        def person(role, n):
            return SimpleNamespace(id=n, role_name=role, email=f"{role}@example.com")

        everyone = [person("employee", 1), person("release_bot", 2),
                    person("client", 3), person("administrator", 4)]
        with patch.object(UserRepository, "list_announcement_recipients", return_value=everyone):
            chosen = UserRepository.list_release_recipients(MagicMock())
        self.assertEqual([u.role_name for u in chosen], ["employee", "administrator"])

    def test_the_weekly_report_audience_is_untouched(self):
        # list_announcement_recipients is shared with the weekly and monthly
        # reports; excluding the pipeline account there is a different change.
        from app.repositories.user import UserRepository
        self.assertTrue(callable(UserRepository.list_announcement_recipients))
        self.assertEqual(UserRepository.NON_DESKTOP_ROLES, ("release_bot", "client"))


# ─────────────────────────────────────────────────────────────────────────
# Who may publish
# ─────────────────────────────────────────────────────────────────────────


class PublishAuthorityTests(unittest.TestCase):
    """The pipeline's key registers builds; only a person decides they ship."""

    def setUp(self):
        self.token, self.row = _mint()
        self.bot = _bot_user()
        self.db = _FakeSession(self.row)
        app.dependency_overrides[get_db] = lambda: self.db
        self.client = TestClient(app)
        self.patcher = patch(f"app.services.service_credential.UserRepository.get_by_id",
                             return_value=self.bot)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        app.dependency_overrides.clear()

    @property
    def auth(self):
        return {"Authorization": f"Bearer {self.token}"}

    def test_the_release_key_cannot_publish_a_row(self):
        with patch("app.api.desktop_release.DesktopReleaseService.set_status") as svc:
            response = self.client.post("/desktop/releases/11/publish", headers=self.auth)
        self.assertEqual(response.status_code, 403, response.text)
        svc.assert_not_called()

    def test_the_release_key_cannot_publish_a_version(self):
        with patch("app.api.desktop_release.DesktopReleaseService.publish_version") as svc:
            response = self.client.post("/desktop/releases/versions/1.3.2/publish", headers=self.auth)
        self.assertEqual(response.status_code, 403, response.text)
        svc.assert_not_called()

    def test_the_release_key_cannot_roll_a_release_back(self):
        with patch("app.api.desktop_release.DesktopReleaseService.set_status") as svc:
            response = self.client.post("/desktop/releases/11/rollback", headers=self.auth)
        self.assertEqual(response.status_code, 403, response.text)
        svc.assert_not_called()

    def test_the_release_key_cannot_publish_through_a_status_patch(self):
        with patch("app.api.desktop_release.DesktopReleaseService.update_release") as svc:
            response = self.client.patch(
                "/desktop/releases/11", json={"status": "published"}, headers=self.auth)
        self.assertEqual(response.status_code, 403, response.text)
        svc.assert_not_called()

    def test_the_release_key_can_still_amend_notes(self):
        with patch("app.api.desktop_release.DesktopReleaseService.update_release",
                   return_value=_release_row()):
            response = self.client.patch(
                "/desktop/releases/11", json={"release_notes": "Fixed."}, headers=self.auth)
        self.assertEqual(response.status_code, 200, response.text)

    def test_the_release_key_can_read_readiness(self):
        report = policy.Readiness(version="1.3.2")
        with patch("app.api.desktop_release.DesktopReleaseService.readiness", return_value=report):
            response = self.client.get("/desktop/releases/versions/1.3.2/readiness", headers=self.auth)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["ready"])


class AdministratorPublishTests(unittest.TestCase):

    def setUp(self):
        self.db = _FakeSession()
        app.dependency_overrides[get_db] = lambda: self.db
        self.client = TestClient(app)
        from app.core.security import create_access_token
        self.admin = _person("administrator", user_id=238)
        self.headers = {"Authorization": f"Bearer {create_access_token({'user_id': 238})}"}
        self.patcher = patch("app.core.security.UserRepository.get_by_id", return_value=self.admin)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        app.dependency_overrides.clear()

    def test_an_administrator_can_publish_a_complete_version(self):
        published = [SimpleNamespace(**{**_release_row().__dict__, "status": "published",
                                        "signed": True, "signer": "Monitra"})]
        with patch("app.api.desktop_release.DesktopReleaseService.publish_version",
                   return_value=(published, False)):
            response = self.client.post("/desktop/releases/versions/1.3.2/publish", headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["version"], "1.3.2")
        self.assertFalse(response.json()["announcement_queued"])

    def test_an_incomplete_version_is_a_422_that_lists_every_problem(self):
        problems = [policy.Problem("missing", "The darwin/arm64 artifact has not been registered.", "darwin/arm64"),
                    policy.Problem("unsigned", "The artifact was registered as unsigned.", "win32")]
        with patch("app.api.desktop_release.DesktopReleaseService.publish_version",
                   side_effect=ReleasePolicyViolation(problems)):
            response = self.client.post("/desktop/releases/versions/1.3.2/publish", headers=self.headers)
        self.assertEqual(response.status_code, 422, response.text)
        body = response.json()["detail"]
        self.assertEqual([p["code"] for p in body["problems"]], ["missing", "unsigned"])

    def test_a_policy_violation_at_registration_is_a_422_and_a_duplicate_a_409(self):
        payload = {"version": "1.3.2", "platform": "win32", "architecture": None,
                   "download_url": f"{GH}/v1.3.2/Monitra-Setup-1.3.2.exe",
                   "sha256": "c" * 64, "file_size": 31_000_000}
        with patch("app.api.desktop_release.DesktopReleaseService.create_release",
                   side_effect=ReleasePolicyViolation([policy.Problem("url", "bad host", "win32")])):
            self.assertEqual(self.client.post("/desktop/releases", json=payload,
                                              headers=self.headers).status_code, 422)
        with patch("app.api.desktop_release.DesktopReleaseService.create_release",
                   side_effect=ReleaseConflict("already registered")):
            self.assertEqual(self.client.post("/desktop/releases", json=payload,
                                              headers=self.headers).status_code, 409)

    def test_a_non_version_in_the_path_is_refused_before_any_work(self):
        with patch("app.api.desktop_release.DesktopReleaseService.publish_version") as svc:
            response = self.client.post("/desktop/releases/versions/latest/publish", headers=self.headers)
        self.assertEqual(response.status_code, 422)
        svc.assert_not_called()

    def test_an_employee_cannot_publish(self):
        employee = _person("employee", user_id=51)
        from app.core.security import create_access_token
        headers = {"Authorization": f"Bearer {create_access_token({'user_id': 51})}"}
        with patch("app.core.security.UserRepository.get_by_id", return_value=employee), \
                patch("app.api.desktop_release.DesktopReleaseService.publish_version") as svc:
            response = self.client.post("/desktop/releases/versions/1.3.2/publish", headers=headers)
        self.assertEqual(response.status_code, 403)
        svc.assert_not_called()


if __name__ == "__main__":
    unittest.main()

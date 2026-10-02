"""Desktop update check and fleet version visibility.

Unit tests in the style of the rest of this suite: the session and the
repository are mocked, so the policy is exercised without a database. What
they pin down is the part a client must not be trusted with -- whether an
update actually exists, and what counts as "newer".
"""

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.models.desktop_release import Platform, ReleaseStatus
from app.schemas.desktop_release import DesktopReleaseCreate
from app.services.desktop_release import (
    DesktopReleaseService, ReleaseConflict, ReleasePolicyViolation,
    normalize_architecture, normalize_platform, parse_client_version, version_tuple,
)

SVC = "app.services.desktop_release"


def released(**overrides):
    """A published release row, as the repository would hand one back."""
    row = dict(
        version="1.1.0", version_major=1, version_minor=1, version_patch=0,
        platform=Platform.WINDOWS, architecture=None,
        download_url="https://github.com/KairavThakar90/monitra/releases/download/v1.1.0/Monitra-Setup-1.1.0.exe",
        file_size=90_000_000, sha256="a" * 64,
        release_notes="Faster startup.", release_notes_url=None,
        status=ReleaseStatus.PUBLISHED, force_update=False,
        min_supported_version=None, published_at=None,
        signed=True, signer="Monitra",
    )
    row.update(overrides)
    return SimpleNamespace(**row)


class ParseClientVersionTests(unittest.TestCase):

    def test_reads_the_version_the_desktop_sends(self):
        self.assertEqual(parse_client_version("Monitra/1.2.3"), "1.2.3")

    def test_ignores_clients_that_are_not_monitra(self):
        # The React frontend and any browser hit the same API. Recording their
        # User-Agent as a desktop version would make the fleet view fiction.
        self.assertIsNone(parse_client_version("Mozilla/5.0 (Windows NT 10.0)"))
        self.assertIsNone(parse_client_version("MonitraFake/1.0.0"))
        self.assertIsNone(parse_client_version(None))
        self.assertIsNone(parse_client_version(""))

    def test_rejects_a_non_numeric_version(self):
        self.assertIsNone(parse_client_version("Monitra/1.0.0-rc1"))


class VersionComparisonTests(unittest.TestCase):

    def test_parses_only_major_minor_patch(self):
        self.assertEqual(version_tuple("1.10.2"), (1, 10, 2))
        for bad in ("1.0", "1.0.0.0", "v1.0.0", "1.0.0-rc1", "", None):
            self.assertIsNone(version_tuple(bad), bad)

    def test_update_available_only_when_strictly_newer(self):
        available = DesktopReleaseService.is_update_available
        self.assertTrue(available("1.0.0", "1.0.1"))
        self.assertTrue(available("1.9.0", "1.10.0"))  # not string comparison
        self.assertFalse(available("1.0.1", "1.0.1"))
        self.assertFalse(available("1.2.0", "1.0.1"))  # never prompt downgrades

    def test_unknown_versions_never_prompt(self):
        # An unidentified client is not evidence that it is out of date, and a
        # deployment that does not know its latest release must not guess.
        available = DesktopReleaseService.is_update_available
        self.assertFalse(available(None, "1.0.1"))
        self.assertFalse(available("1.0.0", None))
        self.assertFalse(available("garbage", "1.0.1"))


class ConfiguredLatestVersionTests(unittest.TestCase):

    def test_unset_means_unknown_not_zero(self):
        with patch(f"{SVC}.settings") as settings:
            settings.DESKTOP_LATEST_VERSION = ""
            self.assertIsNone(DesktopReleaseService.configured_latest_version())

    def test_a_misconfigured_value_is_refused_rather_than_served(self):
        with patch(f"{SVC}.settings") as settings:
            settings.DESKTOP_LATEST_VERSION = "latest"
            self.assertIsNone(DesktopReleaseService.configured_latest_version())


class LatestVersionResponseTests(unittest.TestCase):
    """The configuration fallback: a deployment with no releases registered.

    These pin the behaviour that existed before ``desktop_releases`` did, and
    they still hold — a deployment that has published no rows answers exactly
    as it always has. The release table is stubbed empty here on purpose; the
    table-driven path is covered by ``ReleaseTableTests`` below.
    """

    def setUp(self):
        releases = patch(f"{SVC}.DesktopReleaseRepository").start()
        releases.latest_published.return_value = None
        releases.latest_published_any_platform.return_value = None
        self.addCleanup(patch.stopall)

    def _user(self):
        user = MagicMock()
        user.id = 7
        user.organization_id = 1
        return user

    def test_no_configured_release_returns_an_honest_empty_answer(self):
        with patch(f"{SVC}.settings") as settings, \
                patch(f"{SVC}.DesktopClientVersionRepository") as repo:
            settings.DESKTOP_LATEST_VERSION = ""
            settings.DESKTOP_DOWNLOAD_URL = "https://example.invalid/download"
            result = DesktopReleaseService.latest_version(
                MagicMock(), self._user(), "1.0.0", "win32"
            )
        self.assertIsNone(result.latest_version)
        # No version means no download link either: a link on its own would
        # invite a prompt for a release that does not exist.
        self.assertIsNone(result.download_url)
        self.assertFalse(result.update_available)
        # The client's version is still recorded -- fleet visibility does not
        # depend on there being a newer release.
        repo.upsert.assert_called_once()

    def test_a_newer_release_is_offered_with_its_download_url(self):
        with patch(f"{SVC}.settings") as settings, \
                patch(f"{SVC}.DesktopClientVersionRepository"):
            settings.DESKTOP_LATEST_VERSION = "1.1.0"
            settings.DESKTOP_DOWNLOAD_URL = "https://github.com/KairavThakar90/monitra/releases"
            settings.DESKTOP_RELEASE_NOTES_URL = "https://example.invalid/notes"
            result = DesktopReleaseService.latest_version(
                MagicMock(), self._user(), "1.0.1", "darwin"
            )
        self.assertEqual(result.latest_version, "1.1.0")
        self.assertEqual(result.download_url, "https://github.com/KairavThakar90/monitra/releases")
        self.assertTrue(result.update_available)
        self.assertEqual(result.client_version, "1.0.1")

    def test_recording_a_version_never_fails_the_request(self):
        # Version visibility is diagnostics. Failing the update check because
        # a diagnostics write failed would be the worse outcome.
        db = MagicMock()
        with patch(f"{SVC}.settings") as settings, \
                patch(f"{SVC}.DesktopClientVersionRepository") as repo:
            settings.DESKTOP_LATEST_VERSION = "1.1.0"
            settings.DESKTOP_DOWNLOAD_URL = ""
            settings.DESKTOP_RELEASE_NOTES_URL = ""
            repo.upsert.side_effect = RuntimeError("database is down")
            result = DesktopReleaseService.latest_version(
                db, self._user(), "1.0.0", "win32"
            )
        self.assertTrue(result.update_available)
        db.rollback.assert_called_once()

    def test_an_unidentified_client_is_not_recorded(self):
        with patch(f"{SVC}.settings") as settings, \
                patch(f"{SVC}.DesktopClientVersionRepository") as repo:
            settings.DESKTOP_LATEST_VERSION = "1.1.0"
            settings.DESKTOP_DOWNLOAD_URL = ""
            settings.DESKTOP_RELEASE_NOTES_URL = ""
            DesktopReleaseService.latest_version(MagicMock(), self._user(), None, None)
        repo.upsert.assert_not_called()


class FleetVersionsTests(unittest.TestCase):

    def setUp(self):
        # No releases registered, so "latest" still comes from configuration.
        releases = patch(f"{SVC}.DesktopReleaseRepository").start()
        releases.latest_published_any_platform.return_value = None
        self.addCleanup(patch.stopall)

    def test_counts_users_per_version(self):
        user = MagicMock()
        user.organization_id = 1
        now = datetime.now(timezone.utc)
        rows = [
            SimpleNamespace(
                user_id=index, app_version=version, platform="win32",
                first_seen_at=now, last_seen_at=now,
            )
            for index, version in enumerate(("1.0.0", "1.0.0", "1.1.0"))
        ]

        with patch(f"{SVC}.settings") as settings, \
                patch(f"{SVC}.DesktopClientVersionRepository") as repo:
            settings.DESKTOP_LATEST_VERSION = "1.1.0"
            repo.list_for_organization.return_value = rows
            result = DesktopReleaseService.fleet_versions(MagicMock(), user)

        self.assertEqual(result.counts, {"1.0.0": 2, "1.1.0": 1})
        self.assertEqual(result.latest_version, "1.1.0")
        self.assertEqual(len(result.clients), 3)


class PlatformNormalisationTests(unittest.TestCase):
    """The several spellings each platform and CPU answers to.

    Left unfolded, an Apple Silicon Mac asking for `arm64` would miss a build
    registered as `aarch64` and fall back to a build for a different chip —
    a download that cannot run, which looks to the user like a broken update.
    """

    def test_platform_spellings_fold_together(self):
        for value in ("win32", "Windows", "WIN32", "cygwin"):
            self.assertEqual(normalize_platform(value), Platform.WINDOWS, value)
        for value in ("darwin", "macOS", "osx"):
            self.assertEqual(normalize_platform(value), Platform.MACOS, value)
        for value in ("linux", "linux2"):
            self.assertEqual(normalize_platform(value), Platform.LINUX, value)

    def test_an_unknown_platform_is_left_alone_not_guessed(self):
        # It will simply match no release, which is the right outcome: better
        # to find nothing than to fall through to somebody else's artifact.
        self.assertEqual(normalize_platform("freebsd"), "freebsd")
        self.assertIsNone(normalize_platform(None))
        self.assertIsNone(normalize_platform("  "))

    def test_architecture_spellings_fold_together(self):
        for value in ("AMD64", "x86_64", "x64"):
            self.assertEqual(normalize_architecture(value), "x86_64", value)
        for value in ("arm64", "aarch64", "ARM64"):
            self.assertEqual(normalize_architecture(value), "arm64", value)
        self.assertIsNone(normalize_architecture(None))


class MinimumVersionTests(unittest.TestCase):

    def test_below_the_floor_is_detected(self):
        below = DesktopReleaseService.is_below_minimum
        self.assertTrue(below("1.0.0", "1.1.0"))
        self.assertTrue(below("1.9.0", "1.10.0"))  # not a string comparison
        self.assertFalse(below("1.1.0", "1.1.0"))  # at the floor is allowed
        self.assertFalse(below("2.0.0", "1.1.0"))

    def test_an_unreadable_answer_never_locks_anyone_out(self):
        # This is the field that stops someone using their own application, so
        # it fails open. A malformed or absent floor must never be grounds for
        # a lockout, or one bad value takes the whole fleet offline.
        below = DesktopReleaseService.is_below_minimum
        self.assertFalse(below("1.0.0", None))
        self.assertFalse(below(None, "1.1.0"))
        self.assertFalse(below("1.0.0", "garbage"))
        self.assertFalse(below("garbage", "1.1.0"))


class ReleaseTableTests(unittest.TestCase):
    """The table-driven answer: what a client is actually offered.

    These describe the release table's own policy, independent of the
    "oldest client that may install itself" floor (which has its own tests in
    test_desktop_release_safety.py), so the floor is switched off here. The
    clients in these tests are 1.0.x and would otherwise be below it.
    """

    def setUp(self):
        from app.core.config import settings as real_settings

        floor = patch.object(real_settings, "DESKTOP_AUTO_UPDATE_MIN_CLIENT_VERSION", "")
        floor.start()
        self.addCleanup(floor.stop)

    def _user(self):
        user = MagicMock()
        user.id = 7
        user.organization_id = 1
        return user

    def _check(self, release, client_version, platform="win32", arch="AMD64"):
        with patch(f"{SVC}.DesktopReleaseRepository") as releases, \
                patch(f"{SVC}.DesktopClientVersionRepository"):
            releases.latest_published.return_value = release
            return DesktopReleaseService.latest_version(
                MagicMock(), self._user(), client_version, platform, arch
            )

    def test_a_published_release_carries_everything_needed_to_install_it(self):
        result = self._check(released(), "1.0.0")
        self.assertTrue(result.update_available)
        self.assertEqual(result.latest_version, "1.1.0")
        # The checksum is the whole reason the desktop may run what it fetched.
        self.assertEqual(result.sha256, "a" * 64)
        self.assertEqual(result.file_size, 90_000_000)
        self.assertEqual(result.release_notes, "Faster startup.")
        self.assertEqual(result.platform, Platform.WINDOWS)

    def test_a_client_already_current_is_offered_nothing(self):
        result = self._check(released(), "1.1.0")
        self.assertFalse(result.update_available)
        self.assertFalse(result.force_update)

    def test_a_newer_client_is_never_told_to_downgrade(self):
        result = self._check(released(), "2.0.0")
        self.assertFalse(result.update_available)

    def test_the_release_table_wins_over_configuration(self):
        # A deployment that still has DESKTOP_LATEST_VERSION set must not have
        # it override a real registered release, or the two would disagree and
        # the stale one would win at random.
        with patch(f"{SVC}.settings") as settings, \
                patch(f"{SVC}.DesktopReleaseRepository") as releases, \
                patch(f"{SVC}.DesktopClientVersionRepository"):
            settings.DESKTOP_LATEST_VERSION = "0.9.0"
            settings.DESKTOP_DOWNLOAD_URL = "https://stale.invalid/old.exe"
            releases.latest_published.return_value = released()
            result = DesktopReleaseService.latest_version(
                MagicMock(), self._user(), "1.0.0", "win32", "AMD64"
            )
        self.assertEqual(result.latest_version, "1.1.0")
        self.assertNotIn("stale.invalid", result.download_url)

    def test_a_client_that_names_no_platform_gets_no_installable_answer(self):
        # Without a platform there is no way to know which artifact fits, so
        # the answer falls back to configuration -- which carries no checksum
        # and therefore cannot drive an install.
        with patch(f"{SVC}.settings") as settings, \
                patch(f"{SVC}.DesktopReleaseRepository") as releases, \
                patch(f"{SVC}.DesktopClientVersionRepository"):
            settings.DESKTOP_LATEST_VERSION = "1.1.0"
            settings.DESKTOP_DOWNLOAD_URL = "https://example.invalid/releases"
            settings.DESKTOP_RELEASE_NOTES_URL = ""
            result = DesktopReleaseService.latest_version(
                MagicMock(), self._user(), "1.0.0", None, None
            )
        releases.latest_published.assert_not_called()
        self.assertIsNone(result.sha256)
        self.assertFalse(result.force_update)

    def test_a_mandatory_release_forces_a_client_behind_it(self):
        result = self._check(released(force_update=True), "1.0.0")
        self.assertTrue(result.update_available)
        self.assertTrue(result.force_update)

    def test_a_mandatory_release_never_forces_a_client_already_on_it(self):
        # There would be nothing to update to, so forcing would lock the user
        # out of a perfectly current installation.
        result = self._check(released(force_update=True), "1.1.0")
        self.assertFalse(result.force_update)

    def test_a_minimum_supported_version_forces_only_clients_below_it(self):
        release = released(version="2.0.0", min_supported_version="1.5.0")
        self.assertTrue(self._check(release, "1.0.0").force_update)
        # Behind the latest, but at or above the floor: an ordinary optional
        # update, which is the distinction the whole field exists to make.
        forced = self._check(release, "1.6.0")
        self.assertTrue(forced.update_available)
        self.assertFalse(forced.force_update)


class ReleaseCreationTests(unittest.TestCase):

    def _payload(self, **overrides):
        data = dict(
            version="1.2.0", platform="Windows", architecture="AMD64",
            download_url="https://github.com/KairavThakar90/monitra/releases/download/v1.2.0/Monitra-Setup-1.2.0.exe",
            sha256="B" * 64, file_size=1234,
        )
        data.update(overrides)
        return DesktopReleaseCreate(**data)

    def test_a_new_release_is_registered_as_a_draft(self):
        db = MagicMock()
        with patch(f"{SVC}.DesktopReleaseRepository") as repo:
            repo.get_artifact.return_value = None
            release = DesktopReleaseService.create_release(db, self._payload())
        # CI can prove a build starts; it cannot decide the build is good.
        self.assertEqual(release.status, ReleaseStatus.DRAFT)
        self.assertIsNone(release.published_at)

    def test_the_version_is_split_into_sortable_numbers(self):
        db = MagicMock()
        with patch(f"{SVC}.DesktopReleaseRepository") as repo:
            repo.get_artifact.return_value = None
            release = DesktopReleaseService.create_release(
                db, self._payload(
                    version="1.10.3",
                    download_url="https://github.com/KairavThakar90/monitra/releases/download/v1.10.3/Monitra-Setup-1.10.3.exe",
                )
            )
        # These columns are what make "newest" an ORDER BY rather than a string
        # comparison, under which 1.9.0 would sort above 1.10.3.
        self.assertEqual(
            (release.version_major, release.version_minor, release.version_patch),
            (1, 10, 3),
        )

    def test_platform_and_architecture_are_stored_folded(self):
        db = MagicMock()
        with patch(f"{SVC}.DesktopReleaseRepository") as repo:
            repo.get_artifact.return_value = None
            release = DesktopReleaseService.create_release(db, self._payload())
        self.assertEqual(release.platform, Platform.WINDOWS)
        self.assertEqual(release.architecture, "x86_64")

    def test_the_checksum_is_stored_lower_cased(self):
        db = MagicMock()
        with patch(f"{SVC}.DesktopReleaseRepository") as repo:
            repo.get_artifact.return_value = None
            release = DesktopReleaseService.create_release(db, self._payload())
        # Get-FileHash reports upper case and shasum lower case for the same
        # bytes; storing whichever arrived would fail an intact download.
        self.assertEqual(release.sha256, "b" * 64)

    def test_registering_the_same_artifact_twice_is_refused(self):
        db = MagicMock()
        with patch(f"{SVC}.DesktopReleaseRepository") as repo:
            repo.get_artifact.return_value = object()
            with self.assertRaises(ReleaseConflict):
                DesktopReleaseService.create_release(db, self._payload())

    def test_publishing_stamps_the_date_once(self):
        release = released(status=ReleaseStatus.DRAFT, published_at=None)
        DesktopReleaseService._apply_status(release, ReleaseStatus.PUBLISHED)
        first = release.published_at
        self.assertIsNotNone(first)
        # Withdrawn, then published again: the original date stands, because
        # that is when the build was first offered to users.
        DesktopReleaseService._apply_status(release, ReleaseStatus.ROLLED_BACK)
        DesktopReleaseService._apply_status(release, ReleaseStatus.PUBLISHED)
        self.assertEqual(release.published_at, first)

    def test_an_unknown_status_is_refused(self):
        with self.assertRaises(ValueError):
            DesktopReleaseService._apply_status(released(), "live")


class PublicDownloadTests(unittest.TestCase):

    def test_nothing_published_is_an_honest_unavailable(self):
        with patch(f"{SVC}.DesktopReleaseRepository") as repo:
            repo.latest_published.return_value = None
            answer = DesktopReleaseService.public_latest(
                MagicMock(), platform="darwin", architecture="arm64"
            )
        self.assertFalse(answer.available)
        self.assertIsNone(answer.download_url)

    def test_a_published_release_is_offered_with_its_checksum(self):
        with patch(f"{SVC}.DesktopReleaseRepository") as repo:
            repo.latest_published.return_value = released()
            answer = DesktopReleaseService.public_latest(
                MagicMock(), platform="Windows"
            )
        self.assertTrue(answer.available)
        self.assertEqual(answer.version, "1.1.0")
        self.assertEqual(answer.sha256, "a" * 64)

    def test_an_unknown_platform_is_refused_rather_than_guessed(self):
        answer = DesktopReleaseService.public_latest(MagicMock(), platform=None)
        self.assertFalse(answer.available)

    def test_the_index_lists_macos_once_per_architecture(self):
        # This project ships arm64 and x86_64 separately and says so. A single
        # "macOS" download would have to pick one, and half the Macs would get
        # a build that cannot run.
        with patch(f"{SVC}.DesktopReleaseRepository") as repo:
            repo.latest_published.return_value = None
            repo.latest_published_any_platform.return_value = None
            index = DesktopReleaseService.public_index(MagicMock())
        self.assertEqual(
            set(index.downloads), {"windows", "macos-arm64", "macos-x86_64"}
        )


if __name__ == "__main__":
    unittest.main()


# ── The release credential's authority ──────────────────────────────────────


def test_release_bot_holds_exactly_one_permission():
    """The CI credential can do its job and nothing else.

    This is the whole point of the role. Registering and publishing a release
    decides what every installed client downloads and then executes, so the
    credential that does it lives in a CI secret -- and a secret that leaks
    should cost a bad release, not the organization's staff data. If someone
    widens this set, that trade is silently gone.
    """
    from app.core.permissions import ROLE_PERMISSIONS

    assert ROLE_PERMISSIONS["release_bot"] == {"manage_desktop_releases"}


def test_release_bot_is_not_reachable_from_any_provider_role():
    """No provider account can be mapped onto the pipeline's identity.

    The alias table only renames roles; a provider that started returning
    "release_bot" must not thereby mint a release manager.
    """
    from app.core.permissions import PROVIDER_ROLE_ALIASES

    assert "release_bot" not in PROVIDER_ROLE_ALIASES.values()


def test_release_bot_cannot_read_or_write_anything_else():
    """Spot-check against the authority the admin roles carry."""
    from app.core.permissions import ROLE_PERMISSIONS

    bot = ROLE_PERMISSIONS["release_bot"]
    for forbidden in (
        "manage_employees", "view_employees", "screenshots:delete",
        "time_entries:view_all", "projects:create", "manual_time_entries:approve",
    ):
        assert forbidden not in bot

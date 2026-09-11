"""
How an observed window becomes a stored application identity.

These tests exist because the Reports page once showed a single grey
"Others" arc holding 94.5% of a month's tracked time. Two faults produced
it, and both are pinned down here:

* applications were stored under whatever the operating system reported
  (``chrome``, ``ms-teams``, ``python``), so one product arrived under
  several names, each holding a slice of the time and each small enough to
  be ranked out of the top of the list; and
* the desktop fabricated identities -- ``Idle/System``, ``Unknown
  Application`` -- for samples that identified nothing, and stored them as
  though they were programs somebody had used.

The regression that matters most is at the bottom: a legitimate,
identifiable application must never end up under a catch-all name.

The last class checks the desktop and backend catalogues against each other.
It reads both files, as ``test_validation_framework.py`` does for the two
validation rule sets, so changing one and forgetting the other fails here
rather than in production -- where it would appear as one application
occupying two rows of every report.
"""
from __future__ import annotations

import importlib.util
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from background_services.activity.app_usage_service import AppUsageService
from tracking.app_identity import (
    ALIAS_INDEX,
    APPLICATION_CATALOGUE,
    MAX_APPLICATION_NAME_LENGTH,
    ClassificationStatus,
    UnclassifiedReason,
    canonical_application_name,
    canonical_domain,
    resolve_application,
)
from tracking.browsers import UrlSource, get_browser_manager
from tracking.browsers.manager import BrowserManager, normalize_domain_and_url


class StubRuntime:
    def __init__(self, session=None):
        self.timer = MagicMock()
        self.timer.active_session.return_value = (
            {"entry_id": 101} if session is None else session
        )


def window(process_name, title, exe_path=None):
    """One `get_active_window_details` reading."""
    return (process_name, title, exe_path, 4242, 1)


# ── 1-2. Known applications on both platforms ────────────────────────────────

class KnownApplicationTests(unittest.TestCase):
    def test_a_known_windows_executable_resolves_to_the_product_name(self):
        for process, expected in (
            ("chrome", "Google Chrome"),
            ("chrome.exe", "Google Chrome"),
            ("msedge", "Microsoft Edge"),
            ("firefox", "Mozilla Firefox"),
            ("Code", "Visual Studio Code"),
            ("sublime_text", "Sublime Text"),
            ("ms-teams", "Microsoft Teams"),
            ("explorer", "File Explorer"),
            ("notepad++", "Notepad++"),
        ):
            with self.subTest(process=process):
                identity = resolve_application(process_name=process)
                self.assertEqual(identity.name, expected)
                self.assertEqual(identity.status, ClassificationStatus.CLASSIFIED)

    def test_a_known_macos_display_name_resolves_to_the_same_product_name(self):
        """The two platforms must not produce two rows for one application."""
        for mac_name, windows_name in (
            ("Google Chrome", "chrome"),
            ("Microsoft Edge", "msedge"),
            ("Mozilla Firefox", "firefox"),
            ("Visual Studio Code", "Code"),
            ("Microsoft Teams", "ms-teams"),
            ("Finder", "finder"),
        ):
            with self.subTest(mac=mac_name):
                self.assertEqual(
                    resolve_application(process_name=mac_name).name,
                    resolve_application(process_name=windows_name).name,
                )

    def test_the_executable_path_is_preferred_over_the_reported_name(self):
        """The binary on disk is the stable identity; a display name is not."""
        identity = resolve_application(
            process_name="Some Renamed Window",
            executable_path=r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        )
        self.assertEqual(identity.name, "Google Chrome")

    def test_a_macos_bundle_path_resolves(self):
        identity = resolve_application(
            process_name=None, executable_path="/Applications/Google Chrome.app"
        )
        self.assertEqual(identity.name, "Google Chrome")

    def test_resolution_is_idempotent(self):
        """The backend resolves again on ingest, so a second pass over an
        already-canonical name must be a no-op or the two layers would fight."""
        for canonical in APPLICATION_CATALOGUE:
            with self.subTest(name=canonical):
                self.assertEqual(canonical_application_name(canonical), canonical)

    def test_monitra_is_not_reported_as_the_python_interpreter(self):
        """A developer's day used to show hours of an application called
        "python". It is this product, under this product's name."""
        self.assertEqual(resolve_application(process_name="python").name, "Monitra")
        self.assertEqual(resolve_application(process_name="Monitra.exe").name, "Monitra")


# ── 5-11. Everything the catalogue does not know ─────────────────────────────

class UnknownApplicationTests(unittest.TestCase):
    def test_an_unknown_executable_keeps_its_own_name_verbatim(self):
        identity = resolve_application(process_name="Antigravity IDE")
        self.assertEqual(identity.name, "Antigravity IDE")
        self.assertEqual(identity.status, ClassificationStatus.PARTIALLY_CLASSIFIED)
        self.assertEqual(identity.reason, UnclassifiedReason.NOT_IN_CATALOGUE)
        self.assertEqual(identity.process_name, "Antigravity IDE")

    def test_an_unknown_executable_keeps_the_vendors_own_casing(self):
        """The row has to stay recognisable to whoever reads it."""
        self.assertEqual(
            resolve_application(process_name="ShellExperienceHost2").name,
            "ShellExperienceHost2",
        )

    def test_a_missing_process_name_is_not_an_application(self):
        for missing in (None, "", "   ", "\\"):
            with self.subTest(value=repr(missing)):
                identity = resolve_application(process_name=missing)
                self.assertIsNone(identity.name)
                self.assertFalse(identity.is_recordable)
                self.assertEqual(identity.status, ClassificationStatus.UNKNOWN)
                self.assertEqual(
                    identity.reason, UnclassifiedReason.NO_PROCESS_IDENTIFIER
                )

    def test_nothing_ever_resolves_to_a_catch_all_name(self):
        """The heart of the bug. No input -- known, unknown, empty or
        malformed -- may produce a generic bucket name."""
        forbidden = {"other", "others", "unknown", "unknown application",
                     "idle/system", "no active window", "uncategorized",
                     "unclassified", "n/a"}
        probes = [
            None, "", "  ", "chrome", "Antigravity IDE", "ShellExperienceHost",
            "python", "\\", "/", '"', "a" * 400,
        ]
        for probe in probes:
            with self.subTest(probe=repr(probe)):
                name = resolve_application(process_name=probe).name
                if name is not None:
                    self.assertNotIn(name.strip().lower(), forbidden)

    def test_a_very_long_identifier_is_bounded_to_what_the_column_holds(self):
        """An over-long name would be rejected by the backend schema and the
        batch would retry forever, losing every record behind it."""
        identity = resolve_application(process_name="x" * 1000)
        self.assertEqual(len(identity.name), MAX_APPLICATION_NAME_LENGTH)


# ── 12-13, 18-19. The sampling loop ──────────────────────────────────────────

class SamplingLoopTests(unittest.TestCase):
    def setUp(self):
        self.cache = MagicMock()
        self.service = AppUsageService(StubRuntime(), self.cache)

    @patch("background_services.activity.app_usage_service.get_active_window_details")
    def test_a_sample_that_identifies_nothing_records_nothing(self, active_window):
        """No foreground window at all -- a locked screen, or a refused
        process query. The previous behaviour stored an application called
        "Idle/System" and synced it as real usage."""
        active_window.return_value = window("Code", "main.py")
        self.service.start_tracker({"entry_id": 101})
        self.service.tick()

        self.service._segment_start = time.monotonic() - 20.0
        active_window.return_value = (None, None, None, None, None)
        self.service.tick()

        # The editor segment that really was measured is kept...
        self.cache.save_app_usage.assert_called_once()
        self.assertEqual(
            self.cache.save_app_usage.call_args.kwargs["application_name"],
            "Visual Studio Code",
        )
        # ...and nothing is opened for the unidentifiable stretch.
        self.assertIsNone(self.service._segment_start)

        self.cache.save_app_usage.reset_mock()
        self.service.tick()
        self.cache.save_app_usage.assert_not_called()

    @patch("background_services.activity.app_usage_service.get_active_window_details")
    def test_rapid_switching_produces_one_canonical_segment_per_application(
        self, active_window
    ):
        self.service.start_tracker({"entry_id": 101})
        for process in ("chrome", "Code", "ms-teams", "explorer", "chrome"):
            active_window.return_value = window(process, f"{process} window")
            self.service.tick()
            self.service._segment_start -= 3.0

        recorded = [
            call.kwargs["application_name"]
            for call in self.cache.save_app_usage.call_args_list
        ]
        self.assertEqual(
            recorded,
            ["Google Chrome", "Visual Studio Code", "Microsoft Teams", "File Explorer"],
        )

    @patch("background_services.activity.app_usage_service.get_active_window_details")
    def test_a_long_run_in_one_application_stays_one_application(self, active_window):
        """A capped flush must not change the identity mid-way."""
        active_window.return_value = window("Code", "main.py")
        self.service.start_tracker({"entry_id": 101})
        self.service.tick()
        for _ in range(3):
            self.service._segment_start = (
                time.monotonic() - AppUsageService.MAX_SEGMENT_SECONDS - 1
            )
            self.service.tick()

        names = {
            call.kwargs["application_name"]
            for call in self.cache.save_app_usage.call_args_list
        }
        self.assertEqual(names, {"Visual Studio Code"})


# ── 14-16. Offline capture, and the entry id that arrives later ──────────────

class OfflineCaptureTests(unittest.TestCase):
    """A timer started offline has no backend entry id for as long as the
    machine stays offline. Segments measured in that window used to be
    dropped on every application switch, so an offline morning contributed
    no application usage at all -- and that missing time is exactly what the
    broken chart then drew as "Others"."""

    def setUp(self):
        self.cache = MagicMock()
        self.session = {"entry_id": None, "client_op": "timer:9:2026-09-11T10:00:00"}
        self.service = AppUsageService(StubRuntime(self.session), self.cache)

    def test_segments_measured_before_the_entry_id_exists_are_still_stored(self):
        with patch(
            "background_services.activity.app_usage_service.get_active_window_details"
        ) as active_window:
            active_window.return_value = window("Code", "main.py")
            self.service.start_tracker(self.session)
            self.service.tick()
            self.service._segment_start -= 10.0
            active_window.return_value = window("chrome", "Dashboard")
            self.service.tick()

        self.cache.save_app_usage.assert_called_once()
        kwargs = self.cache.save_app_usage.call_args.kwargs
        self.assertIsNone(kwargs["time_entry_id"])
        self.assertEqual(kwargs["client_op"], self.session["client_op"])
        # The identity survives the offline path intact.
        self.assertEqual(kwargs["application_name"], "Visual Studio Code")
        self.assertGreaterEqual(kwargs["duration_seconds"], 10)

    def test_the_late_entry_id_adopts_what_was_buffered(self):
        self.service.start_tracker(self.session)
        self.cache.bind_app_usage_to_entry.return_value = 3

        self.service.bind_entry_id(555)

        self.cache.bind_app_usage_to_entry.assert_called_once_with(
            self.session["client_op"], 555
        )
        self.assertEqual(self.service._entry_id, 555)

    def test_a_session_with_no_key_at_all_is_not_written(self):
        """Without a client_op nothing could ever adopt the row, so writing it
        would be queueing a record that can never be uploaded."""
        session = {"entry_id": None}
        service = AppUsageService(StubRuntime(session), self.cache)
        with patch(
            "background_services.activity.app_usage_service.get_active_window_details"
        ) as active_window:
            active_window.return_value = window("Code", "main.py")
            service.start_tracker(session)
            service.tick()
            service._segment_start -= 10.0
            active_window.return_value = window("chrome", "Dashboard")
            service.tick()
        self.cache.save_app_usage.assert_not_called()


# ── 3-4. Browsers, with and without a readable URL ───────────────────────────

class BrowserIdentityTests(unittest.TestCase):
    def setUp(self):
        self.manager = BrowserManager()

    def _observe(self, process, title, url):
        with patch.object(
            type(self.manager.get_adapter(process)), "_read_address_bar",
            return_value=url,
        ):
            return self.manager.extract_browser_info(process, title, 1)

    def test_a_browser_with_a_url_records_the_browser_and_the_domain(self):
        observation = self._observe("chrome", "Dashboard", "https://github.com/monitra")
        self.assertEqual(observation.browser_name, "Google Chrome")
        self.assertEqual(observation.domain, "github.com")
        self.assertEqual(observation.url_source, UrlSource.ADDRESS_BAR)
        self.assertTrue(observation.has_url)

    def test_a_browser_without_a_readable_url_is_still_that_browser(self):
        """Acceptance criterion 4: a missing URL must not cost the browser its
        identity. The time is still captured as application usage against it."""
        observation = self._observe("firefox", "Some Page", None)
        self.assertIsNotNone(observation)
        self.assertEqual(observation.browser_name, "Mozilla Firefox")
        self.assertIsNone(observation.domain)
        self.assertIsNone(observation.url)
        self.assertEqual(observation.url_source, UrlSource.UNAVAILABLE)
        self.assertFalse(observation.has_url)

    def test_chromium_relatives_are_not_all_called_google_chrome(self):
        """One adapter reads all the Chromium omniboxes, but the browsers are
        different products. Reporting Brave as Chrome both named a browser the
        user does not have and disagreed with the application-usage row for
        the same session."""
        for process, expected in (
            ("chrome", "Google Chrome"),
            ("brave", "Brave"),
            ("vivaldi", "Vivaldi"),
            ("opera", "Opera"),
            ("msedge", "Microsoft Edge"),
        ):
            with self.subTest(process=process):
                observation = self._observe(process, "Page", "https://example.com/a")
                self.assertEqual(observation.browser_name, expected)
                # The very same catalogue names the application-usage row.
                self.assertEqual(
                    resolve_application(process_name=process).name, expected
                )

    def test_a_non_browser_application_is_not_a_browser_observation(self):
        self.assertIsNone(self.manager.extract_browser_info("Code", "main.py", 1))

    def test_the_shared_manager_is_the_one_used_in_production(self):
        self.assertIsInstance(get_browser_manager(), BrowserManager)


class DomainTests(unittest.TestCase):
    def test_www_is_not_a_different_site(self):
        self.assertEqual(canonical_domain("www.github.com"), "github.com")
        self.assertEqual(canonical_domain("github.com"), "github.com")
        self.assertEqual(canonical_domain("WWW.GitHub.Com."), "github.com")

    def test_a_real_subdomain_is_kept(self):
        """docs.google.com and mail.google.com are genuinely different places."""
        self.assertEqual(canonical_domain("docs.google.com"), "docs.google.com")
        self.assertEqual(canonical_domain("mail.google.com"), "mail.google.com")

    def test_nothing_identifiable_stays_nothing(self):
        for empty in (None, "", "   "):
            self.assertIsNone(canonical_domain(empty))

    def test_the_extracted_domain_is_canonical(self):
        domain, url = normalize_domain_and_url("https://www.bing.com/search?q=x")
        self.assertEqual(domain, "bing.com")
        # The address itself is the real address, untouched.
        self.assertEqual(url, "https://www.bing.com/search?q=x")

    def test_an_unreadable_page_yields_no_domain_rather_than_a_placeholder(self):
        self.assertEqual(normalize_domain_and_url(None, None), (None, None))


# ── The two catalogues must not drift ────────────────────────────────────────

class CatalogueParityTests(unittest.TestCase):
    """The desktop resolves an identity before queueing, and the backend
    resolves it again on ingest. If the two tables disagree, one application
    occupies two rows of every report -- silently, and for as long as it
    takes someone to notice."""

    @staticmethod
    def _backend_module():
        path = (
            Path(__file__).resolve().parents[2]
            / "backend" / "app" / "core" / "activity_identity.py"
        )
        if not path.exists():  # pragma: no cover - only in a desktop-only checkout
            raise unittest.SkipTest(f"backend catalogue not present at {path}")
        spec = importlib.util.spec_from_file_location("_backend_activity_identity", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_both_catalogues_hold_the_same_mapping(self):
        backend = self._backend_module()
        self.assertEqual(
            APPLICATION_CATALOGUE, backend.APPLICATION_CATALOGUE,
            "desktop and backend application catalogues have diverged",
        )
        self.assertEqual(ALIAS_INDEX, backend.ALIAS_INDEX)

    def test_both_catalogues_agree_on_the_name_limit(self):
        self.assertEqual(
            MAX_APPLICATION_NAME_LENGTH,
            self._backend_module().MAX_APPLICATION_NAME_LENGTH,
        )

    def test_both_resolve_the_same_inputs_the_same_way(self):
        backend = self._backend_module()
        for probe in ("chrome", "Google Chrome", "ms-teams", "python",
                      "Antigravity IDE", "ShellExperienceHost", "", None):
            with self.subTest(probe=repr(probe)):
                self.assertEqual(
                    canonical_application_name(probe),
                    backend.canonical_application_name(probe),
                )

    def test_both_canonicalize_domains_the_same_way(self):
        backend = self._backend_module()
        for probe in ("www.github.com", "GitHub.com.", "docs.google.com", "", None):
            with self.subTest(probe=repr(probe)):
                self.assertEqual(canonical_domain(probe), backend.canonical_domain(probe))

    def test_no_alias_is_claimed_by_two_products(self):
        """`_build_index` raises on a collision, so importing the module at
        all is the assertion. This states it explicitly."""
        seen = {}
        for canonical, aliases in APPLICATION_CATALOGUE.items():
            for alias in (canonical,) + tuple(aliases):
                key = alias.strip().lower()
                # Repeating a spelling inside one product's own entry is
                # harmless; two products claiming it is the bug.
                self.assertEqual(
                    seen.get(key, canonical), canonical,
                    f"{alias!r} is claimed by both {seen.get(key)!r} and {canonical!r}",
                )
                seen[key] = canonical


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

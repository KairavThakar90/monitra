import unittest
from unittest.mock import MagicMock, patch
import time
from datetime import datetime, timezone

from tracking.browsers import (
    ChromeAdapter, EdgeAdapter, FirefoxAdapter, BrowserManager, UrlSource,
    get_browser_manager
)
from tracking.browsers.manager import normalize_domain_and_url
from background_services.activity.url_usage_service import UrlUsageService


class TestBrowserAdapters(unittest.TestCase):
    def test_chrome_adapter_detection(self):
        adapter = ChromeAdapter()
        self.assertTrue(adapter.is_supported_app("chrome.exe"))
        self.assertTrue(adapter.is_supported_app("brave.exe"))
        self.assertFalse(adapter.is_supported_app("code.exe"))

    def test_chrome_adapter_detection_on_macos_display_names(self):
        """macOS reports NSWorkspace's localizedName() (a display name),
        not an executable name -- "Google Chrome", not "chrome.exe"."""
        adapter = ChromeAdapter()
        self.assertTrue(adapter.is_supported_app("Google Chrome"))
        self.assertTrue(adapter.is_supported_app("Brave Browser"))
        self.assertFalse(adapter.is_supported_app("Visual Studio Code"))

    def test_edge_adapter_detection(self):
        adapter = EdgeAdapter()
        self.assertTrue(adapter.is_supported_app("msedge.exe"))
        self.assertFalse(adapter.is_supported_app("firefox.exe"))

    def test_edge_adapter_detection_on_macos_display_name(self):
        adapter = EdgeAdapter()
        self.assertTrue(adapter.is_supported_app("Microsoft Edge"))

    def test_firefox_adapter_detection(self):
        adapter = FirefoxAdapter()
        self.assertTrue(adapter.is_supported_app("firefox.exe"))
        self.assertFalse(adapter.is_supported_app("chrome.exe"))

    def test_firefox_adapter_detection_on_macos_display_name(self):
        """macOS's Firefox.app localizedName() is exactly "Firefox" --
        already covered by the existing lowercase "firefox" entry, no
        adapter change was needed here, only Chrome and Edge's sets."""
        adapter = FirefoxAdapter()
        self.assertTrue(adapter.is_supported_app("Firefox"))

    def test_browser_manager_routing(self):
        manager = get_browser_manager()
        self.assertTrue(manager.is_browser_app("chrome.exe"))
        self.assertTrue(manager.is_browser_app("msedge.exe"))
        self.assertTrue(manager.is_browser_app("firefox.exe"))
        self.assertFalse(manager.is_browser_app("devenv.exe"))

    def test_domain_and_url_normalization(self):
        domain, norm_url = normalize_domain_and_url("https://github.com/organization/repository/issues/")
        self.assertEqual(domain, "github.com")
        self.assertEqual(norm_url, "https://github.com/organization/repository/issues")

        domain2, norm_url2 = normalize_domain_and_url(None, "Issues · repository · GitHub")
        self.assertEqual(domain2, "github.com")
        self.assertEqual(norm_url2, "https://github.com")

    def test_unidentifiable_page_yields_no_domain_and_no_url(self):
        """The bug this guards: a title with no site in it used to normalise
        to the sentinel domain "unknown-domain", which the summary builder
        then rendered as the clickable link "https://unknown-domain" -- a URL
        the user had never visited."""
        domain, url = normalize_domain_and_url(None, "ChatGPT - SMS")
        self.assertIsNone(domain)
        self.assertIsNone(url)


class TestUrlUsageService(unittest.TestCase):
    def setUp(self):
        self.runtime = MagicMock()
        self.cache = MagicMock()
        self.service = UrlUsageService(self.runtime, self.cache)

    def test_start_and_stop_tracker(self):
        session = {"entry_id": 123}
        self.service.start_tracker(session)
        self.assertTrue(self.service._tracking)
        self.assertEqual(self.service._entry_id, 123)

        self.service.stop_tracker()
        self.assertFalse(self.service._tracking)
        self.assertIsNone(self.service._entry_id)

    @patch("background_services.activity.url_usage_service.get_active_window_details")
    def test_session_accumulation_and_flush_on_app_switch(self, mock_active_window):
        # 1. Start tracker
        self.service.start_tracker({"entry_id": 100})

        # 2. First tick: Chrome active on GitHub
        mock_active_window.return_value = ("chrome.exe", "Issues · repository · GitHub", None, 100, 0)
        self.service.tick()

        self.assertEqual(self.service._current_browser, "Google Chrome")
        self.assertEqual(self.service._current_domain, "github.com")
        initial_event_id = self.service._current_client_event_id
        self.assertIsNotNone(initial_event_id)

        # 3. Fast forward time slightly (same URL session, 10s elapsed)
        now = time.monotonic()
        self.service._session_start = now - 10.0
        self.service._last_observed = now

        # 4. User switches away to VS Code
        mock_active_window.return_value = ("code.exe", "main.py - Visual Studio Code", None, 101, 0)
        self.service.tick()

        # 5. Verify local cache received save_url_usage with stable client_event_id
        self.cache.save_url_usage.assert_called_once()
        kwargs = self.cache.save_url_usage.call_args.kwargs
        self.assertEqual(kwargs["time_entry_id"], 100)
        self.assertEqual(kwargs["browser_name"], "Google Chrome")
        self.assertEqual(kwargs["domain"], "github.com")
        self.assertEqual(kwargs["client_event_id"], initial_event_id)
        self.assertGreaterEqual(kwargs["duration_seconds"], 10)

    @patch("background_services.activity.url_usage_service.get_active_window_details")
    def test_url_switch_finalizes_previous_session(self, mock_active_window):
        self.service.start_tracker({"entry_id": 100})

        # Tick 1: GitHub
        mock_active_window.return_value = ("chrome.exe", "Issues · GitHub", None, 100, 0)
        self.service.tick()

        now = time.monotonic()
        self.service._session_start = now - 15.0
        self.service._last_observed = now

        # Tick 2: Stack Overflow
        mock_active_window.return_value = ("chrome.exe", "Python questions - Stack Overflow", None, 100, 0)
        self.service.tick()

        # Verify previous session was saved
        self.cache.save_url_usage.assert_called_once()
        call_kwargs = self.cache.save_url_usage.call_args.kwargs
        self.assertEqual(call_kwargs["domain"], "github.com")
        self.assertGreaterEqual(call_kwargs["duration_seconds"], 15)

        # Verify new session started
        self.assertEqual(self.service._current_domain, "stackoverflow.com")

    @patch("background_services.activity.url_usage_service.get_active_window_details")
    def test_a_shutdown_while_tracking_writes_the_open_session_once(self, mock_active_window):
        """The runtime stops this service, then `TimerService.on_stop` stops
        its trackers. Both flush, and the second write reused the session's
        `client_event_id` -- an IntegrityError in the local queue."""
        self.service.start_tracker({"entry_id": 100})
        mock_active_window.return_value = ("chrome.exe", "Issues · GitHub", None, 100, 0)
        self.service.tick()
        now = time.monotonic()
        self.service._session_start = now - 10.0
        self.service._last_observed = now

        self.service.on_stop(1000)
        self.service.stop_tracker()

        self.cache.save_url_usage.assert_called_once()
        self.assertEqual(self.cache.save_url_usage.call_args.kwargs["domain"], "github.com")


class TestAddressBarExtraction(unittest.TestCase):
    """The real URL source: the browser's address bar, read via UI Automation.

    Before this existed, `ChromeAdapter._extract_via_uia` returned None
    unconditionally and every URL in the product was inferred from a window
    title -- which is why a ChatGPT conversation, whose title contains no
    site at all, surfaced in the UI as "https://unknown-domain".
    """

    def test_address_bar_url_is_preferred_over_the_window_title(self):
        adapter = ChromeAdapter()
        with patch(
            "tracking.browsers.base.read_address_bar",
            return_value="https://chatgpt.com/c/6a96c4ec",
        ):
            browser, url, title, source = adapter.extract_url_info(
                1234, "ChatGPT - SMS - Google Chrome"
            )
        self.assertEqual(browser, "Google Chrome")
        self.assertEqual(url, "https://chatgpt.com/c/6a96c4ec")
        self.assertEqual(title, "ChatGPT - SMS")
        self.assertEqual(source, UrlSource.ADDRESS_BAR)

    def test_unreadable_address_bar_and_siteless_title_report_unavailable(self):
        adapter = ChromeAdapter()
        with patch("tracking.browsers.base.read_address_bar", return_value=None):
            _, url, title, source = adapter.extract_url_info(
                1234, "ChatGPT - SMS - Google Chrome"
            )
        self.assertIsNone(url)
        self.assertEqual(title, "ChatGPT - SMS")
        self.assertEqual(source, UrlSource.UNAVAILABLE)

    def test_address_bar_is_read_once_per_window_title(self):
        """A UIA round trip costs milliseconds; an unchanged window must be
        answered from the adapter's memo rather than re-entering COM on every
        two-second sample."""
        adapter = ChromeAdapter()
        with patch(
            "tracking.browsers.base.read_address_bar",
            return_value="https://example.com/a",
        ) as reader:
            for _ in range(5):
                adapter.extract_url_info(1234, "Example - Google Chrome")
            self.assertEqual(reader.call_count, 1)
            adapter.extract_url_info(1234, "Another page - Google Chrome")
            self.assertEqual(reader.call_count, 2)

    def test_manager_reports_unavailable_for_a_browser_with_no_readable_url(self):
        manager = BrowserManager()
        with patch("tracking.browsers.base.read_address_bar", return_value=None):
            observation = manager.extract_browser_info("chrome.exe", "ChatGPT - SMS", 1234)
        self.assertIsNotNone(observation)
        self.assertFalse(observation.has_url)
        self.assertIsNone(observation.domain)
        self.assertIsNone(observation.url)
        self.assertEqual(observation.url_source, UrlSource.UNAVAILABLE)

    def test_manager_returns_none_for_a_non_browser(self):
        manager = BrowserManager()
        self.assertIsNone(
            manager.extract_browser_info("code.exe", "main.py - Visual Studio Code", 1)
        )


class TestUrlUsageHonesty(unittest.TestCase):
    def setUp(self):
        self.runtime = MagicMock()
        self.cache = MagicMock()
        self.service = UrlUsageService(self.runtime, self.cache)

    @patch("background_services.activity.url_usage_service.get_active_window_details")
    def test_browser_without_a_readable_url_records_nothing(self, mock_window):
        """No URL record at all is the honest outcome -- the time is still
        captured as application usage against the browser itself. Recording a
        placeholder domain is what produced the fake "https://unknown-domain"
        row in the Activity panel."""
        self.service.start_tracker({"entry_id": 100})
        mock_window.return_value = ("chrome.exe", "ChatGPT - SMS", None, 100, 4001)

        with patch("tracking.browsers.base.read_address_bar", return_value=None):
            self.service.tick()
            self.service.tick()
            self.service.stop_tracker()

        self.cache.save_url_usage.assert_not_called()

    @patch("background_services.activity.url_usage_service.get_active_window_details")
    def test_title_change_on_the_same_page_does_not_split_the_segment(self, mock_window):
        self.service.start_tracker({"entry_id": 100})
        mock_window.return_value = ("chrome.exe", "ChatGPT - SMS", None, 100, 4002)

        with patch(
            "tracking.browsers.base.read_address_bar",
            return_value="https://chatgpt.com/c/abc",
        ):
            self.service.tick()
            started = self.service._current_client_event_id
            # Same page, new title (the conversation just gained a name).
            mock_window.return_value = ("chrome.exe", "Renamed chat", None, 100, 4002)
            self.service.tick()

        self.cache.save_url_usage.assert_not_called()
        self.assertEqual(self.service._current_client_event_id, started)
        self.assertEqual(self.service._current_title, "Renamed chat")

    @patch("background_services.activity.url_usage_service.get_active_window_details")
    def test_real_url_is_persisted_with_its_full_path(self, mock_window):
        self.service.start_tracker({"entry_id": 100})
        mock_window.return_value = ("chrome.exe", "ChatGPT - SMS", None, 100, 4003)

        with patch(
            "tracking.browsers.base.read_address_bar",
            return_value="https://chatgpt.com/c/6a96c4ec",
        ):
            self.service.tick()
            now = time.monotonic()
            self.service._session_start = now - 12.0
            self.service._last_observed = now
            self.service.stop_tracker()

        self.cache.save_url_usage.assert_called_once()
        kwargs = self.cache.save_url_usage.call_args.kwargs
        self.assertEqual(kwargs["url"], "https://chatgpt.com/c/6a96c4ec")
        self.assertEqual(kwargs["domain"], "chatgpt.com")
        self.assertEqual(kwargs["browser_name"], "Google Chrome")
        self.assertGreaterEqual(kwargs["duration_seconds"], 12)

    @patch("background_services.activity.url_usage_service.get_active_window_details")
    def test_unobserved_gap_is_not_claimed_as_browsing_time(self, mock_window):
        """Sleep/hibernate: the loop stops sampling, and the time it was not
        watching is not the user's browsing time."""
        self.service.start_tracker({"entry_id": 100})
        mock_window.return_value = ("chrome.exe", "ChatGPT", None, 100, 4004)

        with patch(
            "tracking.browsers.base.read_address_bar",
            return_value="https://chatgpt.com/c/abc",
        ):
            self.service.tick()
            now = time.monotonic()
            self.service._session_start = now - 3610.0
            self.service._last_observed = now - 3600.0  # last real sample, an hour ago
            self.service.tick()

        self.cache.save_url_usage.assert_called_once()
        # 10s of observed use, not the 3610s the wall clock advanced.
        self.assertEqual(
            self.cache.save_url_usage.call_args.kwargs["duration_seconds"], 10
        )


class TestUrlUsageSummary(unittest.TestCase):
    def test_records_without_a_domain_are_skipped_not_labelled_unknown(self):
        from background_services.activity.url_usage import build_url_usage_summary
        from core.time_format import ist_today

        api_client = MagicMock()
        api_client.get.side_effect = RuntimeError("offline")
        cache = MagicMock()
        cache.get_unsynced_url_usage_between.return_value = [
            {"domain": None, "url": None, "page_title": "ChatGPT - SMS",
             "duration_seconds": 60},
            {"domain": "chatgpt.com", "url": "https://chatgpt.com/c/abc",
             "page_title": "ChatGPT - SMS", "duration_seconds": 30},
        ]

        rows = build_url_usage_summary(api_client, cache, day=ist_today())

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["url"], "https://chatgpt.com/c/abc")
        self.assertNotIn("unknown", str(rows).lower())


class TestTitleHeuristicFalsePositives(unittest.TestCase):
    """Found live: a Firefox window with no page open was reported as
    browsing x.com, because the single-letter site keyword "x" matched the
    letter inside "mozilla firefox"."""

    def test_a_site_keyword_must_match_a_whole_word(self):
        self.assertEqual(normalize_domain_and_url(None, "Mozilla Firefox"), (None, None))
        self.assertEqual(normalize_domain_and_url(None, "Inbox"), (None, None))

    def test_a_real_site_keyword_still_matches(self):
        domain, url = normalize_domain_and_url(None, "Home / X")
        self.assertEqual(domain, "x.com")
        self.assertEqual(url, "https://x.com")

    def test_edge_window_decoration_is_stripped_from_the_page_title(self):
        """Live sample: Edge appends the tab count and the profile name."""
        adapter = EdgeAdapter()
        with patch("tracking.browsers.base.read_address_bar",
                   return_value="https://app.hubstaff.com/dashboard"):
            _, _, title, _ = adapter.extract_url_info(
                5001,
                "Hubstaff - Dashboard and 12 more pages - Personal - Microsoft​ Edge",
            )
        self.assertEqual(title, "Hubstaff - Dashboard")

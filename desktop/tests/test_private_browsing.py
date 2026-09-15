"""Private/incognito window detection, and its journey through URL tracking.

Every string in here is a **real** window title or accessible name, recorded
from Chrome, Edge and Firefox windows open on a Windows 11 development machine
— not invented for the test. That matters more than usual for this feature,
because the two things that make it hard are both counter-intuitive and both
would be missed by plausible-looking fixtures:

  1. A Chrome incognito window's title is ``'Wikipedia - Google Chrome'``.
     There is no marker in it at all, so every title-based implementation
     silently reports incognito as normal.
  2. Ordinary windows are full of the word. A page whose title ends
     ``(Incognito)`` produces the window title
     ``'Quarterly report (Incognito) - Google Chrome'``, and a normal window
     showing documentation about incognito mode has the word throughout its
     accessibility tree.

So the detector reads one specific element — the Chromium `BrowserRootView`,
whose accessible name the browser composes from its own off-the-record state —
and requires the marker in trailing position. Both traps above are regression
tests here.
"""
from __future__ import annotations

import pytest

from tracking.browsers import BrowserManager, UrlSource
from tracking.browsers.chrome import ChromeAdapter
from tracking.browsers.edge import EdgeAdapter
from tracking.browsers.firefox import FirefoxAdapter
from tracking.browsers.private_mode import PrivateState, PrivateWindowDetector

# ── Recorded from real windows ───────────────────────────────────────────────

CHROME_NORMAL_TITLE = "Example Domain - Google Chrome"
CHROME_NORMAL_ROOT = "Example Domain - Google Chrome"

CHROME_INCOGNITO_TITLE = "Wikipedia - Google Chrome"
CHROME_INCOGNITO_ROOT = "Wikipedia - Google Chrome (Incognito)"

EDGE_NORMAL_TITLE = "Example Domain - Personal - Microsoft​ Edge"
EDGE_NORMAL_ROOT = "Example Domain - Microsoft Edge"

EDGE_INPRIVATE_TITLE = "Search - Microsoft Bing - [InPrivate] - Microsoft​ Edge"
EDGE_INPRIVATE_ROOT = "Search - Microsoft Bing - Microsoft Edge (InPrivate)"

#: A normal window whose *page* title ends in a parenthetical containing the
#: word. Opened for real in Chrome to confirm the shape.
CHROME_PAGE_TITLED_INCOGNITO_TITLE = "Quarterly report (Incognito) - Google Chrome"
CHROME_PAGE_TITLED_INCOGNITO_ROOT = "Quarterly report (Incognito) - Google Chrome"

FIREFOX_NORMAL_TITLE = "Example Domain — Mozilla Firefox"
FIREFOX_PRIVATE_TITLE = (
    "DuckDuckGo - Protection. Privacy. Peace of mind. "
    "— Mozilla Firefox Private Browsing"
)


class _FakeRootView:
    """Stands in for the UI Automation read, recording what was asked for."""

    def __init__(self, names):
        #: hwnd -> accessible name, or None for "could not read".
        self.names = names
        self.calls = []

    def __call__(self, hwnd, class_name):
        self.calls.append((hwnd, class_name))
        return self.names.get(hwnd)


@pytest.fixture
def detector(monkeypatch):
    """A detector whose UIA read is stubbed, on a platform it believes is Windows."""
    def _build(names):
        from tracking.browsers import private_mode

        reader = _FakeRootView(names)
        monkeypatch.setattr(private_mode, "read_element_name_by_class", reader)
        monkeypatch.setattr(private_mode.sys, "platform", "win32")
        instance = PrivateWindowDetector()
        return instance, reader

    return _build


# ── Detection ────────────────────────────────────────────────────────────────

class TestChromiumDetection:
    def test_a_chrome_incognito_window_is_detected_despite_a_plain_title(self, detector):
        # The headline case: nothing in the window title says "incognito".
        assert "incognito" not in CHROME_INCOGNITO_TITLE.lower()
        det, _ = detector({11: CHROME_INCOGNITO_ROOT})
        state, marker = det.detect(11, CHROME_INCOGNITO_TITLE)
        assert state == PrivateState.PRIVATE
        assert marker.lower() == "incognito"

    def test_a_normal_chrome_window_is_not_private(self, detector):
        det, _ = detector({12: CHROME_NORMAL_ROOT})
        assert det.detect(12, CHROME_NORMAL_TITLE)[0] == PrivateState.NORMAL

    def test_an_edge_inprivate_window_is_detected(self, detector):
        det, _ = detector({13: EDGE_INPRIVATE_ROOT})
        state, marker = det.detect(13, EDGE_INPRIVATE_TITLE)
        assert state == PrivateState.PRIVATE
        assert marker.lower() == "inprivate"

    def test_a_normal_edge_window_is_not_private(self, detector):
        det, _ = detector({14: EDGE_NORMAL_ROOT})
        assert det.detect(14, EDGE_NORMAL_TITLE)[0] == PrivateState.NORMAL

    def test_a_page_titled_incognito_in_a_normal_window_is_not_private(self, detector):
        # The false positive that a word search, or a parenthetical matched
        # anywhere in the string, would produce. The parenthetical here is
        # mid-string: the browser name follows it.
        det, _ = detector({15: CHROME_PAGE_TITLED_INCOGNITO_ROOT})
        state, marker = det.detect(15, CHROME_PAGE_TITLED_INCOGNITO_TITLE)
        assert state == PrivateState.NORMAL
        assert marker is None

    def test_an_app_window_whose_own_title_ends_in_a_parenthetical_is_not_private(
        self, detector
    ):
        # A progressive web app has no ' - Google Chrome' suffix, so its
        # accessible name really can end with the page's own parenthetical.
        # The window title ends with the same one, which is what gives it away.
        det, _ = detector({16: "Notes (Draft)"})
        assert det.detect(16, "Notes (Draft)")[0] == PrivateState.NORMAL

    def test_the_same_app_window_in_incognito_is_still_detected(self, detector):
        det, _ = detector({17: "Notes (Draft) (Incognito)"})
        assert det.detect(17, "Notes (Draft)")[0] == PrivateState.PRIVATE

    def test_a_guest_window_is_treated_as_off_the_record(self, detector):
        det, _ = detector({18: "Example Domain - Google Chrome (Guest)"})
        state, marker = det.detect(18, "Example Domain - Google Chrome")
        assert state == PrivateState.PRIVATE
        assert marker.lower() == "guest"

    def test_an_unfamiliar_localised_marker_is_still_detected(self, detector):
        # German Chrome says "(Inkognito)". The verdict rests on the structure,
        # so a locale the catalogue does not list still works.
        det, _ = detector({19: "Beispiel - Google Chrome (Inkognito)"})
        assert det.detect(19, "Beispiel - Google Chrome")[0] == PrivateState.PRIVATE

    def test_an_unreadable_window_is_unknown_not_normal(self, detector):
        # A failed read must never be recorded as "was not private".
        det, _ = detector({20: None})
        state, marker = det.detect(20, CHROME_NORMAL_TITLE)
        assert state == PrivateState.UNKNOWN
        assert PrivateState.to_bool(state) is None

    def test_detection_is_unavailable_rather_than_false_off_windows(self, monkeypatch):
        from tracking.browsers import private_mode

        monkeypatch.setattr(private_mode.sys, "platform", "darwin")
        det = PrivateWindowDetector()
        assert det.detect(1, CHROME_INCOGNITO_TITLE)[0] == PrivateState.UNKNOWN


class TestFirefoxDetection:
    """Gecko has no BrowserRootView, but it does mark its own title."""

    def test_a_private_window_is_detected_from_its_title_suffix(self):
        det = PrivateWindowDetector()
        state, marker = det.detect(0, FIREFOX_PRIVATE_TITLE, is_firefox=True)
        assert state == PrivateState.PRIVATE
        assert marker == "private browsing"

    def test_a_normal_window_is_not_private(self):
        det = PrivateWindowDetector()
        assert det.detect(0, FIREFOX_NORMAL_TITLE, is_firefox=True)[0] == PrivateState.NORMAL

    def test_a_page_merely_titled_private_browsing_is_not_private(self):
        # The suffix is matched whole, so the bare words in a page title do not
        # trigger it -- the browser name has to follow them.
        det = PrivateWindowDetector()
        state, _ = det.detect(
            0, "Private Browsing — Mozilla Firefox", is_firefox=True
        )
        assert state == PrivateState.NORMAL

    def test_no_title_at_all_is_unknown(self):
        det = PrivateWindowDetector()
        assert det.detect(0, "", is_firefox=True)[0] == PrivateState.UNKNOWN


class TestTransitions:
    def test_moving_from_a_normal_window_to_a_private_one_is_seen(self, detector):
        det, _ = detector({1: CHROME_NORMAL_ROOT, 2: CHROME_INCOGNITO_ROOT})
        assert det.detect(1, CHROME_NORMAL_TITLE)[0] == PrivateState.NORMAL
        assert det.detect(2, CHROME_INCOGNITO_TITLE)[0] == PrivateState.PRIVATE

    def test_moving_back_to_the_normal_window_is_seen(self, detector):
        det, _ = detector({1: CHROME_NORMAL_ROOT, 2: CHROME_INCOGNITO_ROOT})
        det.detect(2, CHROME_INCOGNITO_TITLE)
        assert det.detect(1, CHROME_NORMAL_TITLE)[0] == PrivateState.NORMAL

    def test_a_verdict_is_cached_per_window_rather_than_re_read_every_sample(
        self, detector
    ):
        # Private state cannot change for a live window, and the URL tracker
        # samples every two seconds; re-entering COM each time would be pure
        # cost. Caching is safe precisely because the state is fixed.
        det, reader = detector({2: CHROME_INCOGNITO_ROOT})
        for _ in range(10):
            assert det.detect(2, CHROME_INCOGNITO_TITLE)[0] == PrivateState.PRIVATE
        assert len(reader.calls) == 1

    def test_the_cache_never_grows_without_bound(self, detector):
        det, _ = detector({})
        for hwnd in range(PrivateWindowDetector.MAX_CACHE_ENTRIES + 50):
            det.detect(hwnd + 1, CHROME_NORMAL_TITLE)
        assert len(det._cache) <= PrivateWindowDetector.MAX_CACHE_ENTRIES

    def test_the_lookup_asks_for_the_browser_root_view_by_class(self, detector):
        det, reader = detector({2: CHROME_INCOGNITO_ROOT})
        det.detect(2, CHROME_INCOGNITO_TITLE)
        assert reader.calls == [(2, "BrowserRootView")]


# ── Title cleaning ───────────────────────────────────────────────────────────

class TestPrivateTitleDecorations:
    """A private window's page title must not keep the browser's marker."""

    def test_edges_inprivate_marker_is_stripped_from_the_page_title(self):
        adapter = EdgeAdapter()
        assert adapter._clean_title(EDGE_INPRIVATE_TITLE) == "Search - Microsoft Bing"

    def test_a_normal_edge_title_is_unchanged_by_the_private_handling(self):
        adapter = EdgeAdapter()
        assert adapter._clean_title("Example Domain - Microsoft​ Edge") == "Example Domain"

    def test_firefoxs_private_suffix_is_stripped(self):
        adapter = FirefoxAdapter()
        cleaned = adapter._clean_title(FIREFOX_PRIVATE_TITLE)
        assert cleaned == "DuckDuckGo - Protection. Privacy. Peace of mind."

    def test_a_normal_firefox_title_still_loses_only_the_browser_name(self):
        adapter = FirefoxAdapter()
        assert adapter._clean_title(FIREFOX_NORMAL_TITLE) == "Example Domain"

    def test_a_chrome_title_containing_a_dash_is_not_rewritten(self):
        # The separator cleanup runs only when a private decoration was
        # actually removed, so ordinary titles keep their punctuation.
        adapter = ChromeAdapter()
        assert adapter._clean_title("A - B - C - Google Chrome") == "A - B - C"


# ── Through the tracking pipeline ────────────────────────────────────────────

class _StubAdapter(ChromeAdapter):
    """A Chrome adapter with a fixed address-bar answer."""

    def __init__(self, url):
        super().__init__()
        self._url = url

    def _read_address_bar(self, hwnd, window_title):
        return self._url


class TestObservationCarriesPrivateState:
    def _manager(self, url, root_names, monkeypatch):
        from tracking.browsers import private_mode

        monkeypatch.setattr(
            private_mode, "read_element_name_by_class",
            lambda hwnd, cls: root_names.get(hwnd),
        )
        monkeypatch.setattr(private_mode.sys, "platform", "win32")
        return BrowserManager(adapters=[_StubAdapter(url)])

    def test_a_private_window_records_its_url_through_the_ordinary_pipeline(
        self, monkeypatch
    ):
        manager = self._manager(
            "https://wikipedia.org/wiki/Privacy", {2: CHROME_INCOGNITO_ROOT}, monkeypatch
        )
        observation = manager.extract_browser_info("chrome", CHROME_INCOGNITO_TITLE, 2)
        assert observation.is_private is True
        assert observation.domain == "wikipedia.org"
        assert observation.url == "https://wikipedia.org/wiki/Privacy"
        assert observation.url_source == UrlSource.ADDRESS_BAR

    def test_a_normal_window_is_marked_not_private_rather_than_unknown(
        self, monkeypatch
    ):
        manager = self._manager(
            "https://example.com", {1: CHROME_NORMAL_ROOT}, monkeypatch
        )
        observation = manager.extract_browser_info("chrome", CHROME_NORMAL_TITLE, 1)
        assert observation.is_private is False

    def test_a_private_window_with_no_readable_url_still_reports_the_browser(
        self, monkeypatch
    ):
        # The limitation case: private state known, URL not. Nothing may be
        # invented for the URL, and the browser activity must not vanish.
        #
        # The page title is one that identifies no site, which is what makes
        # this the unavailable case rather than the weaker title-derived one.
        manager = self._manager(None, {2: CHROME_INCOGNITO_ROOT}, monkeypatch)
        observation = manager.extract_browser_info(
            "chrome", "Quarterly report - Google Chrome", 2
        )
        assert observation is not None
        assert observation.is_private is True
        assert observation.has_url is False
        assert observation.url is None and observation.domain is None
        assert observation.url_source == UrlSource.UNAVAILABLE

    def test_an_undeterminable_private_state_is_none_not_false(self, monkeypatch):
        manager = self._manager("https://example.com", {}, monkeypatch)
        observation = manager.extract_browser_info("chrome", CHROME_NORMAL_TITLE, 9)
        assert observation.is_private is None


# ── Through the URL usage service and its queue ──────────────────────────────

class TestPrivateBrowsingReachesTheQueue:
    """Private browsing uses the ordinary URL pipeline, with one extra field.

    There is deliberately no separate path for it: same segment logic, same
    `client_event_id`, same offline queue, same retry. A second pipeline for
    incognito would be exactly the duplicate background implementation
    DO_NOT_DO.md is about.
    """

    @pytest.fixture
    def service(self, monkeypatch):
        from unittest.mock import MagicMock

        from background_services.activity import url_usage_service as module
        from tracking.browsers import private_mode

        root_names = {2: CHROME_INCOGNITO_ROOT, 1: CHROME_NORMAL_ROOT}
        monkeypatch.setattr(
            private_mode, "read_element_name_by_class",
            lambda hwnd, cls: root_names.get(hwnd),
        )
        monkeypatch.setattr(private_mode.sys, "platform", "win32")

        cache = MagicMock()
        svc = module.UrlUsageService(MagicMock(), cache)
        svc._browser_manager = BrowserManager(
            adapters=[_StubAdapter("https://wikipedia.org/wiki/Privacy")]
        )
        return svc, cache, module

    def _window(self, module, monkeypatch, hwnd, title):
        monkeypatch.setattr(
            module, "get_active_window_details",
            lambda: ("chrome.exe", title, None, 100, hwnd),
        )

    def test_a_private_session_is_flushed_with_is_private_true(
        self, service, monkeypatch
    ):
        import time as _time

        svc, cache, module = service
        svc.start_tracker({"entry_id": 100})
        self._window(module, monkeypatch, 2, CHROME_INCOGNITO_TITLE)
        svc.tick()
        assert svc._current_is_private is True

        now = _time.monotonic()
        svc._session_start = now - 12.0
        svc._last_observed = now
        # Switch away to close the segment.
        monkeypatch.setattr(
            module, "get_active_window_details",
            lambda: ("code.exe", "main.py - Visual Studio Code", None, 101, 0),
        )
        svc.tick()

        cache.save_url_usage.assert_called_once()
        kwargs = cache.save_url_usage.call_args.kwargs
        assert kwargs["is_private"] is True
        assert kwargs["domain"] == "wikipedia.org"
        assert kwargs["duration_seconds"] >= 12
        # The ordinary idempotency key, not a private-specific one.
        assert kwargs["client_event_id"]

    def test_switching_from_normal_to_private_closes_the_segment(
        self, service, monkeypatch
    ):
        import time as _time

        svc, cache, module = service
        svc.start_tracker({"entry_id": 100})
        self._window(module, monkeypatch, 1, CHROME_NORMAL_TITLE)
        svc.tick()
        assert svc._current_is_private is False

        now = _time.monotonic()
        svc._session_start = now - 8.0
        svc._last_observed = now

        # The same URL, now in an incognito window. It is a different browsing
        # session and must not be merged into the normal-window record.
        self._window(module, monkeypatch, 2, CHROME_INCOGNITO_TITLE)
        svc.tick()

        cache.save_url_usage.assert_called_once()
        assert cache.save_url_usage.call_args.kwargs["is_private"] is False
        assert svc._current_is_private is True

    def test_switching_back_from_private_to_normal_closes_it_too(
        self, service, monkeypatch
    ):
        import time as _time

        svc, cache, module = service
        svc.start_tracker({"entry_id": 100})
        self._window(module, monkeypatch, 2, CHROME_INCOGNITO_TITLE)
        svc.tick()

        now = _time.monotonic()
        svc._session_start = now - 8.0
        svc._last_observed = now

        self._window(module, monkeypatch, 1, CHROME_NORMAL_TITLE)
        svc.tick()

        cache.save_url_usage.assert_called_once()
        assert cache.save_url_usage.call_args.kwargs["is_private"] is True
        assert svc._current_is_private is False

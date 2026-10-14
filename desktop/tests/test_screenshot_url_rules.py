"""Website privacy rules must match the way an admin expects them to.

The rule check used to delete the `*` from the URL pattern and look for the
rest as a substring of the address, so a rule such as
`https://*.upwork.com/*` could never match anything and the screenshot was
simply taken. These tests pin the matching, and -- through the real
`_check_authorized` -- that a matching rule really stops the capture.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from background_services.screenshot import capture, screenshot_service
from background_services.screenshot.url_rules import url_matches_rule

WORK = "https://www.upwork.com/nx/find-work/best-matches?q=python"


def rule(pattern: str = "", domain: str = "upwork.com") -> dict:
    return {"id": 1, "domain": domain, "url_pattern": pattern}


class TestUrlMatchesRule:
    @pytest.mark.parametrize(
        "pattern",
        [
            "https://www.upwork.com/*",
            "https://upwork.com/*",  # a rule without www still covers www
            "https://*.upwork.com/*",  # used to become "https://.upwork.com/"
            "*.upwork.com/*",
            "*upwork.com*",
            "upwork.com",
            "www.upwork.com/*",
            "http://upwork.com/*",  # scheme is ignored
            "HTTPS://UpWork.COM/*",  # and so is case
            "https://upwork.com/nx",  # a path is a prefix
            "upwork.com/nx/*/best-matches",  # * inside a path
        ],
    )
    def test_matches_the_site_however_the_rule_was_written(self, pattern):
        assert url_matches_rule(WORK, rule(pattern)) is True

    @pytest.mark.parametrize(
        "pattern",
        [
            "https://app.slack.com/*",
            "https://upwork.com/jobs/*",  # a different part of the same site
            "https://notupwork.com/*",  # shares a suffix, not a domain
            "upwork.com.evil.example/*",
        ],
    )
    def test_does_not_match_other_sites_or_other_paths(self, pattern):
        assert url_matches_rule(WORK, rule(pattern)) is False

    def test_the_host_covers_its_subdomains_and_the_bare_site(self):
        assert url_matches_rule("https://community.upwork.com/x", rule("https://upwork.com/*"))
        assert url_matches_rule("https://upwork.com/", rule("https://www.upwork.com/*"))

    def test_an_address_bar_url_with_a_port_or_without_a_scheme(self):
        assert url_matches_rule("upwork.com/nx", rule("https://upwork.com/*"))
        assert url_matches_rule("https://upwork.com:8443/nx", rule("https://upwork.com/*"))

    def test_a_blank_pattern_falls_back_to_the_domain(self):
        assert url_matches_rule(WORK, rule("", domain="upwork.com")) is True
        assert url_matches_rule("https://github.com/", rule("", domain="upwork.com")) is False

    def test_a_rule_with_nothing_to_match_matches_nothing(self):
        assert url_matches_rule(WORK, rule("", domain="")) is False

    @pytest.mark.parametrize("url", [None, "", "   "])
    def test_no_url_matches_no_rule(self, url):
        assert url_matches_rule(url, rule("*")) is False


# ── through the real authorisation door ─────────────────────────────────────


class _Screen:
    def __init__(self):
        self.reads = 0

    def __call__(self):
        self.reads += 1
        return None


@pytest.fixture
def screen(monkeypatch):
    spy = _Screen()
    monkeypatch.setattr(capture, "capture_all_displays", spy)
    return spy


@pytest.fixture
def service(qapp, cache):
    runtime = SimpleNamespace(tasks=None, timer=SimpleNamespace(active_session=lambda: None))
    svc = screenshot_service.ScreenshotService(runtime, cache)
    yield svc
    svc.stop_tracker()


SESSION = {"entry_id": 4021, "task_id": 77, "project_id": 3}


def _browser_showing(monkeypatch, url):
    monkeypatch.setattr(
        screenshot_service, "get_active_window_details",
        lambda: ("chrome", "Upwork - Google Chrome", None, None, 1),
    )
    manager = SimpleNamespace(
        extract_browser_info=lambda *_args: SimpleNamespace(url=url)
    )
    monkeypatch.setattr(screenshot_service, "get_browser_manager", lambda: manager)


def _excluding_site(service, pattern):
    service._privacy_config = {
        "applications": [],
        "urls": [{"id": 5, "domain": "upwork.com", "url_pattern": pattern}],
        "excluded_applications": [],
        "excluded_urls": [{"url_id": 5}],
    }


def test_an_excluded_website_stops_the_capture(service, screen, monkeypatch):
    _browser_showing(monkeypatch, WORK)
    _excluding_site(service, "https://*.upwork.com/*")
    service.start_tracker(SESSION)

    allowed, _entry, _client_op, reason = service._check_authorized(
        service._current_generation()
    )

    assert allowed is False
    assert "url excluded by privacy config" in reason
    result = service._capture_now(0, service._current_generation())
    assert result is not None and result.get("excluded") is True
    assert screen.reads == 0


def test_another_website_still_captures(service, monkeypatch):
    _browser_showing(monkeypatch, "https://github.com/KairavThakar90/monitra")
    _excluding_site(service, "https://*.upwork.com/*")
    service.start_tracker(SESSION)

    allowed, entry_id, _client_op, _reason = service._check_authorized(
        service._current_generation()
    )

    assert allowed is True
    assert entry_id == SESSION["entry_id"]


def test_a_rule_that_is_not_switched_on_for_the_member_does_nothing(service, monkeypatch):
    _browser_showing(monkeypatch, WORK)
    _excluding_site(service, "https://*.upwork.com/*")
    service._privacy_config["excluded_urls"] = []
    service.start_tracker(SESSION)

    allowed, _entry, _client_op, _reason = service._check_authorized(
        service._current_generation()
    )

    assert allowed is True

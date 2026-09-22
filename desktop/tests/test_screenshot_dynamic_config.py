"""
Screenshot capture frequency follows the admin's own per-user setting.

Mirrors `test_idle_time.py`'s coverage of `IdleService.apply_user_profile`/
`_refresh_config` for the screenshot side: the desktop never hardcodes how
often it captures once a user's `capture_frequency` is known, it is seeded
from the login profile, and it is corrected periodically for a mid-session
admin change -- exactly the pattern idle threshold already established.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from background_services.screenshot import screenshot_service as module
from background_services.screenshot import config


class FakeScreenshotApi:
    """Records every call; never touches the network."""

    def __init__(self):
        self.config = {"capture_frequency": 10}
        self.config_calls = 0
        self.config_error = None
        self.privacy_config = {"applications": [], "urls": [], "excluded_applications": [], "excluded_urls": []}
        self.privacy_config_calls = 0
        self.privacy_config_error = None

    def get_config(self):
        self.config_calls += 1
        if self.config_error is not None:
            raise self.config_error
        return dict(self.config)

    def get_privacy_config(self):
        self.privacy_config_calls += 1
        if self.privacy_config_error is not None:
            raise self.privacy_config_error
        return dict(self.privacy_config)


def _submit(fn, on_success=None, on_error=None, key=None, **kwargs):
    """A synchronous stand-in for `TaskRunner.submit`."""
    try:
        result = fn()
    except BaseException as exc:  # noqa: BLE001
        if on_error:
            on_error(exc)
        return None
    if on_success:
        on_success(result)
    return object()


@pytest.fixture
def api():
    return FakeScreenshotApi()


@pytest.fixture
def service(qapp, cache, api):
    runtime = SimpleNamespace(
        tasks=SimpleNamespace(submit=_submit),
        timer=SimpleNamespace(active_session=lambda: None),
    )
    svc = module.ScreenshotService(runtime, cache, api)
    yield svc
    svc.stop_tracker()


# ── Seeding from the login profile ──────────────────────────────────────────

def test_before_any_profile_the_default_window_is_unchanged(service):
    """Nothing before login/session-verify may change today's behaviour."""
    assert service._window_seconds() == config.window_seconds()


def test_apply_user_profile_sets_the_window(service):
    service.apply_user_profile({"capture_frequency": 5})
    assert service._window_seconds() == 5 * 60


def test_a_nested_user_payload_is_understood(service):
    service.apply_user_profile({"user": {"capture_frequency": 20}})
    assert service._window_seconds() == 20 * 60


def test_a_missing_field_is_ignored(service):
    service.apply_user_profile({"capture_frequency": 5})
    service.apply_user_profile({"some_other_field": 1})
    assert service._window_seconds() == 5 * 60, "a payload with nothing to say left the value alone"


def test_a_non_positive_frequency_is_ignored(service):
    service.apply_user_profile({"capture_frequency": 5})
    service.apply_user_profile({"capture_frequency": 0})
    assert service._window_seconds() == 5 * 60, "a bad read must never blank a good value"


def test_a_non_numeric_frequency_is_ignored(service):
    service.apply_user_profile({"capture_frequency": 5})
    service.apply_user_profile({"capture_frequency": "soon"})
    assert service._window_seconds() == 5 * 60


def test_not_a_dict_is_ignored(service):
    service.apply_user_profile(None)
    assert service._window_seconds() == config.window_seconds()


# ── Periodic refresh ─────────────────────────────────────────────────────────

def test_refresh_config_applies_a_changed_value(service, api):
    api.config = {"capture_frequency": 7}
    service._refresh_config()
    assert api.config_calls == 1
    assert service._window_seconds() == 7 * 60


def test_a_failed_refresh_keeps_the_last_known_value(service, api):
    service.apply_user_profile({"capture_frequency": 12})
    api.config_error = RuntimeError("network error")
    service._refresh_config()
    assert service._window_seconds() == 12 * 60, "a network blip must not blank a good value"


def test_refresh_with_no_api_service_is_a_no_op(qapp, cache):
    runtime = SimpleNamespace(
        tasks=SimpleNamespace(submit=_submit),
        timer=SimpleNamespace(active_session=lambda: None),
    )
    svc = module.ScreenshotService(runtime, cache)  # no screenshot_api at all
    try:
        svc._refresh_config()  # must not raise
        assert svc._window_seconds() == config.window_seconds()
    finally:
        svc.stop_tracker()


def test_the_refresh_timer_interval_matches_idles_own_cadence(service):
    # Consistency between the two settings: an admin's change to either one
    # should reach a running desktop within the same window.
    from background_services.idle.idle_service import CONFIG_REFRESH_SECONDS as IDLE_SECONDS

    assert module.ScreenshotService.CONFIG_REFRESH_SECONDS == IDLE_SECONDS
    assert service._config_refresh_timer.interval() == IDLE_SECONDS * 1000


# ── Privacy exclusions ───────────────────────────────────────────────────────
#
# `capture_frequency` has a login-time seed straight from the `/auth/me`
# payload -- no network round trip needed. Privacy exclusions have no such
# field to seed from and are fetched from their own endpoint, so without an
# explicit login-time fetch they stayed at `{}` -- and therefore unenforced,
# since `_check_authorized` only applies the privacy check `if
# self._privacy_config:` -- until the periodic timer first fired, up to
# `CONFIG_REFRESH_SECONDS` after the app started. An admin's exclusion must
# be in effect before the first capture can happen, not minutes later.

def test_logging_in_fetches_privacy_config_immediately(service, api):
    api.privacy_config = {
        "applications": [{"id": 1, "process_name": "chrome.exe"}],
        "urls": [], "excluded_applications": [{"application_id": 1}], "excluded_urls": [],
    }
    assert service._privacy_config == {}

    service.apply_user_profile({"capture_frequency": 10})

    assert api.privacy_config_calls == 1
    assert service._privacy_config == api.privacy_config


def test_a_failed_privacy_fetch_at_login_does_not_raise(service, api):
    api.privacy_config_error = RuntimeError("network error")
    service.apply_user_profile({"capture_frequency": 10})  # must not raise
    assert service._privacy_config == {}


def test_privacy_config_seeding_does_not_reapply_a_stale_capture_frequency(service, api):
    """`_refresh_privacy_config` must not also re-fetch and apply
    `get_config()` -- that would let a login's own explicit capture_frequency
    be clobbered by whatever the config endpoint happens to answer at that
    same moment."""
    api.config = {"capture_frequency": 999}
    service.apply_user_profile({"capture_frequency": 5})
    assert service._window_seconds() == 5 * 60
    assert api.config_calls == 0

"""A capture must not happen before the privacy config has had its first
chance to load.

Confirmed against a real desktop session's own log: `_check_authorized` used
to gate the exclusion check on `if self._privacy_config:` alone, and `{}` is
indistinguishable from "not fetched yet" -- so any capture whose scheduled
window fell inside the real network round trip after login went through
completely unchecked. A short capture frequency (an admin testing with a
1-minute interval, or simply a machine that reaches its first scheduled
window soon after starting) made this the *first* capture of the session --
exactly the one an admin's exclusion most needs to cover.

The fix holds captures (retried in 2 seconds via the same path
`_on_captured` already uses for an excluded app) until the privacy config's
first fetch has resolved -- success or failure, so a genuinely unreachable
backend cannot block every future capture forever, only the very first one.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from background_services.screenshot import screenshot_service as module

SESSION = {"entry_id": 4021, "task_id": 77, "project_id": 3}


class FakeScreenshotApi:
    def __init__(self):
        self.privacy_config = {
            "applications": [{"id": 1, "process_name": "chrome.exe"}],
            "urls": [], "excluded_applications": [{"application_id": 1}], "excluded_urls": [],
        }

    def get_config(self):
        return {"capture_frequency": 10}

    def get_privacy_config(self):
        return dict(self.privacy_config)


class DeferredTasks:
    """Captures every submission without running it, like the real pool
    between `submit()` returning and a background thread actually finishing
    the work. `resolve()` lets a test decide exactly when that happens."""

    def __init__(self):
        self.pending: dict[str, tuple] = {}

    def submit(self, fn, *, on_success=None, on_error=None, key=None, **kwargs):
        self.pending[key] = (fn, on_success, on_error)
        return object()

    def resolve(self, key: str) -> None:
        fn, on_success, on_error = self.pending.pop(key)
        try:
            result = fn()
        except BaseException as exc:  # noqa: BLE001
            if on_error:
                on_error(exc)
            return
        if on_success:
            on_success(result)


@pytest.fixture
def tasks():
    return DeferredTasks()


@pytest.fixture
def service(qapp, cache, tasks):
    runtime = SimpleNamespace(tasks=tasks, timer=SimpleNamespace(active_session=lambda: None))
    svc = module.ScreenshotService(runtime, cache, FakeScreenshotApi())
    yield svc
    svc.stop_tracker()


def test_a_capture_is_held_until_the_first_fetch_resolves(service, tasks, monkeypatch):
    monkeypatch.setattr(
        module, "get_active_window_details",
        lambda: ("chrome.exe", "", None, None, None),
    )
    service._refresh_privacy_config()  # submitted, not yet resolved
    service.start_tracker(SESSION)

    allowed, _entry, _client_op, reason = service._check_authorized(
        service._current_generation()
    )

    assert allowed is False
    assert reason == "privacy_config_pending"
    result = service._capture_now(0, service._current_generation())
    assert result == {"excluded": True, "reason": "privacy_config_pending"}


def test_the_capture_is_authorized_normally_once_the_fetch_resolves(service, tasks, monkeypatch):
    monkeypatch.setattr(
        module, "get_active_window_details",
        lambda: ("Code.exe", "", None, None, None),
    )
    service._refresh_privacy_config()
    service.start_tracker(SESSION)
    assert service._check_authorized(service._current_generation())[0] is False

    tasks.resolve("screenshot-privacy-config")

    allowed, entry_id, _client_op, _reason = service._check_authorized(
        service._current_generation()
    )
    assert allowed is True
    assert entry_id == SESSION["entry_id"]


def test_a_failed_first_fetch_still_unblocks_capture(service, tasks, monkeypatch):
    """An unreachable backend must not hold every future capture forever --
    only ever the one waiting on the very first attempt."""
    monkeypatch.setattr(
        module, "get_active_window_details",
        lambda: ("Code.exe", "", None, None, None),
    )
    monkeypatch.setattr(
        service._screenshot_api, "get_privacy_config",
        lambda: (_ for _ in ()).throw(RuntimeError("network error")),
    )
    service._refresh_privacy_config()
    service.start_tracker(SESSION)
    assert service._check_authorized(service._current_generation())[0] is False

    tasks.resolve("screenshot-privacy-config")

    allowed, _entry, _client_op, _reason = service._check_authorized(
        service._current_generation()
    )
    assert allowed is True


def test_no_api_service_configured_is_not_gated(qapp, cache, tasks):
    """`screenshot_api is None` (most of this file's own tests, and any
    build with no backend configured) has no privacy config to ever arrive,
    so it must behave exactly as before this fix -- never held."""
    runtime = SimpleNamespace(tasks=tasks, timer=SimpleNamespace(active_session=lambda: None))
    svc = module.ScreenshotService(runtime, cache)  # no screenshot_api
    try:
        svc.start_tracker(SESSION)
        allowed, _entry, _client_op, _reason = svc._check_authorized(
            svc._current_generation()
        )
        assert allowed is True
    finally:
        svc.stop_tracker()

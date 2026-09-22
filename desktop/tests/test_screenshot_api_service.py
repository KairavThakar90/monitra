"""`ScreenshotApiService.get_privacy_config` must call a path that actually
exists on the backend.

`backend/app/api/screenshot_privacy.py` registers its router once, at its own
internal `/api/v1/screenshot` prefix -- unlike `/screenshots/config`
(`time_entry_screenshot.py`), which happens to also be registered a second
time at the bare root in `backend/app/main.py`. A privacy-config request built
without the `/api/v1` prefix has nowhere to land and 404s every time,
regardless of what the admin configured -- confirmed against the live
deployment's own log:

    httpx.HTTPStatusError: Client error '404 Not Found' for url
    'https://monitra-lvzq.vercel.app/screenshot/privacy-config'
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.screenshot.service import ScreenshotApiService


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _SpyClient:
    """Records the exact path every `get()` call was made with."""

    def __init__(self, payload=None):
        self.calls = []
        self._payload = payload if payload is not None else {}

    def get(self, path, params=None, headers=None, timeout=None, skip_auth_refresh=False):
        self.calls.append(path)
        return _Response(self._payload)


@pytest.fixture
def client():
    return _SpyClient()


@pytest.fixture
def service(client):
    return ScreenshotApiService(client)


def test_get_privacy_config_requests_the_versioned_path(service, client):
    service.get_privacy_config()
    assert client.calls == ["/api/v1/screenshot/privacy-config"]


def test_get_config_still_requests_its_own_unversioned_path(service, client):
    """`/screenshots/config` is deliberately unversioned -- it works because
    `time_entry_screenshot_router` is registered a second time at the bare
    root in `main.py`. This pins that this file did not change it while
    fixing the sibling method above."""
    service.get_config()
    assert client.calls == ["/screenshots/config"]

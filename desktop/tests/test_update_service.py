"""
The Phase 0 update notice.

The behaviours pinned here are the ones that would otherwise reappear as the
failures DO_NOT_DO.md already records:

* the announcement is **edge-triggered** — the backend keeps answering "8.1.0
  is available" on every poll, and a level-triggered notification would be a
  toast every six hours (and, with `check_now()`, potentially far more often);
* an "unknown" answer is never rendered as an update;
* a failed check holds quietly and can never affect anything the user is doing.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.api.exceptions import ApiError
from background_services.network import NetworkState
from background_services.update import UpdateService


class FakeUpdateApi:
    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error
        self.calls = 0

    def get_latest_version(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.payload


class FakeNotifications:
    def __init__(self):
        self.messages = []

    def notify(self, message, level=None, title=None, key=None, link=None):
        self.messages.append((message, key, link))
        return True


def make_service(payload=None, error=None, *, signed_in=True,
                 network_state=NetworkState.BACKEND_REACHABLE):
    """An UpdateService with its runtime stubbed and no thread started.

    `tick()` is called directly: it is written to run off the GUI thread and
    touches no widgets, so it is exercisable without starting the loop.
    """
    notifications = FakeNotifications()
    runtime = SimpleNamespace(
        api_client=SimpleNamespace(access_token="token" if signed_in else None),
        network=SimpleNamespace(network_state=network_state),
        notifications=notifications,
        storage=None,
    )
    api = FakeUpdateApi(payload, error)
    service = UpdateService(runtime, api)
    # The first tick is the deliberate startup delay; step past it so each
    # test exercises a real check.
    service.tick()
    return service, api, notifications


# The announced versions are deliberately far ahead of the installed build: the
# client now checks that an "update" is strictly newer than what is running, so
# a payload naming a version at or below it is (correctly) not an update.
AVAILABLE = {
    "latest_version": "8.1.0",
    "download_url": "https://example.invalid/releases",
    "release_notes_url": None,
    "update_available": True,
    "client_version": "1.0.1",
}


def test_first_tick_defers_rather_than_checking_at_startup():
    notifications = FakeNotifications()
    runtime = SimpleNamespace(
        api_client=SimpleNamespace(access_token="token"),
        network=SimpleNamespace(network_state=NetworkState.BACKEND_REACHABLE),
        notifications=notifications, storage=None,
    )
    api = FakeUpdateApi(AVAILABLE)
    service = UpdateService(runtime, api)

    delay = service.tick()

    assert api.calls == 0
    assert delay == UpdateService.FIRST_CHECK_DELAY_MS


def test_announces_a_newer_release_once():
    service, api, notifications = make_service(AVAILABLE)

    service.tick()
    service.tick()
    service.tick()

    assert api.calls == 3, "the check itself still runs on every tick"
    # ...but the user is told exactly once. A notification per poll is the
    # level-triggered storm this service exists to avoid.
    assert len(notifications.messages) == 1
    message, key, link = notifications.messages[0]
    assert "8.1.0" in message
    assert key == "update-available:8.1.0"
    # The download URL travels as a *link*, not as text in the body: a
    # platform toast renders plain text, so a URL written into the message is
    # not clickable and vanishes when the user clicks the toast.
    assert link == "https://example.invalid/releases"


def test_a_release_with_no_download_url_still_announces_without_a_dead_link():
    service, _api, notifications = make_service(
        dict(AVAILABLE, download_url=None)
    )

    service.tick()

    message, _key, link = notifications.messages[0]
    assert link is None
    # It must not invite a click that goes nowhere.
    assert "Click here" not in message


def test_announces_again_when_a_further_release_appears():
    service, api, notifications = make_service(AVAILABLE)
    service.tick()

    api.payload = dict(AVAILABLE, latest_version="8.2.0")
    service.tick()

    assert [key for _, key, _link in notifications.messages] == [
        "update-available:8.1.0", "update-available:8.2.0",
    ]


def test_an_unknown_latest_version_is_never_announced():
    # The deployment has not been told what the current release is. That is an
    # honest unknown; prompting for it would point users at nothing.
    service, api, notifications = make_service({
        "latest_version": None, "download_url": None,
        "release_notes_url": None, "update_available": False,
        "client_version": "1.0.1",
    })

    service.tick()

    assert notifications.messages == []
    assert service.latest_release is not None
    assert service.latest_release["latest_version"] is None


def test_an_up_to_date_client_is_not_notified():
    service, _api, notifications = make_service(
        dict(AVAILABLE, update_available=False)
    )

    service.tick()

    assert notifications.messages == []


def test_holds_while_signed_out_without_calling_the_backend():
    service, api, notifications = make_service(AVAILABLE, signed_in=False)

    delay = service.tick()

    assert api.calls == 0
    assert delay == UpdateService.HOLD_INTERVAL_MS
    assert notifications.messages == []


def test_holds_while_offline():
    service, api, _notifications = make_service(
        AVAILABLE, network_state=NetworkState.NO_NETWORK
    )

    delay = service.tick()

    assert api.calls == 0
    assert delay == UpdateService.HOLD_INTERVAL_MS


def test_a_failed_check_is_silent_and_backs_off():
    # An older deployment without the endpoint answers 404. That is a normal
    # state during a rollout, not something to tell the user about.
    service, _api, notifications = make_service(
        error=ApiError("Update check failed (HTTP 404).", status_code=404)
    )

    delay = service.tick()

    assert notifications.messages == []
    assert service.latest_release is None
    assert delay >= int(UpdateService.HOLD_INTERVAL_MS * 0.85)


def test_logout_clears_what_was_announced():
    service, _api, notifications = make_service(AVAILABLE)
    service.tick()
    assert len(notifications.messages) == 1

    service.reset_session()
    service.tick()   # the deliberate first-check delay again
    service.tick()

    # The next user is told in their own session rather than inheriting the
    # previous user's "already announced" flag.
    assert len(notifications.messages) == 2


def test_the_service_is_registered_with_the_runtime(runtime):
    # Background work is registered with ApplicationRuntime, never started ad
    # hoc, and shutdown is the reverse of start order.
    names = [service.name for service in runtime.services.services]
    assert "updates" in names
    assert names.index("updates") > names.index("notifications")
    assert names.index("updates") > names.index("network")


def test_the_client_identifies_its_version_on_every_request():
    # Fleet visibility depends on this header being present: the backend reads
    # the desktop version out of it.
    from app.api.client import ApiClient
    from version import VERSION

    client = ApiClient(base_url="https://example.invalid")
    try:
        headers = client._prepare_headers()
    finally:
        client.close()

    assert headers["User-Agent"] == f"Monitra/{VERSION}"
    assert headers["X-Monitra-Platform"] == sys.platform


if __name__ == "__main__":
    pytest.main([__file__])

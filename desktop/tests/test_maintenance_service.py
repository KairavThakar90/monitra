"""
The maintenance notice, at the service.

What is pinned here, and the failure each one would otherwise reappear as:

* the notice is **edge-triggered** -- the backend answers "true" on every poll
  while it is on, and a level-triggered signal would re-show the card and
  re-send the tray toast every thirty seconds (DO_NOT_DO.md, the storm);
* a failed poll changes nothing -- the notice is not cleared because the
  backend could not be reached, and not raised because it could not be
  reached either; "offline" belongs to the network service;
* the service is connected to **nothing else**: not the timer, not the
  trackers, not the screenshot scheduler, not the sync consumer. A notice
  that could stop tracking is the one thing this feature must never be.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.api.exceptions import ApiConnectionError, ApiError, ApiHttpError
from app.maintenance.service import MaintenanceApiService
from background_services.maintenance import (
    MAINTENANCE_BODY, MAINTENANCE_STATUS_LABEL, MAINTENANCE_TITLE, MaintenanceService,
)
from background_services.maintenance.maintenance_service import NOTIFY_KEY_OFF, NOTIFY_KEY_ON
from background_services.network import NetworkState

DESKTOP_ROOT = Path(__file__).resolve().parent.parent


class FakeMaintenanceApi:
    def __init__(self, active=False, error=None):
        self.active = active
        self.error = error
        self.calls = 0

    def get_maintenance_status(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return {"maintenance_mode": self.active, "updated_at": None,
                "server_time": "2026-09-16T10:00:00+00:00"}


class FakeNotifications:
    def __init__(self):
        self.messages = []

    def notify(self, message, level=None, title=None, key=None, link=None):
        self.messages.append((message, title, key))
        return True


def make_service(active=False, error=None, *, signed_in=True,
                 network_state=NetworkState.BACKEND_REACHABLE):
    """A MaintenanceService with its runtime stubbed and no thread started.

    `tick()` is called directly: it runs off the GUI thread and touches no
    widgets, so it is exercisable without starting the loop.
    """
    notifications = FakeNotifications()
    runtime = SimpleNamespace(
        api_client=SimpleNamespace(access_token="token" if signed_in else None),
        network=SimpleNamespace(network_state=network_state),
        notifications=notifications,
        storage=None,
    )
    api = FakeMaintenanceApi(active, error)
    service = MaintenanceService(runtime, api)
    changes = []
    service.maintenance_changed.connect(changes.append)
    service.tick()   # the deliberate startup delay
    return service, api, notifications, changes


# ── Schedule and holds ────────────────────────────────────────────────────


def test_first_tick_defers_rather_than_checking_at_startup():
    runtime = SimpleNamespace(
        api_client=SimpleNamespace(access_token="token"),
        network=SimpleNamespace(network_state=NetworkState.BACKEND_REACHABLE),
        notifications=FakeNotifications(), storage=None,
    )
    api = FakeMaintenanceApi()
    service = MaintenanceService(runtime, api)

    assert service.tick() == MaintenanceService.FIRST_CHECK_DELAY_MS
    assert api.calls == 0


def test_holds_while_signed_out_without_calling_the_backend():
    service, api, _n, changes = make_service(True, signed_in=False)
    assert service.tick() == MaintenanceService.HOLD_INTERVAL_MS
    assert api.calls == 0
    assert changes == []
    assert service.maintenance_mode is False


def test_holds_while_offline():
    service, api, _n, _c = make_service(True, network_state=NetworkState.NO_NETWORK)
    assert service.tick() == MaintenanceService.HOLD_INTERVAL_MS
    assert api.calls == 0


def test_a_healthy_check_is_jittered_around_the_interval():
    service, _api, _n, _c = make_service(False)
    delay = service.tick()
    assert int(MaintenanceService.CHECK_INTERVAL_MS * 0.85) <= delay
    assert delay <= int(MaintenanceService.CHECK_INTERVAL_MS * 1.15)


# ── Edges ─────────────────────────────────────────────────────────────────


def test_an_initial_off_answer_shows_nothing():
    service, _api, notifications, changes = make_service(False)
    service.tick()
    service.tick()
    assert changes == []
    assert notifications.messages == []
    assert service.maintenance_mode is False


def test_off_to_on_is_announced_once_and_then_held_silently():
    service, api, notifications, changes = make_service(False)
    service.tick()                    # off
    api.active = True
    service.tick()                    # off -> on: the edge
    service.tick()                    # on -> on
    service.tick()                    # on -> on

    assert api.calls == 4, "the poll itself still runs every tick"
    assert changes == [True]
    assert service.maintenance_mode is True
    assert len(notifications.messages) == 1
    message, title, key = notifications.messages[0]
    assert title == MAINTENANCE_TITLE
    assert MAINTENANCE_BODY in message
    assert MAINTENANCE_STATUS_LABEL in message
    assert key == NOTIFY_KEY_ON


def test_an_initial_on_answer_is_announced_once():
    # Signing in while the notice is already on: the first answer is "on",
    # and there was nothing before it -- that is still an edge.
    service, _api, notifications, changes = make_service(True)
    service.tick()
    service.tick()
    assert changes == [True]
    assert [key for _m, _t, key in notifications.messages] == [NOTIFY_KEY_ON]


def test_on_to_off_clears_once_and_then_stays_quiet():
    service, api, notifications, changes = make_service(True)
    service.tick()                    # on
    api.active = False
    service.tick()                    # on -> off
    service.tick()                    # off -> off

    assert changes == [True, False]
    assert service.maintenance_mode is False
    assert [key for _m, _t, key in notifications.messages] == [NOTIFY_KEY_ON, NOTIFY_KEY_OFF]


def test_a_second_maintenance_window_is_announced_again():
    service, api, _n, changes = make_service(True)
    service.tick()
    api.active = False
    service.tick()
    api.active = True
    service.tick()
    assert changes == [True, False, True]


# ── Failure keeps the last answer ─────────────────────────────────────────


def test_a_failed_poll_neither_clears_nor_raises_the_notice():
    service, api, notifications, changes = make_service(True)
    service.tick()                    # on
    api.error = ApiError("Maintenance check failed (HTTP 503).", status_code=503)
    delay = service.tick()
    service.tick()

    assert changes == [True], "the notice stays exactly where it was"
    assert service.maintenance_mode is True
    assert len(notifications.messages) == 1
    assert delay >= int(MaintenanceService.HOLD_INTERVAL_MS * 0.85)

    api.error = None
    api.active = False
    service.tick()                    # the backend is back and says off
    assert changes == [True, False]


def test_an_older_backend_without_the_endpoint_is_a_quiet_hold():
    service, _api, notifications, changes = make_service(
        error=ApiError("Maintenance check failed (HTTP 404).", status_code=404)
    )
    service.tick()
    assert changes == []
    assert notifications.messages == []
    assert service.maintenance_mode is False


# ── Sessions ──────────────────────────────────────────────────────────────


def test_logout_clears_a_showing_notice_through_the_same_edge():
    service, _api, _n, changes = make_service(True)
    service.tick()
    assert changes == [True]

    service.reset_session()

    assert changes == [True, False]
    assert service.maintenance_mode is False


def test_logout_with_no_notice_showing_emits_nothing():
    service, _api, _n, changes = make_service(False)
    service.tick()
    service.reset_session()
    assert changes == []


def test_the_next_session_is_told_again_once():
    service, _api, notifications, changes = make_service(True)
    service.tick()
    service.reset_session()
    service.tick()
    service.tick()
    assert changes == [True, False, True]
    assert [key for _m, _t, key in notifications.messages] == [NOTIFY_KEY_ON, NOTIFY_KEY_ON]


def test_check_now_before_the_loop_started_is_harmless():
    service, _api, _n, _c = make_service(False)
    service.check_now()   # no thread yet: a no-op, not a crash


# ── The API wrapper ───────────────────────────────────────────────────────


class _StubClient:
    def __init__(self, outcome):
        self.outcome = outcome
        self.paths = []

    def get(self, path, params=None, headers=None, timeout=None):
        self.paths.append(path)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return SimpleNamespace(json=lambda: self.outcome)


def test_the_wrapper_asks_the_status_endpoint_and_returns_the_payload():
    client = _StubClient({"maintenance_mode": True})
    assert MaintenanceApiService(client).get_maintenance_status() == {"maintenance_mode": True}
    assert client.paths == ["/system/maintenance-status"]


@pytest.mark.parametrize("exc", [
    ApiHttpError(404, "not found"),
    ApiHttpError(503, "down"),
    ApiConnectionError("no route"),
    RuntimeError("something odd"),
])
def test_every_failure_is_flattened_to_an_api_error(exc):
    with pytest.raises(ApiError):
        MaintenanceApiService(_StubClient(exc)).get_maintenance_status()


# ── Ownership and isolation ───────────────────────────────────────────────


def test_the_service_is_registered_with_the_runtime(runtime):
    names = [service.name for service in runtime.services.services]
    assert "maintenance" in names
    assert names.index("maintenance") > names.index("notifications")
    assert names.index("maintenance") > names.index("network")
    # And it is not one of the timer's trackers.
    assert runtime.maintenance not in runtime.timer._trackers


def test_the_public_api_exposes_it_and_nothing_more(runtime):
    from background_services.public_api import BackgroundApi
    api = BackgroundApi(runtime)
    assert api.maintenance is runtime.maintenance
    assert api.maintenance_mode() is False


def test_the_notice_touches_no_tracking_service():
    """The whole promise: maintenance mode cannot stop, pause or alter
    tracking, because the code paths do not exist."""
    # Attribute access on a tracking service, or a call to one of its verbs.
    # The notice's own prose says "your activity is being saved", so bare
    # words are not what is looked for -- a `.activity` is.
    forbidden = re.compile(
        r"\.(timer|activity|app_usage|url_usage|screenshot|screenshots|sync|idle)\b"
        r"|\b(start_tracking|stop_tracking|start_tracker|stop_tracker|enqueue)\("
    )
    for path in (
        DESKTOP_ROOT / "background_services" / "maintenance" / "maintenance_service.py",
        DESKTOP_ROOT / "app" / "maintenance" / "service.py",
        DESKTOP_ROOT / "ui" / "maintenance_toast.py",
    ):
        code = "\n".join(
            line for line in path.read_text(encoding="utf-8").splitlines()
            if not line.strip().startswith("#")
        )
        # Strip docstrings, which legitimately *talk about* the timer.
        code = re.sub(r'"""[\s\S]*?"""', "", code)
        assert not forbidden.search(code), f"{path.name} references a tracking service"

    # And the other direction: nothing in the runtime reads the notice.
    for sub in ("timer", "activity", "screenshot", "sync", "idle", "network", "recovery"):
        for path in (DESKTOP_ROOT / "background_services" / sub).rglob("*.py"):
            assert "maintenance" not in path.read_text(encoding="utf-8").lower(), path


if __name__ == "__main__":
    pytest.main([__file__])

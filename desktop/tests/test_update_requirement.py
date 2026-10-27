"""
The mandatory-update requirement: the backend's deadline, kept and enforced.

What is pinned, and why each matters:

* **The deadline is the server's.** The desktop measures what is left from
  the server's own `server_time` plus the time elapsed on its monotonic clock
  since the answer arrived. Moving the system clock by a year changes
  nothing; after a restart the wall clock stands in, clamped so that a clock
  set backwards cannot push the deadline out.
* **Blocking is an edge, and it happens while the app is open.** The loop
  wakes at the deadline, offline or signed out, and `blocked_changed(True)`
  fires once. The backend's own `blocked` is honoured at once.
* **A restart keeps the deadline** (persisted with the server's instants) and
  **an installed update clears it** -- by the version actually running, never
  by the installer's word.
* **"Later" is refused once blocked; a manual check re-offers a postponed
  release**; a withdrawn requirement releases the block.
* **Every outcome is reported** to the backend, durably, with a client event
  id, and retried on the next successful check.
* **The rule lives below the UI**: the timer refuses a new start while
  blocked, the main window swaps the dashboard for the update page, and the
  API client asks for a re-check when the backend answers 426.
"""
from __future__ import annotations

import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import version
from app.api.exceptions import ApiError, ApiHttpError
from background_services.network import NetworkState
from background_services.update import UpdateService
from background_services.update import state as state_module
from background_services.update import update_service as service_module
from background_services.update.state import UpdateRequirement
from background_services.update.update_service import (
    INSTALL_ATTEMPT_KEY, PENDING_REPORTS_KEY, REQUIREMENT_KEY,
)
from tests.test_update_installer import FakeCache, FakeTasks, FakeUpdateApi, INSTALLABLE

HOUR = 3600.0
T_SERVER = 1_800_000_000.0        # the server's clock when the answer arrives
INSTALLED = version.VERSION
_MAJOR, _MINOR, _PATCH = (int(p) for p in INSTALLED.split("."))
NEWER = f"{_MAJOR}.{_MINOR}.{_PATCH + 1}"


def _iso(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()


def mandatory(**overrides):
    """A release-management answer: required, inside its 24-hour grace."""
    data = dict(
        INSTALLABLE, latest_version=NEWER, update_available=True,
        mandatory=True, required_version=NEWER, release_title="Autumn release",
        grace_hours=24, first_seen_at=_iso(T_SERVER), deadline_at=_iso(T_SERVER + 24 * HOUR),
        grace_active=True, blocked=False, server_time=_iso(T_SERVER), force_update=False,
    )
    data.update(overrides)
    return data


class Clock:
    """Both clocks the requirement reads, movable independently.

    Replaces the `time` name inside the state module only -- never the global
    `time.monotonic`, which every other deadline in the process reads. The
    fake backend below stamps its answers with this clock's wall time, as the
    real one stamps them with its own.
    """

    def __init__(self, monotonic=5_000.0, wall=T_SERVER):
        self._monotonic = monotonic
        self._wall = wall

    def monotonic(self):
        return self._monotonic

    def time(self):
        return self._wall

    def advance(self, seconds):
        self._monotonic += seconds
        self._wall += seconds


#: The clock the fake backend stamps its answers with (set by the fixture).
CURRENT_CLOCK = {"clock": None}


@pytest.fixture
def clock(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(state_module, "time", clock)
    monkeypatch.setattr(service_module, "_now", clock.time)
    monkeypatch.setitem(CURRENT_CLOCK, "clock", clock)
    return clock


class ReportingApi(FakeUpdateApi):
    """The update API with the report endpoint, recording and optionally refusing.

    A mandatory answer is stamped with the fake server's *current* clock, as
    the real backend stamps `server_time` on every answer; the deadline stays
    where the first answer put it.
    """

    def __init__(self, payload=None, error=None, *, report_error=None):
        super().__init__(payload, error)
        self.reports = []
        self.report_error = report_error

    def get_latest_version(self):
        payload = super().get_latest_version()
        clock = CURRENT_CLOCK.get("clock")
        if isinstance(payload, dict) and payload.get("mandatory") and clock is not None:
            payload = dict(payload, server_time=_iso(clock.time()))
        return payload

    def report_install_outcome(self, report):
        if self.report_error is not None:
            raise self.report_error
        self.reports.append(report)
        return {"id": len(self.reports), **report}


def make_service(payload=None, error=None, *, cache=None, tasks=None, signed_in=True,
                 network_state=NetworkState.BACKEND_REACHABLE, report_error=None):
    runtime = SimpleNamespace(
        api_client=SimpleNamespace(access_token="token" if signed_in else None),
        network=SimpleNamespace(network_state=network_state),
        notifications=SimpleNamespace(notify=lambda *a, **k: True),
        tasks=tasks,
        storage=None,
    )
    api = ReportingApi(payload, error, report_error=report_error)
    service = UpdateService(runtime, api, cache=cache if cache is not None else FakeCache())
    return service, api


def checked(payload=None, **kwargs):
    """A service past its startup tick, with its edges recorded."""
    service, api = make_service(payload, **kwargs)
    edges = {"blocked": [], "requirement": [], "offers": []}
    service.blocked_changed.connect(lambda b: edges["blocked"].append(b))
    service.requirement_changed.connect(lambda r: edges["requirement"].append(r))
    service.update_offered.connect(lambda r, m, s: edges["offers"].append((r.version, m, s)))
    # The parts of `on_start` under test, without starting a loop thread on a
    # stub runtime: the previous install's report and the persisted requirement.
    service._report_previous_install()
    service._load_requirement()
    service.tick()   # startup deferral
    return service, api, edges


# ═══════════════════════════════════════════════════════════════════════════
# The requirement and its clocks
# ═══════════════════════════════════════════════════════════════════════════


class TestRequirementParsing:

    def test_an_optional_answer_is_no_requirement(self):
        assert UpdateRequirement.from_payload(dict(INSTALLABLE, mandatory=False)) is None
        assert UpdateRequirement.from_payload(INSTALLABLE) is None
        assert UpdateRequirement.from_payload(None) is None

    def test_a_mandatory_answer_carries_the_servers_instants(self, clock):
        requirement = UpdateRequirement.from_payload(mandatory())
        assert requirement is not None
        assert requirement.required_version == NEWER
        assert requirement.deadline_at == T_SERVER + 24 * HOUR
        assert requirement.server_time == T_SERVER
        assert requirement.title == "Autumn release"
        assert requirement.grace_hours == 24
        assert requirement.blocked_by_server is False
        assert requirement.remaining_seconds() == 24 * 3600

    def test_an_unreadable_deadline_is_no_deadline_but_still_a_requirement(self, clock):
        requirement = UpdateRequirement.from_payload(mandatory(deadline_at="soon"))
        assert requirement is not None
        assert requirement.deadline_at is None
        assert requirement.remaining_seconds() is None
        assert requirement.expired() is False

    def test_an_unreadable_required_version_is_no_requirement(self):
        assert UpdateRequirement.from_payload(mandatory(required_version="latest", latest_version="x")) is None


class TestRequirementClocks:

    def test_remaining_time_follows_the_monotonic_clock_not_the_wall_clock(self, clock):
        requirement = UpdateRequirement.from_payload(mandatory())
        clock._wall += 365 * 24 * HOUR          # the user sets the clock a year ahead
        assert requirement.remaining_seconds() == 24 * 3600
        clock._wall -= 2 * 365 * 24 * HOUR      # ...then a year behind
        assert requirement.remaining_seconds() == 24 * 3600
        clock.advance(23 * HOUR)
        assert requirement.remaining_seconds() == 3600
        clock.advance(HOUR)
        assert requirement.remaining_seconds() == 0
        assert requirement.expired()

    def test_after_a_restart_the_wall_clock_stands_in_but_never_runs_backwards(self, clock):
        stored = UpdateRequirement.from_payload(mandatory()).to_record()
        clock.advance(23 * HOUR)
        reread = UpdateRequirement.from_record(stored)
        assert reread.remaining_seconds(restarted=True) == 3600
        clock._wall = T_SERVER - 10 * HOUR       # clock set back past the receipt while shut down
        assert reread.remaining_seconds(restarted=True) == 24 * 3600, \
            "a clock behind the receipt time reads as no time elapsed, never as more time left"
        clock._wall = T_SERVER + 26 * HOUR
        assert reread.remaining_seconds(restarted=True) == 0
        assert reread.expired(restarted=True)

    def test_the_servers_blocked_is_authoritative(self, clock):
        requirement = UpdateRequirement.from_payload(mandatory(blocked=True))
        assert requirement.expired()
        assert requirement.remaining_seconds() == 24 * 3600, "the figure is still the server's"

    def test_a_record_round_trips(self, clock):
        original = UpdateRequirement.from_payload(mandatory())
        reread = UpdateRequirement.from_record(original.to_record())
        assert reread.required_version == original.required_version
        assert reread.deadline_at == original.deadline_at
        assert reread.server_time == original.server_time
        assert reread.title == "Autumn release"
        assert UpdateRequirement.from_record({"required_version": "x"}) is None
        assert UpdateRequirement.from_record("junk") is None


# ═══════════════════════════════════════════════════════════════════════════
# The service: grace, the deadline edge, restart, withdrawal
# ═══════════════════════════════════════════════════════════════════════════


class TestGraceAndBlocking:

    def test_a_mandatory_answer_inside_the_grace_is_offered_with_later_and_persisted(self, clock):
        cache = FakeCache()
        service, _api, edges = checked(mandatory(), cache=cache)
        service.tick()
        assert edges["offers"] == [(NEWER, True, "initial")]
        assert service.is_blocked is False
        assert edges["blocked"] == []
        assert service.requirement is not None
        assert service.requirement_remaining_seconds() == 24 * 3600
        assert cache.state[REQUIREMENT_KEY]["required_version"] == NEWER
        assert service.force_update_pending is False, "a client that may still postpone is not forced"

    def test_the_block_happens_at_the_deadline_while_the_app_stays_open(self, clock):
        service, _api, edges = checked(mandatory())
        service.tick()
        clock.advance(23 * HOUR + 3599)
        service.tick()
        assert edges["blocked"] == [], "one second before the deadline"
        clock.advance(1)
        service.tick()
        assert edges["blocked"] == [True]
        assert service.is_blocked
        assert service.force_update_pending
        service.tick()
        assert edges["blocked"] == [True], "an edge, not a level"

    def test_the_loop_wakes_for_the_deadline(self, clock):
        service, _api, _edges = checked(mandatory())
        service.tick()
        interval = service.tick()
        assert interval <= 24 * 3600 * 1000 + 1000
        clock.advance(24 * HOUR - 90)
        assert service.tick() <= 91 * 1000

    def test_the_deadline_passes_while_offline_or_signed_out(self, clock):
        service, api, edges = checked(mandatory())
        service.tick()
        api.error = ApiError("Could not check for updates: network error.")
        clock.advance(25 * HOUR)
        service.tick()
        assert edges["blocked"] == [True]
        assert api.calls >= 2, "the check was attempted, and the block did not wait for it"

    def test_the_servers_blocked_blocks_at_once(self, clock):
        service, _api, edges = checked(mandatory(blocked=True, grace_active=False, force_update=True))
        service.tick()
        assert edges["blocked"] == [True]
        assert edges["offers"] == [(NEWER, True, "initial")]

    def test_a_restart_inside_the_grace_keeps_the_deadline_without_a_backend(self, clock):
        cache = FakeCache()
        service, _api, _edges = checked(mandatory(), cache=cache)
        service.tick()
        clock.advance(23 * HOUR)
        again, _api2, edges2 = checked(error=ApiError("offline"), cache=cache)
        assert again.requirement is not None
        assert again.requirement_remaining_seconds() == 3600
        assert again.is_blocked is False
        again.tick()
        clock.advance(2 * HOUR)
        again.tick()
        assert edges2["blocked"] == [True], "blocked by the persisted deadline, no backend needed"

    def test_a_restart_after_the_deadline_blocks_before_the_first_check(self, clock):
        cache = FakeCache()
        service, _api, _edges = checked(mandatory(), cache=cache)
        service.tick()
        clock.advance(30 * HOUR)
        again, _api2, edges2 = checked(error=ApiError("offline"), cache=cache)
        assert again.is_blocked
        assert edges2["blocked"] == [True]

    def test_a_restart_cannot_reset_the_deadline_however_the_clock_was_moved(self, clock):
        cache = FakeCache()
        service, _api, _edges = checked(mandatory(), cache=cache)
        service.tick()
        clock._wall -= 48 * HOUR                 # clock set two days back before the restart
        clock._monotonic = 1.0                   # a fresh process
        again, _api2, _edges2 = checked(error=ApiError("offline"), cache=cache)
        assert again.requirement_remaining_seconds() == 24 * 3600, "never more than the server gave"

    def test_installing_the_required_version_clears_the_requirement(self, clock, monkeypatch):
        cache = FakeCache()
        service, _api, _edges = checked(mandatory(), cache=cache)
        service.tick()
        clock.advance(30 * HOUR)
        monkeypatch.setattr(version, "VERSION", NEWER)
        again, _api2, edges2 = checked(error=ApiError("offline"), cache=cache)
        assert again.requirement is None
        assert again.is_blocked is False
        assert edges2["blocked"] == []
        assert cache.state.get(REQUIREMENT_KEY) is None

    def test_a_withdrawn_requirement_releases_the_block(self, clock):
        service, api, edges = checked(mandatory(), cache=FakeCache())
        service.tick()
        clock.advance(25 * HOUR)
        service.tick()
        assert edges["blocked"] == [True]
        api.payload = dict(INSTALLABLE, update_available=False, latest_version=INSTALLED)
        service.tick()
        assert edges["blocked"] == [True, False]
        assert service.requirement is None
        assert service.is_blocked is False

    def test_the_backend_dropping_mandatory_releases_the_block_too(self, clock):
        service, api, edges = checked(mandatory(), cache=FakeCache())
        service.tick()
        clock.advance(25 * HOUR)
        service.tick()
        api.payload = mandatory(mandatory=False, required_version=None, deadline_at=None)
        service.tick()
        assert edges["blocked"] == [True, False]
        assert edges["requirement"][-1] is None

    def test_a_requirement_is_re_offered_on_every_check_as_a_reminder_after_later(self, clock):
        service, _api, edges = checked(mandatory(), cache=FakeCache())
        service.tick()
        service.postpone()
        service.tick()
        assert edges["offers"] == [(NEWER, True, "initial"), (NEWER, True, "reminder")]

    def test_later_is_refused_once_blocked(self, clock):
        cache = FakeCache()
        service, _api, _edges = checked(mandatory(), cache=cache)
        service.tick()
        clock.advance(25 * HOUR)
        service.tick()
        service.postpone()
        assert cache.state.get(service_module.POSTPONED_KEY) is None

    def test_logout_releases_the_block_and_forgets_the_requirement(self, clock):
        service, _api, edges = checked(mandatory(blocked=True), cache=FakeCache())
        service.tick()
        assert service.is_blocked
        service.reset_session()
        assert edges["blocked"] == [True, False]
        assert service.requirement is None


class TestManualCheck:

    def test_a_manual_check_re_offers_a_postponed_optional_release(self, clock):
        service, _api, edges = checked(dict(INSTALLABLE, latest_version=NEWER), cache=FakeCache())
        service.tick()
        service.postpone()
        service.tick()
        assert edges["offers"] == [(NEWER, False, "initial")], "a scheduled check stays quiet"
        service.check_now(manual=True)
        service.tick()
        assert edges["offers"][-1] == (NEWER, False, "reminder")
        service.tick()
        assert len(edges["offers"]) == 2, "the manual flag is consumed by one check"


# ═══════════════════════════════════════════════════════════════════════════
# Reports to the backend
# ═══════════════════════════════════════════════════════════════════════════


class TestInstallReports:

    def test_a_failed_attempt_is_reported_with_its_category(self, clock, monkeypatch):
        tasks = FakeTasks()
        cache = FakeCache()
        service, api, _edges = checked(mandatory(), cache=cache, tasks=tasks)
        service.tick()
        monkeypatch.setattr(service_module, "can_install", lambda: None)
        service._fail("Checksum mismatch.", detail="x", category="checksum-failed")
        assert len(api.reports) == 1
        report = api.reports[0]
        assert report["target_version"] == NEWER
        assert report["from_version"] == INSTALLED
        assert report["outcome"] == "verification-failed"
        assert report["category"] == "checksum-failed"
        assert report["client_event_id"]
        assert report["occurred_at"] and report["client_time"]
        assert cache.state.get(PENDING_REPORTS_KEY) == []

    def test_an_undeliverable_report_waits_for_the_next_successful_check(self, clock):
        tasks = FakeTasks()
        cache = FakeCache()
        service, api, _edges = checked(mandatory(), cache=cache, tasks=tasks,
                                       report_error=ApiError("network"))
        service.tick()
        service._fail("Download failed.", category="timeout")
        assert api.reports == []
        assert len(cache.state[PENDING_REPORTS_KEY]) == 1
        api.report_error = None
        service.tick()
        assert len(api.reports) == 1
        assert cache.state[PENDING_REPORTS_KEY] == []

    def test_the_previous_installs_result_is_reported_with_where_it_came_from(self, clock, monkeypatch):
        tasks = FakeTasks()
        cache = FakeCache({INSTALL_ATTEMPT_KEY: {"from_version": "1.0.0", "target_version": INSTALLED}})
        monkeypatch.setattr(
            service_module, "read_and_clear_result",
            lambda: {"target": INSTALLED, "exit_code": "0", "stage": "started"},
        )
        service, api, _edges = checked(mandatory(), cache=cache, tasks=tasks)
        assert service.last_install_outcome == ("success", INSTALLED)
        assert len(api.reports) == 1
        assert api.reports[0]["outcome"] == "success"
        assert api.reports[0]["from_version"] == "1.0.0"
        assert cache.state.get(INSTALL_ATTEMPT_KEY) is None

    def test_a_failed_installer_is_reported_as_failed_never_success(self, clock, monkeypatch):
        tasks = FakeTasks()
        monkeypatch.setattr(
            service_module, "read_and_clear_result",
            lambda: {"target": NEWER, "exit_code": "2", "stage": "installer"},
        )
        service, api, _edges = checked(mandatory(), cache=FakeCache(), tasks=tasks)
        assert service.last_install_outcome == ("failed", NEWER)
        assert api.reports[0]["outcome"] == "failed"
        service.tick()
        assert service.requirement is not None, "the requirement stands until the version runs"
        assert service.requirement.required_version == NEWER

    def test_the_queue_is_bounded(self, clock):
        cache = FakeCache()
        service, _api, _edges = checked(mandatory(), cache=cache, tasks=None)
        service.tick()
        for index in range(30):
            service._fail(f"fail {index}", category="timeout")
        assert len(cache.state[PENDING_REPORTS_KEY]) == service_module.MAX_PENDING_REPORTS


# ═══════════════════════════════════════════════════════════════════════════
# Below the UI: the client, the timer and the main window
# ═══════════════════════════════════════════════════════════════════════════


class TestApiClientHook:

    def _client(self, status, monkeypatch):
        from app.api.client import ApiClient

        client = ApiClient(base_url="https://backend.invalid")

        def handler(request):
            return httpx.Response(status, json={"detail": {"code": "desktop_update_required"}})

        # The pool is built in the constructor; replace it, and any pool a
        # recycle would build, with one over the fake transport.
        monkeypatch.setattr(client, "_new_http_client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))
        client._client = client._new_http_client()
        client.access_token = "token"
        return client

    def test_a_426_asks_for_a_re_check_once_a_minute(self, monkeypatch):
        client = self._client(426, monkeypatch)
        calls = []
        client.set_upgrade_required_hook(lambda: calls.append(time.monotonic()))
        for _ in range(3):
            with pytest.raises(ApiHttpError) as caught:
                client.post("/time-entries/start", json_data={})
            assert caught.value.status_code == 426
        assert len(calls) == 1
        client.close()

    def test_other_refusals_do_not_touch_the_hook(self, monkeypatch):
        client = self._client(403, monkeypatch)
        calls = []
        client.set_upgrade_required_hook(lambda: calls.append(1))
        with pytest.raises(ApiHttpError):
            client.post("/time-entries/start", json_data={})
        assert calls == []
        client.close()


class TestTimerRefusal:

    def test_a_blocked_update_refuses_a_new_start_but_never_a_stop(self, runtime, clock):
        errors = []
        runtime.timer.timer_error.connect(lambda m: errors.append(m))
        requirement = UpdateRequirement.from_payload(mandatory(blocked=True))
        runtime.updates._set_requirement(requirement, persist=False)
        assert runtime.updates.is_blocked

        runtime.timer.start_tracking(1, 2, "A task")
        assert errors == [runtime.timer.UPDATE_BLOCKED_MESSAGE]
        assert runtime.timer.is_running() is False

        runtime.timer.switch_tracking(1, 3, "Another")
        assert errors[-1] == runtime.timer.UPDATE_BLOCKED_MESSAGE
        # Stopping is never refused: nothing to stop here, and no error either.
        before = list(errors)
        runtime.timer.stop_tracking()
        assert errors == before

    def test_a_running_timer_keeps_running_through_the_block(self, runtime, clock, monkeypatch):
        monkeypatch.setattr(runtime.timer, "_trackers", [])
        runtime.timer.start_tracking(1, 2, "A task")
        assert runtime.timer.is_running()
        runtime.updates._set_requirement(UpdateRequirement.from_payload(mandatory(blocked=True)), persist=False)
        assert runtime.updates.is_blocked
        assert runtime.timer.is_running(), "enforcement never ends a session"
        runtime.timer.stop_tracking()
        assert runtime.timer.is_running() is False


@pytest.fixture
def isolated_settings(qapp, tmp_path, monkeypatch):
    import main as main_module
    from PySide6.QtCore import QSettings

    store = QSettings(str(tmp_path / "window.ini"), QSettings.Format.IniFormat)
    monkeypatch.setattr(main_module, "window_settings", lambda: store)
    return store


@pytest.fixture
def window(qapp, runtime, isolated_settings, monkeypatch):
    import main as main_module

    monkeypatch.setattr(main_module.MainWindow, "_on_exit_ready", lambda self: None)
    win = main_module.MainWindow(runtime)
    # No network: the dashboard's loads are captured instead of run.
    win._dashboard.api.run_in_background = lambda fn, **kw: object()
    yield win
    win._dashboard.reset_state()
    win.deleteLater()


class TestMainWindowGate:

    def test_the_deadline_swaps_the_dashboard_for_the_update_page_and_back(self, window, runtime, clock):
        window._enter_dashboard({"id": 1, "name": "Ada"}, announce=False)
        assert window._stack.currentWidget() is window._dashboard
        runtime.updates._set_requirement(UpdateRequirement.from_payload(mandatory(blocked=True)), persist=False)
        assert window._stack.currentWidget() is window._update_screen
        assert "Update required: Autumn release" in window._update_screen._title.text()
        assert NEWER in window._update_screen._required_row.value_label.text()
        runtime.updates._set_requirement(None, persist=False)
        assert window._stack.currentWidget() is window._dashboard

    def test_signing_in_after_the_deadline_lands_on_the_update_page(self, window, runtime, clock):
        runtime.updates._set_requirement(UpdateRequirement.from_payload(mandatory(blocked=True)), persist=False)
        assert window._stack.currentWidget() is window._login, "the login page is never gated"
        window._enter_dashboard({"id": 1, "name": "Ada"}, announce=False)
        assert window._stack.currentWidget() is window._update_screen

    def test_the_page_without_an_installable_release_offers_the_manual_path(self, window, runtime, clock):
        window._enter_dashboard({"id": 1, "name": "Ada"}, announce=False)
        runtime.updates._latest = {"download_url": "https://staff.peakworkos.com/download"}
        runtime.updates._set_requirement(UpdateRequirement.from_payload(mandatory(blocked=True)), persist=False)
        screen = window._update_screen
        assert screen._update_button.isEnabled() is False
        assert screen._manual_button.isHidden() is False
        assert "cannot update itself" in screen._blurb.text()

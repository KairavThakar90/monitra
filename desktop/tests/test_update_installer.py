"""
The auto-updater: schedule, verification, state machine and handoff.

What is pinned here is the set of properties that make an updater safe to ship
rather than merely functional:

* **Nothing unverified is ever executed.** A payload without a checksum, or
  with a URL that is not absolute HTTPS, produces no installable release at
  all — so there is no code path that could run it.
* **A failure always leaves a working application.** Every error route ends
  back at IDLE with the artifact deleted and nothing about the installation
  touched.
* **The ten-hour schedule survives a restart**, because it is measured from a
  persisted timestamp rather than from launch — and a *failed* check never
  moves that timestamp, so an outage cannot postpone the next attempt.
* **Concurrency is refused, not merely unlikely.** The state machine rejects a
  second download; the task runner's key rejects a second task.
"""
from __future__ import annotations

import hashlib
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import version
from app.api.exceptions import ApiError
from background_services.network import NetworkState
from background_services.update import ReleaseInfo, UpdateService, UpdateState
from background_services.update.downloader import DownloadError, download_and_verify
from background_services.update.state import can_transition


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


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


class FakeCache:
    """The two `app_state` methods the service uses, in memory."""

    def __init__(self, initial=None):
        self.state = dict(initial or {})

    def load_app_state(self, key):
        return self.state.get(key)

    def save_app_state(self, key, value):
        self.state[key] = value


class FakeTasks:
    """A TaskRunner that runs the work immediately, on this thread.

    Enough to exercise the orchestration: the real runner's threading is its
    own concern and is covered by its own tests. `key` de-duplication is
    modelled because it is one of the two concurrency guards.
    """

    def __init__(self):
        self.in_flight = set()
        self.submitted = 0

    def submit(self, fn, *, on_success=None, on_error=None, key=None,
               pass_handle=False, guard_generation=True):
        if key is not None and key in self.in_flight:
            return None
        self.submitted += 1
        handle = SimpleNamespace(cancelled=False, id="task", key=key)
        if key is not None:
            self.in_flight.add(key)
        try:
            result = fn(handle) if pass_handle else fn()
        except BaseException as exc:  # noqa: BLE001 - mirrors the real runner
            if on_error is not None:
                on_error(exc)
        else:
            if on_success is not None:
                on_success(result)
        finally:
            if key is not None:
                self.in_flight.discard(key)
        return handle


def make_service(payload=None, error=None, *, cache=None, tasks=None,
                 signed_in=True, network_state=NetworkState.BACKEND_REACHABLE):
    notifications = SimpleNamespace(notify=lambda *a, **k: True)
    runtime = SimpleNamespace(
        api_client=SimpleNamespace(access_token="token" if signed_in else None),
        network=SimpleNamespace(network_state=network_state),
        notifications=notifications,
        tasks=tasks,
        storage=None,
    )
    api = FakeUpdateApi(payload, error)
    service = UpdateService(runtime, api, cache=cache)
    return service, api


#: An artifact URL this build is willing to fetch from (an approved host).
GH_SETUP = "https://github.com/acme/monitra/releases/download/v9.9.9/Monitra-Setup-9.9.9.exe"

INSTALLABLE = {
    "latest_version": "9.9.9",
    "download_url": "https://github.com/acme/monitra/releases/download/v9.9.9/Monitra-Setup-9.9.9.exe",
    "release_notes_url": None,
    "release_notes": "Faster startup.",
    "sha256": "a" * 64,
    "file_size": 1024,
    "update_available": True,
    "force_update": False,
    "client_version": version.VERSION,
}


# ---------------------------------------------------------------------------
# The trust boundary: what may be installed at all
# ---------------------------------------------------------------------------


def test_a_release_with_a_checksum_is_installable():
    release = ReleaseInfo.from_payload(INSTALLABLE)
    assert release is not None
    assert release.version == "9.9.9"
    assert release.sha256 == "a" * 64
    assert release.installable


def test_a_payload_without_a_checksum_yields_no_installable_release():
    # This is what an older deployment answers during a rollout. It must still
    # *announce*, and it must never install: running an artifact nothing can be
    # compared against is the one thing this feature must not do.
    assert ReleaseInfo.from_payload(dict(INSTALLABLE, sha256=None)) is None
    assert ReleaseInfo.from_payload(dict(INSTALLABLE, sha256="not-a-digest")) is None


@pytest.mark.parametrize("bad_url", [
    "javascript:alert(1)",
    "file:///C:/Windows/System32/calc.exe",
    "data:text/html,<script>",
    "ftp://example.invalid/setup.exe",
    "",
    None,
])
def test_a_url_that_is_not_http_is_refused(bad_url):
    # "Never execute arbitrary URLs returned without validation": a response
    # that names something other than a web address produces nothing to act on.
    assert ReleaseInfo.from_payload(dict(INSTALLABLE, download_url=bad_url)) is None


def test_a_malformed_version_is_refused():
    for bad in ("1.0", "v9.9.9", "9.9.9-rc1", None, 999):
        assert ReleaseInfo.from_payload(dict(INSTALLABLE, latest_version=bad)) is None


def test_force_update_must_be_literally_true():
    # This flag decides whether a person can keep using their own application,
    # so anything that is merely truthy is not enough.
    for value in ("yes", 1, "true", None):
        release = ReleaseInfo.from_payload(dict(INSTALLABLE, force_update=value))
        assert release is not None and release.force_update is False
    assert ReleaseInfo.from_payload(dict(INSTALLABLE, force_update=True)).force_update


def test_an_unexpected_payload_shape_is_survived():
    for junk in (None, [], "nope", {}, {"update_available": True}):
        assert ReleaseInfo.from_payload(junk) is None


# ---------------------------------------------------------------------------
# The state machine
# ---------------------------------------------------------------------------


def test_a_download_cannot_be_restarted_from_a_later_state():
    # Nothing that follows a download may re-enter it. This is what makes "two
    # downloads writing the same file" impossible rather than merely unlikely.
    for state in (UpdateState.VERIFYING, UpdateState.READY_TO_INSTALL,
                  UpdateState.INSTALLING):
        assert not can_transition(state, UpdateState.DOWNLOADING), state
    # And a download is only ever entered from a release actually on offer.
    assert not can_transition(UpdateState.IDLE, UpdateState.DOWNLOADING)
    assert not can_transition(UpdateState.CHECKING, UpdateState.DOWNLOADING)
    assert can_transition(UpdateState.UPDATE_AVAILABLE, UpdateState.DOWNLOADING)


def test_failure_is_reachable_from_every_working_state():
    # A failed update must always be able to hand back a usable application.
    for state in (UpdateState.CHECKING, UpdateState.DOWNLOADING,
                  UpdateState.VERIFYING, UpdateState.READY_TO_INSTALL,
                  UpdateState.INSTALLING):
        assert can_transition(state, UpdateState.FAILED), state
    assert can_transition(UpdateState.FAILED, UpdateState.IDLE)


def test_an_install_cannot_be_started_without_a_verified_download():
    assert not can_transition(UpdateState.IDLE, UpdateState.INSTALLING)
    assert not can_transition(UpdateState.UPDATE_AVAILABLE, UpdateState.INSTALLING)
    assert not can_transition(UpdateState.DOWNLOADING, UpdateState.INSTALLING)
    # Only from READY_TO_INSTALL, which is only reachable through VERIFYING.
    assert can_transition(UpdateState.READY_TO_INSTALL, UpdateState.INSTALLING)


def test_an_illegal_transition_is_refused_rather_than_applied():
    service, _api = make_service()
    assert service.update_state == UpdateState.IDLE
    assert service._set_update_state(UpdateState.INSTALLING) is False
    assert service.update_state == UpdateState.IDLE


# ---------------------------------------------------------------------------
# The ten-hour schedule, across restarts
# ---------------------------------------------------------------------------


def test_a_fresh_install_is_checked_shortly_after_startup():
    service, _api = make_service(cache=FakeCache())
    # Nothing recorded means nothing is known, which is treated as due: a new
    # install should find out about a release rather than wait ten hours.
    assert service._startup_delay_ms() == UpdateService.FIRST_CHECK_DELAY_MS


def test_a_restart_after_ten_hours_checks_immediately():
    eleven_hours_ago = time.time() - 11 * 3600
    service, _api = make_service(
        cache=FakeCache({"updates.last_check_at": eleven_hours_ago})
    )
    assert service._startup_delay_ms() == UpdateService.FIRST_CHECK_DELAY_MS


def test_a_restart_within_the_interval_waits_out_the_remainder():
    # Closed and reopened after one hour: nine hours of the interval are left,
    # and restarting must not turn the schedule into "check on every launch".
    service, _api = make_service(
        cache=FakeCache({"updates.last_check_at": time.time() - 3600})
    )
    delay = service._startup_delay_ms()
    assert delay > UpdateService.FIRST_CHECK_DELAY_MS
    assert 8.5 * 3600 * 1000 < delay <= 9 * 3600 * 1000


def test_a_successful_check_records_when_it_happened():
    cache = FakeCache()
    service, api = make_service(INSTALLABLE, cache=cache)
    service.tick()   # the deliberate startup delay
    service.tick()
    assert api.calls == 1
    assert isinstance(cache.state.get("updates.last_check_at"), float)


def test_a_failed_check_does_not_move_the_schedule():
    # If it did, a backend that was down for a minute would postpone the next
    # attempt by the full ten hours.
    cache = FakeCache()
    service, _api = make_service(
        error=ApiError("Update check failed (HTTP 503).", status_code=503), cache=cache
    )
    service.tick()
    service.tick()
    assert "updates.last_check_at" not in cache.state


def test_a_held_check_does_not_move_the_schedule():
    cache = FakeCache()
    service, api = make_service(INSTALLABLE, cache=cache, signed_in=False)
    service.tick()
    service.tick()
    assert api.calls == 0
    assert "updates.last_check_at" not in cache.state


def test_a_clock_that_jumped_forward_is_not_trusted():
    # A timestamp in the future is a clock that moved, not a check that
    # happened. Treated as unknown so the schedule recovers by itself rather
    # than parking the next check years away.
    service, _api = make_service(
        cache=FakeCache({"updates.last_check_at": time.time() + 10 * 86400})
    )
    assert service.last_check_at() is None
    assert service._startup_delay_ms() == UpdateService.FIRST_CHECK_DELAY_MS


def test_the_scheduled_interval_is_about_ten_hours():
    service, _api = make_service(INSTALLABLE, cache=FakeCache())
    service.tick()
    delay = service.tick()
    # Jittered, so a fleet does not check in lockstep.
    assert 8.5 * 3600 * 1000 <= delay <= 11.5 * 3600 * 1000


# ---------------------------------------------------------------------------
# Offering an update
# ---------------------------------------------------------------------------


def test_an_installable_release_is_offered_for_the_dialog():
    offered = []
    service, _api = make_service(INSTALLABLE, cache=FakeCache())
    service.update_offered.connect(lambda release, forced: offered.append((release, forced)))
    service.tick()
    service.tick()

    assert len(offered) == 1
    release, forced = offered[0]
    assert release.version == "9.9.9"
    assert forced is False
    assert service.update_state == UpdateState.UPDATE_AVAILABLE


def test_a_release_with_no_checksum_is_announced_but_not_offered():
    # The rollout case: an older backend. The user is told, through the
    # notification path that predates the installer, and nothing is offered
    # for installation because nothing could be verified.
    offered, announced = [], []
    service, _api = make_service(dict(INSTALLABLE, sha256=None), cache=FakeCache())
    service.update_offered.connect(lambda *a: offered.append(a))
    service.update_available.connect(lambda *a: announced.append(a))
    service.tick()
    service.tick()

    assert offered == []
    assert len(announced) == 1
    assert service.pending_release is None


def test_a_mandatory_release_is_flagged():
    offered = []
    service, _api = make_service(
        dict(INSTALLABLE, force_update=True), cache=FakeCache()
    )
    service.update_offered.connect(lambda release, forced: offered.append(forced))
    service.tick()
    service.tick()

    assert offered == [True]
    assert service.force_update_pending is True


def test_a_mandatory_release_is_re_offered_on_every_check():
    # The user cannot carry on until it is resolved, so a dialog they escaped
    # by restarting has to come back. An optional one is offered once.
    offered = []
    service, _api = make_service(
        dict(INSTALLABLE, force_update=True), cache=FakeCache()
    )
    service.update_offered.connect(lambda *a: offered.append(a))
    service.tick()
    service.tick()
    service.tick()
    assert len(offered) == 2


def test_an_optional_release_is_offered_once_per_session():
    offered = []
    service, _api = make_service(INSTALLABLE, cache=FakeCache())
    service.update_offered.connect(lambda *a: offered.append(a))
    service.tick()
    service.tick()
    service.tick()
    service.tick()
    # The backend keeps answering "9.9.9 is available"; offering on each answer
    # would be the level-triggered storm DO_NOT_DO.md records.
    assert len(offered) == 1


def test_a_withdrawn_release_clears_a_mandatory_lockout():
    # This is the rollback lever. A force flag published in error must be
    # takeable back, or it is a permanent lockout with no remedy.
    service, api = make_service(dict(INSTALLABLE, force_update=True), cache=FakeCache())
    service.tick()
    service.tick()
    assert service.force_update_pending is True

    api.payload = dict(INSTALLABLE, update_available=False)
    service.tick()

    assert service.force_update_pending is False
    assert service.pending_release is None
    assert service.update_state == UpdateState.IDLE


def test_a_mandatory_lockout_is_never_persisted():
    # A client that once saw a force flag must not be able to lock its user out
    # forever while the backend is unreachable and unable to say otherwise.
    cache = FakeCache()
    service, _api = make_service(dict(INSTALLABLE, force_update=True), cache=cache)
    service.tick()
    service.tick()
    assert service.force_update_pending is True

    restarted, _api2 = make_service(cache=cache)
    assert restarted.force_update_pending is False


# ---------------------------------------------------------------------------
# Download and verification
# ---------------------------------------------------------------------------


def _serve(monkeypatch, body: bytes, *, status: int = 200):
    """Point the downloader's httpx client at a canned response."""
    import httpx

    class FakeStream:
        def __init__(self):
            self.headers = {"Content-Length": str(len(body))}
            self.status_code = status

        def raise_for_status(self):
            if status >= 400:
                raise httpx.HTTPStatusError(
                    "boom", request=httpx.Request("GET", "https://x.invalid"),
                    response=httpx.Response(status),
                )

        def iter_bytes(self, size):
            for start in range(0, len(body), size):
                yield body[start:start + size]

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def stream(self, method, url):
            return FakeStream()

    monkeypatch.setattr(
        "background_services.update.downloader.httpx.Client", FakeClient
    )


@pytest.fixture
def scratch(tmp_path, monkeypatch):
    monkeypatch.setenv("MONITRA_DATA_DIR", str(tmp_path))
    from core import paths

    paths.reset_cache()
    yield tmp_path
    paths.reset_cache()


def test_a_matching_artifact_is_kept(scratch, monkeypatch):
    body = b"installer bytes" * 100
    _serve(monkeypatch, body)
    result = download_and_verify(
        url=GH_SETUP,
        expected_sha256=hashlib.sha256(body).hexdigest(),
        version="9.9.9",
        expected_size=len(body),
    )
    assert result.path.is_file()
    assert result.path.read_bytes() == body
    assert result.size_bytes == len(body)


def test_a_mismatched_checksum_is_rejected_and_the_file_discarded(scratch, monkeypatch):
    body = b"tampered"
    _serve(monkeypatch, body)
    with pytest.raises(DownloadError) as caught:
        download_and_verify(
            url=GH_SETUP,
            expected_sha256="b" * 64,
            version="9.9.9",
        )
    assert "verified" in caught.value.message
    # Nothing installable is left behind: a file that failed its checksum is
    # either corrupt or not what the backend described.
    leftovers = list((scratch / "updates").glob("*"))
    assert leftovers == [], leftovers


def test_a_truncated_download_is_rejected(scratch, monkeypatch):
    body = b"half"
    _serve(monkeypatch, body)
    with pytest.raises(DownloadError) as caught:
        download_and_verify(
            url=GH_SETUP,
            expected_sha256=hashlib.sha256(body).hexdigest(),
            version="9.9.9",
            expected_size=len(body) * 4,
        )
    assert "incomplete" in caught.value.message
    assert list((scratch / "updates").glob("*")) == []


def test_a_non_https_url_is_refused_before_any_request(scratch, monkeypatch):
    # An update channel is the one thing that must not be downgradeable.
    def explode(*a, **k):
        raise AssertionError("no request should be made")

    monkeypatch.setattr(
        "background_services.update.downloader.httpx.Client", explode
    )
    with pytest.raises(DownloadError) as caught:
        download_and_verify(
            url="http://example.invalid/setup.exe",
            expected_sha256="a" * 64, version="9.9.9",
        )
    assert "securely" in caught.value.message


def test_a_server_error_leaves_nothing_behind(scratch, monkeypatch):
    _serve(monkeypatch, b"", status=503)
    with pytest.raises(DownloadError):
        download_and_verify(
            url=GH_SETUP,
            expected_sha256="a" * 64, version="9.9.9",
        )
    assert list((scratch / "updates").glob("*")) == []


def test_cancellation_stops_promptly_and_discards_the_partial(scratch, monkeypatch):
    body = b"x" * (1024 * 1024)
    _serve(monkeypatch, body)
    with pytest.raises(DownloadError) as caught:
        download_and_verify(
            url=GH_SETUP,
            expected_sha256=hashlib.sha256(body).hexdigest(),
            version="9.9.9",
            should_stop=lambda: True,
        )
    assert "cancelled" in caught.value.message
    assert list((scratch / "updates").glob("*")) == []


def test_progress_is_reported_while_downloading(scratch, monkeypatch):
    body = b"y" * (700 * 1024)
    _serve(monkeypatch, body)
    seen = []
    download_and_verify(
        url=GH_SETUP,
        expected_sha256=hashlib.sha256(body).hexdigest(),
        version="9.9.9",
        expected_size=len(body),
        on_progress=lambda received, total: seen.append((received, total)),
    )
    assert seen, "progress must be reported for a visible progress bar"
    assert seen[-1] == (len(body), len(body))
    assert [received for received, _ in seen] == sorted(r for r, _ in seen)


def test_stale_downloads_are_cleared_at_startup(scratch):
    from background_services.update.downloader import (
        clear_stale_downloads, updates_dir,
    )

    leftover = updates_dir() / "Monitra-1.0.0.exe.part"
    leftover.write_bytes(b"interrupted")
    assert clear_stale_downloads() == 1
    assert not leftover.exists()


# ---------------------------------------------------------------------------
# Orchestration: start_update
# ---------------------------------------------------------------------------


def _offered_service(scratch, monkeypatch, payload=None, body=b"installer"):
    tasks = FakeTasks()
    service, _api = make_service(
        payload or dict(INSTALLABLE, sha256=hashlib.sha256(body).hexdigest(),
                        file_size=len(body)),
        cache=FakeCache(), tasks=tasks,
    )
    _serve(monkeypatch, body)
    # An installed build is assumed: `can_install()` refuses a source checkout,
    # which is what the test suite runs as.
    monkeypatch.setattr(
        "background_services.update.update_service.can_install", lambda: None
    )
    # The artifact here is a few bytes, not a signed installer. Signature
    # verification has its own tests (test_update_hardening.py, and against real
    # signed and unsigned binaries); what is under test in this module is the
    # orchestration around it, so the step is a pass-through.
    monkeypatch.setattr(
        "background_services.update.update_service.verify_installer",
        lambda path: None,
    )
    service.tick()
    service.tick()
    return service, tasks


def test_a_verified_download_launches_the_installer(scratch, monkeypatch):
    launched = []
    monkeypatch.setattr(
        "background_services.update.update_service.launch_installer",
        lambda path, version_name: launched.append((path, version_name)),
    )
    service, tasks = _offered_service(scratch, monkeypatch)
    started = []
    service.install_started.connect(lambda name: started.append(name))

    assert service.start_update() is True

    assert len(launched) == 1
    assert launched[0][1] == "9.9.9"
    assert started == ["9.9.9"]
    assert service.update_state == UpdateState.INSTALLING


def test_a_failed_checksum_never_reaches_the_installer(scratch, monkeypatch):
    launched = []
    monkeypatch.setattr(
        "background_services.update.update_service.launch_installer",
        lambda *a: launched.append(a),
    )
    failures = []
    service, _tasks = _offered_service(
        scratch, monkeypatch, payload=dict(INSTALLABLE, sha256="c" * 64)
    )
    service.update_failed.connect(lambda message: failures.append(message))

    service.start_update()

    assert launched == []
    assert len(failures) == 1
    # And the updater is back at rest, so the user can try again.
    assert service.update_state == UpdateState.IDLE


def test_an_installer_that_will_not_start_leaves_the_app_usable(scratch, monkeypatch):
    from background_services.update.installer import InstallError

    def refuse(path, version_name):
        raise InstallError("The update could not be started.", detail="denied")

    monkeypatch.setattr(
        "background_services.update.update_service.launch_installer", refuse
    )
    failures = []
    service, _tasks = _offered_service(scratch, monkeypatch)
    service.update_failed.connect(lambda message: failures.append(message))

    service.start_update()

    assert failures == ["The update could not be started."]
    assert service.update_state == UpdateState.IDLE


def test_a_second_start_is_refused_while_one_is_running(scratch, monkeypatch):
    monkeypatch.setattr(
        "background_services.update.update_service.launch_installer",
        lambda *a: None,
    )
    service, tasks = _offered_service(scratch, monkeypatch)
    assert service.start_update() is True
    # INSTALLING is busy: a second click cannot start a second download or a
    # second installer.
    assert service.start_update() is False
    assert tasks.submitted == 1


def test_nothing_is_started_without_an_installable_release(scratch, monkeypatch):
    service, tasks = _offered_service(
        scratch, monkeypatch, payload=dict(INSTALLABLE, sha256=None)
    )
    assert service.start_update() is False
    assert tasks.submitted == 0


def test_a_source_checkout_says_so_rather_than_half_installing(scratch, monkeypatch):
    tasks = FakeTasks()
    body = b"installer"
    service, _api = make_service(
        dict(INSTALLABLE, sha256=hashlib.sha256(body).hexdigest()),
        cache=FakeCache(), tasks=tasks,
    )
    service.tick()
    service.tick()
    failures = []
    service.update_failed.connect(lambda message: failures.append(message))

    # `can_install()` is not patched here: the suite runs from source, which is
    # exactly the case it refuses.
    assert service.start_update() is False
    assert tasks.submitted == 0
    assert failures and "installed build" in failures[0]
    assert service.update_state == UpdateState.IDLE


def test_a_check_is_skipped_while_an_install_is_under_way(scratch, monkeypatch):
    monkeypatch.setattr(
        "background_services.update.update_service.launch_installer",
        lambda *a: None,
    )
    service, _tasks = _offered_service(scratch, monkeypatch)
    service.start_update()
    calls_before = service._update_api.calls

    service.tick()

    # A scheduled check must not run underneath an install and re-answer a
    # question the user has already acted on.
    assert service._update_api.calls == calls_before


def test_a_manual_check_is_dropped_while_busy(scratch, monkeypatch):
    monkeypatch.setattr(
        "background_services.update.update_service.launch_installer",
        lambda *a: None,
    )
    service, _tasks = _offered_service(scratch, monkeypatch)
    service.start_update()
    woken = []
    service.wake = lambda: woken.append(1)

    service.check_now()

    assert woken == []


# ---------------------------------------------------------------------------
# What a manual check reports when it finds no update
# ---------------------------------------------------------------------------


def test_a_failed_check_is_not_reported_as_being_up_to_date():
    from ui.dashboard_window import manual_check_outcome

    message, _level, key = manual_check_outcome(None, "1.0.1")
    assert key == "update-check-failed"
    assert "could not check" in message


def test_a_deployment_with_no_published_release_is_reported_as_unknown():
    """The bug this pins: an empty release table is not "you are up to date".

    The backend answered "I do not know what the latest release is", and the
    client rendered that as "1.0.1 is the latest version" -- asserting the one
    fact the user had asked for and that nobody had supplied. Every deployment
    answers this way until its first release is published, so it is the state
    a person testing the feature sees first.
    """
    from ui.dashboard_window import manual_check_outcome

    payload = {"latest_version": None, "update_available": False}
    message, _level, key = manual_check_outcome(payload, "1.0.1")
    assert key == "update-unknown"
    assert "1.0.1 is the latest version" not in message
    assert "No release information" in message


def test_clicking_the_badge_before_the_first_check_goes_and_asks():
    """The bug this pins: "Updates (1)" claiming nothing was published.

    The badge is restored from the durable record the moment the window is
    built, so it is on screen before this session has asked the backend
    anything. The release details behind it are deliberately not persisted, so
    for the first half-minute of a session the badge is real and the details
    are simply not fetched yet. Clicking then must ask, not conclude.
    """
    from ui.dashboard_window import update_menu_action

    assert update_menu_action(None, None, None) == "check"


def test_a_deployment_that_published_no_url_is_still_reported_as_such():
    # The distinction the fix turns on: checked-and-empty is a real fact about
    # the deployment, and stays worth saying.
    from ui.dashboard_window import update_menu_action

    checked = {"latest_version": "1.1.0", "update_available": True}
    assert update_menu_action(None, None, checked) == "no-location"


def test_an_installable_release_opens_the_dialog_not_a_browser():
    from ui.dashboard_window import update_menu_action

    release = ReleaseInfo.from_payload(INSTALLABLE)
    assert update_menu_action(release, "https://x.invalid/a.exe", {}) == "dialog"


def test_an_announce_only_release_falls_back_to_the_browser():
    # An older deployment: a URL but no checksum, so nothing is installable and
    # the download page is the honest destination.
    from ui.dashboard_window import update_menu_action

    checked = {"latest_version": "1.1.0", "update_available": True}
    assert update_menu_action(None, "https://x.invalid/a.exe", checked) == "browser"


def test_being_current_is_only_claimed_when_a_version_was_named():
    from ui.dashboard_window import manual_check_outcome

    payload = {"latest_version": "1.0.1", "update_available": False}
    message, _level, key = manual_check_outcome(payload, "1.0.1")
    assert key == "update-current"
    assert "1.0.1 is the latest version" in message


if __name__ == "__main__":
    pytest.main([__file__])

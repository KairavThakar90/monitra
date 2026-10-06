"""A missing screenshot is never silent: it is captured, retried, or explained.

Production showed windows with tracked time and measured activity and no
screenshot -- one here and there, and in the worst cases a whole afternoon --
while the timer ran and the desktop looked healthy. These tests reproduce each
way the capture pipeline used to lose a window without saying so, and pin the
rule that replaced it:

    Every expected capture ends in an image in the durable queue, a retry that
    is still inside its window, or an explicit recorded outcome for the window.

The failures are injected into the real `ScreenshotService`, real `LocalCache`
and real `TimerService`; only the screen, the clock and the pool are faked.
Each class names the production failure it stands for.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from background_services.screenshot import config, health, scheduler
from background_services.screenshot.capture import MergedCapture
from background_services.screenshot.compositor import Placement
from background_services.screenshot.displays import Display

pytest.importorskip("PIL", reason="Pillow is required to process screenshots")

#: A window boundary, so the window runs [NOW, NOW + 600).
NOW = 1_757_000_400.0
WINDOW = 600
SESSION = {"entry_id": 4021, "task_id": 77, "project_id": 3, "client_op": "op-1"}


def _merge(width: int = 640, height: int = 480) -> MergedCapture:
    from background_services.screenshot import compositor

    display = Display(number=1, left=0, top=0, width=width, height=height, is_primary=True)
    placement = Placement(display=display, pixels=bytes([40, 80, 120, 255] * (width * height)),
                          width=width, height=height)
    return MergedCapture(placements=[placement], bounds=compositor.canvas_bounds([display]),
                         displays=[display])


class Clock:
    """One mutable instant shared by every clock the service reads."""

    def __init__(self, now: float = NOW) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class InlineTasks:
    """The pool, run synchronously, through the same submit/on_success path."""

    def __init__(self) -> None:
        self.submissions = []

    def submit(self, fn, on_success=None, on_error=None, key=None, **kwargs):
        self.submissions.append({"key": key, **kwargs})
        try:
            result = fn()
        except BaseException as exc:  # noqa: BLE001
            if on_error:
                on_error(exc)
            return None
        if on_success:
            on_success(result)
        return object()


class DeferredTasks:
    """A pool whose work finishes only when the test says so."""

    def __init__(self) -> None:
        self.submissions = []
        self.pending = []

    #: Bookkeeping that is local and instantaneous in production; only the
    #: capture and the network fetches are the work a test wants to hold back.
    INLINE_PREFIXES = ("screenshot-event:", "screenshot-status")

    def submit(self, fn, on_success=None, on_error=None, key=None, **kwargs):
        entry = {"fn": fn, "on_success": on_success, "on_error": on_error,
                 "key": key, **kwargs}
        self.submissions.append(entry)
        if str(key).startswith(self.INLINE_PREFIXES):
            result = fn()
            if on_success:
                on_success(result)
            return object()
        self.pending.append(entry)
        return object()

    def finish(self, entry) -> None:
        self.pending.remove(entry)
        try:
            result = entry["fn"]()
        except BaseException as exc:  # noqa: BLE001
            if entry["on_error"]:
                entry["on_error"](exc)
            return
        if entry["on_success"]:
            entry["on_success"](result)

    def captures(self):
        return [e for e in self.pending if str(e["key"]).startswith("screenshot-capture")]


class FakeScreenshotApi:
    def __init__(self, privacy=None):
        self.privacy = privacy if privacy is not None else {}
        self.privacy_calls = 0

    def get_config(self):
        return {"capture_frequency": 10}

    def get_privacy_config(self):
        self.privacy_calls += 1
        return dict(self.privacy)


@pytest.fixture(autouse=True)
def _a_reproducible_plan():
    """The planner picks random instants. One of them can be the very second
    tracking starts (1 in ~600), which takes a capture the test did not ask
    for; a fixed seed keeps every run the same run."""
    import random

    random.seed(20261006)
    yield


@pytest.fixture
def cache_root(tmp_path, monkeypatch):
    from core import paths

    monkeypatch.setenv("MONITRA_DATA_DIR", str(tmp_path / "monitra"))
    paths.reset_cache()
    yield tmp_path / "monitra" / config.CACHE_DIR_NAME
    paths.reset_cache()


@pytest.fixture
def clock(monkeypatch):
    from background_services.screenshot import screenshot_service as module

    fake = Clock()
    monkeypatch.setattr(module, "time", SimpleNamespace(time=fake))
    monkeypatch.setattr(module, "_wall_time", fake)
    return fake


@pytest.fixture
def screen(monkeypatch, clock):
    """A scripted screen: each read pops the next result (None = unreadable)."""
    from background_services.screenshot import screenshot_service as module

    class Screen:
        def __init__(self):
            self.script = []
            self.reads = 0

        def read(self):
            self.reads += 1
            outcome = self.script.pop(0) if self.script else "ok"
            if isinstance(outcome, BaseException):
                raise outcome
            if outcome is None:
                return None
            return _merge()

    fake = Screen()
    monkeypatch.setattr(module.capture, "supported", lambda: True)
    monkeypatch.setattr(module.capture, "capture_all_displays", fake.read)
    return fake


def _service(qapp, cache, tasks, api=None, runtime_extra=None):
    from background_services.screenshot import screenshot_service as module

    runtime = SimpleNamespace(
        storage=cache.storage,
        timer=SimpleNamespace(active_session=lambda: dict(SESSION)),
        tasks=tasks,
        sync=SimpleNamespace(wake=lambda: None),
        session_manager=SimpleNamespace(user_info={"id": 9}),
        **(runtime_extra or {}),
    )
    return module.ScreenshotService(runtime, cache, api)


@pytest.fixture
def service(qapp, cache, cache_root, clock, screen):
    svc = _service(qapp, cache, InlineTasks())
    yield svc
    svc.stop_tracker()


def _fire(svc, clock, at=None):
    """Make the schedule take a capture at `at` (default: now)."""
    if at is not None:
        clock.now = at
    svc._planned_times = [clock.now]
    svc._on_due()


def _advance_to_retry(svc, clock):
    """Move the clock to the retry the service scheduled and let it run."""
    assert svc._planned_times, "no retry was scheduled"
    clock.now = svc._planned_times[0] + 0.1
    svc._on_due()


def _events(cache):
    return cache.get_pending_screenshot_events()


# ── The schedule cannot silently die ──────────────────────────────────────────

class TestTheScheduleCannotDie:
    """Production: after one exception the single-shot timer was never re-armed,
    so a session produced no screenshots for the rest of the day while the timer
    and every other service looked fine."""

    def test_an_exception_in_the_planner_does_not_end_the_schedule(
        self, service, clock, monkeypatch
    ):
        service.start_tracker(SESSION)
        assert service._due_timer.isActive()

        def explode(*args, **kwargs):
            raise RuntimeError("the planner broke")

        monkeypatch.setattr(service, "_plan", explode)
        clock.now = NOW + 700           # a new window, so `_plan` runs
        service._due_timer.stop()
        service._on_due()

        assert service._scheduler_errors == 1
        assert service._due_timer.isActive(), "the timer must be armed again after an error"

    def test_after_the_fault_clears_the_next_wakeup_captures(
        self, service, clock, screen, cache, monkeypatch
    ):
        service.start_tracker(SESSION)
        real_plan = service._plan
        monkeypatch.setattr(service, "_plan", lambda *a, **k: (_ for _ in ()).throw(ValueError("x")))
        clock.now = NOW + 650
        service._on_due()
        monkeypatch.setattr(service, "_plan", real_plan)
        clock.now = NOW + 655
        service._on_due()                             # the fault has cleared: the window is planned
        _fire(service, clock, NOW + 660)
        assert len(cache.get_pending_screenshots()) == 1

    def test_arming_itself_failing_falls_back_to_a_plain_wakeup(self, service, monkeypatch):
        service.start_tracker(SESSION)
        service._due_timer.stop()
        monkeypatch.setattr(service, "_arm", lambda: (_ for _ in ()).throw(OverflowError("big")))
        service._on_due()
        assert service._due_timer.isActive()

    def test_the_watchdog_revives_a_schedule_that_is_not_running(
        self, service, clock, cache
    ):
        service.start_tracker(SESSION)
        service._due_timer.stop()                     # whatever stopped it
        assert not service._due_timer.isActive()

        service._on_health_tick()

        assert service._due_timer.isActive()

    def test_the_watchdog_leaves_a_stopped_service_alone(self, service):
        service.start_tracker(SESSION)
        service.stop_tracker()
        service._on_health_tick()
        assert not service._due_timer.isActive()
        assert not service._health_timer.isActive()


# ── A failed capture is retried inside its window, then explained ─────────────

class TestAFailedCaptureIsNotAWindowLost:
    """Production: `capture_all_displays()` returned None (a locked screen, a
    display that was asleep, a remote-desktop reconnect). The instant had been
    spent and nothing rescheduled, so the window ended with no screenshot and
    no record that one had ever been due."""

    def test_an_unreadable_screen_is_retried_within_the_window(
        self, service, clock, screen, cache
    ):
        service.start_tracker(SESSION)
        screen.script = [None]                         # first read fails
        _fire(service, clock, NOW + 30)

        assert cache.get_pending_screenshots() == []
        assert service._planned_times, "a retry must be scheduled"
        retry_at = service._planned_times[0]
        assert NOW + 30 < retry_at < NOW + WINDOW - service.CAPTURE_RETRY_MARGIN_SECONDS
        assert service._due_timer.isActive()
        assert _events(cache) == [], "one failed attempt is not yet an outcome"

        _advance_to_retry(service, clock)

        assert len(cache.get_pending_screenshots()) == 1, "the retry produced the screenshot"
        assert _events(cache) == [], "a window that ends with an image needs no explanation"

    def test_a_window_whose_every_attempt_fails_is_recorded_not_dropped(
        self, service, clock, screen, cache
    ):
        service.start_tracker(SESSION)
        screen.script = [None] * 20
        _fire(service, clock, NOW + 1)
        while service._planned_times and clock.now < NOW + WINDOW:
            _advance_to_retry(service, clock)

        assert cache.get_pending_screenshots() == []
        events = _events(cache)
        assert len(events) == 1
        event = events[0]
        assert event["event_state"] == "failed"
        assert event["reason"] == "screen_unreadable"
        assert event["attempts"] == service.CAPTURE_MAX_ATTEMPTS
        assert event["window_start"] == scheduler_iso(NOW)
        assert event["time_entry_id"] == 4021
        assert screen.reads == service.CAPTURE_MAX_ATTEMPTS, "bounded, not retried forever"

    def test_retries_stop_before_they_could_land_in_the_next_window(
        self, service, clock, screen
    ):
        service.start_tracker(SESSION)
        screen.script = [None]
        _fire(service, clock, NOW + 596)              # four seconds left
        # No retry is scheduled that the next window would claim as its own.
        assert all(t < NOW + WINDOW for t in service._planned_times)
        assert service._outcome.reported, "no time left: it is recorded now"

    def test_the_next_window_starts_clean_and_clears_the_failure(
        self, service, clock, screen, cache
    ):
        service.start_tracker(SESSION)
        screen.script = [None] * 10
        _fire(service, clock, NOW + 1)
        while service._planned_times and clock.now < NOW + WINDOW:
            _advance_to_retry(service, clock)
        assert service._consecutive_failed_windows == 1

        screen.script = []                             # the screen works again
        clock.now = NOW + WINDOW + 5
        service._on_due()
        _fire(service, clock)

        assert len(cache.get_pending_screenshots()) == 1
        assert service._consecutive_failed_windows == 0

    def test_a_window_that_ends_with_a_retry_pending_is_recorded_at_the_boundary(
        self, service, clock, screen, cache
    ):
        service.start_tracker(SESSION)
        screen.script = [None]
        _fire(service, clock, NOW + 560)
        assert service._planned_times and _events(cache) == []

        clock.now = NOW + WINDOW + 1                   # the window ends first
        service._on_due()

        events = _events(cache)
        assert [e["event_state"] for e in events] == ["failed"]
        assert events[0]["window_start"] == scheduler_iso(NOW)

    def test_stopping_with_a_failure_outstanding_records_it(
        self, service, clock, screen, cache
    ):
        service.start_tracker(SESSION)
        screen.script = [None]
        _fire(service, clock, NOW + 100)
        service.stop_tracker()
        events = _events(cache)
        assert len(events) == 1 and events[0]["event_state"] == "failed"
        assert events[0]["time_entry_id"] == 4021, "recorded before the session's id is revoked"

    def test_an_exception_in_the_capture_is_a_failure_not_a_vanished_window(
        self, service, clock, screen, cache
    ):
        service.start_tracker(SESSION)
        screen.script = [OSError("gdi32.GetDIBits() failed")]
        _fire(service, clock, NOW + 30)
        assert service._planned_times, "retried"
        assert service._last_failure_reason == "capture_exception"

    @pytest.mark.parametrize("stage,reason", [
        ("encode", "encode_failed"),
        ("write", "store_failed"),
        ("queue", "queue_failed"),
    ])
    def test_every_stage_after_the_grab_reports_a_failure_reason(
        self, service, clock, screen, cache, monkeypatch, stage, reason
    ):
        from background_services.screenshot import screenshot_service as module

        if stage == "encode":
            monkeypatch.setattr(module.image_processor, "process_merged", lambda m: None)
        elif stage == "write":
            monkeypatch.setattr(module.store, "write_screenshot", lambda *a, **k: None)
        else:
            def broken(**kwargs):
                raise RuntimeError("database is locked")
            monkeypatch.setattr(cache, "save_screenshot", broken)

        service.start_tracker(SESSION)
        result = service._capture_now(scheduler.window_index(NOW + 10, WINDOW),
                                      service._current_generation())
        assert result["failed"] == reason
        assert "index" in result

    def test_a_failed_queue_write_leaves_no_orphan_file(
        self, service, clock, screen, cache, cache_root, monkeypatch
    ):
        def broken(**kwargs):
            raise RuntimeError("database is locked")

        monkeypatch.setattr(cache, "save_screenshot", broken)
        service.start_tracker(SESSION)
        service._capture_now(1, service._current_generation())
        files = list(cache_root.rglob("*.webp")) if cache_root.exists() else []
        assert files == []

    def test_the_budget_is_spent_only_by_a_capture_that_exists(
        self, service, clock, screen, cache
    ):
        service.start_tracker(SESSION)
        screen.script = [None]
        _fire(service, clock, NOW + 30)
        assert service._spent(scheduler.window_index(NOW, WINDOW)) == 0

    def test_the_image_is_stamped_when_the_screen_was_read_not_when_it_finished_encoding(
        self, service, clock, screen, cache, monkeypatch
    ):
        """Production: `captured_at` was taken after the encode, so a capture
        grabbed in the last second of a window was filed in the next one and the
        window it was taken for showed activity and no screenshot."""
        from datetime import datetime, timezone

        from background_services.screenshot import screenshot_service as module

        stamps = []
        real_datetime = module.datetime

        class Recording(real_datetime):
            @classmethod
            def now(cls, tz=None):
                stamps.append("stamp")
                return real_datetime.now(tz)

        order = []
        monkeypatch.setattr(module, "datetime", Recording)
        real_process = module.image_processor.process_merged

        def process(merged):
            order.append(("encode", len(stamps)))
            return real_process(merged)

        monkeypatch.setattr(module.image_processor, "process_merged", process)
        service.start_tracker(SESSION)
        service._capture_now(1, service._current_generation())
        assert order and order[0][1] >= 1, "the capture instant was taken before the encode ran"


def scheduler_iso(epoch: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


# ── A capture that never returns ──────────────────────────────────────────────

class TestAStuckCaptureCannotBlockEveryLaterOne:
    """Production: the capture ran under one de-duplication key. A call that
    never returned (a wedged display call, UI Automation on a hung browser, a
    machine that slept mid-grab) held the key for ever, and every later capture
    was dropped with a debug-level message -- the long runs of "No capture"."""

    @pytest.fixture
    def deferred(self, qapp, cache, cache_root, clock, screen):
        tasks = DeferredTasks()
        svc = _service(qapp, cache, tasks)
        yield svc, tasks
        svc.stop_tracker()

    def test_a_capture_still_running_does_not_cost_the_window_its_instant(
        self, deferred, clock
    ):
        svc, tasks = deferred
        svc.start_tracker(SESSION)
        _fire(svc, clock, NOW + 10)
        assert len(tasks.captures()) == 1

        _fire(svc, clock, NOW + 20)                   # the next instant, capture still running
        assert len(tasks.captures()) == 1, "never two at once"
        assert svc._planned_times and svc._planned_times[0] <= clock.now + 6, (
            "the instant is kept and re-examined shortly, not dropped"
        )

    def test_a_capture_that_never_returns_is_abandoned_and_the_window_retried(
        self, deferred, clock, cache
    ):
        svc, tasks = deferred
        svc.start_tracker(SESSION)
        _fire(svc, clock, NOW + 10)
        first = tasks.captures()[0]

        clock.now = NOW + 10 + svc.CAPTURE_STUCK_SECONDS + 1
        svc._on_health_tick()                         # the watchdog notices

        assert svc._inflight is None
        assert svc._last_failure_reason == "capture_stuck"
        assert svc._planned_times, "a retry is scheduled"

        _advance_to_retry(svc, clock)
        second = [e for e in tasks.captures() if e is not first]
        assert len(second) == 1, "a new capture is submitted"
        assert second[0]["key"] != first["key"], (
            "under its own key, so the wedged task cannot block it"
        )

    def test_the_abandoned_capture_reporting_back_late_is_still_honoured(
        self, deferred, clock, cache, screen
    ):
        svc, tasks = deferred
        svc.start_tracker(SESSION)
        _fire(svc, clock, NOW + 10)
        first = tasks.captures()[0]
        clock.now = NOW + 10 + svc.CAPTURE_STUCK_SECONDS + 1
        svc._on_health_tick()
        assert svc._abandoned

        tasks.finish(first)                           # it finally completes

        assert not svc._abandoned
        assert len(cache.get_pending_screenshots()) == 1, "its image is queued, not discarded"

    def test_a_stuck_capture_in_a_window_that_ended_is_recorded(
        self, deferred, clock, cache
    ):
        svc, tasks = deferred
        svc.start_tracker(SESSION)
        _fire(svc, clock, NOW + 10)
        clock.now = NOW + WINDOW + 200
        svc._on_due()
        events = _events(cache)
        assert any(e["reason"] == "capture_stuck" and e["event_state"] == "failed"
                   for e in events)

    def test_a_pool_that_refuses_the_work_does_not_spend_the_instant(
        self, qapp, cache, cache_root, clock, screen
    ):
        class Refusing:
            def submit(self, *args, **kwargs):
                return None

        svc = _service(qapp, cache, Refusing())
        try:
            svc.start_tracker(SESSION)
            _fire(svc, clock, NOW + 10)
            assert svc._inflight is None
            assert svc._planned_times, "retried shortly"
        finally:
            svc.stop_tracker()


# ── Sleep, resume ─────────────────────────────────────────────────────────────

class TestSleepAndResume:
    """Production: after a laptop slept, a capture that was on the pool when the
    lid closed never finished and the schedule behind it never recovered."""

    def test_a_resume_abandons_a_capture_that_slept_through_its_window(
        self, qapp, cache, cache_root, clock, screen
    ):
        tasks = DeferredTasks()
        svc = _service(qapp, cache, tasks)
        try:
            svc.start_tracker(SESSION)
            _fire(svc, clock, NOW + 10)
            stale = tasks.captures()[0]

            clock.now = NOW + 3 * 3600                # three hours later
            svc.on_system_resumed(3 * 3600)

            events = _events(cache)
            assert any(e["reason"] == "capture_stuck" and e["window_start"] == scheduler_iso(NOW)
                       for e in events), "the window lost to the sleep is accounted for"
            # And the schedule is alive in the new window.
            assert svc._due_timer.isActive()
            _fire(svc, clock)
            fresh = [e for e in tasks.captures() if e is not stale]
            assert fresh, "a capture is taken in the window the machine woke into"
        finally:
            svc.stop_tracker()

    def test_resume_while_not_tracking_does_nothing(self, service):
        service.on_system_resumed(600)
        assert not service._due_timer.isActive()

    def test_a_resume_with_several_overdue_instants_takes_one_capture(
        self, service, clock, cache
    ):
        service.start_tracker(SESSION)
        service._planned_times = [NOW + 100, NOW + 200, NOW + 300]
        clock.now = NOW + 400
        service.on_system_resumed(900)
        assert len(cache.get_pending_screenshots()) == 1


# ── The privacy gate cannot hold capture for ever ─────────────────────────────

class TestThePrivacyGateSelfHeals:
    """Production risk: capture is held until the privacy configuration has been
    fetched, and the gate opened only for one particular request. If that
    request's callback was dropped (a sign-in landed while it was in flight)
    `_privacy_config_loaded` stayed False and every capture of the process was
    retried every two seconds, for ever, with nothing on screen."""

    def _svc(self, qapp, cache, clock, tasks):
        return _service(qapp, cache, tasks, api=FakeScreenshotApi())

    def test_the_fetch_is_not_dropped_by_a_sign_in_that_lands_mid_flight(
        self, qapp, cache, cache_root, clock, screen
    ):
        tasks = DeferredTasks()
        svc = self._svc(qapp, cache, clock, tasks)
        try:
            svc._refresh_privacy_config()
            entry = tasks.submissions[-1]
            assert entry["key"] == "screenshot-privacy-config"
            assert entry["guard_generation"] is False
        finally:
            svc.stop_tracker()

    def test_any_successful_config_refresh_opens_the_gate(
        self, qapp, cache, cache_root, clock, screen
    ):
        tasks = DeferredTasks()
        svc = self._svc(qapp, cache, clock, tasks)
        try:
            assert svc._privacy_config_loaded is False
            svc._refresh_config()
            tasks.finish(tasks.pending[-1])
            assert svc._privacy_config_loaded is True
        finally:
            svc.stop_tracker()

    def test_a_held_capture_asks_for_the_configuration_again(
        self, qapp, cache, cache_root, clock, screen
    ):
        tasks = DeferredTasks()
        svc = self._svc(qapp, cache, clock, tasks)
        try:
            svc.start_tracker(SESSION)
            clock.now = NOW + 100
            svc._on_captured({"excluded": True, "reason": "privacy_config_pending"})
            first_fetches = [e for e in tasks.submissions if e["key"] == "screenshot-privacy-config"]
            assert len(first_fetches) == 1

            clock.now += svc.PRIVACY_REFETCH_SECONDS + 1
            svc._on_captured({"excluded": True, "reason": "privacy_config_pending"})
            fetches = [e for e in tasks.submissions if e["key"] == "screenshot-privacy-config"]
            assert len(fetches) == 2, "the gate is not left waiting on a request that may be gone"
        finally:
            svc.stop_tracker()

    def test_a_gate_held_for_minutes_is_reported_not_silent(
        self, qapp, cache, cache_root, clock, screen
    ):
        tasks = DeferredTasks()
        svc = self._svc(qapp, cache, clock, tasks)
        try:
            svc.start_tracker(SESSION)
            clock.now = NOW + 10
            svc._on_captured({"excluded": True, "reason": "privacy_config_pending"})
            clock.now = NOW + 10 + svc.PRIVACY_PENDING_REPORT_SECONDS + 5
            svc._on_captured({"excluded": True, "reason": "privacy_config_pending"})
            assert svc._outcome.held == "privacy_config"

            clock.now = NOW + WINDOW + 1
            svc._on_due()
            events = [e for e in _events(cache)]
            assert [(e["event_state"], e["reason"]) for e in events] == [
                ("blocked", "privacy_config_unavailable")
            ]
        finally:
            svc.stop_tracker()


# ── Held back on purpose: explained, not "No capture" ─────────────────────────

class TestWindowsHeldBackOnPurposeAreExplained:
    def test_a_window_blocked_by_the_os_is_recorded_once(
        self, service, clock, cache
    ):
        service.start_tracker(SESSION)
        clock.now = NOW + 40
        service._on_captured({"blocked": "denied", "detail": "x"})
        service._on_captured({"blocked": "denied", "detail": "x"})
        clock.now = NOW + WINDOW + 1
        service._on_due()
        events = _events(cache)
        assert [(e["event_state"], e["reason"]) for e in events] == [
            ("blocked", "screen_recording_blocked")
        ]

    def test_a_window_held_by_a_privacy_rule_is_recorded_as_such(
        self, service, clock, cache
    ):
        service.start_tracker(SESSION)
        clock.now = NOW + 40
        service._on_captured({"excluded": True,
                              "reason": "application excluded by privacy config (chrome)"})
        clock.now = NOW + WINDOW + 1
        service._on_due()
        assert [(e["event_state"], e["reason"]) for e in _events(cache)] == [
            ("excluded", "privacy_rule")
        ]

    def test_a_held_window_that_then_captures_needs_no_event(
        self, service, clock, cache, screen
    ):
        service.start_tracker(SESSION)
        clock.now = NOW + 40
        service._on_captured({"excluded": True, "reason": "application excluded by privacy config (x)"})
        _fire(service, clock, NOW + 50)
        clock.now = NOW + WINDOW + 1
        service._on_due()
        assert _events(cache) == []
        assert len(cache.get_pending_screenshots()) == 1

    def test_a_machine_that_cannot_capture_says_so_each_window(
        self, service, clock, cache, monkeypatch
    ):
        from background_services.screenshot import screenshot_service as module

        monkeypatch.setattr(module.capture, "supported", lambda: False)
        service.start_tracker(SESSION)
        clock.now = NOW + WINDOW + 1
        service._on_due()
        assert [(e["event_state"], e["reason"]) for e in _events(cache)] == [
            ("unavailable", "capture_unavailable")
        ]

    def test_a_window_with_nothing_wrong_records_nothing(
        self, service, clock, cache, screen
    ):
        service.start_tracker(SESSION)
        _fire(service, clock, NOW + 5)
        clock.now = NOW + WINDOW + 1
        service._on_due()
        assert _events(cache) == []


# ── What the person is told ───────────────────────────────────────────────────

class TestTheDesktopSaysWhatIsHappening:
    def _status(self, svc):
        svc._refresh_status()
        return svc.status()

    def test_a_confirmed_upload_reads_as_uploaded_not_captured(
        self, service, clock, cache, screen
    ):
        service.start_tracker(SESSION)
        _fire(service, clock, NOW + 5)
        # Captured, queued, not yet confirmed: honest about it.
        assert self._status(service)["state"] == health.UPLOADING

        record = cache.get_pending_screenshots()[0]
        cache.complete_screenshot(record["id"])
        cache.save_app_state(config.LAST_UPLOAD_STATE_KEY,
                             {"at": scheduler_iso(NOW + 20), "captured_at": None})
        status = self._status(service)
        assert status["state"] == health.OK
        assert "uploaded" in status["headline"].lower()

    def test_a_failed_window_is_surfaced_and_clears_on_the_next_capture(
        self, service, clock, cache, screen
    ):
        changes = []
        service.status_changed.connect(changes.append)
        service.start_tracker(SESSION)
        screen.script = [None] * 10
        _fire(service, clock, NOW + 1)
        while service._planned_times and clock.now < NOW + WINDOW:
            _advance_to_retry(service, clock)
        assert self._status(service)["state"] == health.FAILED

        screen.script = []
        clock.now = NOW + WINDOW + 5
        service._on_due()
        _fire(service, clock)
        assert self._status(service)["state"] == health.UPLOADING
        assert [c["state"] for c in changes][-2:] == [health.FAILED, health.UPLOADING]

    def test_the_state_is_edge_triggered_not_repeated(self, service, clock, cache, screen):
        changes = []
        service.status_changed.connect(changes.append)
        service.start_tracker(SESSION)
        for _ in range(5):
            service._refresh_status()
        assert len(changes) <= 1

    def test_one_failure_is_not_a_popup_but_a_failed_state_notifies_once(
        self, qapp, cache, cache_root, clock, screen
    ):
        told = []
        svc = _service(qapp, cache, InlineTasks(), runtime_extra={
            "notifications": SimpleNamespace(notify=lambda *a, **k: told.append(k.get("key")))
        })
        try:
            svc.start_tracker(SESSION)
            screen.script = [None] * 10
            _fire(svc, clock, NOW + 1)
            assert told == [], "one failed attempt, with retries to come, says nothing"
            while svc._planned_times and clock.now < NOW + WINDOW:
                _advance_to_retry(svc, clock)
            svc._refresh_status()
            svc._refresh_status()
            assert told.count("screenshot-failed") == 1
        finally:
            svc.stop_tracker()


# ── The pure rules ────────────────────────────────────────────────────────────

class TestHealthRules:
    def q(self, **kw):
        base = {"uploading": 0, "unattributed": 0, "parked": 0,
                "max_retry_count": 0, "oldest_pending_age": 0.0}
        base.update(kw)
        return base

    def test_a_quiet_stopped_timer_says_nothing(self):
        assert health.derive_status(tracking=False, queue=self.q()).state == health.INACTIVE

    def test_a_stopped_timer_with_work_outstanding_still_reports_it(self):
        status = health.derive_status(tracking=False, queue=self.q(uploading=2))
        assert status.state == health.UPLOADING

    def test_a_single_retry_is_not_a_warning(self):
        status = health.derive_status(
            tracking=True, queue=self.q(uploading=1, max_retry_count=1, oldest_pending_age=8))
        assert status.state == health.UPLOADING and status.severity == health.SEVERITY_INFO

    def test_a_long_wait_becomes_retrying(self):
        status = health.derive_status(
            tracking=True, queue=self.q(uploading=1, oldest_pending_age=health.RETRYING_AFTER_SECONDS))
        assert status.state == health.RETRYING and status.severity == health.SEVERITY_WARNING

    def test_many_attempts_become_retrying_even_if_recent(self):
        status = health.derive_status(
            tracking=True,
            queue=self.q(uploading=1, max_retry_count=health.RETRYING_AFTER_ATTEMPTS))
        assert status.state == health.RETRYING

    def test_a_refused_upload_is_a_failure(self):
        assert health.derive_status(tracking=True, queue=self.q(parked=1)).state == health.FAILED

    def test_a_failed_capture_names_its_reason_in_words(self):
        status = health.derive_status(
            tracking=True, consecutive_failed_windows=1, last_failure_reason="screen_unreadable",
            queue=self.q())
        assert status.state == health.FAILED
        assert "screen could not be read" in status.detail
        assert "still being tracked" in status.detail

    def test_blocked_outranks_everything_while_tracking(self):
        status = health.derive_status(
            tracking=True, blocked_state="denied", consecutive_failed_windows=3,
            queue=self.q(uploading=1))
        assert status.state == health.BLOCKED

    def test_ok_requires_a_confirmed_upload_on_record(self):
        waiting = health.derive_status(tracking=True, queue=self.q())
        assert waiting.state == health.WAITING
        ok = health.derive_status(tracking=True, queue=self.q(),
                                  last_upload_at="2026-10-06T05:04:00+00:00")
        assert ok.state == health.OK

    def test_an_unknown_reason_is_shown_not_hidden(self):
        assert health.reason_text("brand_new_reason") == "brand new reason"
        assert health.reason_text(None) == "an unknown problem"

    def test_unattributed_captures_read_as_uploading_not_as_done(self):
        status = health.derive_status(tracking=True, queue=self.q(unattributed=2))
        assert status.state == health.UPLOADING


# ── Timer lifecycle: idle, recovery, stop/start, task switch ──────────────────

class TestTimerLifecycleNeverEndsTheSchedule:
    """The scenarios from the reliability brief, against the real TimerService."""

    @pytest.fixture
    def rig(self, qapp, cache, cache_root, clock, screen):
        from tests.test_break_in_out import _new_timer

        timer = _new_timer(cache)
        tasks = InlineTasks()
        shots = _service(qapp, cache, tasks)
        shots.runtime.timer = timer
        timer.register_tracker(shots)
        yield timer, shots, cache
        timer.stop(timeout_ms=500)
        shots.stop_tracker()

    def test_starting_the_timer_arms_exactly_one_schedule(self, rig, clock):
        timer, shots, cache = rig
        timer.start_tracking(1, 7, "Task A1")
        assert shots._tracking and shots._due_timer.isActive()
        assert shots._health_timer.isActive()

    @pytest.fixture
    def idle_rig(self, qapp, cache, cache_root, clock, screen):
        """The real timer and the real idle service, with capture registered."""
        from tests.test_interruption_idle import _finish, _process
        from tests.test_timer_service import FakeTimeEntryService

        runtime = _process(cache, FakeTimeEntryService(entry_id=42))
        shots = _service(qapp, cache, InlineTasks())
        shots.runtime.timer = runtime.timer
        runtime.timer.register_tracker(shots)
        yield runtime, shots, cache
        shots.stop_tracker()
        _finish(runtime)

    def _go_idle(self, runtime):
        import time as real_time

        # The detector will not claim inactivity from before monitoring began,
        # so the session is made an hour old (as the idle tests themselves do).
        runtime.idle._monitoring_since = real_time.monotonic() - 3600
        runtime.activity.idle = runtime.idle.idle_minutes * 60 + 5
        runtime.idle.tick()
        assert runtime.idle.pending_period() is not None, "the idle popup opened"

    def test_an_idle_period_does_not_end_capture(self, idle_rig, clock):
        """Brief: an idle transition must never end capture. The idle service
        decides about time; it never reaches a tracker."""
        runtime, shots, cache = idle_rig
        runtime.timer.start_tracking(1, 7, "Task")
        generation = shots._current_generation()

        self._go_idle(runtime)

        assert shots._tracking and shots._due_timer.isActive()
        assert shots._current_generation() == generation, "no new session, no aborted capture"
        _fire(shots, clock, NOW + 120)                  # the due instant arrives while idle
        assert len(cache.get_pending_screenshots()) == 1

    def test_keep_and_resume_after_idle_leaves_capture_running(self, idle_rig, clock):
        runtime, shots, cache = idle_rig
        runtime.timer.start_tracking(1, 7, "Task")
        self._go_idle(runtime)
        runtime.idle.resolve(True, "resume")
        assert runtime.timer.is_running()
        assert shots._tracking and shots._due_timer.isActive()
        clock.now = NOW + WINDOW + 30
        shots._on_due()
        _fire(shots, clock)
        assert len(cache.get_pending_screenshots()) == 1

    def test_discard_and_resume_after_idle_leaves_capture_running(self, idle_rig, clock):
        runtime, shots, cache = idle_rig
        runtime.timer.start_tracking(1, 7, "Task")
        self._go_idle(runtime)
        runtime.idle.resolve(False, "resume")
        assert shots._tracking and shots._due_timer.isActive()

    def test_idle_then_stop_ends_capture_and_the_next_start_restores_it(self, idle_rig, clock):
        """The user *chose* to stop; that is a stop. The next start must bring the
        schedule back rather than leave a session with no capture."""
        runtime, shots, cache = idle_rig
        runtime.timer.start_tracking(1, 7, "Task")
        self._go_idle(runtime)
        runtime.idle.resolve(False, "stop")
        assert not shots._tracking
        runtime.timer.start_tracking(1, 7, "Task")
        assert shots._tracking and shots._due_timer.isActive()

    def test_stop_start_cycles_leave_one_schedule_and_no_duplicate_capture(
        self, rig, clock, screen
    ):
        timer, shots, cache = rig
        for _ in range(5):
            timer.start_tracking(1, 7, "Task A1")
            timer.stop_tracking()
        timer.start_tracking(1, 7, "Task A1")
        assert shots._due_timer.isActive()
        _fire(shots, clock, NOW + 20)
        assert len(cache.get_pending_screenshots()) == 1

        # Stop and start again inside the same window: it has already been paid
        # for, so nothing more is planned -- recovery never means a second shot.
        timer.stop_tracking()
        timer.start_tracking(1, 7, "Task A1")
        assert shots._planned_times == []
        clock.now = NOW + 100
        shots._on_due()
        assert len(cache.get_pending_screenshots()) == 1
        assert shots._due_timer.isActive(), "and the schedule is still armed for the next window"

    def test_switching_task_re_attributes_capture_to_the_new_session(self, rig, clock, screen):
        timer, shots, cache = rig
        timer.start_tracking(1, 7, "Task A1")
        first_client_op = shots._client_op
        timer.switch_tracking(1, 8, "Task A2")
        assert shots._tracking and shots._due_timer.isActive()
        assert shots._client_op != first_client_op, "a new session, a new key"
        _fire(shots, clock, NOW + 20)
        row = cache.get_pending_screenshots()[0]
        assert row["time_entry_id"] == shots._entry_id

    def test_recovery_after_a_power_cut_recreates_the_schedule(
        self, qapp, cache, cache_root, clock, screen
    ):
        """A process dies with the timer running; the next process recovers the
        session, and the recovered session must come with a live scheduler."""
        from tests.test_break_in_out import _new_timer

        first = _new_timer(cache)
        first_shots = _service(qapp, cache, InlineTasks())
        first_shots.runtime.timer = first
        first.register_tracker(first_shots)
        first.start_tracking(1, 7, "Task A1")
        first._tick_timer.stop()                        # the power goes: no on_stop, no cleanup
        first_shots._due_timer.stop()
        first_shots._health_timer.stop()

        second = _new_timer(cache)
        second_shots = _service(qapp, cache, InlineTasks())
        second_shots.runtime.timer = second
        second.register_tracker(second_shots)
        try:
            recovered = second.recover(previous_run={"last_heartbeat": 0.0,
                                                    "clean_shutdown": False})
            assert recovered is not None
            assert second_shots._tracking and second_shots._due_timer.isActive()
            _fire(second_shots, clock, NOW + 25)
            assert len(cache.get_pending_screenshots()) == 1
        finally:
            second.stop(timeout_ms=500)
            second_shots.stop_tracker()
            first.stop(timeout_ms=500)

    def test_a_queued_screenshot_survives_the_process_that_took_it(
        self, qapp, cache, cache_root, clock, screen, tmp_path
    ):
        from storage.manager import StorageManager
        from sync.local_cache import LocalCache

        svc = _service(qapp, cache, InlineTasks())
        svc.start_tracker(SESSION)
        _fire(svc, clock, NOW + 10)
        pending = cache.get_pending_screenshots()
        assert len(pending) == 1
        svc.stop_tracker()

        reopened = LocalCache(storage=StorageManager(str(tmp_path / "test-cache.db")))
        survivors = reopened.get_pending_screenshots()
        assert [r["id"] for r in survivors] == [pending[0]["id"]]
        from pathlib import Path

        assert Path(survivors[0]["local_file_path"]).exists(), "the file is there too"

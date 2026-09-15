"""The rule that only an actively tracked task may cause a screenshot.

The whole of this file protects one sentence: **no active running time entry
means zero new captures.** The application being open, the user being logged
in, the window sitting in the system tray and this service being started are
each, on their own, not permission to photograph someone's screen.

The interesting failure is not "does the scheduler start" — that was already
gated. It is the gap between scheduling and capturing. The schedule is armed
on the GUI thread; the capture runs on a pool thread some milliseconds or
minutes later, and a stop can land in between. So these tests assert against
`capture.capture_all_displays` itself: not that the file was deleted, not
that the queue row was skipped, but that **the screen was never read at all**.
An image that is never taken cannot leak; one that is taken and deleted was
still taken.

This is asserted at the whole-capture entry point rather than per display, so
the guarantee holds however many screens are attached: an unauthorised capture
reads *no* display, not merely fewer of them.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from background_services.screenshot import capture, screenshot_service


class _Screen:
    """Records every attempt to read the screen, and returns nothing."""

    def __init__(self):
        self.reads = 0

    def __call__(self):
        self.reads += 1
        return None  # no frame, so nothing downstream runs


@pytest.fixture
def screen(monkeypatch):
    spy = _Screen()
    monkeypatch.setattr(capture, "capture_all_displays", spy)
    return spy


@pytest.fixture
def service(qapp, cache):
    """A ScreenshotService on a throwaway cache, with no runtime services.

    `tasks` is None on purpose for most of these: `_submit_capture` returns
    early without it, which keeps the pool out of tests that are about
    authorisation rather than about threading.
    """
    runtime = SimpleNamespace(tasks=None, timer=SimpleNamespace(active_session=lambda: None))
    svc = screenshot_service.ScreenshotService(runtime, cache)
    yield svc
    svc.stop_tracker()


SESSION = {"entry_id": 4021, "task_id": 77, "project_id": 3}


class TestNothingButTrackingAuthorizes:
    def test_a_freshly_constructed_service_authorizes_nothing(self, service):
        # Constructing and starting the service is what happens on every
        # launch, for every logged-in user, tracking or not.
        allowed, _entry, _client_op, reason = service._check_authorized(service._current_generation())
        assert allowed is False
        assert reason == "timer_stopped"

    def test_an_open_app_that_never_started_a_task_never_reads_the_screen(
        self, service, screen
    ):
        # The user opens Monitra, logs in, and does nothing else for hours.
        # `_on_due` is what a fired schedule timer calls; here nothing ever
        # armed it, so driving it directly is the strongest form of the test.
        for _ in range(50):
            service._on_due()
        assert screen.reads == 0

    def test_starting_the_service_is_not_starting_a_timer(self, service, screen):
        # on_start does recovery and cache pruning. Neither is tracking.
        service.on_start()
        service._on_due()
        assert screen.reads == 0
        assert service._tracking is False

    def test_tracking_is_the_only_thing_that_authorizes(self, service):
        service.start_tracker(SESSION)
        allowed, entry_id, _client_op, _reason = service._check_authorized(service._current_generation())
        assert allowed is True
        assert entry_id == 4021


class TestStopRevokesImmediately:
    def test_a_capture_scheduled_before_the_stop_never_reads_the_screen(
        self, service, screen
    ):
        # The mandatory scenario: start, let a capture be scheduled, stop
        # before it fires, then let it fire. This is the race the generation
        # token exists for — `_capture_now` is running on the pool thread with
        # a generation captured back when the timer was still live.
        service.start_tracker(SESSION)
        scheduled_generation = service._current_generation()

        service.stop_tracker()

        assert service._capture_now(0, scheduled_generation) is None
        assert screen.reads == 0

    def test_the_check_happens_before_the_capture_not_after(self, service, screen):
        # Capturing and then discarding would mean the screen was photographed
        # at a moment the user was not tracking. Deleting the file afterwards
        # does not undo that, so the ordering is the requirement.
        service.start_tracker(SESSION)
        generation = service._current_generation()
        service.stop_tracker()
        service._capture_now(0, generation)
        assert screen.reads == 0

    def test_shutdown_revokes_too(self, service, screen):
        # The runtime drains the task pool after stopping services; a capture
        # still queued there must not photograph the screen on the way out.
        service.start_tracker(SESSION)
        generation = service._current_generation()
        service.on_stop(timeout_ms=1000)
        assert service._capture_now(0, generation) is None
        assert screen.reads == 0

    def test_stopping_twice_is_harmless(self, service):
        service.start_tracker(SESSION)
        service.stop_tracker()
        service.stop_tracker()
        allowed, _entry, _client_op, _reason = service._check_authorized(service._current_generation())
        assert allowed is False


class TestTaskSwitching:
    def test_a_capture_scheduled_for_task_a_cannot_fire_during_task_b(
        self, service, screen
    ):
        service.start_tracker({"entry_id": 100, "task_id": 1})
        generation_a = service._current_generation()

        service.stop_tracker()
        service.start_tracker({"entry_id": 200, "task_id": 2})

        allowed, _entry, _client_op, reason = service._check_authorized(generation_a)
        assert allowed is False
        assert reason == "stale_scheduler_generation"
        assert service._capture_now(0, generation_a) is None
        assert screen.reads == 0

    def test_task_b_captures_are_attributed_to_task_b(self, service):
        service.start_tracker({"entry_id": 100, "task_id": 1})
        service.stop_tracker()
        service.start_tracker({"entry_id": 200, "task_id": 2})

        allowed, entry_id, _client_op, _reason = service._check_authorized(service._current_generation())
        assert allowed is True
        assert entry_id == 200

    def test_a_restart_of_the_same_task_still_invalidates_the_old_generation(
        self, service
    ):
        # Start, stop, start the *same* task again. An entry-id comparison
        # alone would wave this through if the backend reissued the same id;
        # the generation is what actually distinguishes the two sessions.
        service.start_tracker({"entry_id": 100, "task_id": 1})
        stale = service._current_generation()
        service.stop_tracker()
        service.start_tracker({"entry_id": 100, "task_id": 1})

        allowed, _entry, _client_op, reason = service._check_authorized(stale)
        assert allowed is False
        assert reason == "stale_scheduler_generation"


class TestOfflineStartStillWorks:
    def test_binding_a_backend_id_does_not_invalidate_pending_captures(self, service):
        # An offline start has no entry id yet, so captures are legitimately
        # scheduled with None and the id arrives later in the *same* session.
        # Bumping the generation on bind would abort exactly the screenshots
        # that offline capture exists to preserve.
        service.start_tracker({"entry_id": None, "task_id": 1})
        generation = service._current_generation()

        service.bind_entry_id(9001)

        assert service._current_generation() == generation
        allowed, entry_id, _client_op, _reason = service._check_authorized(generation)
        assert allowed is True
        # The capture records the id current at capture time, not the stale
        # None it was scheduled with.
        assert entry_id == 9001

    def test_a_capture_is_authorized_before_any_backend_id_exists(self, service):
        service.start_tracker({"entry_id": None, "task_id": 1})
        allowed, entry_id, _client_op, _reason = service._check_authorized(service._current_generation())
        assert allowed is True
        assert entry_id is None


class TestRecoveryOnLaunch:
    def test_recovery_alone_does_not_authorize_capture(self, service, screen):
        # on_start requeues interrupted uploads and prunes orphans. A launch
        # with no running timer must leave the scheduler inactive — a valid
        # login session is not a tracking session.
        service.on_start()
        allowed, _entry, _client_op, _reason = service._check_authorized(service._current_generation())
        assert allowed is False
        assert screen.reads == 0

    def test_a_recovered_timer_authorizes_through_the_same_single_door(self, service):
        # TimerService.recover() calls start_tracker like every other path,
        # so recovery gets no special case and no second entry point.
        service.on_start()
        service.start_tracker(SESSION)
        allowed, entry_id, _client_op, _reason = service._check_authorized(service._current_generation())
        assert allowed is True
        assert entry_id == 4021


class TestQueueAndCacheStayCleanWhenUnauthorized:
    def test_an_aborted_capture_writes_no_file_and_no_queue_row(
        self, service, screen, cache
    ):
        service.start_tracker(SESSION)
        generation = service._current_generation()
        service.stop_tracker()

        assert service._capture_now(0, generation) is None
        assert screen.reads == 0
        assert cache.get_screenshot_backlog_paths() == []

    def test_already_captured_screenshots_survive_a_stop(self, service, cache):
        # Stopping the timer stops *new* captures. It must not discard a
        # screenshot that was legitimately taken while tracking was live —
        # that one still belongs to the user's tracked time and still uploads.
        cache.save_screenshot(
            client_screenshot_id="kept-1",
            local_file_path="/tmp/ss_kept-1.webp",
            captured_at="2026-09-08T10:00:00+00:00",
            window_start="2026-09-08T10:00:00+00:00",
            width=1000, height=1000, file_size_bytes=1234,
            time_entry_id=4021, monitor_number=1,
        )
        service.start_tracker(SESSION)
        service.stop_tracker()
        assert len(cache.get_screenshot_backlog_paths()) == 1

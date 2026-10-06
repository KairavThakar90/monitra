"""The screenshot service and macOS Screen Recording.

`test_screen_access.py` proves the permission *detection*. These tests prove
what the service does with it: a refused capture reads nothing, queues nothing
and spends none of the window's budget; the user is told once, with an
actionable message; and capture resumes by itself when the permission takes
effect. Nothing here needs a Mac -- the OS answers are scripted -- and nothing
here claims to show what a real Mac answers.
"""
from __future__ import annotations

import time
from types import SimpleNamespace
from typing import List

import pytest

from background_services.notifications import NotificationLevel
from background_services.screenshot import capture, screen_access, screenshot_service
from background_services.screenshot.screen_access import AccessStatus, ScreenAccess

SESSION = {"entry_id": 4021, "task_id": 77, "project_id": 3}


class _Notifications:
    def __init__(self):
        self.sent: List[dict] = []

    def notify(self, message, level="info", title="Monitra", key=None, link=None):
        self.sent.append(
            {"message": message, "level": level, "title": title, "key": key, "link": link}
        )
        return True


class _Tasks:
    """Records submissions; runs them inline when asked to."""

    def __init__(self, run=False):
        self.run = run
        self.keys: List[str] = []

    def submit(self, fn, on_success=None, on_error=None, key=None, **_kw):
        self.keys.append(key)
        if self.run:
            result = fn()
            if on_success is not None:
                on_success(result)


@pytest.fixture
def notifications():
    return _Notifications()


@pytest.fixture
def tasks():
    return _Tasks()


@pytest.fixture
def screen(monkeypatch):
    reads = []
    monkeypatch.setattr(capture, "capture_all_displays", lambda: reads.append(1))
    return reads


@pytest.fixture
def mac_status(monkeypatch):
    """Script what the permission check answers, and count the prompts."""

    class Script:
        status = AccessStatus(ScreenAccess.DENIED, "scripted")
        prompts = 0

    script = Script()
    monkeypatch.setattr(screen_access, "_is_macos", lambda: True)
    monkeypatch.setattr(screen_access, "check_screen_access", lambda: script.status)

    def prompt():
        script.prompts += 1
        return False

    # The low-level call, so the real once-per-process guard is in the path.
    monkeypatch.setattr(screen_access, "_request", prompt)
    return script


@pytest.fixture
def service(qapp, cache, notifications, tasks):
    runtime = SimpleNamespace(
        tasks=tasks,
        timer=SimpleNamespace(active_session=lambda: None),
        notifications=notifications,
    )
    svc = screenshot_service.ScreenshotService(runtime, cache)
    yield svc
    svc.stop_tracker()


class TestARefusedCaptureIsNotAScreenshot:
    def test_nothing_is_read_queued_or_spent(self, service, cache, screen, mac_status):
        service.start_tracker(SESSION)
        generation = service._current_generation()

        result = service._capture_now(0, generation)

        assert result == {"blocked": "denied", "detail": "scripted"}
        assert screen == [], "the screen was read although macOS had refused it"
        assert cache.count_screenshots_by_status() == {} or not any(
            cache.count_screenshots_by_status().values()
        ), "a refused capture queued a row"
        assert service._spent(0) == 0, "a refused capture spent the window's budget"

    def test_restart_required_is_refused_exactly_like_denied(
        self, service, screen, mac_status
    ):
        mac_status.status = AccessStatus(ScreenAccess.RESTART_REQUIRED, "scripted")
        service.start_tracker(SESSION)

        result = service._capture_now(0, service._current_generation())

        assert result["blocked"] == "restart_required"
        assert screen == []

    def test_the_permission_is_checked_after_authorisation_and_privacy(
        self, service, screen, mac_status
    ):
        # A stopped timer must not even reach the permission question: the
        # check is the *second* gate, never a way past the first.
        service.start_tracker(SESSION)
        generation = service._current_generation()
        service.stop_tracker()
        assert service._capture_now(0, generation) is None
        assert screen == []

    def test_a_granted_capture_proceeds_to_read_the_screen(
        self, service, screen, mac_status
    ):
        mac_status.status = AccessStatus(ScreenAccess.GRANTED, "scripted")
        service.start_tracker(SESSION)

        service._capture_now(0, service._current_generation())

        assert screen == [1]

    def test_off_macos_the_gate_is_a_no_op_and_capture_proceeds(
        self, service, screen, monkeypatch
    ):
        # The Windows path: conftest has already made `_is_macos` False.
        monkeypatch.setattr(
            screen_access, "request_screen_access",
            lambda: (_ for _ in ()).throw(AssertionError("Windows must never prompt")),
        )
        service.start_tracker(SESSION)

        service._capture_now(0, service._current_generation())

        assert screen == [1]


class TestTheUserIsToldOnce:
    def test_denied_notifies_once_with_a_link_to_the_switch(
        self, service, notifications, mac_status
    ):
        service.start_tracker(SESSION)

        for _ in range(4):  # four retries in a row
            service._on_captured({"blocked": "denied", "detail": "scripted"})

        assert len(notifications.sent) == 1
        sent = notifications.sent[0]
        assert sent["level"] == NotificationLevel.WARNING
        assert sent["link"] == screen_access.SETTINGS_URL
        assert "Screen & System Audio Recording" in sent["message"]
        assert "timer is not affected" in sent["message"]

    def test_the_system_prompt_is_requested_for_denied_only_once(
        self, service, mac_status
    ):
        service.start_tracker(SESSION)
        service._on_captured({"blocked": "denied"})
        service._on_captured({"blocked": "denied"})
        assert mac_status.prompts == 1

    def test_restart_required_says_so_without_a_misleading_settings_link(
        self, service, notifications, mac_status
    ):
        service.start_tracker(SESSION)
        service._on_captured({"blocked": "restart_required"})

        sent = notifications.sent[0]
        assert sent["link"] is None
        assert "reopen" in sent["message"].lower()
        assert mac_status.prompts == 0, "re-asking would only confuse a granted user"

    def test_the_signal_fires_on_the_edge_not_on_every_retry(self, service, mac_status):
        service.start_tracker(SESSION)
        seen = []
        service.capture_blocked.connect(seen.append)

        service._on_captured({"blocked": "denied"})
        service._on_captured({"blocked": "denied"})
        service._on_captured({"blocked": "restart_required"})   # a different state is a new edge

        assert seen == ["denied", "restart_required"]

    def test_a_new_tracking_session_is_told_afresh(self, service, notifications, mac_status):
        service.start_tracker(SESSION)
        service._on_captured({"blocked": "denied"})
        service.stop_tracker()
        service.start_tracker({"entry_id": 4022, "task_id": 78})
        service._on_captured({"blocked": "denied"})

        assert len(notifications.sent) == 2
        assert mac_status.prompts == 1, "but the OS prompt is still once per process"

    def test_an_unknown_state_is_treated_as_denied_not_ignored(self, service, notifications):
        service.start_tracker(SESSION)
        service._on_captured({"blocked": "something-new"})
        assert len(notifications.sent) == 1


class TestCaptureResumesByItself:
    def test_a_retry_is_planned_ahead_of_everything_else(self, service, mac_status):
        service.start_tracker(SESSION)
        # `start_tracker` plans the window's capture instants at random; one
        # that happens to fall inside the next 30 seconds is rightly kept
        # ahead of the retry, which made this test fail about one run in ten.
        service._planned_times = []
        before = time.time()
        service._on_captured({"blocked": "denied"})

        first = service._planned_times[0]
        assert before + service.PERMISSION_RETRY_SECONDS - 1 <= first
        assert first <= time.time() + service.PERMISSION_RETRY_SECONDS + 1
        assert service._due_timer.isActive()

    def test_repeated_refusals_do_not_pile_up_retries(self, service, mac_status):
        service.start_tracker(SESSION)
        for _ in range(5):
            service._on_captured({"blocked": "denied"})
        near = [t for t in service._planned_times
                if t < time.time() + service.PERMISSION_RETRY_SECONDS + 1]
        assert len(near) <= 5  # each retry replaces the instant it was armed for
        assert service._planned_times == sorted(service._planned_times)

    def test_a_real_capture_ends_the_block_and_says_so(
        self, service, notifications, mac_status
    ):
        service.start_tracker(SESSION)
        service._on_captured({"blocked": "denied"})
        notifications.sent.clear()
        emitted = []
        service.screenshot_captured.connect(emitted.append)

        service._on_captured({"client_screenshot_id": "x", "time_entry_id": 4021})

        assert service._access_blocked_state is None
        assert len(emitted) == 1
        assert [n["key"] for n in notifications.sent] == ["screenshot-permission:restored"]
        assert notifications.sent[0]["level"] == NotificationLevel.SUCCESS

    def test_ordinary_captures_never_announce_a_resumption(
        self, service, notifications, mac_status
    ):
        service.start_tracker(SESSION)
        service._on_captured({"client_screenshot_id": "x"})
        assert notifications.sent == []

    def test_a_privacy_excluded_retry_is_not_mistaken_for_a_resumed_capture(
        self, service, notifications, mac_status
    ):
        service.start_tracker(SESSION)
        service._on_captured({"blocked": "denied"})
        notifications.sent.clear()

        service._on_captured({"excluded": True, "reason": "application excluded"})

        assert service._access_blocked_state == "denied"
        assert notifications.sent == []


class TestStartTellsTheUserImmediately:
    def test_start_probes_on_the_pool_not_on_the_gui_thread(
        self, service, tasks, mac_status
    ):
        service.start_tracker(SESSION)
        assert "screenshot-access-probe" in tasks.keys

    def test_a_denied_probe_notifies_at_start(self, service, notifications, mac_status):
        service.runtime.tasks = _Tasks(run=True)
        service.start_tracker(SESSION)
        assert len(notifications.sent) == 1
        assert notifications.sent[0]["link"] == screen_access.SETTINGS_URL

    def test_a_granted_probe_says_nothing(self, service, notifications, mac_status):
        mac_status.status = AccessStatus(ScreenAccess.GRANTED, "scripted")
        service.runtime.tasks = _Tasks(run=True)
        service.start_tracker(SESSION)
        assert notifications.sent == []

    def test_a_probe_that_lands_after_stop_is_ignored(self, service, notifications, mac_status):
        service.start_tracker(SESSION)
        service.stop_tracker()
        service._on_access_probed(AccessStatus(ScreenAccess.DENIED, "late"))
        assert notifications.sent == []

    def test_windows_does_no_probe_at_all(self, service, tasks):
        # conftest: `_is_macos` is False, so `required()` is False.
        service.start_tracker(SESSION)
        assert "screenshot-access-probe" not in tasks.keys

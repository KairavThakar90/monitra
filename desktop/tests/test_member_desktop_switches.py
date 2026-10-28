"""The per-member desktop switches an administrator sets in User Management.

Four things can be turned off for one member, and each must switch off *its own*
feature and nothing else:

* **screenshot capture** -- `ScreenshotService` (covered in depth by
  `test_screenshot_settings.py`; only its independence is checked here);
* **desktop notifications** -- the administrator's reminders and messages, shown by
  `WellbeingService` from the schedule `NotificationScheduleService` holds;
* **activity tracking** -- keyboard/mouse activity (`ActivityService`) and the
  applications and websites used (`AppUsageService`, `UrlUsageService`);
* **idle detection** -- `IdleService`, which keeps its own switch and is deliberately
  *not* part of "activity": it reads `ActivityService.idle_seconds()`, a plain system
  call that works whether or not a session is being tracked.

The real services are used throughout; only the OS (input listeners, the foreground
window), the clock and the network are faked. Each section says what it pins and the
failure it stands for.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from background_services.activity.activity_service import ActivityService
from background_services.activity.app_usage_service import AppUsageService
from background_services.activity.url_usage_service import UrlUsageService
from background_services.idle.idle_service import IdleService
from background_services.network import NetworkState
from background_services.notifications.schedule_service import (
    MEMBER_ENABLED_STATE_KEY,
    SCHEDULE_STATE_KEY,
    NotificationScheduleService,
    parse_schedule,
)
from background_services.public_api import BackgroundApi
from background_services.screenshot import screenshot_service as screenshot_module
from background_services.wellbeing import WellbeingService
from core.time_format import IST

SESSION = {"entry_id": 101, "client_op": "op-1"}


def _counts(keystrokes=0, clicks=0, movements=0):
    return {"keystrokes": keystrokes, "clicks": clicks, "movements": movements}


class InlineTasks:
    """The task pool, run synchronously through the same submit/on_success path."""

    def __init__(self):
        self.submissions = []

    def submit(self, fn, on_success=None, on_error=None, key=None, **kwargs):
        self.submissions.append(key)
        try:
            result = fn()
        except BaseException as exc:  # noqa: BLE001
            if on_error:
                on_error(exc)
            return None
        if on_success:
            on_success(result)
        return object()


class FakeActivityApi:
    def __init__(self, answer=None, error=None):
        self.answer = {"activity_tracking_enabled": True} if answer is None else answer
        self.error = error
        self.calls = 0

    def get_config(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return dict(self.answer)


# ═══ Activity tracking: keyboard and mouse ═══════════════════════════════════


@pytest.fixture
def activity(qapp):
    runtime = SimpleNamespace(
        timer=MagicMock(active_session=MagicMock(return_value=dict(SESSION))),
        tasks=InlineTasks(),
        api_client=SimpleNamespace(access_token="token"),
    )
    cache = MagicMock()
    api = FakeActivityApi()
    service = ActivityService(runtime, cache, api)
    counter = MagicMock()
    counter.supported = True
    counter.start.return_value = True
    counter.snapshot_and_reset.return_value = _counts()
    counter.drain_watched_presses.return_value = {}
    service._counter = counter
    probe = MagicMock()
    probe.sample.return_value = {"active": True, "mouse": False}
    probe.idle_seconds.return_value = 42.0
    service._probe = probe
    service.test_cache, service.test_api, service.test_counter = cache, api, counter
    return service


class TestActivityIsOnUnlessAnAdministratorTurnsItOff:
    def test_a_machine_that_has_heard_nothing_measures(self, activity):
        assert activity.tracking_enabled() is True
        activity.start_tracker(SESSION)
        assert activity._tracking is True
        activity.test_counter.start.assert_called_once()

    def test_the_sign_in_profile_carries_the_switch(self, activity):
        activity.apply_user_profile({"activity_tracking_enabled": False})
        assert activity.tracking_enabled() is False
        activity.apply_user_profile({"user": {"activity_tracking_enabled": True}})
        assert activity.tracking_enabled() is True

    def test_a_profile_without_the_switch_means_on_not_what_the_last_member_had(self, activity):
        activity.apply_user_profile({"activity_tracking_enabled": False})
        activity.apply_user_profile({"capture_frequency": 10})
        assert activity.tracking_enabled() is True

    @pytest.mark.parametrize("damaged", [None, "false", "0", 0, 1, [], {}])
    def test_only_a_real_boolean_moves_the_switch(self, activity, damaged):
        activity.set_tracking_enabled(False)
        activity.set_tracking_enabled(damaged)
        assert activity.tracking_enabled() is False, "a damaged answer must not turn tracking on"

        activity.set_tracking_enabled(True)
        activity.set_tracking_enabled(damaged)
        assert activity.tracking_enabled() is True, "a damaged answer must not turn tracking off"

    def test_the_change_is_announced_once_per_change(self, activity):
        seen = []
        activity.tracking_enabled_changed.connect(seen.append)

        activity.set_tracking_enabled(False)
        activity.set_tracking_enabled(False)
        activity.set_tracking_enabled(True)
        activity.set_tracking_enabled(True)

        assert seen == [False, True]


class TestWhileActivityTrackingIsOff:
    def test_nothing_is_listened_to_measured_recorded_or_uploaded(self, activity):
        activity.set_tracking_enabled(False)
        activity.start_tracker(SESSION)

        for _ in range(3):
            activity.test_counter.snapshot_and_reset.return_value = _counts(keystrokes=9, clicks=4, movements=50)
            activity.tick()
        activity.stop_tracker()

        assert activity._tracking is False
        activity.test_counter.start.assert_not_called()
        activity.test_cache.save_activity_sample.assert_not_called()
        activity.test_cache.save_unwanted_activity.assert_not_called()
        assert activity.current_percent() == 0

    def test_the_unwanted_activity_rules_do_not_run_either(self, activity):
        activity.set_tracking_enabled(False)
        activity._monitor = MagicMock()
        activity.start_tracker(SESSION)
        activity.tick()
        activity._monitor.start_session.assert_not_called()
        activity._monitor.feed.assert_not_called()

    def test_turning_it_off_mid_session_writes_out_what_was_measured_and_stops_listening(self, activity):
        activity.start_tracker(SESSION)
        activity.test_counter.snapshot_and_reset.return_value = _counts(keystrokes=7, clicks=2, movements=40)
        activity.tick()
        activity.tick()

        activity.set_tracking_enabled(False)

        kwargs = activity.test_cache.save_activity_sample.call_args.kwargs
        assert (kwargs["time_entry_id"], kwargs["window_seconds"], kwargs["keyboard_strokes"]) == (101, 2, 14)
        activity.test_counter.stop.assert_called()
        assert activity._tracking is False

        saved = activity.test_cache.save_activity_sample.call_count
        activity.tick()
        activity.tick()
        assert activity.test_cache.save_activity_sample.call_count == saved, "nothing more is recorded"

    def test_turning_it_back_on_mid_session_resumes_against_the_same_entry(self, activity):
        activity.set_tracking_enabled(False)
        activity.start_tracker(SESSION)
        activity.test_counter.start.assert_not_called()

        activity.set_tracking_enabled(True)

        assert activity._tracking is True
        activity.test_counter.start.assert_called_once()
        activity.test_counter.snapshot_and_reset.return_value = _counts(keystrokes=5)
        activity.tick()
        activity.stop_tracker()
        assert activity.test_cache.save_activity_sample.call_args.kwargs["time_entry_id"] == 101

    def test_switching_it_on_with_no_timer_running_starts_nothing(self, activity):
        activity.set_tracking_enabled(False)
        activity.set_tracking_enabled(True)
        assert activity._tracking is False
        activity.test_counter.start.assert_not_called()

    def test_a_session_that_ended_while_off_is_not_resumed_by_a_later_switch(self, activity):
        activity.set_tracking_enabled(False)
        activity.start_tracker(SESSION)
        activity.stop_tracker()

        activity.set_tracking_enabled(True)

        assert activity._tracking is False

    def test_the_card_is_told_it_is_off_not_that_a_permission_is_missing(self, activity):
        activity.set_tracking_enabled(False)
        assert activity.capture_status()["disabled"] is True
        activity.set_tracking_enabled(True)
        assert activity.capture_status()["disabled"] is False

    def test_a_stale_permission_needed_state_is_cleared_when_tracking_is_switched_off(self, activity):
        activity.test_counter.start.return_value = False
        activity.start_tracker(SESSION)
        assert activity.capture_status()["blocked"] is not None

        activity.set_tracking_enabled(False)

        assert activity.capture_status()["blocked"] is None


class TestIdleDetectionStillWorksWithActivityTrackingOff:
    def test_the_inactivity_reading_is_a_plain_call_that_does_not_depend_on_tracking(self, activity):
        activity.set_tracking_enabled(False)
        activity.start_tracker(SESSION)

        assert activity.idle_seconds() == 42.0

    def test_idle_keeps_its_own_switch_whatever_the_activity_switch_says(self):
        idle = IdleService(MagicMock(), MagicMock())
        idle.apply_user_profile({"idle_enabled": True, "idle_minutes": 7, "activity_tracking_enabled": False})
        assert (idle.idle_enabled, idle.idle_minutes) == (True, 7)

        idle.apply_user_profile({"idle_enabled": False, "idle_minutes": 7, "activity_tracking_enabled": True})
        assert idle.idle_enabled is False


class TestTheActivityConfigRefresh:
    def test_an_administrators_change_reaches_a_running_desktop(self, activity):
        activity.test_api.answer = {"activity_tracking_enabled": False}
        activity._refresh_config()
        assert activity.tracking_enabled() is False

        activity.test_api.answer = {"activity_tracking_enabled": True}
        activity._refresh_config()
        assert activity.tracking_enabled() is True

    def test_an_answer_without_the_switch_changes_nothing(self, activity):
        activity.set_tracking_enabled(False)
        activity.test_api.answer = {}
        activity._refresh_config()
        assert activity.tracking_enabled() is False

    def test_a_failed_refresh_keeps_the_last_good_setting(self, activity):
        activity.set_tracking_enabled(False)
        activity.test_api.error = RuntimeError("offline")
        activity._refresh_config()
        assert activity.tracking_enabled() is False

    def test_the_refresh_is_asked_for_only_when_due_and_signed_in(self, activity):
        asked = []
        activity._config_refresh_due.connect(lambda: asked.append(1))

        activity.tick()
        assert asked == [], "not yet due"

        activity._config_read_at = time.monotonic() - activity.CONFIG_REFRESH_SECONDS - 1
        activity.runtime.api_client.access_token = None
        activity.tick()
        assert asked == [], "nobody is signed in"

        activity.runtime.api_client.access_token = "token"
        activity.tick()
        assert asked == [1]
        activity.tick()
        assert asked == [1], "claimed before it was emitted, so it is not asked for again at once"

    def test_what_the_poll_reports_is_written_into_the_persisted_profile(self, activity):
        """So a restart while offline seeds from what the administrator last set, not from
        the profile as it was at sign-in."""
        remembered = []
        activity.runtime.session_manager = SimpleNamespace(
            user_info={"activity_tracking_enabled": True},
            update_user_info=lambda **fields: remembered.append(fields),
        )
        activity.test_api.answer = {"activity_tracking_enabled": False}
        activity._refresh_config()
        assert remembered == [{"activity_tracking_enabled": False}]
        activity.test_api.answer = {}
        activity._refresh_config()
        assert remembered == [{"activity_tracking_enabled": False}], "an answer without the switch says nothing"

    def test_refresh_now_asks_at_once_but_not_while_signed_out(self, activity):
        activity.runtime.api_client.access_token = None
        activity.refresh_config_now()
        assert activity.test_api.calls == 0
        activity.runtime.api_client.access_token = "token"
        activity.refresh_config_now()
        assert activity.test_api.calls == 1

    def test_the_restored_session_profile_seeds_it_before_any_request_answers(self, activity):
        activity.runtime.session_manager = SimpleNamespace(user_info={"activity_tracking_enabled": False})
        activity.on_start()
        try:
            assert activity.tracking_enabled() is False
        finally:
            activity.on_stop(1000)


# ═══ Activity tracking: applications and websites ════════════════════════════


class Switch:
    """What the app and URL trackers ask: the activity service's switch."""

    def __init__(self, on=True):
        self.on = on

    def tracking_enabled(self):
        return self.on


@pytest.fixture(params=["app_usage", "url_usage"])
def usage(request, qapp):
    switch = Switch()
    runtime = SimpleNamespace(
        activity=switch,
        timer=MagicMock(active_session=MagicMock(return_value=dict(SESSION))),
    )
    cls = AppUsageService if request.param == "app_usage" else UrlUsageService
    service = cls(runtime, MagicMock())
    service.test_switch = switch
    return service


class TestApplicationsAndWebsitesFollowTheActivitySwitch:
    def test_they_are_recorded_when_it_is_on(self, usage):
        usage.start_tracker(SESSION)
        assert usage._tracking is True

    def test_they_are_not_recorded_when_a_session_starts_with_it_off(self, usage):
        usage.test_switch.on = False
        usage.start_tracker(SESSION)

        assert usage._tracking is False
        assert usage.tick() == usage.IDLE_INTERVAL_MS, "the loop idles; nothing is sampled"
        usage.stop_tracker()

    def test_turning_it_off_mid_session_stops_recording_and_on_resumes(self, usage):
        usage.start_tracker(SESSION)
        usage.test_switch.on = False
        usage.on_activity_tracking_changed(False)
        assert usage._tracking is False

        usage.test_switch.on = True
        usage.on_activity_tracking_changed(True)
        assert usage._tracking is True
        assert usage._entry_id == 101, "it resumes against the entry the session already has"

    def test_what_was_open_is_written_out_when_it_is_turned_off(self, usage):
        usage.start_tracker(SESSION)
        flush = "_flush_segment" if isinstance(usage, AppUsageService) else "_flush_session"
        spy = MagicMock()
        setattr(usage, flush, spy)

        usage.on_activity_tracking_changed(False)

        spy.assert_called_once()

    def test_a_change_with_no_session_running_does_nothing(self, usage):
        usage.on_activity_tracking_changed(True)
        assert usage._tracking is False
        usage.on_activity_tracking_changed(False)
        assert usage._tracking is False

    def test_a_session_that_ended_while_off_is_not_resumed(self, usage):
        usage.test_switch.on = False
        usage.start_tracker(SESSION)
        usage.stop_tracker()

        usage.on_activity_tracking_changed(True)

        assert usage._tracking is False

    def test_with_no_activity_service_at_all_they_record_as_they_always_did(self, qapp):
        service = AppUsageService(SimpleNamespace(timer=MagicMock()), MagicMock())
        service.start_tracker(SESSION)
        assert service._tracking is True

    def test_the_real_signal_connects_the_activity_service_to_both_trackers(self, qapp):
        runtime = SimpleNamespace(timer=MagicMock(active_session=MagicMock(return_value=dict(SESSION))))
        activity_service = ActivityService(runtime, MagicMock())
        activity_service._counter = MagicMock(start=MagicMock(return_value=True))
        runtime.activity = activity_service
        app_usage = AppUsageService(runtime, MagicMock())
        url_usage = UrlUsageService(runtime, MagicMock())
        activity_service.tracking_enabled_changed.connect(app_usage.on_activity_tracking_changed)
        activity_service.tracking_enabled_changed.connect(url_usage.on_activity_tracking_changed)
        for tracker in (activity_service, app_usage, url_usage):
            tracker.start_tracker(SESSION)
        assert (activity_service._tracking, app_usage._tracking, url_usage._tracking) == (True, True, True)

        activity_service.set_tracking_enabled(False)
        assert (activity_service._tracking, app_usage._tracking, url_usage._tracking) == (False, False, False)

        activity_service.set_tracking_enabled(True)
        assert (activity_service._tracking, app_usage._tracking, url_usage._tracking) == (True, True, True)


# ═══ Desktop notifications ═══════════════════════════════════════════════════


class FakeCache:
    """An app_state store that round-trips through JSON, like the real one."""

    def __init__(self, store=None):
        self.store = dict(store or {})
        self.saves = []

    def load_app_state(self, key):
        raw = self.store.get(key)
        return json.loads(raw) if raw is not None else None

    def save_app_state(self, key, value):
        self.saves.append(key)
        self.store[key] = json.dumps(value)


def _payload(version=1, enabled=None):
    body = {
        "version": version, "updated_at": "2026-10-01T09:00:00Z", "server_time": "2026-10-01T09:30:12Z",
        "builtin": [], "custom": [],
    }
    if enabled is not None:
        body["enabled"] = enabled
    return body


class FakeScheduleApi:
    def __init__(self, response=None):
        self.response = response

    def get_schedule(self):
        return self.response

    def open_stream(self, since, *, read_timeout, since_config=None):
        from app.api.exceptions import ApiError

        raise ApiError("no change stream", status_code=404)


def make_schedule_service(response=None, cache=None, first_tick=True):
    wakes = []
    runtime = SimpleNamespace(
        api_client=SimpleNamespace(access_token="token"),
        network=SimpleNamespace(network_state=NetworkState.BACKEND_REACHABLE),
        wellbeing=SimpleNamespace(wake=lambda: wakes.append(1)),
        storage=None,
    )
    cache = cache if cache is not None else FakeCache()
    service = NotificationScheduleService(runtime, FakeScheduleApi(response), cache)
    if first_tick:
        service.tick()
    return service, cache, wakes


class TestTheNotificationSwitchOnTheSchedule:
    def test_a_machine_that_has_heard_nothing_shows_notifications(self):
        service, *_ = make_schedule_service()
        assert service.notifications_enabled is True

    def test_the_sign_in_profile_carries_the_switch_and_wakes_the_scheduler_once(self):
        service, _cache, wakes = make_schedule_service()
        before = len(wakes)

        service.apply_user_profile({"notifications_enabled": False})
        service.apply_user_profile({"notifications_enabled": False})

        assert service.notifications_enabled is False
        assert len(wakes) == before + 1, "an edge, not a level"

        service.apply_user_profile({"user": {"notifications_enabled": True}})
        assert service.notifications_enabled is True

    def test_a_profile_without_the_switch_means_on_not_what_the_last_member_had(self):
        service, *_ = make_schedule_service()
        service.apply_user_profile({"notifications_enabled": False})
        service.apply_user_profile({"capture_frequency": 10})
        assert service.notifications_enabled is True

    @pytest.mark.parametrize("damaged", [None, "false", 0, 1, [], {}])
    def test_only_a_real_boolean_moves_it(self, damaged):
        service, *_ = make_schedule_service()
        service.apply_user_profile({"notifications_enabled": False})
        service.apply_user_profile({"notifications_enabled": damaged})
        assert service.notifications_enabled is False

    def test_every_answer_carries_it_and_a_change_is_picked_up_without_the_version_moving(self):
        service, _cache, wakes = make_schedule_service(_payload(version=4, enabled=False))
        service.tick()
        assert service.notifications_enabled is False

        service._api.response = _payload(version=4, enabled=True)   # same schedule, member switched back on
        service._need_fetch = True
        before = len(wakes)
        service.tick()

        assert service.notifications_enabled is True
        assert len(wakes) > before, "the scheduler is woken so it resumes at once"

    def test_an_answer_without_it_changes_nothing(self):
        service, *_ = make_schedule_service(_payload(version=2))
        service.apply_user_profile({"notifications_enabled": False})
        service.tick()
        assert service.notifications_enabled is False

    def test_it_is_remembered_across_a_restart_so_an_offline_start_stays_quiet(self):
        service, cache, _wakes = make_schedule_service(_payload(version=2, enabled=False))
        service.tick()
        assert MEMBER_ENABLED_STATE_KEY in cache.saves

        restarted, _cache, _ = make_schedule_service(cache=cache)    # offline: nothing is fetched

        assert restarted.notifications_enabled is False

    def test_it_is_written_once_per_change_not_once_per_poll(self):
        service, cache, _wakes = make_schedule_service(_payload(version=2, enabled=False))
        for _ in range(4):
            service._need_fetch = True
            service.tick()
        assert cache.saves.count(MEMBER_ENABLED_STATE_KEY) == 1

    def test_the_persisted_copy_never_overwrites_what_the_profile_just_said(self):
        cache = FakeCache({MEMBER_ENABLED_STATE_KEY: json.dumps(False)})
        service, _cache, _wakes = make_schedule_service(cache=cache, first_tick=False)
        service.apply_user_profile({"notifications_enabled": True})     # arrives before the first tick

        service.tick()

        assert service.notifications_enabled is True

    def test_signing_out_forgets_it_so_the_next_member_starts_from_their_own_profile(self):
        service, *_ = make_schedule_service()
        service.apply_user_profile({"notifications_enabled": False})

        service.reset_session()

        assert service.notifications_enabled is True

    def test_the_deployments_schedule_is_untouched_by_a_member_being_off(self):
        service, *_ = make_schedule_service(_payload(version=9, enabled=False))
        service.tick()
        assert service.schedule.version == 9


FRIDAY = datetime(2026, 9, 11, tzinfo=IST)
TICK_SECONDS = WellbeingService.interval_ms / 1000
CLOCK_START = 1000.0


class FakeNotifications:
    def __init__(self):
        self.shown = []

    def notify(self, message, level=None, title=None, key=None, link=None, native=False):
        self.shown.append(key)
        return True


def make_wellbeing(*, enabled, schedule=None, start):
    notifications = FakeNotifications()
    source = SimpleNamespace(
        schedule=None if schedule is None else parse_schedule(schedule),
        ready=True, notifications_enabled=enabled,
    )
    runtime = SimpleNamespace(
        api_client=SimpleNamespace(access_token="token"), notifications=notifications,
        cache=None, storage=None, notification_schedule=source,
    )
    service = WellbeingService(runtime, None)
    service.DEFAULT_MAX_PER_HOUR = 1000
    service._clock = CLOCK_START
    service._ist = start
    service._now_monotonic = lambda: service._clock
    service._now_ist = lambda: service._ist
    return service, notifications, source


def run_as_the_loop_does(service, minutes):
    end = service._clock + minutes * 60
    while service._clock < end:
        delay_ms = service.tick()
        step = (service.interval_ms if delay_ms is None else delay_ms) / 1000.0
        service._clock += step
        service._ist += timedelta(seconds=step)


class TestAMemberWhoIsOffIsShownNothing:
    LUNCH = FRIDAY.replace(hour=13, minute=29)

    def test_a_member_who_is_on_is_reminded(self):
        service, notifications, _ = make_wellbeing(enabled=True, start=self.LUNCH)
        run_as_the_loop_does(service, 5)
        assert "wellbeing:lunch" in notifications.shown

    def test_a_member_who_is_off_is_shown_no_reminder_at_all(self):
        service, notifications, _ = make_wellbeing(enabled=False, start=self.LUNCH)
        run_as_the_loop_does(service, 120)
        assert notifications.shown == []

    def test_the_gate_names_the_reason(self):
        service, _notifications, _ = make_wellbeing(enabled=False, start=self.LUNCH)
        assert "turned off" in service._gate_reason()

    def test_a_custom_notification_and_a_push_are_held_back_too(self):
        schedule = {
            "version": 3, "server_time": "2026-09-11T08:00:00Z", "builtin": [],
            "custom": [{"id": "c1", "title": "Standup", "message": "Now.", "time": "13:30", "weekdays": [4]}],
            "pushes": [{"id": "p1", "title": "Heads up", "message": "Restart at noon.", "seconds_ago": 5}],
        }
        service, notifications, _ = make_wellbeing(enabled=False, schedule=schedule, start=self.LUNCH)
        run_as_the_loop_does(service, 30)
        assert notifications.shown == []

    def test_switching_it_back_on_resumes_the_reminders_without_a_restart(self):
        service, notifications, source = make_wellbeing(enabled=False, start=self.LUNCH)
        run_as_the_loop_does(service, 4)
        assert notifications.shown == []

        source.notifications_enabled = True
        run_as_the_loop_does(service, 5)

        assert "wellbeing:lunch" in notifications.shown

    def test_switching_it_off_mid_day_stops_the_next_reminder(self):
        service, notifications, source = make_wellbeing(enabled=True, start=FRIDAY.replace(hour=13, minute=20))
        run_as_the_loop_does(service, 5)
        source.notifications_enabled = False
        run_as_the_loop_does(service, 30)
        assert "wellbeing:lunch" not in notifications.shown

    def test_a_schedule_source_that_does_not_know_the_switch_changes_nothing(self):
        service, notifications, source = make_wellbeing(enabled=True, start=self.LUNCH)
        del source.notifications_enabled
        run_as_the_loop_does(service, 5)
        assert "wellbeing:lunch" in notifications.shown


# ═══ Each switch acts on its own feature only ════════════════════════════════


def _profile(**off):
    base = {
        "capture_frequency": 10, "idle_enabled": True, "idle_minutes": 5,
        "screenshots_enabled": True, "notifications_enabled": True, "activity_tracking_enabled": True,
    }
    base.update({name: False for name in off})
    return base


@pytest.fixture
def features(qapp):
    """Every feature that holds one of the four switches, real, fed one profile."""
    runtime = SimpleNamespace(
        timer=MagicMock(), tasks=InlineTasks(), api_client=SimpleNamespace(access_token="token"),
        session_manager=SimpleNamespace(user_info=None),
    )
    activity = ActivityService(runtime, MagicMock(), FakeActivityApi())
    schedule, _cache, _wakes = make_schedule_service()
    runtime.activity = activity
    runtime.notification_schedule = schedule
    screenshot = screenshot_module.ScreenshotService(
        SimpleNamespace(tasks=InlineTasks(), timer=MagicMock(), session_manager=SimpleNamespace(user_info={"id": 1})),
        MagicMock(), None,
    )
    idle = IdleService(MagicMock(), MagicMock())
    api = BackgroundApi.__new__(BackgroundApi)
    api._runtime = SimpleNamespace(
        activity=activity, notification_schedule=schedule, screenshot=screenshot, idle=idle,
    )

    def apply(profile):
        # The two the dashboard seeds through their own existing calls, and the new one.
        idle.apply_user_profile(profile)
        screenshot.apply_user_profile(profile)
        api.apply_member_switches_profile(profile)

    def state():
        return {
            "screenshots": screenshot._screenshots_are_enabled(),
            "notifications": schedule.notifications_enabled,
            "activity": activity.tracking_enabled(),
            "idle": idle.idle_enabled,
        }

    return apply, state


@pytest.mark.parametrize("switch,feature", [
    ("screenshots_enabled", "screenshots"),
    ("notifications_enabled", "notifications"),
    ("activity_tracking_enabled", "activity"),
    ("idle_enabled", "idle"),
])
def test_turning_one_switch_off_turns_off_that_feature_and_no_other(features, switch, feature):
    apply, state = features

    apply(_profile(**{switch: False}))

    assert state() == {name: name != feature for name in ("screenshots", "notifications", "activity", "idle")}


def test_every_combination_of_the_four_switches_is_applied_independently(features):
    apply, state = features
    names = ("screenshots_enabled", "notifications_enabled", "activity_tracking_enabled", "idle_enabled")
    features_by_switch = dict(zip(names, ("screenshots", "notifications", "activity", "idle")))

    for mask in range(16):
        off = [name for index, name in enumerate(names) if mask & (1 << index)]
        apply(_profile(**{name: False for name in off}))
        assert state() == {
            features_by_switch[name]: name not in off for name in names
        }, f"switches off: {off}"


def test_the_profile_seed_is_one_call_per_feature_and_does_not_reach_across():
    runtime = SimpleNamespace(
        activity=MagicMock(), notification_schedule=MagicMock(), screenshot=MagicMock(), idle=MagicMock(),
    )
    api = BackgroundApi.__new__(BackgroundApi)
    api._runtime = runtime

    api.apply_member_switches_profile({"activity_tracking_enabled": False})

    runtime.activity.apply_user_profile.assert_called_once_with({"activity_tracking_enabled": False})
    runtime.notification_schedule.apply_user_profile.assert_called_once_with({"activity_tracking_enabled": False})
    runtime.screenshot.apply_user_profile.assert_not_called()
    runtime.idle.apply_user_profile.assert_not_called()


# ═══ Every informational notification, not only the administrator's ══════════
#
# The first version of the notifications switch held back only the scheduled
# reminders, so a member who was switched off still saw "Logged in", "Pending activity
# synced", "Back online" and the rest -- which is what "desktop notifications are off"
# looks like to the person at the machine. The gate is now in `NotificationService`, the
# one place every notification passes through.

from background_services.notifications.notification_service import NotificationLevel, NotificationService  # noqa: E402


def make_notifier(qapp, source=None):
    runtime = SimpleNamespace() if source is None else SimpleNamespace(notification_schedule=source)
    svc = NotificationService(runtime)
    svc._available = True
    svc._tray = MagicMock()
    svc._icon = MagicMock()
    svc._icon.isNull.return_value = False
    svc._popup_enabled = False
    return svc


@pytest.fixture
def off_notifier(qapp):
    source = SimpleNamespace(notifications_enabled=False)
    svc = make_notifier(qapp, source)
    svc.test_source = source
    yield svc
    svc._dismiss_timer.stop()


class TestAMemberWhoIsOffIsShownNoInformationalNotification:
    @pytest.mark.parametrize("level", [NotificationLevel.INFO, NotificationLevel.SUCCESS])
    def test_informational_notifications_are_not_shown(self, off_notifier, level):
        assert off_notifier.notify("Logged in successfully.", level, key="login") is False
        off_notifier._tray.showMessage.assert_not_called()

    @pytest.mark.parametrize("level", [NotificationLevel.WARNING, NotificationLevel.ERROR])
    def test_a_warning_or_an_error_is_still_shown_because_it_needs_the_person(self, off_notifier, level):
        assert off_notifier.notify("Screen Recording permission is needed.", level, key="perm") is True
        off_notifier._tray.showMessage.assert_called_once()

    def test_the_default_level_is_informational_so_it_is_silenced_too(self, off_notifier):
        assert off_notifier.notify("Back online. Syncing pending activity.", key="net") is False

    def test_a_member_who_is_on_is_shown_everything_as_before(self, qapp):
        svc = make_notifier(qapp, SimpleNamespace(notifications_enabled=True))
        for index, level in enumerate((NotificationLevel.INFO, NotificationLevel.SUCCESS,
                                       NotificationLevel.WARNING, NotificationLevel.ERROR)):
            assert svc.notify(f"message {index}", level, key=f"k{index}") is True
        assert svc._tray.showMessage.call_count == 4
        svc._dismiss_timer.stop()

    def test_the_switch_is_read_at_the_moment_of_each_notification(self, off_notifier):
        assert off_notifier.notify("one", key="a") is False
        off_notifier.test_source.notifications_enabled = True
        assert off_notifier.notify("two", key="b") is True
        off_notifier.test_source.notifications_enabled = False
        assert off_notifier.notify("three", key="c") is False

    def test_a_message_that_was_not_shown_does_not_use_up_the_dedupe_or_the_rate_limit(self, off_notifier):
        for _ in range(100):
            off_notifier.notify("Logged in successfully.", key="login")
        off_notifier.test_source.notifications_enabled = True

        assert off_notifier.notify("Logged in successfully.", key="login") is True, (
            "the same key is shown the moment the member is switched back on"
        )

    def test_nothing_can_silence_a_notification_but_an_explicit_false(self, qapp):
        for source in (None, SimpleNamespace(), SimpleNamespace(notifications_enabled=None),
                       SimpleNamespace(notifications_enabled="no"), SimpleNamespace(notifications_enabled=0),
                       MagicMock()):
            svc = make_notifier(qapp, source)
            assert svc.notify("hello", key="h") is True, source
            svc._dismiss_timer.stop()

    def test_the_wellbeing_reminders_are_silenced_by_it_as_well(self, off_notifier):
        # They are INFO notifications, so they are caught here even if the scheduler's
        # own gate were bypassed.
        assert off_notifier.notify("Drink water", NotificationLevel.INFO, title="Hydrate",
                                   key="wellbeing:hydrate", native=True) is False
        off_notifier._tray.showMessage.assert_not_called()

    def test_driven_by_the_real_schedule_service_and_the_sign_in_profile(self, qapp):
        schedule, _cache, _wakes = make_schedule_service()
        svc = make_notifier(qapp, schedule)

        schedule.apply_user_profile({"notifications_enabled": False})
        assert svc.notify("Pending activity synced successfully.", NotificationLevel.SUCCESS, key="sync") is False
        assert svc.notify("You are offline.", NotificationLevel.WARNING, key="offline") is True

        schedule.apply_user_profile({"notifications_enabled": True})
        assert svc.notify("Pending activity synced successfully.", NotificationLevel.SUCCESS, key="sync2") is True
        svc._dismiss_timer.stop()

    def test_the_next_member_to_sign_in_is_not_silenced_by_the_last(self, qapp):
        schedule, _cache, _wakes = make_schedule_service()
        svc = make_notifier(qapp, schedule)
        schedule.apply_user_profile({"notifications_enabled": False})
        assert svc.notify("hello", key="1") is False

        schedule.reset_session()                                   # sign-out wipes the member's switch
        schedule.apply_user_profile({"capture_frequency": 10})     # an older backend's profile: no switch

        assert svc.notify("hello", key="2") is True
        svc._dismiss_timer.stop()

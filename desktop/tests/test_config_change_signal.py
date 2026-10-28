"""An administrator's change reaches a connected desktop in seconds, not minutes.

The per-member switches, the screens captured, the screenshot size and quality and
the privacy rules each reach the desktop through a poll it makes every three minutes
(`ScreenshotService`, `ActivityService`, `IdleService`). That poll stays the guarantee.
On top of it, the notification change stream the desktop already holds open carries
the backend's *desktop configuration revision*: when it moves and the change concerns
this desktop, `NotificationScheduleService.config_changed` fires and the runtime asks
the three owners to re-read now. What is pinned here:

* the stream is asked with the revision last heard, and an event that moves it is
  progress even when the schedule's version did not move (the old "same version
  twice is a failure" rule must not swallow it);
* the signal fires on the edge, only when the change concerns this desktop, and the
  runtime routes it to exactly the three refreshes -- which are the same
  de-duplicated pool requests the three-minute timers make;
* a reconnect re-reads the same three, and a sign-out forgets the previous member's
  privacy configuration (and screen exclusions) so the next member waits for their own;
* what a poll says about a member's switch is written into the persisted profile, and
  the chosen screenshot size and quality are persisted, so a restart while offline
  starts from what the administrator last set rather than the defaults;
* a pushed message that arrives on a same-version answer (a restart inside its
  lifetime, a member switched back on) is adopted and shown, not dropped as "the level";
* an announcement is drawn on Monitra's own card with its heading, and what the
  administrator typed is rendered as text, never as markup.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from app.auth.session import SessionManager
from background_services.network import NetworkState
from background_services.notifications.schedule_service import (
    SCHEDULE_STATE_KEY, NotificationScheduleService, parse_schedule,
)
from tests.test_notification_push import (  # noqa: F401  (fixtures and fakes)
    Clock, FakeCache, StreamApi, make_listener, payload, push, quiet,
)


# ── The stream carries the configuration revision ───────────────────────────


def config_event(version, config, concerns, after=3.0):
    def script(clock):
        yield ": connected"
        yield ""
        clock.t += after
        yield "event: schedule"
        yield "data: " + json.dumps({"version": version, "config": config, "concerns": concerns})
        yield ""
    return script


@pytest.mark.parametrize("line, expected", [
    ('data: {"version": 4}', (4, None, False)),
    ('data: {"version": 4, "config": 9, "concerns": true}', (4, 9, True)),
    ('data: {"version": 4, "config": 9, "concerns": false}', (4, 9, False)),
    ('data: {"version": 4, "config": "9", "concerns": "yes"}', (4, None, False)),
    ('data: {"version": 4, "config": -1}', (4, None, False)),
    ('data: {"config": 9}', None),
    (": ping", None),
])
def test_the_event_carries_the_configuration_revision_and_whether_it_concerns_us(line, expected):
    assert NotificationScheduleService._event_payload(line) == expected


def test_the_stream_is_asked_with_the_revision_last_heard():
    service, api, clock, _, _ = make_listener()
    opened = []
    real = api.open_stream

    @contextmanager
    def recording(since, *, read_timeout, since_config=None):
        opened.append(since_config)
        with real(since, read_timeout=read_timeout, since_config=since_config) as stream:
            yield stream

    api.open_stream = recording
    api.scripts = [config_event(1, 7, False), quiet]
    service.tick()                          # fetch, listen: hears revision 7
    service.tick()                          # fetch again (300 ms later), listen again
    assert opened[0] == 0, "the first stream asks from 0"
    assert opened[1:] and set(opened[1:]) == {7}, "every later one asks from what it heard"


def test_a_configuration_change_that_concerns_this_desktop_fires_the_signal_once():
    service, api, clock, _, logs = make_listener()
    fired = []
    service.config_changed.connect(lambda: fired.append(clock.t))
    api.scripts = [config_event(1, 7, True), quiet]

    delay = service.tick()                  # fetch, listen: hears revision 7
    assert len(fired) == 1
    assert delay == service.EVENT_FETCH_DELAY_MS
    assert not service._need_fetch, "the schedule's version did not move, so it is not fetched again"
    service.tick()                          # a quiet listen
    assert len(fired) == 1
    assert len(api.fetches) == 1, "one schedule read for a configuration change, not one per event"
    assert service._stream_up, "a configuration event is progress, not the same version signalled twice"
    assert any("configuration revision 7 (concerns this desktop)" in m for m in logs.messages)


def test_a_configuration_change_for_someone_else_fires_nothing_but_is_still_progress():
    service, api, clock, _, _ = make_listener()
    fired = []
    service.config_changed.connect(lambda: fired.append(1))
    api.scripts = [config_event(1, 7, False), config_event(1, 8, False), quiet]

    service.tick()                          # fetch; revision 7, not for us
    service.tick()                          # fetch; revision 8, not for us -- same schedule version again
    assert fired == []
    assert service._stream_up and service._stream_failures == 0
    assert service._config_revision == 8


def test_the_same_version_and_revision_signalled_twice_is_still_a_failure():
    service, api, clock, _, _ = make_listener()
    api.scripts = [config_event(1, 7, False), config_event(1, 7, False)]
    service.tick()
    service.tick()
    assert service._stream_failures == 1


def test_an_older_backend_without_the_revision_behaves_exactly_as_before():
    from tests.test_notification_push import event

    service, api, clock, _, _ = make_listener()
    fired = []
    service.config_changed.connect(lambda: fired.append(1))
    api.scripts = [event(2), quiet]
    service.tick()                          # fetch v1; the event for v2
    assert fired == [] and service._config_revision == 0 and service._need_fetch


# ── A pushed message on a same-version answer is adopted ────────────────────


def test_a_push_that_arrives_on_the_same_version_after_a_restart_is_adopted_and_the_scheduler_woken():
    cache = FakeCache()
    cache.save_app_state(SCHEDULE_STATE_KEY, parse_schedule(payload(version=9)).to_json())
    clock = Clock()
    woken = []
    runtime = SimpleNamespace(
        api_client=SimpleNamespace(access_token="token"),
        network=SimpleNamespace(network_state=NetworkState.BACKEND_REACHABLE),
        wellbeing=SimpleNamespace(wake=lambda: woken.append(clock.t)),
        storage=None,
    )
    api = StreamApi(clock)
    api.payload = payload(version=9, pushes=[push()])
    api.scripts = [quiet]
    service = NotificationScheduleService(runtime, api, cache)
    service._monotonic = lambda: clock.t

    service.tick()                          # reads the persisted v9: no pushes
    assert service.schedule.version == 9 and service.schedule.pushes == ()
    woken.clear()
    service.tick()                          # fetches v9 *with* the live push
    assert [p.id for p in service.schedule.pushes] == ["p1"]
    assert woken, "the scheduler is asked to look now"
    woken.clear()
    service.tick()                          # listen (quiet)
    service.check_now()
    service.tick()                          # the same v9 with the same push again: nothing new
    assert woken == []


def test_a_member_switched_back_on_while_a_push_is_live_is_shown_it():
    service, api, clock, _, _ = make_listener()
    api.payload = {**payload(version=1, pushes=[]), "enabled": False}
    service.tick()                          # off: the server empties the pushes
    assert service.schedule.pushes == ()
    api.payload = {**payload(version=1, pushes=[push()]), "enabled": True}
    service.check_now()
    service.tick()
    assert [p.id for p in service.schedule.pushes] == ["p1"]


# ── The runtime routes it to the three owners ───────────────────────────────


def test_the_runtime_asks_the_three_configuration_owners_to_re_read_now(runtime, qapp):
    runtime.api_client.access_token = "token"
    submitted = []
    runtime.tasks.submit = lambda fn, on_success=None, on_error=None, key=None, **kw: submitted.append(key)

    runtime.notification_schedule.config_changed.emit()
    qapp.processEvents()

    assert sorted(k for k in submitted if k in ("screenshot-config", "activity-config", "idle-config")) == [
        "activity-config", "idle-config", "screenshot-config",
    ]


def test_a_reconnect_re_reads_the_same_three(runtime, qapp):
    from background_services.network import NetworkState

    runtime.api_client.access_token = "token"
    submitted = []
    runtime.tasks.submit = lambda fn, on_success=None, on_error=None, key=None, **kw: submitted.append(key)

    runtime._on_network_state_changed(NetworkState.BACKEND_REACHABLE)
    qapp.processEvents()

    for key in ("screenshot-config", "activity-config", "idle-config"):
        assert key in submitted, key


def test_nothing_is_asked_while_signed_out(runtime, qapp):
    runtime.api_client.access_token = None
    submitted = []
    runtime.tasks.submit = lambda fn, on_success=None, on_error=None, key=None, **kw: submitted.append(key)
    runtime.activity.refresh_config_now()
    runtime.idle.refresh_config_now()
    assert "activity-config" not in submitted and "idle-config" not in submitted


def test_sign_out_forgets_the_previous_members_privacy_configuration(runtime):
    screenshot = runtime.screenshot
    screenshot._privacy_config = {"excluded_displays": [2]}
    screenshot._privacy_config_loaded = True
    screenshot._reported_displays = ("someone", (1, 2))

    screenshot.reset_session()

    assert screenshot._privacy_config == {}
    assert screenshot._privacy_config_loaded is False
    assert screenshot._reported_displays is None
    assert screenshot._excluded_displays() == frozenset()


# ── What the polls say is remembered for the next start ─────────────────────


class TestTheSessionRemembersWhatThePollSaid:
    def test_a_field_is_written_into_the_profile_in_memory_and_on_disk(self):
        saved = []
        cache = SimpleNamespace(save_session=lambda token, info, **kw: saved.append((token, dict(info))))
        session = SessionManager(cache)
        session.start_session("tok", {"id": 9, "screenshots_enabled": True})

        session.update_user_info(screenshots_enabled=False)

        assert session.user_info == {"id": 9, "screenshots_enabled": False}
        assert session.access_token == "tok"
        assert saved[-1] == ("tok", {"id": 9, "screenshots_enabled": False})

    def test_an_unchanged_value_writes_nothing(self):
        saved = []
        cache = SimpleNamespace(save_session=lambda token, info, **kw: saved.append(1))
        session = SessionManager(cache)
        session.start_session("tok", {"id": 9, "screenshots_enabled": False})
        session.update_user_info(screenshots_enabled=False)
        assert len(saved) == 1

    def test_nothing_happens_while_signed_out(self):
        session = SessionManager(None)
        session.update_user_info(screenshots_enabled=False)
        assert session.user_info is None


class TestTheScreenshotServiceRemembers:
    @pytest.fixture
    def harness(self, qapp, cache, tmp_path, monkeypatch):
        from tests.test_screenshot_settings import FakeScreenshotApi, InlineTasks
        from background_services.screenshot import screenshot_service as module
        from core import paths

        monkeypatch.setenv("MONITRA_DATA_DIR", str(tmp_path / "monitra"))
        paths.reset_cache()
        remembered = []
        runtime = SimpleNamespace(
            storage=cache.storage,
            timer=SimpleNamespace(active_session=lambda: None),
            tasks=InlineTasks(),
            sync=SimpleNamespace(wake=lambda: None),
            api_client=SimpleNamespace(access_token="token"),
            session_manager=SimpleNamespace(
                user_info={"id": 9}, update_user_info=lambda **f: remembered.append(f),
            ),
        )
        api = FakeScreenshotApi()
        yield SimpleNamespace(module=module, runtime=runtime, api=api, cache=cache, remembered=remembered)
        paths.reset_cache()

    def test_the_switch_a_poll_reports_is_written_into_the_profile(self, harness):
        service = harness.module.ScreenshotService(harness.runtime, harness.cache, harness.api)
        harness.api.config_answer = {"capture_frequency": 10, "screenshots_enabled": False}
        service._refresh_config()
        assert harness.remembered == [{"screenshots_enabled": False}]
        assert service._screenshots_are_enabled() is False
        harness.api.config_answer = {"capture_frequency": 10}
        service._refresh_config()
        assert harness.remembered == [{"screenshots_enabled": False}], "an answer without the switch says nothing"

    def test_the_size_and_quality_are_persisted_and_restored_at_the_next_start(self, harness):
        from background_services.screenshot import config

        service = harness.module.ScreenshotService(harness.runtime, harness.cache, harness.api)
        harness.api.config_answer = {
            "capture_frequency": 10, "image_width": 1280, "image_height": 720, "image_quality": 90,
        }
        service._refresh_config()
        assert harness.cache.load_app_state(harness.module.CAPTURE_SETTINGS_STATE_KEY) == {
            "image_width": 1280, "image_height": 720, "image_quality": 90,
        }

        # The next process: nothing fetched yet, offline.
        harness.api.config_answer = {"capture_frequency": 10}
        fresh = harness.module.ScreenshotService(harness.runtime, harness.cache, harness.api)
        assert fresh._current_capture_settings() == config.DEFAULT_CAPTURE_SETTINGS
        fresh._load_capture_settings()
        assert fresh._current_capture_settings() == config.CaptureSettings(1280, 720, 90)

    def test_a_damaged_persisted_setting_leaves_the_default(self, harness):
        from background_services.screenshot import config

        harness.cache.save_app_state(harness.module.CAPTURE_SETTINGS_STATE_KEY, {"image_width": 99999, "image_height": "x"})
        service = harness.module.ScreenshotService(harness.runtime, harness.cache, harness.api)
        service._load_capture_settings()
        assert service._current_capture_settings() == config.DEFAULT_CAPTURE_SETTINGS

    def test_a_restored_session_reads_the_configuration_at_start_not_three_minutes_later(self, harness):
        from background_services.screenshot import config

        harness.api.config_answer = {
            "capture_frequency": 10, "image_width": 1000, "image_height": 800, "image_quality": None,
        }
        service = harness.module.ScreenshotService(harness.runtime, harness.cache, harness.api)
        service.on_start()
        try:
            assert service._current_capture_settings() == config.CaptureSettings(1000, 800, None)
        finally:
            service.on_stop(1000)

    def test_refresh_now_is_the_same_de_duplicated_request_the_timer_makes(self, harness):
        service = harness.module.ScreenshotService(harness.runtime, harness.cache, harness.api)
        service.refresh_config_now()
        assert [s["key"] for s in harness.runtime.tasks.submissions] == ["screenshot-config"]


# ── The Announcement card ───────────────────────────────────────────────────


class TestTheAnnouncementCard:
    def test_an_announcement_has_the_heading_the_mark_and_a_tinted_surface(self, qapp):
        from PySide6.QtCore import Qt

        from background_services.notifications.toast_popup import ANNOUNCEMENT_HEADING, ToastPopup

        card = ToastPopup()
        try:
            assert card.present("Server restart", "Please save your work.", "announcement")
            assert not card._eyebrow.isHidden()
            assert card._eyebrow.text() == ANNOUNCEMENT_HEADING.upper()
            assert card._title.text() == "Server restart"
            assert card._message.text() == "Please save your work."
            assert not card._logo.pixmap().isNull()
            assert "#6D4AFF" in card.styleSheet() and "#F7F6FF" in card.styleSheet()
            with_eyebrow = card.card_height()

            assert card.present("Drink water", "A glass now.", "info")
            assert card._eyebrow.isHidden()
            assert "#F7F6FF" not in card.styleSheet()
            assert card.card_height() < with_eyebrow
            assert card._title.textFormat() == Qt.TextFormat.PlainText
            assert card._message.textFormat() == Qt.TextFormat.PlainText
        finally:
            card.hide()
            card.deleteLater()

    def test_what_the_administrator_typed_is_text_not_markup(self, qapp):
        from background_services.notifications.toast_popup import ToastPopup

        card = ToastPopup()
        try:
            card.present("<b>Bold</b>", "<a href='x'>link</a> &amp; <i>more</i>", "announcement")
            assert card._title.text() == "<b>Bold</b>"
            assert card._message.text() == "<a href='x'>link</a> &amp; <i>more</i>"
            # Plain text: Qt is told not to guess rich text from the leading tag.
            from PySide6.QtCore import Qt
            assert card._title.textFormat() == Qt.TextFormat.PlainText
        finally:
            card.hide()
            card.deleteLater()

    def test_the_notification_service_sends_an_announcement_to_the_card_and_silences_it_for_a_member_who_is_off(self, qapp):
        from background_services.notifications.notification_service import NotificationLevel, NotificationService

        assert NotificationLevel.ANNOUNCEMENT in NotificationService.SUPPRESSED_WHEN_OFF
        assert NotificationLevel.WARNING not in NotificationService.SUPPRESSED_WHEN_OFF

"""Screenshot Management on the desktop: capture on/off, and the size and quality.

An administrator decides three things in the web admin panel and this process has
to follow them:

* **whether this member is captured at all** -- a switch that must hold from the
  first window of a session (it rides on the sign-in profile) and must follow an
  administrator's change within the periodic configuration refresh;
* **the size** of every screenshot, width and height, not necessarily square;
* **the quality**: Automatic (what the desktop always did) or a fixed WebP quality
  that is honoured -- the 135 KB hard limit that shapes Automatic must not quietly
  turn "90%" into "whatever fits".

What is pinned, and the failure each stands for:

* **A machine that has not heard from the server captures exactly as before** --
  1500x1500, Automatic, on. An older backend that sends none of the keys changes
  nothing, and a damaged value is ignored rather than clamped into something nobody
  chose.
* **Off means nothing is read.** No screen is touched, no image queued, and no
  "unresolved window" event is sent for a window in which no capture was expected.
* **On again resumes at once**, mid-window, without a restart.
* **A member's "off" does not outlive them.** A profile that carries no switch
  (an older backend) resets it to on.
* **The limits are the server's.** The bounds here are compared with
  `backend/app/core/screenshot_geometry.py`, so this client is never stricter than
  the server that stores the image.
"""
from __future__ import annotations

import io
import random
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from background_services.screenshot import compositor, config, health, image_processor, scheduler
from background_services.screenshot.capture import MergedCapture, RawCapture
from background_services.screenshot.compositor import Placement
from background_services.screenshot.displays import Display

pytest.importorskip("PIL", reason="Pillow is required to process screenshots")

NOW = 1_757_000_400.0           # a window boundary: the window runs [NOW, NOW + 600)
WINDOW = 600
SESSION = {"entry_id": 4021, "task_id": 77, "project_id": 3, "client_op": "op-1"}

BACKEND_GEOMETRY = (
    Path(__file__).resolve().parents[2] / "backend" / "app" / "core" / "screenshot_geometry.py"
)


# ── Helpers ─────────────────────────────────────────────────────────────────


def _raw(width: int, height: int) -> RawCapture:
    return RawCapture(
        pixels=bytes([40, 80, 120, 255] * (width * height)),
        width=width, height=height, monitor_number=1,
    )


def _noisy(width: int, height: int) -> RawCapture:
    """A frame whose encoded size genuinely responds to quality (seeded noise)."""
    rng = random.Random(20261027)
    return RawCapture(
        pixels=bytes(rng.randrange(256) for _ in range(width * height * 4)),
        width=width, height=height, monitor_number=1,
    )


def _display(number, left, width, height, primary=False):
    return Display(number=number, left=left, top=0, width=width, height=height, is_primary=primary)


def _merged(*displays, noisy=False):
    rng = random.Random(7)
    placements = []
    for d in displays:
        pixels = (
            bytes(rng.randrange(256) for _ in range(d.width * d.height * 4)) if noisy
            else bytes([40, 80, 120, 255] * (d.width * d.height))
        )
        placements.append(Placement(display=d, pixels=pixels, width=d.width, height=d.height))
    return MergedCapture(
        placements=placements, bounds=compositor.canvas_bounds(list(displays)), displays=list(displays),
    )


def _size_of(data: bytes):
    from PIL import Image

    with Image.open(io.BytesIO(data)) as image:
        return image.size


def _backend_constant(name: str) -> int:
    source = BACKEND_GEOMETRY.read_text(encoding="utf-8")
    match = re.search(rf"^{name}\s*=\s*(\d+)\s*$", source, re.MULTILINE)
    assert match, f"{name} is no longer a plain integer in {BACKEND_GEOMETRY.name}"
    return int(match.group(1))


# ── The numbers are the server's ────────────────────────────────────────────


class TestTheLimitsAreTheServers:
    def test_the_bounds_equal_the_backends(self):
        # A client stricter than the server leaves a value the administrator was
        # allowed to save unusable; looser, and a capture the server refuses is
        # parked for ever. They are one set of numbers written twice.
        assert config.MIN_IMAGE_EDGE == _backend_constant("MIN_IMAGE_EDGE")
        assert config.MAX_IMAGE_EDGE == _backend_constant("MAX_IMAGE_EDGE")
        assert config.MIN_IMAGE_QUALITY == _backend_constant("MIN_IMAGE_QUALITY")
        assert config.MAX_IMAGE_QUALITY == _backend_constant("MAX_IMAGE_QUALITY")

    def test_the_defaults_equal_the_backends(self):
        assert config.IMAGE_SIZE == _backend_constant("DEFAULT_IMAGE_EDGE")
        assert config.WEBP_QUALITY_START == _backend_constant("AUTOMATIC_IMAGE_QUALITY")

    def test_a_fixed_quality_file_stays_inside_what_the_server_accepts(self):
        source = (BACKEND_GEOMETRY.parents[1] / "core" / "config.py").read_text(encoding="utf-8")
        upload_limit = eval(re.search(r"SCREENSHOT_MAX_UPLOAD_BYTES:\s*int\s*=\s*([^\n#]+)", source).group(1))
        assert config.FIXED_QUALITY_MAX_BYTES < upload_limit


# ── Reading the server's answer ─────────────────────────────────────────────


class TestParsingTheConfigAnswer:
    def test_nothing_heard_is_the_behaviour_before_the_setting_existed(self):
        assert config.DEFAULT_CAPTURE_SETTINGS == config.CaptureSettings(1500, 1500, None)
        assert config.parse_capture_settings({}) == config.DEFAULT_CAPTURE_SETTINGS
        # An older backend answers only the interval.
        assert config.parse_capture_settings({"capture_frequency": 10}) == config.DEFAULT_CAPTURE_SETTINGS

    def test_a_complete_answer_is_applied(self):
        parsed = config.parse_capture_settings(
            {"image_width": 1280, "image_height": 720, "image_quality": 90}
        )
        assert parsed == config.CaptureSettings(1280, 720, 90)

    def test_a_null_quality_is_automatic_but_an_absent_one_says_nothing(self):
        fixed = config.CaptureSettings(1500, 1500, 75)
        assert config.parse_capture_settings({"image_quality": None}, fixed).quality is None
        assert config.parse_capture_settings({}, fixed).quality == 75, (
            "a key the server did not send must not undo a fixed quality"
        )

    @pytest.mark.parametrize("payload", [
        {"image_width": 100, "image_height": 720},        # below the floor
        {"image_width": 5000, "image_height": 720},       # above the ceiling
        {"image_width": 1280, "image_height": 249},
        {"image_width": 0, "image_height": 0},
        {"image_width": -1280, "image_height": 720},
        {"image_width": "1280", "image_height": 720},     # the server sends numbers
        {"image_width": 1280.0, "image_height": 720},
        {"image_width": True, "image_height": 720},
        {"image_width": None, "image_height": 720},
        {"image_width": 1280},                            # a size is a pair
        {"image_height": 720},
    ])
    def test_a_bad_size_is_ignored_not_clamped(self, payload):
        before = config.CaptureSettings(1000, 800, 60)
        assert config.parse_capture_settings(payload, before) == before

    @pytest.mark.parametrize("quality", [9, 101, 0, -5, "high", "90", 90.5, True, [], {}])
    def test_a_bad_quality_is_ignored_not_clamped(self, quality):
        before = config.CaptureSettings(1500, 1500, 60)
        assert config.parse_capture_settings({"image_quality": quality}, before).quality == 60

    def test_the_boundaries_are_accepted(self):
        for width, height, quality in ((250, 250, 10), (4000, 4000, 100), (250, 4000, 55)):
            parsed = config.parse_capture_settings(
                {"image_width": width, "image_height": height, "image_quality": quality}
            )
            assert (parsed.width, parsed.height, parsed.quality) == (width, height, quality)

    def test_a_good_value_next_to_a_bad_one_is_kept(self):
        parsed = config.parse_capture_settings(
            {"image_width": 1280, "image_height": 720, "image_quality": "high"},
            config.CaptureSettings(1500, 1500, 70),
        )
        assert parsed == config.CaptureSettings(1280, 720, 70)


# ── The image ───────────────────────────────────────────────────────────────


class TestTheSize:
    def test_the_default_is_exactly_what_it_always_was(self):
        processed = image_processor.process(_raw(1920, 1080))
        assert (processed.width, processed.height) == (1500, 1500)

    @pytest.mark.parametrize("width,height", [
        (1280, 720), (720, 1280), (1000, 1000), (1920, 1080), (250, 250), (4000, 250), (2500, 3000),
    ])
    def test_the_image_is_exactly_the_size_chosen_and_really_that_size(self, width, height):
        processed = image_processor.process(
            _raw(1920, 1080), settings=config.CaptureSettings(width, height)
        )
        assert (processed.width, processed.height) == (width, height)
        assert _size_of(processed.data) == (width, height)

    def test_a_widescreen_desktop_is_letterboxed_into_a_wide_canvas_not_squashed(self):
        from PIL import Image

        # 16:9 content in a 16:9 canvas fills it; in a square it would leave bands.
        wide = image_processor.process(_raw(1920, 1080), settings=config.CaptureSettings(1600, 900))
        with Image.open(io.BytesIO(wide.data)) as image:
            rgb = image.convert("RGB")
            assert rgb.getpixel((800, 5)) != config.PAD_COLOR, "no band at the top of a canvas the content fills"

        tall = image_processor.process(_raw(1920, 1080), settings=config.CaptureSettings(900, 1600))
        with Image.open(io.BytesIO(tall.data)) as image:
            rgb = image.convert("RGB")
            top, middle = rgb.getpixel((450, 20)), rgb.getpixel((450, 800))
        assert all(abs(a - b) <= 6 for a, b in zip(top, config.PAD_COLOR)), "padding above the content"
        assert middle != top

    def test_the_older_square_argument_still_works(self):
        processed = image_processor.process(_raw(1920, 1080), size=1000)
        assert (processed.width, processed.height) == (1000, 1000)

    def test_two_displays_keep_the_desks_shape_inside_the_chosen_box(self):
        desk = _merged(_display(1, 0, 1920, 1080, primary=True), _display(2, 1920, 1920, 1080))
        # The default is unchanged: the primary screen at the density a single one gets.
        assert image_processor.process_merged(desk).width == 3000
        assert image_processor.process_merged(desk).height == 844

        chosen = image_processor.process_merged(desk, settings=config.CaptureSettings(1280, 720))
        # Scale = min(1280/1920, 720/1080) = 2/3 on the primary: both screens at that density.
        assert (chosen.width, chosen.height) == (2560, 720)
        assert chosen.display_count == 2

    def test_one_display_in_a_merged_capture_takes_the_single_display_path_with_the_same_settings(self):
        single = _merged(_display(1, 0, 1920, 1080, primary=True))
        processed = image_processor.process_merged(single, settings=config.CaptureSettings(1280, 720))
        assert (processed.width, processed.height) == (1280, 720)

    def test_the_output_size_for_a_square_box_is_what_it_was_for_an_edge(self):
        displays = [_display(1, 0, 1920, 1080, primary=True), _display(2, 1920, 1280, 1024)]
        bounds = compositor.canvas_bounds(displays)
        for edge in (250, 1000, 1500, 2400):
            assert compositor.output_size(bounds, displays, box=(edge, edge)) == compositor.output_size(
                bounds, displays, edge=edge
            )
        assert compositor.output_size(bounds, displays) == compositor.output_size(
            bounds, displays, edge=config.IMAGE_SIZE
        )


class TestTheQuality:
    def test_a_fixed_quality_is_the_quality_the_image_is_encoded_at(self):
        frame = _noisy(1280, 720)
        low = image_processor.process(frame, settings=config.CaptureSettings(1280, 720, 50))
        high = image_processor.process(frame, settings=config.CaptureSettings(1280, 720, 90))

        assert (low.quality, high.quality) == (50, 90)
        assert high.size_bytes > low.size_bytes, "a higher quality is a bigger file; otherwise the setting does nothing"
        assert (high.fallback_applied, high.limit_applied) == (False, False)

    def test_the_hard_limit_that_shapes_automatic_does_not_override_a_fixed_quality(self):
        frame = _noisy(1280, 720)
        automatic = image_processor.process(frame, settings=config.CaptureSettings(1280, 720, None))
        fixed = image_processor.process(frame, settings=config.CaptureSettings(1280, 720, 90))

        assert fixed.size_bytes > automatic.size_bytes
        assert fixed.size_bytes > image_processor._scaled_budget(
            compositor.max_image_bytes(1), config.CaptureSettings(1280, 720, 90)
        ), "90% on a busy screen is not squeezed back under the Automatic limit"

    def test_automatic_quality_is_the_adaptive_search_it_always_was(self):
        processed = image_processor.process(_noisy(1280, 720))
        assert processed.quality <= config.WEBP_QUALITY_START
        assert processed.size_bytes <= compositor.max_image_bytes(1) or processed.quality == config.HARD_LIMIT_QUALITY_MIN

    def test_a_fixed_quality_that_would_be_refused_by_the_server_is_lowered_until_it_fits(self, monkeypatch):
        # Above the upload limit the server answers 413, which is parked for ever:
        # a smaller picture that uploads beats the exact one that never does.
        monkeypatch.setattr(config, "FIXED_QUALITY_MAX_BYTES", 600_000)
        processed = image_processor.process(_noisy(1280, 720), settings=config.CaptureSettings(1280, 720, 100))

        assert processed.quality < 100
        assert processed.size_bytes <= 600_000 or processed.quality == config.MIN_IMAGE_QUALITY

    def test_a_fixed_quality_inside_the_limit_is_never_lowered(self):
        processed = image_processor.process(_noisy(640, 480), settings=config.CaptureSettings(640, 480, 100))
        assert processed.quality == 100

    def test_merged_captures_honour_a_fixed_quality_too(self):
        desk = _merged(_display(1, 0, 800, 600, primary=True), _display(2, 800, 800, 600), noisy=True)
        processed = image_processor.process_merged(desk, settings=config.CaptureSettings(1000, 1000, 80))
        assert processed.quality == 80 and processed.display_count == 2


class TestTheByteBudgetsFollowTheCanvas:
    def test_the_default_geometry_uses_the_very_same_numbers_as_before(self):
        settings = config.DEFAULT_CAPTURE_SETTINGS
        for budget in (config.TARGET_FILE_BYTES, config.fallback_trigger_bytes(), compositor.max_image_bytes(1)):
            assert image_processor._scaled_budget(budget, settings) == budget

    def test_the_default_geometry_does_not_pass_a_scaled_trigger_to_the_fallback(self, monkeypatch):
        seen = {}
        real = image_processor._apply_fallback

        def spy(canvas, primary, trigger_bytes=None):
            seen["trigger"] = trigger_bytes
            return real(canvas, primary, trigger_bytes=trigger_bytes)

        monkeypatch.setattr(image_processor, "_apply_fallback", spy)
        image_processor.process(_raw(1280, 720))
        assert seen["trigger"] is None, "an unconfigured machine's compression must not move by a byte"

    def test_a_larger_canvas_is_given_proportionally_more_bytes_and_a_smaller_one_fewer(self):
        big = config.CaptureSettings(2400, 2400)
        small = config.CaptureSettings(750, 750)
        base = compositor.max_image_bytes(1)
        assert image_processor._scaled_budget(base, big) == round(base * 2.56)
        assert image_processor._scaled_budget(base, small) == round(base * 0.25)

    def test_no_budget_exceeds_what_the_server_accepts_and_none_collapses(self):
        huge = config.CaptureSettings(4000, 4000)
        # The largest canvas multiplies the ordinary budget about sevenfold -- still
        # under the cap -- and a budget that would pass it is held to it.
        assert image_processor._scaled_budget(config.TARGET_FILE_BYTES, huge) < config.FIXED_QUALITY_MAX_BYTES
        assert image_processor._scaled_budget(10 ** 9, huge) == config.FIXED_QUALITY_MAX_BYTES
        tiny = config.CaptureSettings(250, 250)
        assert image_processor._scaled_budget(1, tiny) >= 1024


# ── The service ─────────────────────────────────────────────────────────────


class Clock:
    def __init__(self, now: float = NOW) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class InlineTasks:
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


class FakeScreenshotApi:
    def __init__(self, config_answer=None):
        self.config_answer = config_answer if config_answer is not None else {"capture_frequency": 10}
        self.privacy = {"applications": [], "urls": [], "excluded_applications": [], "excluded_urls": []}

    def get_config(self):
        return dict(self.config_answer)

    def get_privacy_config(self):
        return dict(self.privacy)


@pytest.fixture(autouse=True)
def _a_reproducible_plan():
    random.seed(20261027)
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
    from background_services.screenshot import screenshot_service as module

    class Screen:
        def __init__(self):
            self.reads = 0

        def read(self):
            self.reads += 1
            return _merged(_display(1, 0, 640, 480, primary=True))

    fake = Screen()
    monkeypatch.setattr(module.capture, "supported", lambda: True)
    monkeypatch.setattr(module.capture, "capture_all_displays", fake.read)
    return fake


@pytest.fixture
def api():
    return FakeScreenshotApi()


@pytest.fixture
def service(qapp, cache, cache_root, clock, screen, api):
    from background_services.screenshot import screenshot_service as module

    runtime = SimpleNamespace(
        storage=cache.storage,
        timer=SimpleNamespace(active_session=lambda: dict(SESSION)),
        tasks=InlineTasks(),
        sync=SimpleNamespace(wake=lambda: None),
        session_manager=SimpleNamespace(user_info={"id": 9}),
    )
    svc = module.ScreenshotService(runtime, cache, api)
    svc._privacy_config_loaded = True
    yield svc
    svc.stop_tracker()


def _fire(svc, clock, at=None):
    if at is not None:
        clock.now = at
    svc._planned_times = [clock.now]
    svc._on_due()


class TestTheMembersSwitch:
    def test_a_machine_that_has_heard_nothing_captures(self, service):
        assert service._screenshots_are_enabled() is True
        assert service._current_capture_settings() == config.DEFAULT_CAPTURE_SETTINGS

    def test_the_sign_in_profile_carries_the_switch(self, service):
        service.apply_user_profile({"capture_frequency": 10, "screenshots_enabled": False})
        assert service._screenshots_are_enabled() is False

        service.apply_user_profile({"user": {"capture_frequency": 10, "screenshots_enabled": True}})
        assert service._screenshots_are_enabled() is True

    def test_a_profile_without_the_switch_means_on_not_what_the_last_person_had(self, service):
        # A member switched off signs out; the next person's backend is older and
        # sends no switch. They must not inherit "off".
        service.apply_user_profile({"capture_frequency": 10, "screenshots_enabled": False})
        service.apply_user_profile({"capture_frequency": 10})
        assert service._screenshots_are_enabled() is True

    @pytest.mark.parametrize("damaged", [None, "false", "0", 0, 1, [], {}])
    def test_only_a_real_boolean_moves_the_switch(self, service, damaged):
        service.apply_user_profile({"screenshots_enabled": False})
        service._set_screenshots_enabled(damaged)
        assert service._screenshots_are_enabled() is False, "a damaged answer must not turn capture on"

        service.apply_user_profile({"screenshots_enabled": True})
        service._set_screenshots_enabled(damaged)
        assert service._screenshots_are_enabled() is True, "a damaged answer must not turn capture off"

    def test_the_periodic_refresh_follows_an_administrators_change(self, service, api):
        api.config_answer = {"capture_frequency": 10, "screenshots_enabled": False}
        service._refresh_config()
        assert service._screenshots_are_enabled() is False

        api.config_answer = {"capture_frequency": 10, "screenshots_enabled": True}
        service._refresh_config()
        assert service._screenshots_are_enabled() is True

    def test_an_answer_with_no_switch_changes_nothing(self, service, api):
        service.apply_user_profile({"screenshots_enabled": False})
        api.config_answer = {"capture_frequency": 10}        # a backend older than Screenshot Management
        service._refresh_config()
        assert service._screenshots_are_enabled() is False


class TestWhileScreenshotsAreOff:
    def test_nothing_is_read_queued_or_reported(self, service, clock, screen, cache):
        service.apply_user_profile({"screenshots_enabled": False})
        service.start_tracker(SESSION)

        _fire(service, clock, NOW + 100)
        _fire(service, clock, NOW + 300)

        assert screen.reads == 0, "the screen is not touched"
        assert cache.get_pending_screenshots() == []
        assert service._planned_times == []

    def test_a_window_that_ends_without_a_capture_is_not_an_unresolved_window(self, service, clock, cache):
        service.apply_user_profile({"screenshots_enabled": False})
        service.start_tracker(SESSION)
        service._on_due()
        clock.now = NOW + WINDOW + 5                  # the next window: the old one is closed
        service._on_due()
        service.stop_tracker()

        assert cache.get_pending_screenshot_events() == [], (
            "no capture was expected, so nothing is explained to the backend"
        )

    def test_the_schedule_stays_armed_so_that_switching_back_on_needs_no_restart(self, service, clock):
        service.apply_user_profile({"screenshots_enabled": False})
        service.start_tracker(SESSION)
        assert service._due_timer.isActive()
        assert service._health_timer.isActive()

    def test_a_capture_already_planned_is_refused_before_the_screen_is_read(self, service, clock, screen):
        service.start_tracker(SESSION)
        generation = service._current_generation()
        service._set_screenshots_enabled(False)

        result = service._capture_now(scheduler.window_index(NOW + 10, WINDOW), generation)

        assert result is None
        assert screen.reads == 0

    def test_switching_off_mid_session_drops_what_was_planned(self, service, clock):
        service.start_tracker(SESSION)
        service._planned_times = [NOW + 200, NOW + 400]

        service._set_screenshots_enabled(False)

        assert service._planned_times == []

    def test_switching_back_on_plans_the_rest_of_the_window_at_once(self, service, clock, screen, cache):
        service.apply_user_profile({"screenshots_enabled": False})
        service.start_tracker(SESSION)
        clock.now = NOW + 50
        service._on_due()
        assert service._planned_times == []

        service._set_screenshots_enabled(True)

        assert service._planned_times, "the window's capture is planned without waiting for the next window"
        _fire(service, clock, NOW + 60)
        assert screen.reads == 1
        assert len(cache.get_pending_screenshots()) == 1

    def test_screenshots_already_taken_still_upload(self, service, clock, screen, cache):
        service.start_tracker(SESSION)
        _fire(service, clock, NOW + 30)
        assert len(cache.get_pending_screenshots()) == 1

        service._set_screenshots_enabled(False)

        assert len(cache.get_pending_screenshots()) == 1, "switching off never discards what exists"

    def test_the_user_is_told_why_nothing_is_captured(self, service):
        service.apply_user_profile({"screenshots_enabled": False})
        service.start_tracker(SESSION)

        status = health.derive_status(tracking=True, disabled=not service._screenshots_are_enabled())

        assert status.state == health.DISABLED
        assert status.severity == health.SEVERITY_INFO
        assert "turned off" in status.headline.lower()
        assert "time is still being tracked" in status.detail


class TestSizeAndQualityReachTheCapture:
    def test_the_refresh_applies_what_the_administrator_chose(self, service, api):
        api.config_answer = {
            "capture_frequency": 10, "screenshots_enabled": True,
            "image_width": 1280, "image_height": 720, "image_quality": 90,
        }
        service._refresh_config()

        assert service._current_capture_settings() == config.CaptureSettings(1280, 720, 90)

    def test_going_back_to_automatic_is_applied(self, service, api):
        service._set_capture_settings(config.CaptureSettings(1280, 720, 90))
        api.config_answer = {"capture_frequency": 10, "image_width": 1280, "image_height": 720, "image_quality": None}
        service._refresh_config()

        assert service._current_capture_settings().quality is None

    def test_a_backend_that_predates_the_setting_changes_nothing(self, service, api):
        service._set_capture_settings(config.CaptureSettings(1280, 720, 90))
        api.config_answer = {"capture_frequency": 10}
        service._refresh_config()

        assert service._current_capture_settings() == config.CaptureSettings(1280, 720, 90)

    def test_a_failed_refresh_keeps_the_last_good_setting(self, service, api):
        service._set_capture_settings(config.CaptureSettings(1280, 720, 90))

        def broken():
            raise RuntimeError("offline")

        api.get_config = broken
        service._refresh_config()

        assert service._current_capture_settings() == config.CaptureSettings(1280, 720, 90)

    def test_the_queued_screenshot_has_the_chosen_size(self, service, clock, screen, cache):
        service._set_capture_settings(config.CaptureSettings(1280, 720, 90))
        service.start_tracker(SESSION)

        _fire(service, clock, NOW + 30)

        rows = cache.get_pending_screenshots()
        assert len(rows) == 1
        assert (rows[0]["width"], rows[0]["height"]) == (1280, 720)
        assert _size_of(open(rows[0]["local_file_path"], "rb").read()) == (1280, 720)

    def test_the_settings_in_force_at_capture_time_are_the_ones_used(self, service, clock, screen, cache, monkeypatch):
        from background_services.screenshot import screenshot_service as module

        seen = []
        real = module.image_processor.process_merged

        def spy(merged, **kwargs):
            seen.append(kwargs.get("settings"))
            return real(merged, **kwargs)

        monkeypatch.setattr(module.image_processor, "process_merged", spy)
        service.start_tracker(SESSION)

        service._set_capture_settings(config.CaptureSettings(1000, 1000, 50))
        _fire(service, clock, NOW + 30)
        service._set_capture_settings(config.CaptureSettings(1280, 720, None))
        clock.now = NOW + WINDOW + 5
        service._on_due()                       # the next window is rolled in and planned
        _fire(service, clock, NOW + WINDOW + 30)

        assert seen == [config.CaptureSettings(1000, 1000, 50), config.CaptureSettings(1280, 720, None)]


class TestTheStatusLine:
    def test_off_while_tracking_is_information_not_a_failure(self):
        status = health.derive_status(tracking=True, disabled=True)
        assert (status.state, status.severity) == (health.DISABLED, health.SEVERITY_INFO)

    def test_nothing_is_said_when_not_tracking(self):
        assert health.derive_status(tracking=False, disabled=True).state == health.INACTIVE

    def test_an_upload_still_in_progress_is_still_told(self):
        status = health.derive_status(
            tracking=True, disabled=True, queue={"uploading": 1, "oldest_pending_age": 5.0},
        )
        assert status.state == health.UPLOADING

    def test_an_earlier_refusal_or_failure_is_no_longer_a_statement_about_now(self):
        for kwargs in ({"blocked_state": "denied"}, {"consecutive_failed_windows": 2, "last_failure_reason": "encode_failed"}):
            assert health.derive_status(tracking=True, disabled=True, **kwargs).state == health.DISABLED
            assert health.derive_status(tracking=True, disabled=False, **kwargs).state in (health.BLOCKED, health.FAILED)

    def test_screenshots_on_is_unchanged(self):
        assert health.derive_status(tracking=True).state == health.WAITING

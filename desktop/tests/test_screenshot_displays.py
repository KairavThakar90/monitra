"""Screenshot Management -> Screens, on the desktop: report the screens, honour the choice.

An administrator can see how many screens a member uses and switch capture off for
individual ones. This process has to do two things for that, and neither may cost
anything the capture pipeline already guarantees:

* **Tell the backend which screens it has** -- edge-triggered, off the GUI thread,
  never for a member who is switched off, and sent again after a failure by the
  next configuration refresh rather than by a retry loop of its own.
* **Leave excluded screens out before reading them.** An excluded screen's pixels
  must never exist in this process, so the test counts the reads the (fake) screen
  actually received, not what ended up in the image.

What is pinned, and the failure each stands for:

* **No exclusion means the call it always was.** `capture_all_displays()` is invoked
  with no arguments, so every existing caller and fake keeps working.
* **Excluding some screens captures the rest as one image**, sized to what is
  captured, and a deliberate omission is not reported as an incomplete capture.
* **Every screen excluded is a recorded decision, not a failure to retry**: nothing
  is read, no retry is scheduled, and the window ends with an `excluded` event whose
  reason is `displays_disabled`, so the grid explains it instead of "No capture".
* **A damaged answer excludes nothing.** Anything that is not a list of whole numbers
  (an older backend sends none) means every screen is captured.
* **Only a change is reported**, per member, and a new member on the same machine
  reports again.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from background_services.screenshot import capture, config
from background_services.screenshot.capture import AllDisplaysExcluded

pytest.importorskip("PIL", reason="Pillow is required to process screenshots")

NOW = 1_757_000_400.0
WINDOW = 600
SESSION = {"entry_id": 4021, "task_id": 77, "project_id": 3, "client_op": "op-1"}


# ── A fake `mss` ─────────────────────────────────────────────────────────────


class _Shot:
    def __init__(self, width, height):
        self.width, self.height = width, height
        self.bgra = bytes([40, 80, 120, 255] * (width * height))


class FakeMss:
    """`mss` for a scripted desk. Records which regions were actually grabbed."""

    def __init__(self, screens):
        #: (left, top, width, height, is_primary) per screen, in OS order.
        self.screens = screens
        self.grabbed = []

    def mss(self):
        outer = self

        class _Session:
            @property
            def monitors(self_inner):
                union = {"left": 0, "top": 0, "width": 1, "height": 1}
                return [union] + [
                    {"left": l, "top": t, "width": w, "height": h, "is_primary": p}
                    for (l, t, w, h, p) in outer.screens
                ]

            def grab(self_inner, region):
                outer.grabbed.append((region["left"], region["top"]))
                return _Shot(region["width"], region["height"])

            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *exc):
                return False

        return _Session()


THREE_SCREENS = [(0, 0, 640, 480, True), (640, 0, 640, 480, False), (-640, 0, 640, 480, False)]


@pytest.fixture
def desk(monkeypatch):
    fake = FakeMss(list(THREE_SCREENS))
    monkeypatch.setattr(capture, "_mss_module", fake)
    return fake


# ── Capture: excluded screens are never read ─────────────────────────────────


class TestCaptureLeavesExcludedScreensOut:

    def test_no_exclusion_reads_every_screen_as_it_always_did(self, desk):
        merged = capture.capture_all_displays()

        assert merged.display_count == 3 and merged.displays_expected == 3 and merged.complete
        assert len(desk.grabbed) == 3

    def test_an_excluded_screen_is_never_read(self, desk):
        merged = capture.capture_all_displays(excluded={2})

        # Screen 2 is the one at left=640. It was not grabbed at all.
        assert (640, 0) not in desk.grabbed
        assert sorted(desk.grabbed) == [(-640, 0), (0, 0)]
        assert sorted(p.display.number for p in merged.placements) == [1, 3]

    def test_a_deliberate_omission_is_not_an_incomplete_capture(self, desk):
        merged = capture.capture_all_displays(excluded={2, 3})

        assert merged.displays_expected == 1 and merged.display_count == 1
        assert merged.complete
        assert desk.grabbed == [(0, 0)]

    def test_the_canvas_is_sized_to_what_is_captured(self, desk):
        everything = capture.capture_all_displays()
        one = capture.capture_all_displays(excluded={2, 3})

        assert one.bounds.width < everything.bounds.width
        assert (one.bounds.width, one.bounds.height) == (640, 480)

    def test_the_primary_can_be_the_one_left_out(self, desk):
        merged = capture.capture_all_displays(excluded={1})

        assert sorted(p.display.number for p in merged.placements) == [2, 3]
        assert (0, 0) not in desk.grabbed

    def test_screen_numbers_that_are_not_attached_change_nothing(self, desk):
        merged = capture.capture_all_displays(excluded={9, 12})

        assert merged.display_count == 3

    def test_every_attached_screen_excluded_is_a_decision_not_a_fault(self, desk):
        with pytest.raises(AllDisplaysExcluded) as caught:
            capture.capture_all_displays(excluded={1, 2, 3})

        assert caught.value.attached == 3
        assert desk.grabbed == [], "nothing may be read when there is nothing to capture"

    def test_an_unreadable_screen_is_still_none_not_an_exception(self, monkeypatch, desk):
        monkeypatch.setattr(desk, "screens", [])

        assert capture.capture_all_displays(excluded={1}) is None

    def test_listing_displays_reads_no_pixels(self, desk):
        listed = capture.list_displays()

        assert [d.number for d in listed] == [1, 2, 3]
        assert [d.is_primary for d in listed] == [True, False, False]
        assert listed[2].left == -640
        assert desk.grabbed == []

    def test_listing_without_a_capture_backend_is_empty_not_an_error(self, monkeypatch):
        monkeypatch.setattr(capture, "_mss_module", False)

        assert capture.list_displays() == []


# ── The service ──────────────────────────────────────────────────────────────


class Clock:
    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now


class InlineTasks:
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


class FakeApi:
    def __init__(self, privacy=None, fail_reports=0):
        self.privacy = privacy if privacy is not None else {}
        self.reports = []
        self.fail_reports = fail_reports

    def get_config(self):
        return {"capture_frequency": 10}

    def get_privacy_config(self):
        return dict(self.privacy)

    def report_displays(self, displays):
        if self.fail_reports:
            self.fail_reports -= 1
            raise OSError("offline")
        self.reports.append(displays)
        return {"excluded_displays": []}


@pytest.fixture(autouse=True)
def _a_reproducible_plan():
    import random

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


def _service(qapp, cache, api, user_id=9):
    from background_services.screenshot import screenshot_service as module

    runtime = SimpleNamespace(
        storage=cache.storage,
        timer=SimpleNamespace(active_session=lambda: dict(SESSION)),
        tasks=InlineTasks(),
        sync=SimpleNamespace(wake=lambda: None),
        session_manager=SimpleNamespace(user_info={"id": user_id}),
    )
    return module.ScreenshotService(runtime, cache, api)


@pytest.fixture
def api():
    return FakeApi()


@pytest.fixture
def service(qapp, cache, cache_root, clock, desk, api):
    svc = _service(qapp, cache, api)
    svc._privacy_config_loaded = True
    yield svc
    svc.stop_tracker()


def _fire(svc, clock, at=NOW + 30):
    clock.now = at
    svc._planned_times = [clock.now]
    svc._on_due()


class TestTheServiceFollowsTheAdministratorsChoice:

    def test_excluded_screens_come_from_the_privacy_config(self, service):
        service._privacy_config = {"excluded_displays": [3, 2, 2]}

        assert service._excluded_displays() == frozenset({2, 3})

    @pytest.mark.parametrize("damaged", [
        None, "2", 2, {"2": True}, [True], [0], [-1], ["2"], [2.5], [None],
    ])
    def test_a_damaged_answer_excludes_nothing(self, service, damaged):
        service._privacy_config = {"excluded_displays": damaged}

        assert service._excluded_displays() == frozenset()

    def test_an_older_backend_with_no_key_excludes_nothing(self, service):
        service._privacy_config = {"applications": [], "urls": []}

        assert service._excluded_displays() == frozenset()

    def test_a_capture_with_no_exclusion_calls_the_grab_exactly_as_before(self, service, monkeypatch):
        calls = []
        monkeypatch.setattr(capture, "capture_all_displays", lambda *a, **k: calls.append((a, k)))

        service._grab_screens()

        assert calls == [((), {})]

    def test_a_capture_with_an_exclusion_passes_it_on(self, service, monkeypatch):
        calls = []
        monkeypatch.setattr(capture, "capture_all_displays", lambda *a, **k: calls.append((a, k)))
        service._privacy_config = {"excluded_displays": [2]}

        service._grab_screens()

        assert calls == [((), {"excluded": frozenset({2})})]

    def test_two_of_three_screens_off_queues_one_image_of_the_remaining_screen(self, service, clock, desk, cache):
        service._privacy_config = {"excluded_displays": [2, 3]}
        service.start_tracker(SESSION)

        _fire(service, clock)

        assert desk.grabbed == [(0, 0)]
        queued = cache.get_pending_screenshots()
        assert len(queued) == 1, "one capture event is one image, however many screens were left out"
        assert queued[0]["display_count"] == 1

    def test_every_screen_off_reads_nothing_retries_nothing_and_records_the_window(
        self, service, clock, desk, cache
    ):
        service._privacy_config = {"excluded_displays": [1, 2, 3]}
        service.start_tracker(SESSION)

        _fire(service, clock)

        assert desk.grabbed == [], "an excluded screen must never be read"
        assert service._planned_times == [], "a decision is not retried inside the window"
        # The window ends without an image, so it is recorded -- once.
        service._finalize_current_outcome()
        events = cache.get_pending_screenshot_events()
        assert [(e["event_state"], e["reason"]) for e in events] == [("excluded", "displays_disabled")]

    def test_it_is_not_counted_as_a_failed_window(self, service, clock, cache):
        service._privacy_config = {"excluded_displays": [1, 2, 3]}
        service.start_tracker(SESSION)

        _fire(service, clock)

        assert service._consecutive_failed_windows == 0
        assert service._last_failure_reason is None


class TestReportingTheScreens:

    def test_a_signed_in_member_reports_the_screens_it_has(self, service, api):
        service._report_displays()

        assert len(api.reports) == 1
        assert [d["number"] for d in api.reports[0]] == [1, 2, 3]
        assert api.reports[0][2]["left"] == -640
        assert [d["is_primary"] for d in api.reports[0]] == [True, False, False]
        assert set(api.reports[0][0]) == {"number", "width", "height", "left", "top", "is_primary"}

    def test_an_unchanged_desk_is_not_reported_again(self, service, api):
        service._report_displays()
        service._report_displays()
        service._report_displays()

        assert len(api.reports) == 1

    def test_a_monitor_plugged_in_is_reported(self, service, api, desk):
        service._report_displays()
        desk.screens.append((1280, 0, 800, 600, False))
        service._report_displays()

        assert [len(r) for r in api.reports] == [3, 4]

    def test_a_failed_report_is_sent_again_by_the_next_refresh(self, qapp, cache, cache_root, clock, desk):
        api = FakeApi(fail_reports=1)
        svc = _service(qapp, cache, api)
        svc._report_displays()
        assert api.reports == []
        svc._report_displays()

        assert len(api.reports) == 1

    def test_a_different_member_on_the_same_machine_reports_again(self, qapp, cache, cache_root, clock, desk):
        api = FakeApi()
        svc = _service(qapp, cache, api, user_id=9)
        svc._report_displays()
        svc.runtime.session_manager.user_info = {"id": 10}
        svc._report_displays()

        assert len(api.reports) == 2

    def test_nothing_is_reported_for_a_member_whose_screenshots_are_off(self, service, api):
        service._set_screenshots_enabled(False)
        service._report_displays()

        assert api.reports == []

    def test_nothing_is_reported_when_nobody_is_signed_in(self, qapp, cache, cache_root, clock, desk):
        api = FakeApi()
        svc = _service(qapp, cache, api)
        svc.runtime.session_manager.user_info = {}
        svc._report_displays()

        assert api.reports == []

    def test_nothing_is_reported_when_the_machine_cannot_capture(self, service, api, monkeypatch):
        monkeypatch.setattr(capture, "supported", lambda: False)
        service._report_displays()

        assert api.reports == []

    def test_a_backend_client_without_the_call_is_tolerated(self, qapp, cache, cache_root, clock, desk):
        class OldApi:
            def get_config(self):
                return {"capture_frequency": 10}

            def get_privacy_config(self):
                return {}

        svc = _service(qapp, cache, OldApi())
        svc._report_displays()  # must not raise

    def test_applying_a_profile_reports_the_screens(self, service, api):
        service.apply_user_profile({"id": 9, "screenshots_enabled": True, "capture_frequency": 10})

        assert len(api.reports) == 1

    def test_the_report_runs_on_the_pool_never_on_the_calling_thread(self, service):
        service._report_displays()

        assert "screenshot-displays" in service.runtime.tasks.submissions

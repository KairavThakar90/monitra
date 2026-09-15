"""Which time entry a capture is recorded against, and when that is decided.

A screenshot with no `time_entry_id` is withheld by the uploader — correctly,
because there is nothing to upload it against — and only an adoption can ever
release one. So every path that leaves a row unattributed is a path that loses
a screenshot silently: no exception, no failed upload, no retry, and the file
sitting in the cache protected from every cleanup because it is still queued.

Two such paths have now been found and closed. Both are covered here.

1. A start that failed over to the durable action queue never delivered its
   entry id to the trackers at all, so an entire offline session's captures
   stayed unattributed. Fixed by adopting on the session's `client_op`, both
   in `SyncService` (durably, for a start confirmed after the session ended)
   and in `ScreenshotService.bind_entry_id` (for a session still running).

2. The narrow door this file is mostly about: the id arriving *while a capture
   is being encoded*. `_capture_now` read the entry id once, before touching
   the screen, then spent a second or more grabbing and compressing. If the
   backend confirmed the start inside that second, the `bind_entry_id` that ran
   had no row to adopt yet, and the row written afterwards kept the stale
   `None` for good. Found in a real desktop GUI run, not by a unit test.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from background_services.screenshot import config
from background_services.screenshot.capture import MergedCapture
from background_services.screenshot.compositor import Placement
from background_services.screenshot.displays import Display

pytest.importorskip("PIL", reason="Pillow is required to process screenshots")

#: A fixed instant just after a window boundary. The clock is frozen rather
#: than read: the service replans whenever the window index changes, so a test
#: that forces a capture time and then ticks races the wall clock.
NOW = 1_757_000_400.0


def _merge(width: int = 640, height: int = 480) -> MergedCapture:
    """One display, in the shape `capture_all_displays` reports."""
    display = Display(number=1, left=0, top=0, width=width, height=height,
                      is_primary=True)
    placement = Placement(display=display,
                          pixels=bytes([40, 80, 120, 255] * (width * height)),
                          width=width, height=height)
    from background_services.screenshot import compositor

    return MergedCapture(placements=[placement],
                         bounds=compositor.canvas_bounds([display]),
                         displays=[display])


@pytest.fixture
def cache_root(tmp_path, monkeypatch):
    from core import paths

    monkeypatch.setenv("MONITRA_DATA_DIR", str(tmp_path / "monitra"))
    paths.reset_cache()
    yield tmp_path / "monitra" / config.CACHE_DIR_NAME
    paths.reset_cache()


@pytest.fixture
def service(qapp, cache, cache_root, monkeypatch):
    """A ScreenshotService whose capture and clock are deterministic.

    The task pool runs submissions inline so these stay synchronous while
    still going through the real submit/on_success path.
    """
    from background_services.screenshot import screenshot_service as module

    monkeypatch.setattr(module.capture, "supported", lambda: True)
    monkeypatch.setattr(module.capture, "capture_all_displays", lambda: _merge())
    monkeypatch.setattr(module, "time", SimpleNamespace(time=lambda: NOW))

    def submit(fn, on_success=None, on_error=None, key=None, **kwargs):
        try:
            result = fn()
        except BaseException as exc:  # noqa: BLE001
            if on_error:
                on_error(exc)
            return None
        if on_success:
            on_success(result)
        return object()

    runtime = SimpleNamespace(
        storage=cache.storage,
        timer=SimpleNamespace(active_session=lambda: {}),
        tasks=SimpleNamespace(submit=submit),
        sync=SimpleNamespace(wake=lambda: None),
    )
    svc = module.ScreenshotService(runtime, cache)
    yield svc
    svc.stop_tracker()


def _fire_now(svc) -> None:
    """Make the next wake-up take a capture, with no dependence on real time."""
    svc._on_due()
    svc._planned_times = [NOW]
    svc._on_due()


class TestAnIdArrivingMidEncode:
    def test_it_still_reaches_the_row_that_capture_writes(
        self, service, cache, monkeypatch
    ):
        """The observed failure: id at 16:27:55, row written `entry=None` at 16:27:56.

        Reproduced by letting the id land part-way through the encode, which is
        exactly where the backend's confirmation landed in the real run.
        """
        from background_services.screenshot import screenshot_service as module

        svc = service
        svc.start_tracker({"entry_id": None, "client_op": "op-race"})

        real_process = module.image_processor.process_merged

        def process_then_bind(merged):
            result = real_process(merged)
            # The backend confirms the start while this capture is encoding.
            svc.bind_entry_id(4242)
            return result

        monkeypatch.setattr(module.image_processor, "process_merged", process_then_bind)

        _fire_now(svc)

        assert cache.count_unattributed_screenshots() == 0, (
            "a capture whose entry id arrived mid-encode was left unattributed; "
            "the uploader withholds such a row and nothing would ever fill it in"
        )
        pending = cache.get_pending_screenshots()
        assert len(pending) == 1
        assert pending[0]["time_entry_id"] == 4242

    def test_it_cannot_cross_into_a_later_session(self, service, cache):
        """Re-reading the id at write time must not reach across sessions.

        Picking up whatever id happens to be current would file one task's
        screen under another. The generation is what forbids it: a stop and a
        task switch both advance it, so only the session the capture was
        authorised for can answer.
        """
        svc = service
        svc.start_tracker({"entry_id": None, "client_op": "op-first"})
        stale = svc._current_generation()

        svc.stop_tracker()
        svc.start_tracker({"entry_id": 999, "client_op": "op-second"})

        assert svc._entry_id_for(stale, None) is None
        assert svc._entry_id_for(svc._current_generation(), None) == 999

    def test_an_id_that_never_arrives_leaves_the_row_adoptable(
        self, service, cache
    ):
        """No id at all is still the offline case, and must stay recoverable.

        The row keeps its session key so `bind_screenshots_to_client_op` can
        adopt it later — whether from the live session or, after the session
        has ended, from `SyncService` when the queued start finally lands.
        """
        svc = service
        svc.start_tracker({"entry_id": None, "client_op": "op-offline"})
        _fire_now(svc)

        assert cache.count_unattributed_screenshots() == 1
        assert cache.get_pending_screenshots() == []
        assert cache.bind_screenshots_to_client_op("op-offline", 777) == 1
        assert cache.get_pending_screenshots()[0]["time_entry_id"] == 777

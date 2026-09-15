"""The screenshot pipeline: image, cache, queue, upload and cleanup.

The invariant these exist to protect is the one that loses a user's captured
work if it breaks: **the local file is deleted only after the backend has
confirmed it stored the image.** A test that merely checked "upload was
called" would pass on an implementation that deletes first and uploads after.
"""
from __future__ import annotations

import io
import os
import random
from pathlib import Path

import pytest

from background_services.screenshot import config, image_processor, store
from background_services.screenshot.capture import RawCapture

pytest.importorskip("PIL", reason="Pillow is required to process screenshots")


@pytest.fixture
def cache_root(tmp_path, monkeypatch):
    """Point the whole data directory at a throwaway location."""
    from core import paths

    monkeypatch.setenv("MONITRA_DATA_DIR", str(tmp_path / "monitra"))
    paths.reset_cache()
    yield tmp_path / "monitra" / config.CACHE_DIR_NAME
    paths.reset_cache()


def _raw(width: int, height: int) -> RawCapture:
    """A synthetic BGRA frame of the given geometry."""
    return RawCapture(
        pixels=bytes([40, 80, 120, 255] * (width * height)),
        width=width, height=height, monitor_number=1,
    )


def _noisy(width: int, height: int) -> RawCapture:
    """
    A synthetic frame that actually costs bytes to encode.

    `_raw` is a flat colour, which WebP compresses to almost nothing at any
    quality — useless for exercising a size-driven compressor, because every
    setting produces the same tiny file. Seeded noise gives an image whose
    encoded size genuinely responds to quality, which is what the fallback
    reacts to. The seed keeps the sizes reproducible across runs.
    """
    rng = random.Random(20260908)
    return RawCapture(
        pixels=bytes(rng.randrange(256) for _ in range(width * height * 4)),
        width=width, height=height, monitor_number=1,
    )


class TestImageProcessing:
    def test_the_output_is_always_exactly_1000x1000_webp(self):
        from PIL import Image

        for geometry in ((1920, 1080), (1366, 768), (2560, 1440), (800, 1280)):
            processed = image_processor.process(_raw(*geometry))
            assert processed is not None
            assert (processed.width, processed.height) == (1000, 1000)
            assert processed.mime_type == "image/webp"
            with Image.open(__import__("io").BytesIO(processed.data)) as image:
                assert image.size == (1000, 1000)
                assert image.format == "WEBP"

    def test_a_widescreen_desktop_is_letterboxed_rather_than_squashed(self):
        # A distorted screenshot is not a smaller screenshot, it is a wrong
        # one: the text in it stops being legible, which is the only reason
        # the image is captured at all.
        from PIL import Image

        processed = image_processor.process(_raw(1600, 800))
        with Image.open(__import__("io").BytesIO(processed.data)) as image:
            rgb = image.convert("RGB")
            # 2:1 content on a square canvas leaves a quarter of the height as
            # padding top and bottom, and the content band in the middle.
            top = rgb.getpixel((500, 20))
            middle = rgb.getpixel((500, 500))
        assert _close(top, config.PAD_COLOR)
        assert not _close(middle, config.PAD_COLOR)

    def test_compression_is_aggressive_but_stops_at_a_readable_floor(self):
        processed = image_processor.process(_raw(1920, 1080))
        assert processed.size_bytes > 0
        assert processed.quality >= config.WEBP_QUALITY_MIN
        assert processed.quality <= config.WEBP_QUALITY_START

    def test_the_real_encoded_size_is_reported_not_an_estimate(self):
        processed = image_processor.process(_raw(1280, 720))
        assert processed.size_bytes == len(processed.data)


class TestFallbackCompression:
    """The second pass for screenshots the primary floor leaves oversized.

    The primary search stops at `WEBP_QUALITY_MIN` to protect readability, so a
    dense screen can finish it still over target. These cover the pass that
    handles that case — and, just as importantly, that it stays out of the way
    for every screenshot that does not need it.
    """

    def test_the_trigger_is_independent_of_the_primary_target(self):
        # These answer different questions — "when may the primary stop
        # trying" (120 KB) versus "when is the result worth recompressing"
        # (60 KB) — so tying the trigger back to TARGET_FILE_BYTES would
        # silently stop the fallback ever firing on a normal capture.
        assert config.FALLBACK_TRIGGER_BYTES == 60 * 1024
        assert config.FALLBACK_TRIGGER_BYTES < config.TARGET_FILE_BYTES
        assert config.fallback_trigger_bytes() == 60 * 1024

    def test_a_screenshot_within_the_threshold_is_left_byte_for_byte_alone(self, monkeypatch):
        # The common case by far. A fallback that "helpfully" recompressed
        # every capture would quietly degrade every screenshot the app takes.
        monkeypatch.setenv("MONITRA_SCREENSHOT_FALLBACK_TRIGGER_BYTES", str(4 * 1024 * 1024))
        processed = image_processor.process(_noisy(1920, 1080))
        assert processed.fallback_applied is False
        assert processed.fallback_attempts == 0
        assert processed.size_bytes == processed.primary_size_bytes

    def test_an_oversized_screenshot_is_compressed_further_towards_the_target(self, monkeypatch):
        monkeypatch.setenv("MONITRA_SCREENSHOT_FALLBACK_TRIGGER_BYTES", "1024")
        processed = image_processor.process(_noisy(1920, 1080))
        assert processed.fallback_applied is True
        assert processed.size_bytes < processed.primary_size_bytes
        # The target is a size reduction, not a quality subtraction: 40% off
        # the primary size, computed from the measured primary bytes.
        expected = int(processed.primary_size_bytes * 0.60)
        assert processed.fallback_target_bytes == expected

    def test_the_reported_size_is_the_final_image_not_the_primary_one(self, monkeypatch):
        # This is what reaches the SQLite queue row, the upload payload and the
        # stored metadata. Recording the pre-fallback size would make every
        # downstream byte count a lie.
        monkeypatch.setenv("MONITRA_SCREENSHOT_FALLBACK_TRIGGER_BYTES", "1024")
        processed = image_processor.process(_noisy(1920, 1080))
        assert processed.size_bytes == len(processed.data)
        assert processed.size_bytes != processed.primary_size_bytes

    def test_the_fallback_never_changes_the_geometry_or_the_format(self, monkeypatch):
        # The backend rejects anything that is not exactly 1000x1000 WebP, so a
        # fallback that resized would silently strand every capture it touched.
        from PIL import Image

        monkeypatch.setenv("MONITRA_SCREENSHOT_FALLBACK_TRIGGER_BYTES", "1024")
        processed = image_processor.process(_noisy(1600, 900))
        assert (processed.width, processed.height) == (1000, 1000)
        assert processed.mime_type == "image/webp"
        with Image.open(io.BytesIO(processed.data)) as image:
            assert image.size == (1000, 1000)
            assert image.format == "WEBP"

    def test_the_attempt_count_is_bounded_by_configuration(self, monkeypatch):
        # An unbounded search would burn a pool thread on one capture.
        monkeypatch.setenv("MONITRA_SCREENSHOT_FALLBACK_TRIGGER_BYTES", "1024")
        monkeypatch.setenv("MONITRA_SCREENSHOT_FALLBACK_REDUCTION_PERCENT", "90")
        monkeypatch.setenv("MONITRA_SCREENSHOT_FALLBACK_MAX_ATTEMPTS", "3")
        processed = image_processor.process(_noisy(1920, 1080))
        assert 0 < processed.fallback_attempts <= 3

    def test_it_can_be_switched_off_entirely(self, monkeypatch):
        monkeypatch.setenv("MONITRA_SCREENSHOT_FALLBACK_TRIGGER_BYTES", "1024")
        monkeypatch.setenv("MONITRA_SCREENSHOT_FALLBACK", "0")
        processed = image_processor.process(_noisy(1920, 1080))
        assert processed.fallback_applied is False

    def test_a_fallback_failure_keeps_the_primary_image_rather_than_losing_it(
        self, monkeypatch
    ):
        # The capture is already good; the fallback is only an optimisation.
        # Raising out of here would cost the user a screenshot for nothing.
        monkeypatch.setenv("MONITRA_SCREENSHOT_FALLBACK_TRIGGER_BYTES", "1024")
        primary = image_processor.process(_noisy(1280, 720))

        real_save = __import__("PIL").Image.Image.save
        calls = {"n": 0}

        def exploding_save(self, fp, *args, **kwargs):
            calls["n"] += 1
            if kwargs.get("method") == config.FALLBACK_WEBP_METHOD:
                raise OSError("encoder exploded")
            return real_save(self, fp, *args, **kwargs)

        monkeypatch.setattr(__import__("PIL").Image.Image, "save", exploding_save)
        processed = image_processor.process(_noisy(1280, 720))

        assert processed is not None
        assert processed.fallback_applied is False
        assert processed.size_bytes == primary.primary_size_bytes

    def test_the_quality_ladder_is_bounded_and_reaches_the_configured_floor(self):
        ladder = image_processor._fallback_qualities(45, 20, 5)
        assert len(ladder) <= 5
        assert ladder[-1] == 20
        assert all(45 > q >= 20 for q in ladder)
        assert ladder == sorted(ladder, reverse=True)
        # One rung of room yields exactly that rung.
        assert image_processor._fallback_qualities(21, 20, 5) == [20]
        # No room below the primary quality means no attempts at all, rather
        # than an encode that could only ever make the file bigger. A primary
        # already at or under the floor has nothing left to give.
        assert image_processor._fallback_qualities(20, 20, 5) == []
        assert image_processor._fallback_qualities(15, 20, 5) == []


def _close(a, b, tolerance: int = 12) -> bool:
    return all(abs(x - y) <= tolerance for x, y in zip(a, b))


class TestDailyCache:
    def test_screenshots_are_written_to_a_dated_folder_under_the_data_dir(self, cache_root):
        from datetime import datetime

        when = datetime(2026, 9, 7, 10, 6, 24)
        path = store.write_screenshot("abc-123", b"RIFFxxxxWEBP", when)
        assert path is not None
        assert path.parent.name == "2026-09-07"
        assert path.parent.parent == cache_root
        assert path.read_bytes() == b"RIFFxxxxWEBP"

    def test_a_new_day_gets_its_own_folder_without_a_restart(self, cache_root):
        from datetime import datetime

        store.write_screenshot("a", b"one", datetime(2026, 9, 7, 23, 58))
        store.write_screenshot("b", b"two", datetime(2026, 9, 8, 0, 2))
        assert {p.name for p in cache_root.iterdir()} == {"2026-09-07", "2026-09-08"}

    def test_nothing_is_written_outside_the_monitra_data_directory(self, cache_root):
        path = store.write_screenshot("abc", b"data")
        assert str(cache_root) in str(path)
        for forbidden in ("Desktop", "Documents", "Downloads"):
            assert forbidden not in str(path)

    def test_a_partial_write_never_leaves_a_readable_file(self, cache_root):
        # The file is written under a .part name and renamed, so a crash
        # mid-write cannot leave a truncated image the uploader would send.
        path = store.write_screenshot("abc", b"data")
        assert list(path.parent.glob("*.part")) == []

    def test_a_day_folder_is_removed_only_once_it_is_empty(self, cache_root):
        from datetime import datetime

        when = datetime(2026, 9, 7, 12, 0)
        path = store.write_screenshot("abc", b"data", when)
        store.prune_empty_day_folders([])
        assert path.parent.exists(), "a folder holding a file must not be removed"

        store.delete_screenshot(str(path))
        store.prune_empty_day_folders([])
        assert not path.parent.exists()

    def test_an_image_with_no_queue_row_is_reclaimed(self, cache_root):
        import time as _time

        orphan = store.write_screenshot("orphan", b"data")
        queued = store.write_screenshot("queued", b"data")
        # Age both past the safety window.
        for path in (orphan, queued):
            os.utime(path, (_time.time() - 7200, _time.time() - 7200))

        assert store.prune_orphans([str(queued)]) == 1
        assert not orphan.exists()
        assert queued.exists(), "a file the queue still references must survive"

    def test_a_freshly_written_image_is_never_reclaimed_as_an_orphan(self, cache_root):
        # Its row may still be being inserted; deleting it here would destroy a
        # capture that is about to be queued.
        path = store.write_screenshot("just-written", b"data")
        assert store.prune_orphans([]) == 0
        assert path.exists()

    def test_a_folder_holding_unsynced_work_is_protected_from_pruning(self, cache_root):
        from datetime import datetime

        path = store.write_screenshot("abc", b"data", datetime(2026, 9, 7, 12, 0))
        # The file is gone from disk but the queue still references it, so the
        # folder is not ours to remove yet.
        os.remove(path)
        store.prune_empty_day_folders([str(path)])
        assert path.parent.exists()


class TestQueue:
    """The durable SQLite queue behind every capture."""

    def _queue(self, cache, **overrides):
        payload = dict(
            client_screenshot_id="uuid-1",
            local_file_path="/tmp/ss.webp",
            captured_at="2026-09-07T10:06:24+00:00",
            window_start="2026-09-07T10:00:00+00:00",
            width=1000, height=1000, file_size_bytes=54321,
            time_entry_id=100, monitor_number=1,
        )
        payload.update(overrides)
        return cache.save_screenshot(**payload)

    def test_a_capture_is_queued_before_anything_is_uploaded(self, cache):
        self._queue(cache)
        pending = cache.get_pending_screenshots()
        assert len(pending) == 1
        assert pending[0]["client_screenshot_id"] == "uuid-1"
        assert pending[0]["file_size_bytes"] == 54321

    def test_the_same_capture_cannot_be_queued_twice(self, cache):
        self._queue(cache)
        self._queue(cache, local_file_path="/tmp/other.webp")
        assert len(cache.get_pending_screenshots()) == 1

    def test_a_capture_with_no_entry_id_waits_to_be_attributed(self, cache):
        # A capture taken before the backend issued an entry id has nothing to
        # upload against; uploading it against `None` would be rejected.
        self._queue(cache, time_entry_id=None, client_op="op-1")
        assert cache.get_pending_screenshots() == []

        bound = cache.bind_screenshots_to_client_op("op-1", 555)
        assert bound == 1
        assert cache.get_pending_screenshots()[0]["time_entry_id"] == 555

    def test_only_captures_from_the_same_session_are_adopted(self, cache):
        self._queue(cache, client_screenshot_id="a", time_entry_id=None, client_op="op-1")
        self._queue(cache, client_screenshot_id="b", time_entry_id=None, client_op="op-2")
        assert cache.bind_screenshots_to_client_op("op-1", 555) == 1
        remaining = {r["client_screenshot_id"] for r in cache.get_pending_screenshots()}
        assert remaining == {"a"}

    def test_every_window_of_a_long_offline_session_is_adopted(self, cache):
        # The regression that cost real captures. Adoption used to match on the
        # window tracking *began* in, so a queued start that took longer than
        # one window to land stranded every capture after the first: the
        # uploader withholds a row with no entry id and nothing else ever
        # filled it in. A session is not one window long.
        for index, window in enumerate(
            ("10:00:00", "10:10:00", "10:20:00", "10:30:00")
        ):
            self._queue(
                cache, client_screenshot_id=f"w{index}", time_entry_id=None,
                client_op="op-long", window_start=f"2026-09-07T{window}+00:00",
            )
        assert cache.bind_screenshots_to_client_op("op-long", 777) == 4
        assert len(cache.get_pending_screenshots()) == 4

    def test_a_capture_that_can_never_upload_is_counted_separately(self, cache):
        # It shows as `pending`, which reads as "about to upload" -- but the
        # uploader withholds it, so only an adoption can release it. A count
        # that does not fall is the visible form of a stalled session.
        self._queue(cache, time_entry_id=None, client_op="op-1")
        assert cache.count_screenshots_by_status() == {"pending": 1}
        assert cache.count_unattributed_screenshots() == 1

        cache.bind_screenshots_to_client_op("op-1", 555)
        assert cache.count_unattributed_screenshots() == 0

    def test_adoption_is_idempotent_and_never_resteals_a_bound_capture(self, cache):
        # Both halves of the adoption run: the live session's binder and the
        # sync service's durable one. Whichever is second must change nothing,
        # and must never move a capture onto a different entry.
        self._queue(cache, time_entry_id=None, client_op="op-1")
        assert cache.bind_screenshots_to_client_op("op-1", 555) == 1
        assert cache.bind_screenshots_to_client_op("op-1", 999) == 0
        assert cache.get_pending_screenshots()[0]["time_entry_id"] == 555

    def test_completing_a_screenshot_returns_the_file_to_delete(self, cache):
        self._queue(cache)
        path = cache.complete_screenshot("uuid-1")
        assert path == "/tmp/ss.webp"
        assert cache.get_pending_screenshots() == []

    def test_a_failure_schedules_a_backed_off_retry_rather_than_dropping_it(self, cache):
        self._queue(cache)
        assert cache.fail_screenshot("uuid-1", "network down") is True
        # Backed off, so it is not immediately re-attempted.
        assert cache.get_pending_screenshots() == []
        assert cache.count_screenshots_by_status() == {"pending": 1}

    def test_an_exhausted_screenshot_is_parked_but_its_file_is_kept(self, cache):
        self._queue(cache)
        for _ in range(4):
            cache.fail_screenshot("uuid-1", "still down", max_retries=3)
        assert cache.count_screenshots_by_status() == {"failed": 1}
        # Still referenced, so the file is protected from cleanup: discarding
        # captured evidence because the network was down for a day is not an
        # acceptable outcome.
        assert cache.get_screenshot_backlog_paths() == ["/tmp/ss.webp"]

    def test_an_exhausted_screenshot_is_reclaimed_at_the_next_launch(self, cache):
        # An exhausted screenshot means the backend was rejecting uploads for
        # hours — a misconfiguration someone has since fixed. Its file is still
        # on disk and the capture is still valid, so a launch retries it with a
        # fresh budget instead of discarding real evidence.
        self._queue(cache)
        for _ in range(4):
            cache.fail_screenshot("uuid-1", "storage not configured", max_retries=3)
        assert cache.count_screenshots_by_status() == {"failed": 1}

        assert cache.requeue_screenshots_for_new_run() == 1
        pending = cache.get_pending_screenshots()
        assert len(pending) == 1
        assert pending[0]["retry_count"] == 0, "a reclaimed screenshot gets a full budget"

    def test_a_backed_off_screenshot_is_retried_promptly_at_the_next_launch(self, cache):
        # A launch is when someone has changed something; sitting out a
        # multi-minute backoff inherited from the previous run waits for a
        # condition that no longer holds.
        self._queue(cache)
        cache.fail_screenshot("uuid-1", "backend down")
        assert cache.get_pending_screenshots() == []

        assert cache.requeue_screenshots_for_new_run() == 1
        pending = cache.get_pending_screenshots()
        assert len(pending) == 1
        assert pending[0]["retry_count"] == 1, "a mid-budget retry keeps its count"

    def test_an_upload_interrupted_by_a_crash_is_resumed_on_the_next_run(self, cache):
        self._queue(cache)
        cache.mark_screenshots_uploading(["uuid-1"])
        assert cache.get_pending_screenshots() == []  # claimed

        # The process dies here. The next launch releases the claim.
        assert cache.reset_uploading_screenshots() == 1
        assert len(cache.get_pending_screenshots()) == 1

    def test_the_backlog_lists_every_file_that_has_not_landed(self, cache):
        self._queue(cache, client_screenshot_id="a", local_file_path="/tmp/a.webp")
        self._queue(cache, client_screenshot_id="b", local_file_path="/tmp/b.webp")
        cache.mark_screenshots_uploading(["b"])
        assert sorted(cache.get_screenshot_backlog_paths()) == ["/tmp/a.webp", "/tmp/b.webp"]


class TestUpload:
    """SyncService is the only uploader, and the only thing that deletes."""

    @pytest.fixture
    def sync(self, cache, tmp_path, monkeypatch):
        from types import SimpleNamespace

        from background_services.sync.sync_service import SyncService

        runtime = SimpleNamespace(storage=cache.storage, queue_floor_generation=0)
        entries = SimpleNamespace(upload_screenshot=None)
        service = SyncService(runtime, cache, entries, SimpleNamespace())
        return service, entries

    def _capture(self, cache, tmp_path, name="uuid-1"):
        path = tmp_path / f"{name}.webp"
        path.write_bytes(b"RIFF0000WEBPimage-bytes")
        cache.save_screenshot(
            client_screenshot_id=name, local_file_path=str(path),
            captured_at="2026-09-07T10:06:24+00:00",
            window_start="2026-09-07T10:00:00+00:00",
            width=1000, height=1000, file_size_bytes=path.stat().st_size,
            time_entry_id=100,
        )
        return path

    def test_the_local_file_is_deleted_only_after_a_confirmed_upload(self, sync, cache, tmp_path):
        service, entries = sync
        path = self._capture(cache, tmp_path)
        seen = {}

        def upload(entry_id, image, file_name, metadata, timeout=None):
            # The file must still exist at the moment of upload — an
            # implementation that deleted first would fail here.
            seen["existed_during_upload"] = path.exists()
            seen["entry_id"] = entry_id
            seen["metadata"] = metadata
            seen["bytes"] = image
            return {"success": True}

        entries.upload_screenshot = upload
        service._sync_screenshots()

        assert seen["existed_during_upload"] is True
        assert seen["entry_id"] == 100
        assert seen["bytes"] == b"RIFF0000WEBPimage-bytes"
        assert seen["metadata"]["client_screenshot_id"] == "uuid-1"
        assert not path.exists(), "an uploaded screenshot must be removed locally"
        assert cache.count_screenshots_by_status() == {}

    def test_a_failed_upload_keeps_both_the_file_and_the_queue_row(self, sync, cache, tmp_path):
        from app.api.exceptions import ApiError

        service, entries = sync
        path = self._capture(cache, tmp_path)

        def upload(*args, **kwargs):
            raise ApiError("Network connection error")

        entries.upload_screenshot = upload
        service._sync_screenshots()

        assert path.exists(), "an unsent screenshot must survive to be retried"
        assert cache.count_screenshots_by_status() == {"pending": 1}

    def test_going_offline_and_coming_back_uploads_the_backlog(self, sync, cache, tmp_path):
        from app.api.exceptions import ApiError

        service, entries = sync
        paths = [self._capture(cache, tmp_path, f"uuid-{i}") for i in range(3)]

        entries.upload_screenshot = lambda *a, **k: (_ for _ in ()).throw(ApiError("offline"))
        service._sync_screenshots()
        assert all(p.exists() for p in paths)

        # Back online. The backoff has not elapsed, so the rows are not yet
        # due; clearing it is what a passing retry timer does.
        cache.storage.execute("UPDATE pending_screenshots SET next_retry_at = 0")
        entries.upload_screenshot = lambda *a, **k: {"success": True}
        service._sync_screenshots()

        assert not any(p.exists() for p in paths)
        assert cache.count_screenshots_by_status() == {}

    def test_a_refused_screenshot_keeps_its_file_and_is_parked(self, sync, cache, tmp_path):
        # A backend older than the desktop has no screenshot endpoint and
        # answers 404 — indistinguishable from "that entry is gone". Deleting
        # on 404 destroyed four real captures when a client ran ahead of its
        # server, so a refusal parks the row and keeps the image.
        from app.api.exceptions import ApiError

        service, entries = sync
        path = self._capture(cache, tmp_path)

        for code in (403, 404, 422):
            path.write_bytes(b"RIFF0000WEBPimage-bytes")
            cache.requeue_screenshots_for_new_run()
            entries.upload_screenshot = lambda *a, **k: (_ for _ in ()).throw(
                ApiError("refused", status_code=code)
            )
            service._sync_screenshots()

            assert path.exists(), f"HTTP {code} must not destroy the capture"
            assert cache.count_screenshots_by_status() == {"failed": 1}

    def test_a_parked_screenshot_uploads_once_the_backend_catches_up(self, sync, cache, tmp_path):
        from app.api.exceptions import ApiError

        service, entries = sync
        path = self._capture(cache, tmp_path)
        entries.upload_screenshot = lambda *a, **k: (_ for _ in ()).throw(
            ApiError("no such endpoint", status_code=404)
        )
        service._sync_screenshots()
        assert path.exists()

        # The server is upgraded and the desktop restarts.
        assert cache.requeue_screenshots_for_new_run() == 1
        entries.upload_screenshot = lambda *a, **k: {"success": True}
        service._sync_screenshots()

        assert not path.exists()
        assert cache.count_screenshots_by_status() == {}

    def test_an_expired_session_holds_the_queue_instead_of_burning_retries(self, sync, cache, tmp_path):
        from app.api.exceptions import ApiError

        service, entries = sync
        path = self._capture(cache, tmp_path)

        def upload(*args, **kwargs):
            raise ApiError("Session expired", status_code=401)

        entries.upload_screenshot = upload
        service._sync_screenshots()

        assert path.exists()
        assert service._should_hold() == "awaiting re-authentication"

    def test_a_screenshot_whose_file_vanished_is_dropped_not_retried_forever(self, sync, cache, tmp_path):
        service, entries = sync
        path = self._capture(cache, tmp_path)
        path.unlink()
        entries.upload_screenshot = lambda *a, **k: pytest.fail("nothing to upload")

        service._sync_screenshots()
        assert cache.count_screenshots_by_status() == {}

    def test_the_upload_carries_the_idempotency_key_so_a_retry_cannot_duplicate(self, sync, cache, tmp_path):
        service, entries = sync
        self._capture(cache, tmp_path, "uuid-abc")
        captured = {}
        entries.upload_screenshot = lambda e, i, n, m, t=None: captured.update(m) or {"ok": True}

        service._sync_screenshots()
        assert captured["client_screenshot_id"] == "uuid-abc"
        assert captured["captured_at"] == "2026-09-07T10:06:24+00:00"
        assert captured["width"] == 1000 and captured["height"] == 1000


class TestTimerIntegration:
    """Capture happens while time is tracked, and at no other moment."""

    #: A fixed instant just after a window boundary. The clock is frozen for
    #: these tests rather than read: the service replans whenever the window
    #: index changes, so a test that forces a capture time and then ticks is
    #: racing the wall clock — and it lost, intermittently, whenever the suite
    #: happened to straddle a boundary.
    NOW = 1_757_000_400.0

    @pytest.fixture
    def service(self, qapp, cache, cache_root, monkeypatch):
        from types import SimpleNamespace

        from background_services.screenshot import screenshot_service as module

        grabs = {"count": 0}

        def fake_grab():
            grabs["count"] += 1
            return _raw(640, 480)

        monkeypatch.setattr(module.capture, "supported", lambda: True)
        monkeypatch.setattr(module.capture, "capture_primary_monitor", fake_grab)
        # Only `time.time()` is used here, so a stub with that one name keeps
        # the freeze local to this module instead of patching the clock
        # process-wide.
        monkeypatch.setattr(module, "time", SimpleNamespace(time=lambda: self.NOW))

        # Capture runs on the runtime's task pool. Executing submissions
        # inline keeps these tests synchronous while still exercising the real
        # submit/on_success path the service uses.
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
            timer=SimpleNamespace(active_session=lambda: {"entry_id": 100}),
            sync=SimpleNamespace(wake=lambda: None),
            tasks=SimpleNamespace(submit=submit),
        )
        return module.ScreenshotService(runtime, cache), grabs

    def _fire_now(self, svc):
        """Make the next wake-up take a capture, with no dependence on real time."""
        svc._on_due()                    # plan the (frozen) current window
        svc._planned_times = [self.NOW]  # due exactly now
        svc._on_due()

    def test_a_stopped_timer_captures_nothing(self, service, cache):
        svc, grabs = service
        for _ in range(5):
            svc._on_due()
        assert grabs["count"] == 0
        assert cache.count_screenshots_by_status() == {}

    def test_starting_the_timer_captures_within_the_window_and_queues_it(self, service, cache):
        svc, grabs = service
        svc.start_tracker({"entry_id": 100})
        self._fire_now(svc)

        assert grabs["count"] == 1
        pending = cache.get_pending_screenshots()
        assert len(pending) == 1
        assert pending[0]["time_entry_id"] == 100
        assert pending[0]["width"] == 1000 and pending[0]["height"] == 1000
        assert Path(pending[0]["local_file_path"]).exists()

    def test_stopping_the_timer_stops_capture_immediately(self, service, cache):
        svc, grabs = service
        svc.start_tracker({"entry_id": 100})
        svc.stop_tracker()
        svc._planned_times = [self.NOW]
        for _ in range(5):
            svc._on_due()
        assert grabs["count"] == 0

    def test_a_window_spends_its_budget_once_even_across_a_restart(self, service, cache):
        # The budget lives in app_state, not in the service, precisely so a
        # relaunch four minutes into a window cannot take a second screenshot
        # the window has already paid for.
        svc, grabs = service
        svc.start_tracker({"entry_id": 100})
        self._fire_now(svc)
        assert grabs["count"] == 1

        # A fresh service on the same cache is the restart.
        restarted = type(svc)(svc.runtime, cache)
        restarted.start_tracker({"entry_id": 100})
        for _ in range(3):
            restarted._on_due()
        assert grabs["count"] == 1

    def test_a_capture_taken_before_the_entry_id_arrives_is_attributed_later(self, service, cache):
        svc, _ = service
        svc.runtime.timer.active_session = lambda: {}
        svc.start_tracker({"entry_id": None, "client_op": "op-1"})
        self._fire_now(svc)

        # Queued but not yet uploadable.
        assert cache.get_pending_screenshots() == []
        svc.bind_entry_id(777)
        pending = cache.get_pending_screenshots()
        assert len(pending) == 1 and pending[0]["time_entry_id"] == 777

    def test_a_capture_is_stored_against_the_session_not_against_its_window(self, service, cache):
        # Adoption is keyed on the tracking session, because a session is not
        # one window long: an offline start that takes half an hour to land
        # takes captures in several windows and every one of them belongs to
        # the same entry. Keying on the window tracking began in stranded all
        # but the first, permanently.
        svc, _ = service
        svc.runtime.timer.active_session = lambda: {}
        svc.start_tracker({"entry_id": None, "client_op": "op-1"})
        self._fire_now(svc)

        row = cache.storage.query_one(
            "SELECT client_op, time_entry_id FROM pending_screenshots"
        )
        assert row["client_op"] == "op-1"
        assert row["time_entry_id"] is None

    def test_a_capture_is_not_adopted_by_a_later_session(self, service, cache):
        # Stop and start the same task again and the entry ids differ. A
        # capture from the first session must never be uploaded against the
        # second's entry — that would attribute one task's screen to another.
        svc, _ = service
        svc.runtime.timer.active_session = lambda: {}
        svc.start_tracker({"entry_id": None, "client_op": "op-first"})
        self._fire_now(svc)
        svc.stop_tracker()

        svc.start_tracker({"entry_id": None, "client_op": "op-second"})
        svc.bind_entry_id(999)
        assert cache.get_pending_screenshots() == []
        assert cache.count_unattributed_screenshots() == 1

    def test_a_machine_that_cannot_capture_queues_nothing_and_says_so(self, service, cache, monkeypatch):
        # No placeholder image, exactly as there is no placeholder domain in
        # the URL tracker. An honest absence beats fabricated evidence.
        svc, grabs = service
        monkeypatch.setattr(
            "background_services.screenshot.screenshot_service.capture.supported",
            lambda: False,
        )
        reasons = []
        svc.capture_unavailable.connect(reasons.append)
        svc.start_tracker({"entry_id": 100})
        svc._planned_times = [self.NOW]
        svc._on_due()

        assert grabs["count"] == 0
        assert cache.count_screenshots_by_status() == {}
        assert reasons and "unavailable" in reasons[0]

    def test_the_capture_backend_is_resolved_off_the_gui_thread(self, service, monkeypatch):
        """`mss` and Pillow must not be imported by a GUI-thread slot.

        Both are loaded lazily by `capture.supported()` /
        `image_processor.supported()`, and the only caller of those is
        `_capture_available()`, which `_on_due()` invokes on the GUI thread.
        The first tracking session of every run therefore paid for both
        imports inline — ~50 ms warm, worse from a packaged build — at the
        exact moment the user pressed Start. `on_start` now resolves them on
        the shared pool instead.
        """
        from background_services.screenshot import screenshot_service as module

        svc, _ = service
        submitted = []
        svc.runtime.tasks.submit = (
            lambda fn, **kw: submitted.append((fn, kw)) or object()
        )
        svc.on_start()

        assert submitted, "on_start must resolve the capture backend on the pool"
        fn, kwargs = submitted[0]
        assert kwargs["key"] == "screenshot-warmup"
        # Session-independent: it describes the machine, not the signed-in user.
        assert kwargs["guard_generation"] is False
        # And the submitted callable is what performs the imports.
        loaded = []
        monkeypatch.setattr(module.image_processor, "supported",
                            lambda: loaded.append("pil") or True)
        monkeypatch.setattr(module.capture, "supported",
                            lambda: loaded.append("mss") or True)
        fn()
        assert loaded == ["mss", "pil"]

    def test_the_schedule_sleeps_until_the_next_capture_rather_than_polling(self, service):
        # A per-second poll of an idle window is the shape of defect that
        # produced the historical worker storm; a single-shot timer armed for
        # the real instant is the alternative.
        svc, _ = service
        svc.start_tracker({"entry_id": 100})
        assert svc._due_timer.isActive()
        assert svc._due_timer.interval() >= 250

    def test_the_service_owns_no_thread_of_its_own(self, service):
        # Capture happens once every ten minutes; a dedicated OS thread would
        # idle for 99.9% of its life. The work goes to the shared bounded pool
        # instead, which is what TaskRunner exists for.
        from core.service import BaseService, LoopService

        svc, _ = service
        assert isinstance(svc, BaseService)
        assert not isinstance(svc, LoopService)

    def test_capture_never_runs_on_the_calling_thread_directly(self, service, cache):
        # It must go through the pool: encoding a 4K frame on the GUI thread
        # would freeze the window for the duration.
        svc, grabs = service
        submitted = []
        svc.runtime.tasks.submit = lambda fn, **kw: submitted.append(kw.get("key"))

        svc.start_tracker({"entry_id": 100})
        svc._planned_times = [self.NOW]
        svc._on_due()

        assert submitted == ["screenshot-capture"]
        assert grabs["count"] == 0, "no capture ran outside the pool submission"


class TestQueuedStartAdoption:
    """The failure that lost every screenshot of an offline tracking session.

    A start request that does not succeed in-process fails over to the durable
    action queue. `SyncService` is then the only thing that ever learns the
    entry id — and it used to write that id onto the queued *stop* and nowhere
    else. `TimerService._bind_trackers_to_entry` has a single caller, on the
    in-process success path, so the sub-trackers were never told.

    The result was silent and total: the captures were taken, compressed and
    written to the durable queue correctly, but with `time_entry_id` NULL.
    `get_pending_screenshots` withholds such a row (uploading against `None`
    would be refused), nothing ever filled the id in, and the images were
    protected from every cleanup path precisely because they were still queued.
    They simply never reached Google Drive or the database, with no error
    raised anywhere and the timer itself behaving perfectly.

    Which users this hit was not random: it was whoever's Start round trip
    missed — flaky wifi, a VPN, a proxy, or a cold serverless backend — which
    is exactly the "works for some people and not others" report.
    """

    @pytest.fixture
    def sync(self, cache):
        from types import SimpleNamespace

        from background_services.sync.sync_service import SyncService

        started = {}

        def start_time_entry(project_id, task_id, started_at=None, client_op=None):
            started["client_op"] = client_op
            return {"id": 4242, "start_time": started_at}

        entries = SimpleNamespace(start_time_entry=start_time_entry)
        runtime = SimpleNamespace(storage=cache.storage, queue_floor_generation=0)
        service = SyncService(runtime, cache, entries, SimpleNamespace())
        return service, started

    def _capture(self, cache, client_op, name, window):
        cache.save_screenshot(
            client_screenshot_id=name,
            local_file_path=f"/tmp/{name}.webp",
            captured_at=f"2026-09-07T{window}+00:00",
            window_start=f"2026-09-07T{window}+00:00",
            width=1000, height=1000, file_size_bytes=4096,
            time_entry_id=None, client_op=client_op,
        )

    def test_a_queued_start_attributes_every_capture_it_left_behind(self, sync, cache):
        service, _ = sync
        # Thirty-one minutes of offline tracking: four windows, four captures,
        # none of which could know the entry id when it was taken.
        for index, window in enumerate(("10:00:00", "10:10:00", "10:20:00", "10:30:00")):
            self._capture(cache, "op-offline", f"shot-{index}", window)

        assert cache.get_pending_screenshots() == [], "unattributed captures must not upload"
        assert cache.count_unattributed_screenshots() == 4

        service._handle_start_timer({
            "project_id": 1, "task_id": 2,
            "started_at": "2026-09-07T10:00:00+00:00",
            "client_op": "op-offline",
        })

        pending = cache.get_pending_screenshots()
        assert len(pending) == 4, "every window's capture must now be uploadable"
        assert {row["time_entry_id"] for row in pending} == {4242}
        assert cache.count_unattributed_screenshots() == 0

    def test_the_resolved_entry_id_is_carried_back_for_a_live_session(self, sync, cache):
        # TimerService needs it to bind a session that is still running, so
        # captures taken *after* the start lands carry the id from the start.
        service, _ = sync
        result = service._handle_start_timer({
            "project_id": 1, "task_id": 2, "started_at": None, "client_op": "op-live",
        })
        assert result["entry_id"] == 4242
        assert result["client_op"] == "op-live"

    def test_another_session_s_captures_are_never_adopted(self, sync, cache):
        service, _ = sync
        self._capture(cache, "op-mine", "mine", "10:00:00")
        self._capture(cache, "op-theirs", "theirs", "10:00:00")

        service._handle_start_timer({
            "project_id": 1, "task_id": 2, "started_at": None, "client_op": "op-mine",
        })

        pending = cache.get_pending_screenshots()
        assert [row["client_screenshot_id"] for row in pending] == ["mine"]
        assert cache.count_unattributed_screenshots() == 1

    def test_a_start_still_completes_when_adoption_fails(self, sync, cache, monkeypatch):
        # The start genuinely succeeded on the backend. Failing the action here
        # would retry a start that already created an entry; the adoption is
        # retried at the next launch instead.
        service, _ = sync
        monkeypatch.setattr(
            cache, "bind_screenshots_to_client_op",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("database is locked")),
        )
        result = service._handle_start_timer({
            "project_id": 1, "task_id": 2, "started_at": None, "client_op": "op-x",
        })
        assert result["entry_id"] == 4242

"""Screenshot upload recovery: nothing captured may end up silently unsent.

The invariant, stated once: **if the desktop reported a capture, the capture
either reaches Google Drive or remains in the local queue with its file,
being retried.** These tests pin the ways that used to fail:

* A transient failure (no network, a backend restart, a Drive outage) used to
  be retried twelve times and then parked as `failed` until the next launch.
  Twelve doublings capped at five minutes are spent in about twenty-five
  minutes, and a tray application is not relaunched for days.
* A 2xx from the backend was taken as confirmation even when the body carried
  no Drive file id, so anything answering 200 in front of the API -- a proxy,
  an older backend -- could make the desktop delete its only copy.
* A parked screenshot was only ever revived at launch. Now it is also revived
  when a hold ends (the network is back, the user signed in again) and on an
  hourly interval while the process lives.
"""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from app.api.exceptions import ApiError
from background_services.screenshot.config import (
    PARKED_RETRY_INTERVAL_SECONDS, UPLOAD_RETRY_MAX_DELAY_SECONDS,
)
from background_services.sync.sync_service import SyncService
from core.service import ServiceState


def _confirmed(file_id="drive-file-1", backend_id=1, duplicate=False) -> dict:
    return {
        "success": True,
        "duplicate": duplicate,
        "screenshot": {"id": backend_id, "google_drive_file_id": file_id},
    }


def _refusing(status: int):
    def upload(*args, **kwargs):
        raise ApiError("refused", status_code=status)
    return upload


def _offline(*args, **kwargs):
    raise ApiError("Failed to upload screenshot: Network connection error")


@pytest.fixture
def sync(cache):
    runtime = SimpleNamespace(storage=cache.storage, queue_floor_generation=0)
    entries = SimpleNamespace(upload_screenshot=None)
    service = SyncService(runtime, cache, entries, SimpleNamespace())
    return service, entries


def _capture(cache, tmp_path, name="uuid-1", entry_id=100):
    path = tmp_path / f"{name}.webp"
    path.write_bytes(b"RIFF0000WEBPimage-bytes")
    cache.save_screenshot(
        client_screenshot_id=name, local_file_path=str(path),
        captured_at="2026-09-28T04:06:24+00:00", window_start="2026-09-28T04:00:00+00:00",
        width=1000, height=1000, file_size_bytes=path.stat().st_size,
        time_entry_id=entry_id,
    )
    return path


def _make_due(cache) -> None:
    cache.storage.execute("UPDATE pending_screenshots SET next_retry_at = 0")


def _row(cache, record_id="uuid-1") -> dict:
    return dict(cache.storage.query_one(
        "SELECT status, retry_count, next_retry_at, last_error, updated_at "
        "FROM pending_screenshots WHERE id = ?", (record_id,)
    ))


class TestTransientFailuresNeverPark:
    def test_a_long_outage_keeps_retrying_instead_of_parking(self, sync, cache, tmp_path):
        service, entries = sync
        path = _capture(cache, tmp_path)
        entries.upload_screenshot = _offline

        # Far past the old twelve-attempt budget.
        for _ in range(60):
            _make_due(cache)
            service._sync_screenshots()

        row = _row(cache)
        assert row["status"] == "pending", "a transient failure must never park the capture"
        assert row["retry_count"] == 60
        assert path.exists(), "the file is kept for as long as the upload is unconfirmed"

    def test_the_backoff_is_capped_rather_than_growing_forever(self, sync, cache, tmp_path):
        service, entries = sync
        _capture(cache, tmp_path)
        entries.upload_screenshot = _offline
        for _ in range(40):
            _make_due(cache)
            service._sync_screenshots()
        wait = _row(cache)["next_retry_at"] - time.time()
        assert 0 < wait <= UPLOAD_RETRY_MAX_DELAY_SECONDS * 1.5 + 1, (
            f"next attempt scheduled {wait:.0f}s out; the cap is {UPLOAD_RETRY_MAX_DELAY_SECONDS}s"
        )

    def test_a_server_error_is_transient_too(self, sync, cache, tmp_path):
        service, entries = sync
        _capture(cache, tmp_path)
        for status in (500, 502, 503, 504):
            entries.upload_screenshot = _refusing(status)
            _make_due(cache)
            service._sync_screenshots()
            assert _row(cache)["status"] == "pending", f"HTTP {status} must be retried"

    def test_the_failure_is_recorded_on_the_row(self, sync, cache, tmp_path):
        service, entries = sync
        _capture(cache, tmp_path)
        entries.upload_screenshot = _offline
        service._sync_screenshots()
        row = _row(cache)
        assert "Network connection error" in row["last_error"]
        assert row["next_retry_at"] > time.time()

    def test_recovery_after_the_outage_uploads_exactly_once(self, sync, cache, tmp_path):
        service, entries = sync
        path = _capture(cache, tmp_path)
        entries.upload_screenshot = _offline
        for _ in range(20):
            _make_due(cache)
            service._sync_screenshots()

        calls = []
        entries.upload_screenshot = lambda *a, **k: calls.append(a) or _confirmed()
        _make_due(cache)
        service._sync_screenshots()
        service._sync_screenshots()

        assert len(calls) == 1
        assert not path.exists()
        assert cache.count_screenshots_by_status() == {}


class TestOnlyADriveFileIdConfirmsAnUpload:
    def test_a_2xx_without_a_drive_file_id_keeps_the_file_and_retries(self, sync, cache, tmp_path):
        service, entries = sync
        path = _capture(cache, tmp_path)
        for body in ({"success": True}, {"ok": True}, {}, None, "OK",
                     {"success": True, "screenshot": {"id": 1}},
                     {"success": True, "screenshot": {"id": 1, "google_drive_file_id": None}},
                     {"success": False, "screenshot": {"id": 1, "google_drive_file_id": "x"}}):
            entries.upload_screenshot = lambda *a, **k: body
            _make_due(cache)
            service._sync_screenshots()
            assert path.exists(), f"{body!r} must not be taken as confirmation"
            assert _row(cache)["status"] == "pending"
            assert "without a Drive file id" in _row(cache)["last_error"]

    def test_a_confirmed_record_releases_the_file(self, sync, cache, tmp_path):
        service, entries = sync
        path = _capture(cache, tmp_path)
        entries.upload_screenshot = lambda *a, **k: _confirmed("1AbC")
        service._sync_screenshots()
        assert not path.exists()
        assert cache.count_screenshots_by_status() == {}

    def test_a_duplicate_answer_is_a_confirmation_when_it_carries_the_file_id(self, sync, cache, tmp_path):
        # The retry after a lost response: the backend says "already stored"
        # and names the object. That is exactly what the client needs.
        service, entries = sync
        path = _capture(cache, tmp_path)
        entries.upload_screenshot = lambda *a, **k: _confirmed("1AbC", duplicate=True)
        service._sync_screenshots()
        assert not path.exists()

    def test_the_upload_log_names_the_drive_file(self, sync, cache, tmp_path, caplog):
        import logging

        service, entries = sync
        _capture(cache, tmp_path)
        entries.upload_screenshot = lambda *a, **k: _confirmed("1AbC", backend_id=77)
        with caplog.at_level(logging.INFO):
            service._sync_screenshots()
        line = next(r.getMessage() for r in caplog.records if "SCREENSHOT_UPLOADED" in r.getMessage())
        assert "drive_file=1AbC" in line
        assert "backend_id=77" in line
        assert "id=uuid-1" in line
        assert "entry=100" in line


class TestParkedScreenshotsAreRevived:
    def test_a_refusal_parks_with_the_file_and_the_status_code(self, sync, cache, tmp_path):
        service, entries = sync
        path = _capture(cache, tmp_path)
        for status in (403, 404, 413, 422):
            cache.revive_parked_screenshots()
            entries.upload_screenshot = _refusing(status)
            service._sync_screenshots()
            row = _row(cache)
            assert row["status"] == "failed", f"HTTP {status} should park"
            assert row["last_error"] == f"HTTP {status}"
            assert path.exists()

    def test_a_parked_screenshot_is_offered_again_once_the_interval_has_passed(self, sync, cache, tmp_path):
        service, entries = sync
        _capture(cache, tmp_path)
        entries.upload_screenshot = _refusing(404)
        service._sync_screenshots()
        assert _row(cache)["status"] == "failed"

        # Too soon: an idle pass leaves it parked.
        entries.upload_screenshot = lambda *a, **k: pytest.fail("must not retry yet")
        service._sync_screenshots()
        assert _row(cache)["status"] == "failed"

        # An hour later the server has been fixed; the idle pass revives it
        # and the next pass uploads it.
        cache.storage.execute(
            "UPDATE pending_screenshots SET updated_at = ?",
            (time.time() - PARKED_RETRY_INTERVAL_SECONDS - 1,),
        )
        uploads = []
        entries.upload_screenshot = lambda *a, **k: uploads.append(1) or _confirmed()
        service._sync_screenshots()
        assert _row(cache)["status"] == "pending"
        service._sync_screenshots()
        assert uploads == [1]
        assert cache.count_screenshots_by_status() == {}

    def test_the_end_of_a_network_hold_brings_backed_off_and_parked_rows_forward(self, cache, tmp_path):
        network = SimpleNamespace(network_state="NO_NETWORK")
        runtime = SimpleNamespace(storage=cache.storage, queue_floor_generation=0, network=network)
        entries = SimpleNamespace(upload_screenshot=_offline)
        service = SyncService(runtime, cache, entries, SimpleNamespace())

        backed_off = _capture(cache, tmp_path, "backed-off")
        parked = _capture(cache, tmp_path, "parked", entry_id=101)
        cache.fail_screenshot("backed-off", "network down")
        for _ in range(9):
            cache.fail_screenshot("backed-off", "network down")   # a long backoff
        cache.park_screenshot("parked", "HTTP 404")
        assert cache.get_pending_screenshots() == [], "neither is due while offline"

        # Offline: the consumer holds.
        assert service.tick() == service.HOLD_INTERVAL_MS
        assert service.state == ServiceState.DEGRADED

        # Back online. The hold ends and both rows are attempted now.
        from background_services.network import NetworkState

        network.network_state = next(iter(NetworkState.USABLE))
        uploads = []
        entries.upload_screenshot = lambda entry_id, *a, **k: uploads.append(entry_id) or _confirmed()
        service.tick()

        assert sorted(uploads) == [100, 101]
        assert not backed_off.exists() and not parked.exists()
        assert cache.count_screenshots_by_status() == {}

    def test_signing_in_again_retries_a_screenshot_held_on_a_401(self, sync, cache, tmp_path):
        service, entries = sync
        path = _capture(cache, tmp_path)
        entries.upload_screenshot = _refusing(401)
        service._sync_screenshots()
        assert _row(cache)["status"] == "failed"
        assert service._should_hold() == "awaiting re-authentication"

        service.wake = lambda: None
        service.resume_after_auth()
        assert _row(cache)["status"] == "pending"
        assert _row(cache)["retry_count"] == 0
        entries.upload_screenshot = lambda *a, **k: _confirmed()
        service._sync_screenshots()
        assert not path.exists()

    def test_reviving_never_touches_a_row_that_is_still_pending(self, cache, tmp_path):
        _capture(cache, tmp_path, "pending-one")
        cache.fail_screenshot("pending-one", "blip")
        before = _row(cache, "pending-one")
        assert cache.revive_parked_screenshots() == 0
        assert _row(cache, "pending-one") == before


class TestBudgetedFailuresStillWork:
    """The explicit budget stays available to callers and tests that want it."""

    def test_an_explicit_budget_parks_once_it_is_spent(self, cache, tmp_path):
        _capture(cache, tmp_path)
        for _ in range(3):
            assert cache.fail_screenshot("uuid-1", "x", max_retries=3) is True
        assert cache.fail_screenshot("uuid-1", "x", max_retries=3) is False
        assert _row(cache)["status"] == "failed"

    def test_the_default_has_no_budget(self, cache, tmp_path):
        _capture(cache, tmp_path)
        for _ in range(500):
            assert cache.fail_screenshot("uuid-1", "x") is True
        assert _row(cache)["status"] == "pending"

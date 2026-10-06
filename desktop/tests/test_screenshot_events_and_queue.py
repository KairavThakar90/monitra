"""The durable half of "a missing screenshot is never silent".

`test_screenshot_resilience.py` pins the scheduler. These pin what happens to
its record afterwards, on disk and on the wire:

* a capture event survives a restart and reaches the backend exactly once;
* an upload that keeps failing is reported -- once, on the transition, and not
  for the ordinary retry that lands a minute later;
* a session that ends *involuntarily* no longer takes the user's queued
  screenshots, and the files behind them, with it.
"""
from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.api.exceptions import ApiError
from background_services.screenshot import config
from background_services.sync.sync_service import SyncService


# ── the event queue ──────────────────────────────────────────────────────────

class TestEventQueue:
    def _save(self, cache, event_id="e1", **kw):
        kw.setdefault("reason", "screen_unreadable")
        return cache.save_screenshot_event(
            event_id, kw.pop("state", "failed"), "2026-10-06T05:00:00+00:00",
            "2026-10-06T05:08:00+00:00", **kw,
        )

    def test_an_event_is_idempotent_on_its_id(self, cache):
        self._save(cache, "e1")
        self._save(cache, "e1")
        assert cache.count_screenshot_events() == 1

    def test_a_long_detail_is_bounded(self, cache):
        self._save(cache, "e1", detail="x" * 5000)
        row = cache.storage.query_one("SELECT detail FROM pending_screenshot_events")
        assert len(row["detail"]) == 500

    def test_events_come_back_oldest_first_and_bounded(self, cache):
        for n in range(5):
            self._save(cache, f"e{n}")
            time.sleep(0.002)
        got = cache.get_pending_screenshot_events(limit=3)
        assert [e["id"] for e in got] == ["e0", "e1", "e2"]

    def test_a_completed_event_is_gone(self, cache):
        self._save(cache, "e1")
        cache.complete_screenshot_events(["e1"])
        assert cache.count_screenshot_events() == 0

    def test_a_failed_send_backs_off_with_jitter_and_keeps_the_event(self, cache):
        self._save(cache, "e1")
        cache.fail_screenshot_events(["e1"])
        assert cache.get_pending_screenshot_events() == []
        row = cache.storage.query_one("SELECT status, retry_count, next_retry_at "
                                      "FROM pending_screenshot_events")
        assert (row["status"], row["retry_count"]) == ("pending", 1)
        assert row["next_retry_at"] > time.time()

    def test_an_event_that_exhausts_its_budget_is_parked_then_revived_at_launch(self, cache):
        self._save(cache, "e1")
        for _ in range(cache.SCREENSHOT_EVENT_MAX_RETRIES + 1):
            cache.fail_screenshot_events(["e1"])
        assert cache.storage.query_one(
            "SELECT status FROM pending_screenshot_events")["status"] == "failed"
        cache.requeue_telemetry_for_new_run()
        row = cache.storage.query_one("SELECT status, retry_count FROM pending_screenshot_events")
        assert (row["status"], row["retry_count"]) == ("pending", 0)

    def test_an_event_survives_the_process_that_wrote_it(self, tmp_path):
        from storage.manager import StorageManager
        from sync.local_cache import LocalCache

        path = str(tmp_path / "restart.db")
        first = LocalCache(storage=StorageManager(path))
        self._save(first, "e1", time_entry_id=42, client_screenshot_id="shot-1")
        first.storage.close()

        second = LocalCache(storage=StorageManager(path))
        (event,) = second.get_pending_screenshot_events()
        assert (event["id"], event["time_entry_id"], event["client_screenshot_id"]) == (
            "e1", 42, "shot-1")

    def test_logout_clears_events_with_the_screenshots(self, cache):
        self._save(cache, "e1")
        cache.clear_screenshots()
        assert cache.count_screenshot_events() == 0

    def test_a_different_users_events_are_discarded_and_ones_own_are_kept(self, cache):
        self._save(cache, "mine", owner_user_id=9)
        self._save(cache, "theirs", owner_user_id=3)
        self._save(cache, "unknown")
        cache.discard_screenshot_events_not_owned_by(9)
        ids = {r["id"] for r in cache.storage.query_all("SELECT id FROM pending_screenshot_events")}
        assert ids == {"mine", "unknown"}


# ── ownership of queued captures ─────────────────────────────────────────────

def _capture(cache, tmp_path, name, owner=None, entry_id=100):
    path = tmp_path / f"{name}.webp"
    path.write_bytes(b"RIFF0000WEBPimage-bytes")
    cache.save_screenshot(
        client_screenshot_id=name, local_file_path=str(path),
        captured_at="2026-10-06T05:04:00+00:00", window_start="2026-10-06T05:00:00+00:00",
        width=1000, height=1000, file_size_bytes=path.stat().st_size,
        time_entry_id=entry_id, owner_user_id=owner,
    )
    return path


class TestQueuedCapturesBelongToSomeone:
    def test_only_another_users_captures_are_discarded(self, cache, tmp_path):
        _capture(cache, tmp_path, "mine", owner=9)
        _capture(cache, tmp_path, "theirs", owner=3)
        _capture(cache, tmp_path, "legacy", owner=None)

        gone = cache.discard_screenshots_not_owned_by(9)

        assert [Path(p).name for p in gone] == ["theirs.webp"]
        left = {r["client_screenshot_id"] for r in cache.get_pending_screenshots()}
        assert left == {"mine", "legacy"}

    def test_an_unknown_user_discards_nothing(self, cache, tmp_path):
        _capture(cache, tmp_path, "theirs", owner=3)
        assert cache.discard_screenshots_not_owned_by(None) == []
        assert len(cache.get_pending_screenshots()) == 1

    def test_the_queue_summary_reports_what_the_person_waits_on(self, cache, tmp_path):
        _capture(cache, tmp_path, "a", owner=9)
        _capture(cache, tmp_path, "b", owner=9, entry_id=None)
        cache.storage.execute("UPDATE pending_screenshots SET created_at = ? WHERE id = 'a'",
                              (time.time() - 300,))
        summary = cache.screenshot_queue_summary()
        assert summary["uploading"] == 1
        assert summary["unattributed"] == 1
        assert summary["parked"] == 0
        assert 290 < summary["oldest_pending_age"] < 400

    def test_an_empty_queue_summarises_as_nothing_not_as_an_error(self, cache):
        assert cache.screenshot_queue_summary() == {
            "uploading": 0, "unattributed": 0, "parked": 0,
            "max_retry_count": 0, "oldest_pending_age": 0.0,
        }


class TestTheSessionEndingDoesNotCostTheUserTheirCaptures:
    """Production risk: a 401 that the silent refresh could not resolve ends the
    session through `on_logout`, which deleted every queued screenshot and its
    file -- though the same person signs straight back in and `resume_after_auth`
    exists to revive exactly those rows."""

    def test_an_involuntary_sign_out_keeps_the_queue(self, runtime, tmp_path):
        cache = runtime.cache
        path = _capture(cache, tmp_path, "mine", owner=9)

        runtime.on_logout(involuntary=True)

        assert len(cache.get_pending_screenshots()) == 1
        assert path.exists(), "the file is the only copy until the backend confirms it"

    def test_the_same_user_signing_back_in_keeps_and_resumes_it(self, runtime, tmp_path):
        cache = runtime.cache
        path = _capture(cache, tmp_path, "mine", owner=9)
        runtime.on_logout(involuntary=True)

        runtime.on_login(9)

        assert len(cache.get_pending_screenshots()) == 1
        assert path.exists()

    def test_a_different_user_signing_in_does_not_inherit_the_captures(self, runtime, tmp_path):
        cache = runtime.cache
        mine = _capture(cache, tmp_path, "theirs", owner=9)
        runtime.on_logout(involuntary=True)

        runtime.on_login(3)

        assert cache.get_pending_screenshots() == []
        assert not mine.exists(), "one person's screen is not left on disk under another's session"

    def test_a_deliberate_sign_out_still_discards_the_queue(self, runtime, tmp_path):
        cache = runtime.cache
        path = _capture(cache, tmp_path, "mine", owner=9)

        runtime.on_logout()

        assert cache.get_pending_screenshots() == []
        assert not path.exists()

    def test_resume_wakes_the_screenshot_schedule(self, runtime, monkeypatch):
        told = []
        monkeypatch.setattr(runtime.screenshot, "on_system_resumed", told.append)
        runtime._on_system_resumed(900.0)
        assert told == [900.0]


# ── the wire ─────────────────────────────────────────────────────────────────

@pytest.fixture
def sync(cache):
    runtime = SimpleNamespace(storage=cache.storage, queue_floor_generation=0)
    entries = SimpleNamespace(upload_screenshot=None, record_screenshot_events=None)
    return SyncService(runtime, cache, entries, SimpleNamespace()), entries


def _event(cache, event_id, **kw):
    cache.save_screenshot_event(
        event_id, kw.pop("state", "failed"), "2026-10-06T05:00:00+00:00",
        "2026-10-06T05:08:00+00:00", reason="screen_unreadable", attempts=6, **kw,
    )


class TestEventsReachTheBackendOnce:
    def test_a_batch_is_sent_and_removed(self, sync, cache):
        service, entries = sync
        for n in range(3):
            _event(cache, f"e{n}")
        sent = []
        entries.record_screenshot_events = lambda events: sent.append(events) or {"accepted": 3}

        service._sync_screenshot_events()

        assert [e["client_event_id"] for e in sent[0]] == ["e0", "e1", "e2"]
        assert cache.count_screenshot_events() == 0

    def test_the_payload_carries_no_free_text(self, sync, cache):
        service, entries = sync
        cache.save_screenshot_event("e1", "failed", "2026-10-06T05:00:00+00:00",
                                    "2026-10-06T05:08:00+00:00", reason="store_failed",
                                    detail="C:\\Users\\someone\\private path")
        sent = []
        entries.record_screenshot_events = lambda events: sent.append(events) or {}
        service._sync_screenshot_events()
        assert "detail" not in sent[0][0]
        assert "private" not in str(sent)

    def test_an_unreachable_backend_keeps_the_events_and_the_same_ids(self, sync, cache):
        service, entries = sync
        _event(cache, "e1")
        attempts = []

        def offline(events):
            attempts.append([e["client_event_id"] for e in events])
            raise ApiError("Network connection error")

        entries.record_screenshot_events = offline
        service._sync_screenshot_events()
        assert cache.count_screenshot_events() == 1
        cache.storage.execute("UPDATE pending_screenshot_events SET next_retry_at = 0")
        service._sync_screenshot_events()
        assert attempts == [["e1"], ["e1"]], "the retry carries the same idempotency key"

    def test_a_backend_without_the_endpoint_does_not_lose_the_events(self, sync, cache):
        service, entries = sync
        _event(cache, "e1")

        def missing(events):
            raise ApiError("not found", status_code=404)

        entries.record_screenshot_events = missing
        service._sync_screenshot_events()
        assert cache.count_screenshot_events() == 1

    def test_a_full_batch_asks_to_be_called_again_promptly(self, sync, cache):
        service, entries = sync
        for n in range(service.SCREENSHOT_EVENT_BATCH + 5):
            _event(cache, f"e{n:03d}")
        entries.record_screenshot_events = lambda events: {}
        assert service._sync_screenshot_events() is True
        assert cache.count_screenshot_events() == 5
        assert service._sync_screenshot_events() is False

    def test_nothing_queued_means_no_request(self, sync, cache):
        service, entries = sync
        entries.record_screenshot_events = lambda events: pytest.fail("no request expected")
        assert service._sync_screenshot_events() is False


def _confirmed(file_id="drive-1"):
    return {"success": True, "duplicate": False,
            "screenshot": {"id": 1, "google_drive_file_id": file_id}}


def _offline(*args, **kwargs):
    raise ApiError("Failed to upload screenshot: Network connection error")


def _refusing(status):
    def upload(*args, **kwargs):
        raise ApiError("refused", status_code=status)
    return upload


def _events(cache):
    return cache.storage.query_all(
        "SELECT event_state, reason, attempts, client_screenshot_id, owner_user_id "
        "FROM pending_screenshot_events ORDER BY created_at")


def _age(cache, seconds):
    cache.storage.execute("UPDATE pending_screenshots SET created_at = ?",
                          (time.time() - seconds,))
    cache.storage.execute("UPDATE pending_screenshots SET next_retry_at = 0")


class TestAStuckUploadIsReportedOnTheTransition:
    def test_an_ordinary_retry_is_not_reported(self, sync, cache, tmp_path):
        service, entries = sync
        _capture(cache, tmp_path, "uuid-1", owner=9)
        entries.upload_screenshot = _offline
        service._sync_screenshots()          # a blip: seconds old
        assert _events(cache) == []

    def test_an_upload_failing_for_minutes_is_reported_once(self, sync, cache, tmp_path):
        service, entries = sync
        _capture(cache, tmp_path, "uuid-1", owner=9)
        entries.upload_screenshot = _refusing(502)
        _age(cache, 400)
        for _ in range(5):
            service._sync_screenshots()
            _age(cache, 400)
        rows = _events(cache)
        assert len(rows) == 1, "reported on the transition, not on every attempt"
        assert rows[0]["event_state"] == "upload_retrying"
        assert rows[0]["reason"] == "http_502"
        assert rows[0]["client_screenshot_id"] == "uuid-1"
        assert rows[0]["owner_user_id"] == 9

    def test_a_refusal_is_reported_as_parked_and_names_the_status(self, sync, cache, tmp_path):
        service, entries = sync
        _capture(cache, tmp_path, "uuid-1", owner=9)
        entries.upload_screenshot = _refusing(422)
        service._sync_screenshots()
        rows = _events(cache)
        assert [(r["event_state"], r["reason"]) for r in rows] == [("upload_parked", "http_422")]

    def test_retrying_then_parked_is_two_reports_one_per_transition(self, sync, cache, tmp_path):
        service, entries = sync
        _capture(cache, tmp_path, "uuid-1", owner=9)
        _age(cache, 400)
        entries.upload_screenshot = _offline
        service._sync_screenshots()
        _age(cache, 400)
        entries.upload_screenshot = _refusing(413)
        service._sync_screenshots()
        assert [r["event_state"] for r in _events(cache)] == ["upload_retrying", "upload_parked"]

    def test_a_capture_that_uploads_leaves_no_event_and_records_the_confirmation(
        self, sync, cache, tmp_path
    ):
        service, entries = sync
        path = _capture(cache, tmp_path, "uuid-1", owner=9)
        entries.upload_screenshot = lambda *a, **k: _confirmed()
        service._sync_screenshots()
        assert _events(cache) == []
        assert not path.exists()
        last = cache.load_app_state(config.LAST_UPLOAD_STATE_KEY)
        assert last and last["at"], "the status can now say the screenshot was uploaded"

    def test_an_unconfirmed_success_is_not_recorded_as_an_upload(self, sync, cache, tmp_path):
        service, entries = sync
        _capture(cache, tmp_path, "uuid-1", owner=9)
        entries.upload_screenshot = lambda *a, **k: {"success": True}
        service._sync_screenshots()
        assert cache.load_app_state(config.LAST_UPLOAD_STATE_KEY) is None, (
            "nothing is reported uploaded until the backend names the Drive file"
        )

    def test_reason_codes_are_short_stable_words(self):
        code = SyncService._upload_reason_code
        assert code("HTTP 502") == "http_502"
        assert code("network") == "network"
        assert code("") == "unknown"
        assert code("A very odd / reason: with  stuff!") == "a_very_odd_reason_with_stuff"
        assert len(code("x" * 500)) <= 60

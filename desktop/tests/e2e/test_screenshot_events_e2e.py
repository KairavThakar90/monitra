"""
A window with no image is explained, end to end.

The scenarios behind "No capture" on the admin screenshots page, against a real
backend process, the development database, the real screen and the real Drive:

* the screen cannot be read -> the desktop records why -> the backend shows why
  on the real timeline -> the screen returns -> the image replaces the
  explanation (and the history is kept, never edited);
* Drive refuses the upload -> the desktop keeps the image, reports that it is
  stuck, once -> the timeline shows *pending*, not "No capture" -> Drive
  returns -> the image lands and replaces it;
* stop/start repeatedly -> one schedule, never a missing or doubled one.

Each test gets its **own** disposable principal. They cannot share one: a window
that already holds an image rightly outranks any report about it, so a second
test capturing in the same ten minutes would read "captured" before it began.
The principal, its time entries, its capture events (they cascade from the user)
and every Drive object are removed in teardown.

Opt-in, like the rest of this directory:

    MONITRA_E2E=1 python -m pytest tests/e2e/test_screenshot_events_e2e.py -q -s   (from desktop/)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from tests.e2e.test_screenshot_lifecycle_e2e import (  # noqa: F401  (fixtures and helpers)
    BACKEND_ROOT, _capture_windows, _drain, _pump, _queue_row, _stored_rows,
    api, backend, broken_drive_backend, desktop, drive_litter, drive_required,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("MONITRA_E2E") != "1",
    reason="real-backend E2E; set MONITRA_E2E=1 to run",
)


@pytest.fixture
def principal():
    """A disposable employee for this one test (see the module docstring)."""
    helper = BACKEND_ROOT / "tests" / "e2e_support.py"
    out = subprocess.run(
        [sys.executable, str(helper), "provision"], cwd=str(BACKEND_ROOT),
        capture_output=True, text=True, check=True,
    ).stdout.strip().splitlines()[-1]
    fixture = json.loads(out)
    try:
        yield fixture
    finally:
        subprocess.run(
            [sys.executable, str(helper), "cleanup", json.dumps(fixture)],
            cwd=str(BACKEND_ROOT), capture_output=True, text=True, check=False,
        )


@pytest.fixture
def db(principal):
    engine = create_engine(principal["database_url"], pool_pre_ping=True)
    yield engine
    engine.dispose()


def _timeline(api, window_start_iso: str) -> dict:
    """The real timeline window that starts at `window_start_iso` (IST day of it)."""
    from datetime import datetime, timedelta, timezone

    start = datetime.fromisoformat(window_start_iso)
    ist_day = (start.astimezone(timezone(timedelta(hours=5, minutes=30)))).date().isoformat()
    body = api.get("/time-entry-screenshots/timeline", params={"date": ist_day})
    assert body.status_code == 200, body.text
    for window in body.json()["windows"]:
        if datetime.fromisoformat(window["window_start"]) == start:
            return window
    raise AssertionError(f"no window starting {window_start_iso} in the timeline")


def _event_rows(db, user_id: int, client_screenshot_id: str = None) -> list:
    from sqlalchemy import text

    with db.connect() as conn:
        return [dict(r) for r in conn.execute(
            text("SELECT client_event_id, state, reason, attempts, time_entry_id, "
                 "       window_start, client_screenshot_id "
                 "FROM time_entry_screenshot_events WHERE user_id = :u "
                 "ORDER BY occurred_at, id"),
            {"u": user_id},
        ).mappings().all() if client_screenshot_id is None
            or r["client_screenshot_id"] == client_screenshot_id]


def test_a_failed_window_is_explained_on_the_real_timeline_then_overtaken_by_the_image(
    qapp, desktop, api, db, principal, drive_litter, monkeypatch
):
    """Capture fails -> the desktop records why -> the backend shows why -> the
    screen comes back -> the image replaces the explanation.

    The screen is made unreadable the way a locked machine makes it (the grab
    returns nothing); the scheduler, the queue, the uploader, HTTP, Postgres and
    the timeline endpoint are all the real ones. Retries are collapsed to one
    attempt so the window's failure is final at once -- the retry schedule itself
    is covered, without a clock, by `tests/test_screenshot_resilience.py`.
    """
    from background_services.screenshot import capture as capture_mod
    from background_services.screenshot import config, scheduler

    timer = desktop.timer
    timer.start_tracking(principal["project_id"], principal["task_id"], "E2E explained window")
    _pump(qapp, lambda: timer.entry_id is not None, 30, "the start to be bound")
    entry_id = timer.entry_id
    service = desktop.screenshot

    deadline = time.monotonic() + 60
    while not service._privacy_config_loaded and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.05)
    assert service._privacy_config_loaded

    window_index = scheduler.window_index(time.time(), config.window_seconds())
    window_start = scheduler.window_bounds(window_index, config.window_seconds())[0]
    from datetime import datetime, timezone

    window_start_iso = datetime.fromtimestamp(window_start, tz=timezone.utc).isoformat()

    # ── the screen cannot be read ──
    real_capture = capture_mod.capture_all_displays
    monkeypatch.setattr(capture_mod, "capture_all_displays", lambda: None)
    service.CAPTURE_MAX_ATTEMPTS = 1
    service._outcome = None
    service._roll_window(window_index)
    result = service._capture_now(window_index, service._current_generation())
    assert result["failed"] == "screen_unreadable"
    service._on_captured(result)

    # Written to the durable queue on the pool, then delivered by SyncService.
    desktop.sync.wake()
    _pump(qapp, lambda: len(_event_rows(db, principal["user_id"])) == 1, 60,
          "the event to be queued, delivered and recorded by the backend")
    (event,) = _event_rows(db, principal["user_id"])
    assert (event["state"], event["reason"]) == ("failed", "screen_unreadable")
    assert event["time_entry_id"] == entry_id
    assert event["attempts"] == 1
    _pump(qapp, lambda: desktop.cache.count_screenshot_events() == 0, 30,
          "the local event queue to drain")

    # The timeline now says why, instead of nothing.
    window = _timeline(api, window_start_iso)
    assert window["capture_state"] == "failed"
    assert window["capture_reason"] == "screen_unreadable"
    assert window["screenshot_count"] == 0

    # Replaying the same event records nothing twice, and says so.
    replay = api.post("/time-entry-screenshots/capture-events", json={"events": [{
        "client_event_id": event["client_event_id"], "state": "failed",
        "window_start": window_start_iso, "occurred_at": window_start_iso,
        "reason": "screen_unreadable", "attempts": 1,
    }]})
    assert replay.status_code == 200 and replay.json()["duplicates"] == 1, replay.text
    assert len(_event_rows(db, principal["user_id"])) == 1

    # The grid shows the member even though they have no image all day.
    grid = api.get("/time-entry-screenshots/day").json()
    mine = [m for m in grid["members"] if m["user_id"] == principal["user_id"]]
    assert mine and mine[0]["screenshot_count"] == 0

    # ── the screen comes back: the image outranks the explanation ──
    monkeypatch.setattr(capture_mod, "capture_all_displays", real_capture)
    record = service._capture_now(window_index, service._current_generation())
    assert "window_start" in record, f"capture was withheld: {record}"
    service._on_captured(record)
    _drain(qapp, desktop, 1, db, lambda: entry_id, "the capture to be stored")
    rows = _stored_rows(db, entry_id)
    drive_litter.append(rows[0]["google_drive_file_id"])
    window = _timeline(api, window_start_iso)
    assert window["capture_state"] == "captured"
    assert window["screenshot_count"] == 1
    assert len(_event_rows(db, principal["user_id"])) == 1, "history is kept, never edited"

    timer.stop_tracking()
    _pump(qapp, lambda: not timer.is_running(), 30, "the stop")


def test_an_upload_stuck_on_drive_shows_as_pending_then_as_the_image(
    qapp, desktop, api, db, principal, drive_litter, broken_drive_backend, backend
):
    """Drive refuses, the desktop keeps trying and says so, Drive returns.

    Against a backend whose Drive root is gone the upload is refused with a 5xx
    and the capture stays queued with its file. Once it has been outstanding
    long enough the desktop reports it -- through the same broken backend, whose
    database is fine -- and the real timeline shows the window as *pending*, not
    "No capture". When Drive is back the image lands and replaces it.
    """
    desktop.api_client.base_url = broken_drive_backend
    timer = desktop.timer
    timer.start_tracking(principal["project_id"], principal["task_id"], "E2E pending upload")
    _pump(qapp, lambda: timer.entry_id is not None, 30, "the start to be bound")
    entry_id = timer.entry_id

    (record,) = _capture_windows(desktop, 1)
    client_id = record["client_screenshot_id"]
    window_start_iso = record["window_start"]

    desktop.sync.wake()
    _pump(qapp, lambda: _queue_row(desktop, client_id).get("retry_count", 0) >= 1, 90,
          "the first upload attempt to be refused")
    assert _event_rows(db, principal["user_id"], client_id) == [], (
        "one refusal is not worth reporting"
    )

    # The capture has now been waiting minutes: age it and let the next attempt run.
    desktop.cache.storage.execute(
        "UPDATE pending_screenshots SET created_at = ?, next_retry_at = 0 "
        "WHERE client_screenshot_id = ?", (time.time() - 400, client_id))
    desktop.sync.wake()
    _pump(qapp, lambda: len(_event_rows(db, principal["user_id"], client_id)) >= 1, 90,
          "the stuck upload to be reported")
    (event,) = _event_rows(db, principal["user_id"], client_id)
    assert event["state"] == "upload_retrying"
    assert event["reason"] in ("http_502", "http_503")
    assert event["client_screenshot_id"] == client_id

    window = _timeline(api, window_start_iso)
    assert window["capture_state"] == "pending", window
    assert window["screenshot_count"] == 0
    assert Path(_queue_row(desktop, client_id)["local_file_path"]).exists()

    # Drive is back.
    desktop.api_client.base_url = backend
    desktop.cache.make_screenshots_ready()
    _drain(qapp, desktop, 1, db, lambda: entry_id, "the capture to land once Drive is back", timeout=180)
    rows = _stored_rows(db, entry_id)
    assert len(rows) == 1
    drive_litter.append(rows[0]["google_drive_file_id"])
    window = _timeline(api, window_start_iso)
    assert window["capture_state"] == "captured"
    assert len(_event_rows(db, principal["user_id"], client_id)) == 1, (
        "exactly one report for the whole episode"
    )

    timer.stop_tracking()
    _pump(qapp, lambda: not timer.is_running(), 30, "the stop")


def test_stop_start_cycles_keep_exactly_one_live_schedule(
    qapp, desktop, db, principal, drive_litter
):
    """Stop/start repeatedly: one schedule, no duplicate capture in the window."""
    timer = desktop.timer
    for _ in range(3):
        timer.start_tracking(principal["project_id"], principal["task_id"], "E2E cycles")
        _pump(qapp, lambda: timer.entry_id is not None, 30, "the start to be bound")
        assert desktop.screenshot._tracking and desktop.screenshot._due_timer.isActive()
        timer.stop_tracking()
        _pump(qapp, lambda: not timer.is_running(), 30, "the stop")
        assert not desktop.screenshot._tracking
    timer.start_tracking(principal["project_id"], principal["task_id"], "E2E cycles")
    _pump(qapp, lambda: timer.entry_id is not None, 30, "the final start")
    assert desktop.screenshot._due_timer.isActive()
    timer.stop_tracking()
    _pump(qapp, lambda: not timer.is_running(), 30, "the stop")



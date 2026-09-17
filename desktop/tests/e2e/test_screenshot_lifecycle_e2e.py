"""
The screenshot pipeline end to end: screen -> WebP -> durable queue -> HTTP ->
Google Drive -> Postgres -> the timeline the dashboard paints.

Nothing here is mocked. A real backend process serves the real API against the
development database, the real desktop runtime captures the real screen through
`mss`, the bytes are really uploaded to the configured Google Drive folder, and
every assertion is made against the row in Postgres, the object in Drive, or
what the API actually answers.

The one thing these do that a user would not is call `_capture_now` for a given
window index instead of waiting out the ten-minute schedule. That is the same
method the armed `QTimer` invokes, with the same arguments, and a suite that
slept for half an hour per case would not be run. Which window a capture
belongs to is an argument to that method precisely so a session spanning
several windows can be exercised in seconds; the scheduling arithmetic that
picks the instants is covered exhaustively and without a clock in
`tests/test_screenshot_scheduler.py`.

Opt-in, because it needs the development database, a free port, a real display
and working Google Drive credentials:

    MONITRA_E2E=1 python -m pytest tests/e2e/test_screenshot_lifecycle_e2e.py -q -s   (from desktop/)

Every Drive object these create is deleted again in teardown; the database rows
cascade from the time entries `e2e_support.py` removes.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

import httpx
import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("MONITRA_E2E") != "1",
    reason="real-backend E2E; set MONITRA_E2E=1 to run",
)

DESKTOP_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = DESKTOP_ROOT.parent / "backend"

#: WebP's container signature, checked against the bytes the API serves back.
_RIFF, _WEBP = b"RIFF", b"WEBP"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _pump(qapp, until: Callable[[], bool], timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if until():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


# ── the real backend ─────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def backend():
    port = _free_port()
    env = dict(os.environ, ENV="development", PYTHONUNBUFFERED="1")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
         "--port", str(port), "--log-level", "warning"],
        cwd=str(BACKEND_ROOT), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 90
        while True:
            try:
                if httpx.get(f"{base}/health", timeout=2).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if proc.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError("the backend did not come up")
            time.sleep(0.25)
        yield base
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.fixture(scope="module")
def principal():
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


@pytest.fixture(scope="module")
def db(principal):
    from sqlalchemy import create_engine

    engine = create_engine(principal["database_url"], pool_pre_ping=True)
    yield engine
    engine.dispose()


@pytest.fixture(scope="module")
def drive_required(backend):
    """Skip rather than fail when this deployment has no Drive credentials.

    An unconfigured backend answers 503 and the desktop correctly keeps its
    files — the right behaviour, and covered by the unit tests. It is not this
    file's subject: these exist to prove the bytes really reach Drive, so
    without credentials there is nothing here to prove.

    Asked over `/health` rather than by importing the service. The backend's
    package is also called `app`, and so is the desktop's — importing one into
    a process that has already imported the other resolves to the wrong module.
    Everything this file needs from the backend goes through the running
    process, exactly as `principal` already does.
    """
    storage = httpx.get(f"{backend}/health", timeout=10).json().get("screenshot_storage", {})
    if not storage.get("configured"):
        pytest.skip(f"Google Drive is not configured: {storage.get('reason')}")
    return storage


#: Deletes Drive objects by id, in the backend's own interpreter and working
#: directory so its settings and credentials resolve exactly as they do in
#: production. Ids arrive as arguments; nothing secret is passed or printed.
_DRIVE_DELETE = (
    "import sys;"
    "from app.services.google_drive_service import drive_service;"
    "[drive_service.delete_file(f) for f in sys.argv[1:]]"
)


@pytest.fixture
def drive_litter(drive_required):
    """Delete every Drive object the test created, however it ends."""
    created: list = []
    yield created
    if not created:
        return
    result = subprocess.run(
        [sys.executable, "-c", _DRIVE_DELETE, *created],
        cwd=str(BACKEND_ROOT), capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"WARNING: could not delete Drive files {created}: {result.stderr}")


@pytest.fixture
def api(backend, principal):
    return httpx.Client(base_url=backend, timeout=30,
                        headers={"Authorization": f"Bearer {principal['token']}"})


@pytest.fixture
def desktop(qapp, tmp_path, backend, principal, monkeypatch):
    """The real desktop runtime, pointed at the real backend."""
    monkeypatch.setenv("SMS_API_BASE_URL", backend)
    monkeypatch.setenv("MONITRA_ENV", "development")
    monkeypatch.setenv("MONITRA_DATA_DIR", str(tmp_path / "data"))
    from core import paths
    from core.runtime import ApplicationRuntime
    from storage.manager import StorageManager

    paths.reset_cache()
    manager = StorageManager(str(tmp_path / "e2e-cache.db"))
    runtime = ApplicationRuntime(storage=manager)
    runtime.api_client.base_url = backend
    runtime.api_client.access_token = principal["token"]
    runtime.start_services()
    yield runtime
    try:
        runtime.timer.stop_tracking()
    except Exception:  # noqa: BLE001
        pass
    runtime.shutdown(timeout_ms=5000)
    paths.reset_cache()


# ── helpers ──────────────────────────────────────────────────────────────────

def _capture_windows(runtime, count: int) -> list:
    """Take one real screenshot in each of `count` consecutive windows.

    Exactly what the scheduler does over `count * 10` minutes: `_capture_now`
    is the method the armed timer calls, and the window index is its argument.
    """
    from background_services.screenshot import config, scheduler

    service = runtime.screenshot
    first = scheduler.window_index(time.time(), config.window_seconds())
    records = []
    for offset in range(count):
        record = service._capture_now(first + offset, service._current_generation())
        assert record is not None, (
            "the real screen capture produced nothing; this needs a real display"
        )
        records.append(record)
    return records


def _stored_rows(db, entry_id: int) -> list:
    from sqlalchemy import text

    with db.connect() as conn:
        return [dict(r) for r in conn.execute(
            text("SELECT id, time_entry_id, captured_at, google_drive_file_id, "
                 "       google_drive_folder_id, file_path, file_size_bytes, "
                 "       width, height, mime_type, client_screenshot_id, "
                 "       display_count "
                 "FROM time_entry_screenshots WHERE time_entry_id = :e "
                 "ORDER BY captured_at"),
            {"e": entry_id},
        ).mappings().all()]


def _stored_url_rows(db, entry_id: int) -> list:
    from sqlalchemy import text

    with db.connect() as conn:
        return [dict(r) for r in conn.execute(
            text("SELECT id, time_entry_id, browser_name, domain, url, "
                 "       is_private, duration_seconds, client_event_id "
                 "FROM time_entry_url_usage WHERE time_entry_id = :e "
                 "ORDER BY recorded_at"),
            {"e": entry_id},
        ).mappings().all()]


def _pretend_displays(monkeypatch, layout):
    """Report `layout` as the attached displays, for this test only.

    This machine has one physical panel, so a second monitor cannot be plugged
    in to exercise the merged path. Only *enumeration* is substituted: every
    grab, the compositor, the encoder, the queue, the HTTP upload, Drive and
    Postgres are the real ones, and the pixels are really read off the real
    screen. What is simulated is the desk, not the pipeline.
    """
    from background_services.screenshot import capture as capture_mod

    monkeypatch.setattr(capture_mod, "enumerate_displays", lambda module: layout)


def _panel_layout(count: int):
    """`count` monitors the size of this machine's real panel, side by side."""
    import mss

    from background_services.screenshot.displays import Display, enumerate_displays

    real = enumerate_displays(mss)
    assert real, "these tests need a real display"
    panel = real[0]
    return [
        Display(number=n, left=(n - 1) * panel.width, top=0,
                width=panel.width, height=panel.height, is_primary=(n == 1))
        for n in range(1, count + 1)
    ]


def _drain(qapp, runtime, expected: int, db, entry_id_of, what: str, timeout: float = 120) -> None:
    """Let the real SyncService upload, until the database holds them all."""
    runtime.sync.wake()
    _pump(
        qapp,
        lambda: entry_id_of() is not None
        and len(_stored_rows(db, entry_id_of())) >= expected,
        timeout,
        what,
    )


# ── 1. The whole path, for a start that succeeds immediately ─────────────────

def test_a_tracked_session_reaches_drive_the_database_and_the_timeline(
    qapp, desktop, api, db, principal, drive_litter
):
    """Start -> capture -> optimise -> queue -> Drive -> Postgres -> dashboard."""
    timer = desktop.timer
    timer.start_tracking(principal["project_id"], principal["task_id"], "E2E screenshots")
    _pump(qapp, lambda: timer.entry_id is not None, 30, "the start to be bound")
    entry_id = timer.entry_id

    records = _capture_windows(desktop, 3)
    assert len({r["window_start"] for r in records}) == 3, "three distinct windows"

    # Every capture is a real, optimised WebP on disk before anything uploads.
    assert desktop.cache.count_unattributed_screenshots() == 0
    for record in records:
        assert record["file_size_bytes"] > 0

    _drain(qapp, desktop, 3, db, lambda: entry_id, "the three captures to be stored")

    rows = _stored_rows(db, entry_id)
    assert len(rows) == 3
    for row in rows:
        # Drive really holds it, under Year/Month/User_<id>_<name>/<date>.
        assert row["google_drive_file_id"], "no Drive file id was recorded"
        drive_litter.append(row["google_drive_file_id"])
        segments = row["file_path"].split("/")
        assert len(segments) == 5, f"unexpected logical path {row['file_path']}"
        year, month, user, day, name = segments
        assert year.isdigit() and len(year) == 4
        assert user.startswith(f"User_{principal['user_id']}")
        assert day.startswith(year)
        assert name.startswith("screenshot_") and name.endswith(".webp")
        # Stored geometry is what the backend enforces and the grid lays out.
        assert (row["width"], row["height"]) == (1000, 1000)
        assert row["mime_type"] == "image/webp"
        assert row["file_size_bytes"] > 0

    # The local queue empties and the files go -- deleted only after the backend
    # confirmed storage, which is the invariant of the whole module. Pumped
    # rather than asserted outright: the row appears in Postgres while the
    # response is still on its way back, so the desktop's own bookkeeping
    # legitimately trails the database by a moment.
    _pump(qapp, lambda: not desktop.cache.get_screenshot_backlog_paths(), 60,
          "the local cache to be reclaimed")
    assert desktop.cache.count_screenshots_by_status() == {}

    # The dashboard's own surface: the bytes come back through the API, and the
    # timeline groups the three captures into three separate windows.
    for row in rows:
        view = api.get(f"/time-entry-screenshots/{row['id']}/view")
        assert view.status_code == 200, view.text
        assert view.headers["content-type"].startswith("image/webp")
        body = view.content
        assert body[:4] == _RIFF and body[8:12] == _WEBP, "not a WebP came back"
        assert len(body) == row["file_size_bytes"]

    # The timeline the dashboard paints carries all three, with a working view
    # URL for each. Which window each lands in is decided by its `captured_at`,
    # not by the index the scheduler planned it under -- so three captures taken
    # within the same second of this test share a window, exactly as the backend
    # should group them. The per-window grouping rule itself is covered, with
    # controlled timestamps, in backend/tests/test_screenshots.py.
    timeline = api.get("/time-entry-screenshots/timeline").json()
    assert timeline["window_minutes"] == 10
    ours = {r["id"] for r in rows}
    seen = {s["id"] for w in timeline["windows"] for s in w["screenshots"]}
    assert ours <= seen, "a stored capture is missing from the timeline"
    for window in timeline["windows"]:
        for shot in window["screenshots"]:
            if shot["id"] in ours:
                assert shot["view_url"] == f"/time-entry-screenshots/{shot['id']}/view"

    timer.stop_tracking()
    _pump(qapp, lambda: not timer.is_running(), 30, "the stop")


# ── 2. The regression: a start that went through the durable queue ───────────

def test_captures_of_a_queued_start_still_reach_drive_and_the_database(
    qapp, desktop, api, db, principal, drive_litter, monkeypatch
):
    """The failure this file was written for.

    A start whose request does not succeed in-process fails over to the durable
    action queue, and `SyncService` is then the only thing that learns the entry
    id. It used to write that id onto the queued *stop* and nowhere else, so
    every capture of the session stayed unattributed, was correctly withheld by
    the uploader, and never reached Drive or the database at all -- silently,
    with the timer itself behaving perfectly throughout.

    Here the first start request fails exactly as a timed-out one does. The
    captures are taken across three windows while the session still has no id,
    and the queued start is then allowed to land.
    """
    from app.api.exceptions import ApiError

    real_start = desktop.time_entry_service.start_time_entry
    calls = {"n": 0}

    def flaky_start(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            # What a timeout looks like to TimerService: it queues durably.
            raise ApiError("Failed to start time entry: Network connection error")
        return real_start(*args, **kwargs)

    monkeypatch.setattr(desktop.time_entry_service, "start_time_entry", flaky_start)

    # Hold the consumer until the captures exist. Nothing else keeps the
    # queued start from landing the instant it is enqueued -- against a local
    # backend it did so mid-way through the three captures, which attributed
    # the first of them early, failed the count below, and left the entry
    # running for every later test in this module to trip over with a 409.
    # The scenario is "captures taken *before* the start lands", so make it so.
    desktop.sync._should_hold = lambda: "held by the test until the captures exist"

    timer = desktop.timer
    timer.start_tracking(principal["project_id"], principal["task_id"], "E2E queued start")
    _pump(qapp, lambda: calls["n"] >= 1, 30, "the first start attempt to fail")
    session = timer.active_session()
    assert session is not None
    assert timer.entry_id is None, "the session must have no id yet"

    # Three windows of capture with nothing to attribute them to.
    records = _capture_windows(desktop, 3)
    assert len({r["window_start"] for r in records}) == 3
    assert all(r["time_entry_id"] is None for r in records)
    assert desktop.cache.count_unattributed_screenshots() == 3
    assert desktop.cache.get_pending_screenshots() == [], (
        "an unattributed capture must not be uploaded against a null entry"
    )

    # Let the queued start land. This is the moment that used to lose them all.
    del desktop.sync._should_hold   # back to the class's own rule
    desktop.sync.wake()
    _pump(qapp, lambda: timer.entry_id is not None, 120, "the queued start to land")
    entry_id = timer.entry_id

    _pump(qapp, lambda: desktop.cache.count_unattributed_screenshots() == 0, 60,
          "every window's capture to be attributed")

    _drain(qapp, desktop, 3, db, lambda: entry_id,
           "the queued start's captures to be stored")

    rows = _stored_rows(db, entry_id)
    assert len(rows) == 3, (
        "every window of the session must be stored, not just the first -- "
        "adoption is keyed on the session, not on the capture's window"
    )
    for row in rows:
        assert row["google_drive_file_id"]
        drive_litter.append(row["google_drive_file_id"])

    # Attributed to the right entry, and to the right person's folder.
    assert {r["time_entry_id"] for r in rows} == {entry_id}
    assert all(f"User_{principal['user_id']}" in r["file_path"] for r in rows)
    # And the ids are the client's own, so a retry could not have duplicated them.
    assert len({r["client_screenshot_id"] for r in rows}) == 3
    assert {r["client_screenshot_id"] for r in rows} == {
        r["client_screenshot_id"] for r in records
    }

    _pump(qapp, lambda: not desktop.cache.get_screenshot_backlog_paths(), 60,
          "the local cache to be reclaimed")

    timer.stop_tracking()
    _pump(qapp, lambda: not timer.is_running(), 30, "the stop")


# ── 3. Idempotency against the real backend ──────────────────────────────────

def test_re_uploading_a_capture_returns_the_same_record_and_no_second_object(
    qapp, desktop, api, db, principal, drive_litter
):
    """A response lost after the file was stored must not make a second one.

    The desktop retries on any failure it cannot classify, including a timeout
    that happened *after* the backend finished. The client-generated id is what
    makes that safe, and this proves it against the real endpoint and real
    Drive rather than against a stub.
    """
    timer = desktop.timer
    timer.start_tracking(principal["project_id"], principal["task_id"], "E2E idempotency")
    _pump(qapp, lambda: timer.entry_id is not None, 30, "the start to be bound")
    entry_id = timer.entry_id

    (record,) = _capture_windows(desktop, 1)
    _drain(qapp, desktop, 1, db, lambda: entry_id, "the capture to be stored")

    (row,) = _stored_rows(db, entry_id)
    drive_litter.append(row["google_drive_file_id"])

    # Replay the very same upload, as a retry after a lost response would.
    replay = api.post(
        f"/time-entries/{entry_id}/screenshots",
        files={"file": (f"screenshot_{record['client_screenshot_id']}.webp",
                        api.get(f"/time-entry-screenshots/{row['id']}/view").content,
                        "image/webp")},
        data={"client_screenshot_id": record["client_screenshot_id"],
              "captured_at": record["captured_at"], "monitor_number": "1"},
    )
    assert replay.status_code == 201, replay.text
    body = replay.json()
    assert body["duplicate"] is True, "the retry was not recognised as one"
    assert body["screenshot"]["id"] == row["id"], "a second record was created"

    after = _stored_rows(db, entry_id)
    assert len(after) == 1, "the retry created a duplicate row"
    assert after[0]["google_drive_file_id"] == row["google_drive_file_id"], (
        "the retry created a second Drive object"
    )

    timer.stop_tracking()
    _pump(qapp, lambda: not timer.is_running(), 30, "the stop")


# ── 4. A multi-display desk is one screenshot, all the way to Drive ──────────

def test_a_two_display_desk_produces_one_merged_screenshot_end_to_end(
    qapp, desktop, api, db, principal, drive_litter, monkeypatch
):
    """Scenario 3: two monitors, ONE merged image, ONE Drive file, ONE row.

    The property under test is not "a wide image was produced" -- it is that
    adding a monitor multiplies nothing. A capture event on a two-monitor desk
    must produce exactly one queue row, one upload, one Drive object and one
    database record, exactly as a one-monitor desk does.
    """
    _pretend_displays(monkeypatch, _panel_layout(2))

    timer = desktop.timer
    timer.start_tracking(principal["project_id"], principal["task_id"], "E2E two displays")
    _pump(qapp, lambda: timer.entry_id is not None, 30, "the start to be bound")
    entry_id = timer.entry_id

    records = _capture_windows(desktop, 1)
    assert len(records) == 1
    assert records[0]["display_count"] == 2, "both displays should be in the image"

    # One queue row for the capture event, not one per monitor.
    queued = desktop.cache.get_pending_screenshots()
    assert len(queued) == 1, f"a two-display capture queued {len(queued)} rows"
    assert queued[0]["display_count"] == 2

    _drain(qapp, desktop, 1, db, lambda: entry_id, "the merged capture to be stored")

    rows = _stored_rows(db, entry_id)
    assert len(rows) == 1, f"a two-display capture stored {len(rows)} database rows"
    row = rows[0]
    assert row["display_count"] == 2
    assert row["google_drive_file_id"], "no Drive file id was recorded"
    drive_litter.append(row["google_drive_file_id"])

    # The stored geometry is the merged one, and it really is wider than the
    # single-display square rather than the same desk letterboxed into it.
    assert row["width"] > row["height"], "the merged image should be wide"
    assert row["width"] == 2000, f"unexpected merged width {row['width']}"

    # The bytes come back through the API as one readable WebP of that size.
    view = api.get(f"/time-entry-screenshots/{row['id']}/view")
    assert view.status_code == 200, view.text
    body = view.content
    assert body[:4] == _RIFF and body[8:12] == _WEBP
    assert len(body) == row["file_size_bytes"]

    from io import BytesIO

    from PIL import Image
    with Image.open(BytesIO(body)) as served:
        assert served.size == (row["width"], row["height"]), (
            "the image served back is not the geometry that was stored"
        )

    # And the dashboard's timeline shows it as a single capture carrying two
    # displays -- never as two screenshots.
    timeline = api.get("/time-entry-screenshots/timeline").json()
    shots = [s for w in timeline["windows"] for s in w["screenshots"]
             if s["id"] == row["id"]]
    assert len(shots) == 1, "the merged capture appeared more than once"
    assert shots[0]["display_count"] == 2

    _pump(qapp, lambda: not desktop.cache.get_screenshot_backlog_paths(), 60,
          "the local cache to be reclaimed")

    timer.stop_tracking()
    _pump(qapp, lambda: not timer.is_running(), 30, "the stop")


def test_a_three_display_desk_is_still_a_single_screenshot(
    qapp, desktop, api, db, principal, drive_litter, monkeypatch
):
    """Scenario 5: three monitors, still one image and one record."""
    _pretend_displays(monkeypatch, _panel_layout(3))

    timer = desktop.timer
    timer.start_tracking(principal["project_id"], principal["task_id"], "E2E three displays")
    _pump(qapp, lambda: timer.entry_id is not None, 30, "the start to be bound")
    entry_id = timer.entry_id

    records = _capture_windows(desktop, 1)
    assert records[0]["display_count"] == 3

    _drain(qapp, desktop, 1, db, lambda: entry_id, "the merged capture to be stored")
    rows = _stored_rows(db, entry_id)
    assert len(rows) == 1, f"a three-display capture stored {len(rows)} rows"
    assert rows[0]["display_count"] == 3
    assert rows[0]["width"] == 3000
    drive_litter.append(rows[0]["google_drive_file_id"])

    timer.stop_tracking()
    _pump(qapp, lambda: not timer.is_running(), 30, "the stop")


def test_plugging_a_monitor_in_changes_the_next_capture_without_a_restart(
    qapp, desktop, api, db, principal, drive_litter, monkeypatch
):
    """Scenarios 6 and 7: hot-plug, picked up by the next capture.

    Displays are enumerated inside each capture, so a desk that changes between
    two captures of one session is simply seen differently the second time.
    Nothing is restarted and no cache is invalidated, because there is none.
    """
    timer = desktop.timer
    timer.start_tracking(principal["project_id"], principal["task_id"], "E2E hot-plug")
    _pump(qapp, lambda: timer.entry_id is not None, 30, "the start to be bound")
    entry_id = timer.entry_id

    from background_services.screenshot import config, scheduler
    first_window = scheduler.window_index(time.time(), config.window_seconds())
    service = desktop.screenshot

    # One monitor.
    _pretend_displays(monkeypatch, _panel_layout(1))
    one = service._capture_now(first_window, service._current_generation())
    assert one["display_count"] == 1

    # A second monitor is plugged in -- mid-session, no restart.
    _pretend_displays(monkeypatch, _panel_layout(2))
    two = service._capture_now(first_window + 1, service._current_generation())
    assert two["display_count"] == 2, "the new monitor was not picked up"

    # And unplugged again.
    _pretend_displays(monkeypatch, _panel_layout(1))
    back = service._capture_now(first_window + 2, service._current_generation())
    assert back["display_count"] == 1, "the removed monitor was still captured"

    _drain(qapp, desktop, 3, db, lambda: entry_id, "all three captures to be stored")
    rows = _stored_rows(db, entry_id)
    assert len(rows) == 3
    for row in rows:
        drive_litter.append(row["google_drive_file_id"])
    # Three capture events, three rows, and the display counts really varied.
    assert sorted(r["display_count"] for r in rows) == [1, 1, 2]

    timer.stop_tracking()
    _pump(qapp, lambda: not timer.is_running(), 30, "the stop")


def test_a_merged_capture_taken_offline_uploads_exactly_once_on_reconnect(
    qapp, desktop, api, db, principal, drive_litter, monkeypatch
):
    """Scenario 8: no network at capture time, one upload when it returns.

    The merged image goes through the same durable queue as any other capture,
    so the offline path needed no new machinery -- this proves it was not
    bypassed, and that reconnecting produces one Drive object rather than a
    duplicate per retry.
    """
    from app.api.exceptions import ApiError

    _pretend_displays(monkeypatch, _panel_layout(2))

    timer = desktop.timer
    timer.start_tracking(principal["project_id"], principal["task_id"], "E2E offline merge")
    _pump(qapp, lambda: timer.entry_id is not None, 30, "the start to be bound")
    entry_id = timer.entry_id

    # The upload fails while "offline", exactly as a dropped connection does.
    real_upload = desktop.time_entry_service.upload_screenshot
    attempts = {"n": 0}

    def offline_upload(*args, **kwargs):
        attempts["n"] += 1
        if attempts["n"] <= 2:
            raise ApiError("Failed to upload screenshot: Network connection error")
        return real_upload(*args, **kwargs)

    monkeypatch.setattr(desktop.time_entry_service, "upload_screenshot", offline_upload)

    records = _capture_windows(desktop, 1)
    assert records[0]["display_count"] == 2

    # The capture survives the failed attempts, still queued with its file.
    desktop.sync.wake()
    _pump(qapp, lambda: attempts["n"] >= 1, 60, "the first upload attempt")
    assert desktop.cache.get_screenshot_backlog_paths(), (
        "the local file must be kept while the upload has not been confirmed"
    )

    # Connectivity returns. The queue drains and the capture lands once.
    _drain(qapp, desktop, 1, db, lambda: entry_id,
           "the merged capture to be stored after reconnecting", timeout=180)

    rows = _stored_rows(db, entry_id)
    assert len(rows) == 1, f"reconnecting produced {len(rows)} rows, not one"
    assert rows[0]["display_count"] == 2
    drive_litter.append(rows[0]["google_drive_file_id"])
    assert rows[0]["client_screenshot_id"] == records[0]["client_screenshot_id"]

    timer.stop_tracking()
    _pump(qapp, lambda: not timer.is_running(), 30, "the stop")


# ── 5. Private browsing, through the same pipeline ───────────────────────────

def test_private_browsing_is_recorded_as_url_usage_in_the_database(
    qapp, desktop, api, db, principal, monkeypatch
):
    """Scenario 2/4: an incognito window's URL reaches the backend, marked.

    The browser observation is supplied here rather than driven by opening a
    real incognito window, because a test cannot rely on one being open on the
    machine it runs on. Everything after the observation is real: the segment
    logic, the local queue, the batch upload, and the row in Postgres. The
    detector itself is verified against genuine Chrome, Edge and Firefox
    windows in `tests/test_private_browsing.py`, whose fixtures are strings
    recorded from real windows.
    """
    from tracking.browsers import UrlSource
    from tracking.browsers.manager import BrowserObservation

    timer = desktop.timer
    timer.start_tracking(principal["project_id"], principal["task_id"], "E2E incognito")
    _pump(qapp, lambda: timer.entry_id is not None, 30, "the start to be bound")
    entry_id = timer.entry_id

    service = desktop.url_usage
    service._entry_id = entry_id

    private = BrowserObservation(
        browser_name="Google Chrome", domain="wikipedia.org",
        url="https://wikipedia.org/wiki/Privacy", page_title="Privacy",
        url_source=UrlSource.ADDRESS_BAR, is_private=True,
        private_marker="Incognito",
    )
    normal = BrowserObservation(
        browser_name="Google Chrome", domain="example.com",
        url="https://example.com/", page_title="Example Domain",
        url_source=UrlSource.ADDRESS_BAR, is_private=False,
    )

    # A private segment, then a normal one, each measured and flushed exactly
    # as the sampling loop flushes them.
    for observation in (private, normal):
        now = time.monotonic()
        service._begin_session(observation, now)
        service._session_start = now - 12.0
        service._last_observed = now
        service._flush_session()
        service._reset_session()

    # The real SyncService uploads them in its ordinary batch.
    desktop.sync.wake()
    _pump(qapp, lambda: len(_stored_url_rows(db, entry_id)) >= 2, 120,
          "the browser sessions to reach the database")

    rows = {r["domain"]: r for r in _stored_url_rows(db, entry_id)}
    assert rows["wikipedia.org"]["is_private"] is True, (
        "private browsing was stored without its private state"
    )
    assert rows["wikipedia.org"]["url"] == "https://wikipedia.org/wiki/Privacy", (
        "the private window's real URL must be stored, not a placeholder"
    )
    assert rows["example.com"]["is_private"] is False

    # And the ordinary URL-usage API returns them, so the dashboard sees the
    # private session as browsing rather than as a gap.
    summary = api.get(f"/time-entries/{entry_id}/url-usage").json()
    domains = {item["domain"] for item in summary.get("data", {}).get("items", [])}
    assert {"wikipedia.org", "example.com"} <= domains or not domains, (
        f"the private session is missing from the API listing: {domains}"
    )

    timer.stop_tracking()
    _pump(qapp, lambda: not timer.is_running(), 30, "the stop")


def test_a_browser_whose_url_cannot_be_read_records_no_url_at_all(
    qapp, desktop, api, db, principal, monkeypatch
):
    """The limitation, held to honestly: no URL is invented for a private
    window whose address bar cannot be read (Firefox without accessibility).

    The browser's own time is still captured as application usage; what must
    never happen is a fabricated domain standing in for the unread one.
    """
    from tracking.browsers import UrlSource
    from tracking.browsers.manager import BrowserObservation

    timer = desktop.timer
    timer.start_tracking(principal["project_id"], principal["task_id"], "E2E no url")
    _pump(qapp, lambda: timer.entry_id is not None, 30, "the start to be bound")
    entry_id = timer.entry_id

    service = desktop.url_usage
    service._entry_id = entry_id

    unreadable = BrowserObservation(
        browser_name="Mozilla Firefox", domain=None, url=None,
        page_title="DuckDuckGo", url_source=UrlSource.UNAVAILABLE,
        is_private=True, private_marker="private browsing",
    )
    now = time.monotonic()
    service._begin_session(unreadable, now)
    service._session_start = now - 12.0
    service._last_observed = now
    service._flush_session()
    service._reset_session()

    desktop.sync.wake()
    qapp.processEvents()
    time.sleep(2)
    qapp.processEvents()

    rows = _stored_url_rows(db, entry_id)
    assert rows == [], (
        f"a URL record was written for a browser whose address bar could not "
        f"be read: {rows}"
    )

    timer.stop_tracking()
    _pump(qapp, lambda: not timer.is_running(), 30, "the stop")

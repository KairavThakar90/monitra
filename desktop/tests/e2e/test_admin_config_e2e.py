"""An administrator's configuration, end to end: web API -> database -> stream -> desktop -> behaviour.

Nothing here is mocked. A real backend process serves the real API against the database the
environment names; an administrator principal changes things through the real admin routes
(the member switches, the screenshot size and quality, the screens captured, a pushed
message); two real desktop runtimes -- member A and member B, each with its own storage --
are signed in and show what they do about it. Assertions are made against what the desktop
services report, what they capture (the real screen, decoded), what the notification service
is asked to show, and what the database holds.

Opt-in, like the other end-to-end modules; it needs a free port, a real display and a
database you do not mind writing to (the development database `e2e_support.py` guards, or
a local throwaway one named by `DATABASE_URL_DEV`):

    MONITRA_E2E=1 python -m pytest tests/e2e/test_admin_config_e2e.py -q -s   (from desktop/)

What is measured and pinned:

* **Propagation.** A change made through the admin API reaches the running desktop within
  `PROPAGATION_BUDGET_S` -- the stream path, not the three-minute poll. The measured time is
  printed for every change.
* **Isolation.** A change for member A never reaches member B's desktop.
* **Behaviour, not just state.** Screenshots switched off: a capture is refused before the
  screen is read. A new size: the next real capture, decoded, is that size. A fixed quality:
  the next capture is encoded at it (a lower quality gives a smaller file of the same
  screen). An announcement: the desktop shows it, once, as the Announcement card.
* **Restart.** A second runtime on the same storage, pointed at a backend that is not there,
  starts from what the administrator last set.
* **Authorization.** Every admin route refuses an employee.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import time
from types import SimpleNamespace

import httpx
import pytest
from pathlib import Path

from tests.e2e.test_timing_lifecycle_e2e import (  # noqa: F401  (fixtures)
    BACKEND_ROOT, _free_port, _pump, backend, pytestmark,
)

#: What an online desktop must honour within. The stream delivers in a second or two;
#: five is the acceptance threshold the audit set.
PROPAGATION_BUDGET_S = 5.0
#: Titles carry the run, so a message still live from an earlier run is never mistaken for this one's.
RUN = time.strftime("%H%M%S")


# ── principals ───────────────────────────────────────────────────────────────

def _provision(role=None):
    helper = BACKEND_ROOT / "tests" / "e2e_support.py"
    args = [sys.executable, str(helper), "provision"] + ([role] if role else [])
    out = subprocess.run(args, cwd=str(BACKEND_ROOT), capture_output=True, text=True, check=True)
    return json.loads(out.stdout.strip().splitlines()[-1])


def _cleanup(fixture):
    helper = BACKEND_ROOT / "tests" / "e2e_support.py"
    subprocess.run(
        [sys.executable, str(helper), "cleanup", json.dumps(fixture)],
        cwd=str(BACKEND_ROOT), capture_output=True, text=True, check=False,
    )


@pytest.fixture(scope="module")
def member_a():
    fixture = _provision()
    try:
        yield fixture
    finally:
        _cleanup(fixture)


@pytest.fixture(scope="module")
def member_b():
    fixture = _provision()
    try:
        yield fixture
    finally:
        _cleanup(fixture)


@pytest.fixture(scope="module")
def admin():
    fixture = _provision("administrator")
    try:
        yield fixture
    finally:
        _cleanup(fixture)


@pytest.fixture(scope="module")
def db(member_a):
    from sqlalchemy import create_engine

    engine = create_engine(member_a["database_url"], pool_pre_ping=True)
    yield engine
    engine.dispose()


def _client(backend, principal):
    return httpx.Client(base_url=backend, timeout=30, headers={"Authorization": f"Bearer {principal['token']}"})


@pytest.fixture(scope="module")
def admin_api(backend, admin):
    with _client(backend, admin) as client:
        yield client


@pytest.fixture(scope="module")
def a_api(backend, member_a):
    with _client(backend, member_a) as client:
        yield client


@pytest.fixture(scope="module")
def settings_restored(backend, admin_api, db):
    """The deployment-wide screenshot settings are put back exactly as they were found."""
    from sqlalchemy import text

    with db.connect() as conn:
        before = conn.execute(
            text("SELECT value FROM system_settings WHERE key = 'screenshot_settings'")
        ).scalar()
    yield before
    with db.begin() as conn:
        if before is None:
            conn.execute(text("DELETE FROM system_settings WHERE key = 'screenshot_settings'"))
        else:
            conn.execute(text("UPDATE system_settings SET value = CAST(:v AS jsonb) WHERE key = 'screenshot_settings'"),
                         {"v": json.dumps(before)})


# ── the desktops ─────────────────────────────────────────────────────────────

class Spy:
    """Records what a notification service is asked to show, and lets it show it.

    `calls` is every request; `shown` only those the service actually displayed (it
    returns False for a notification it suppressed -- a member whose notifications are
    off). Both are kept: a call the service rightly refused is still worth seeing.
    """

    def __init__(self, notifications):
        self.calls = []
        self.shown = []
        real = notifications.notify

        def notify(message, level="info", title=None, key=None, link=None, native=None):
            entry = {"message": message, "level": level, "title": title, "key": key,
                     "native": native, "at": time.monotonic()}
            self.calls.append(entry)
            displayed = real(message, level, title=title, key=key, link=link, native=native)
            if displayed:
                self.shown.append(entry)
            return displayed

        notifications.notify = notify

    def of(self, key):
        return [c for c in self.shown if c["key"] == key]


def _runtime(tmp_path, backend, principal, name):
    from core.runtime import ApplicationRuntime
    from storage.manager import StorageManager

    manager = StorageManager(str(tmp_path / f"{name}-cache.db"))
    runtime = ApplicationRuntime(storage=manager)
    runtime.api_client.base_url = backend
    runtime.api_client.access_token = principal["token"]
    # What the dashboard does on sign-in: seed every service from the profile.
    profile = {"id": principal["user_id"], "role_name": "employee",
               "screenshots_enabled": True, "notifications_enabled": True, "activity_tracking_enabled": True,
               "capture_frequency": 10}
    runtime.session_manager.start_session(principal["token"], profile)
    runtime.start_services()
    runtime.on_login(principal["user_id"])
    runtime.screenshot.apply_user_profile(profile)
    runtime.activity.apply_user_profile(profile)
    runtime.notification_schedule.apply_user_profile(profile)
    return SimpleNamespace(runtime=runtime, spy=Spy(runtime.notifications), principal=principal, path=tmp_path)


def _settled(desk):
    """Nothing running and nothing left in the durable queue."""
    return not desk.runtime.timer.is_running() and desk.runtime.cache.get_pending_count() == 0


def _start(desk, qapp, label):
    """Start tracking once the previous session's stop has landed, and wait for the entry id."""
    from PySide6.QtWidgets import QApplication

    _pump(qapp, lambda: _settled(desk), 90, "the previous session to settle before a new start")
    desk.runtime.timer.start_tracking(desk.principal["project_id"], desk.principal["task_id"], label)
    _pump(qapp, lambda: desk.runtime.timer.entry_id is not None, 90, f"the start of {label!r} to be bound")
    QApplication.instance().processEvents()


def _stop(desk, qapp):
    desk.runtime.timer.stop_tracking()
    desk.runtime.sync.wake()
    _pump(qapp, lambda: _settled(desk), 90, "the stop to land")


def _shutdown(desk):
    from PySide6.QtWidgets import QApplication

    try:
        if desk.runtime.timer.is_running():
            desk.runtime.timer.stop_tracking()
            desk.runtime.sync.wake()
        # As the application does on quit: let the queued stop reach the backend before the
        # services are stopped, so the sync worker is not torn down mid-request.
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not _settled(desk):
            QApplication.instance().processEvents()
            time.sleep(0.05)
    except Exception:  # noqa: BLE001
        pass
    desk.runtime.shutdown(timeout_ms=5000)


@pytest.fixture(scope="module")
def schedule_restored(db):
    """The deployment's notification schedule row is cleared of pushes before the desktops
    start -- a message pushed by an earlier run is still live for ten minutes and would be
    shown to them at sign-in -- and put back exactly as it was found afterwards."""
    from sqlalchemy import text

    key = "desktop_notifications"
    with db.connect() as conn:
        before = conn.execute(
            text("SELECT value, updated_at, updated_by_user_id, updated_by_username FROM system_settings WHERE key = :k"),
            {"k": key},
        ).mappings().first()
    if before is not None and isinstance(before["value"], dict) and before["value"].get("pushes"):
        cleared = {k: v for k, v in before["value"].items() if k != "pushes"}
        with db.begin() as conn:
            conn.execute(text("UPDATE system_settings SET value = CAST(:v AS jsonb) WHERE key = :k"),
                         {"v": json.dumps(cleared), "k": key})
    yield before
    with db.begin() as conn:
        if before is None:
            conn.execute(text("DELETE FROM system_settings WHERE key = :k"), {"k": key})
        else:
            conn.execute(
                text("UPDATE system_settings SET value = CAST(:v AS jsonb), updated_at = :at, "
                     "updated_by_user_id = :uid, updated_by_username = :uname WHERE key = :k"),
                {"v": json.dumps(before["value"]), "at": before["updated_at"],
                 "uid": before["updated_by_user_id"], "uname": before["updated_by_username"], "k": key},
            )


@pytest.fixture(scope="module")
def reminders_off(schedule_restored, admin_api):
    """Every built-in reminder switched off for the run, through the real admin route.

    A desktop started inside a daily reminder's grace window (lunch at 13:30 IST, ten
    minutes) shows that reminder first, and the sixty-second spacing between notifications
    then holds an announcement back -- documented behaviour, and not what these measure.
    `schedule_restored` puts the row back afterwards.
    """
    view = admin_api.get("/desktop-notifications")
    assert view.status_code == 200, view.text
    for item in view.json().get("builtin", []):
        if item.get("enabled", True):
            switched = admin_api.put(f"/desktop-notifications/builtin/{item['key']}", json={"enabled": False})
            assert switched.status_code == 200, switched.text


@pytest.fixture(scope="module")
def desks(qapp, tmp_path_factory, backend, member_a, member_b, schedule_restored, reminders_off):
    from core import paths

    root = tmp_path_factory.mktemp("desks")
    os.environ["SMS_API_BASE_URL"] = backend
    os.environ["MONITRA_ENV"] = "development"
    # Not under pytest's own tmp (it keeps only the last few runs): the desktops' logs are
    # the evidence when something fails, and must outlive the run.
    data_root = Path(os.environ.get("MONITRA_E2E_DATA_ROOT", str(root))) / f"admin-config-e2e-{os.getpid()}"
    data_root.mkdir(parents=True, exist_ok=True)
    os.environ["MONITRA_DATA_DIR"] = str(data_root / "data")
    print(f"\nDESKTOP DATA DIR {data_root / 'data'}")
    paths.reset_cache()
    (root / "a").mkdir()
    (root / "b").mkdir()
    a = _runtime(root / "a", backend, member_a, "a")
    b = _runtime(root / "b", backend, member_b, "b")
    # Let both schedule services open their streams (the first stream of a process answers at
    # once with the configuration revision, which also seeds the three polls fresh).
    _pump(qapp, lambda: a.runtime.notification_schedule._stream_up and b.runtime.notification_schedule._stream_up,
          30, "both desktops to hold their change streams")
    yield SimpleNamespace(a=a, b=b)
    _shutdown(a)
    _shutdown(b)
    paths.reset_cache()


def _measure(qapp, until, what, budget=PROPAGATION_BUDGET_S):
    started = time.monotonic()
    _pump(qapp, until, budget + 10, what)
    elapsed = time.monotonic() - started
    print(f"\nPROPAGATION {what}: {elapsed:.2f}s")
    assert elapsed <= budget, f"{what} took {elapsed:.1f}s, over the {budget}s budget"
    return elapsed


def _patch_member(admin_api, user_id, **fields):
    response = admin_api.patch(f"/members/{user_id}", json=fields)
    assert response.status_code == 200, response.text
    return response.json()


def _file_of(desk, record):
    """The queued file a capture record refers to (the record itself carries only the id)."""
    rows = desk.runtime.cache.get_pending_screenshots(limit=200)
    for row in rows:
        if row["client_screenshot_id"] == record["client_screenshot_id"]:
            return row["local_file_path"]
    raise AssertionError(f"capture {record['client_screenshot_id']} is not in the queue")


def _decoded_size(desk, record):
    from PIL import Image

    with Image.open(_file_of(desk, record)) as image:
        image.load()
        return image.size, image.format


def _capture(desk, qapp):
    """One real capture through the service's own capture path, the way the armed timer does it."""
    from background_services.screenshot import config, scheduler

    service = desk.runtime.screenshot
    _pump(qapp, lambda: service._privacy_config_loaded, 30, "the privacy configuration to be fetched")
    index = scheduler.window_index(time.time(), config.window_seconds())
    _capture.counter = getattr(_capture, "counter", 0) + 1
    return service._capture_now(index + _capture.counter, service._current_generation())


# ── 1. the member switches ───────────────────────────────────────────────────

def test_screenshots_switched_off_for_a_reach_a_within_seconds_and_never_b(qapp, desks, admin_api, db):
    a, b = desks.a, desks.b
    assert a.runtime.screenshot._screenshots_are_enabled() and b.runtime.screenshot._screenshots_are_enabled()

    _patch_member(admin_api, a.principal["user_id"], screenshots_enabled=False)
    _measure(qapp, lambda: not a.runtime.screenshot._screenshots_are_enabled(), "screenshots off -> A")
    assert b.runtime.screenshot._screenshots_are_enabled(), "B is untouched"

    # Persisted by the backend, and read back by the admin's directory.
    from sqlalchemy import text
    with db.connect() as conn:
        assert conn.execute(text("SELECT screenshots_enabled FROM users WHERE id = :u"),
                            {"u": a.principal["user_id"]}).scalar() is False
    # Written into A's persisted profile, for a restart while offline.
    assert a.runtime.session_manager.user_info["screenshots_enabled"] is False

    # Behaviour: a capture is refused before the screen is read, while B still captures.
    _start(a, qapp, "E2E A")
    refused = _capture(a, qapp)
    assert refused is None or "window_start" not in (refused or {}), f"A captured although switched off: {refused}"
    assert a.runtime.cache.get_pending_screenshots() == []

    _patch_member(admin_api, a.principal["user_id"], screenshots_enabled=True)
    _measure(qapp, lambda: a.runtime.screenshot._screenshots_are_enabled(), "screenshots on -> A")
    record = _capture(a, qapp)
    assert record and "window_start" in record, f"A does not capture after being switched back on: {record}"
    # Switching on mid-window plans the rest of the window at once, so the service's own
    # capture may have landed too: at least ours, and every one of them A's.
    queued = a.runtime.cache.get_pending_screenshots()
    assert len(queued) >= 1 and all(q["time_entry_id"] == a.runtime.timer.entry_id for q in queued), queued
    _stop(a, qapp)


def test_rapid_changes_end_in_the_last_one_and_a_stale_answer_never_restores_a_switch(qapp, desks, admin_api):
    a = desks.a
    for value in (False, True, False, True, False):
        _patch_member(admin_api, a.principal["user_id"], activity_tracking_enabled=value)
    _measure(qapp, lambda: a.runtime.activity.tracking_enabled() is False, "five activity changes -> A")
    # Nothing in the next ten seconds puts it back.
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        qapp.processEvents()
        assert a.runtime.activity.tracking_enabled() is False
        time.sleep(0.05)
    assert desks.b.runtime.activity.tracking_enabled() is True
    assert a.runtime.session_manager.user_info["activity_tracking_enabled"] is False
    _patch_member(admin_api, a.principal["user_id"], activity_tracking_enabled=True)
    _measure(qapp, lambda: a.runtime.activity.tracking_enabled() is True, "activity on -> A")


def test_notifications_off_for_a_silences_announcements_for_a_only(qapp, desks, admin_api):
    a, b = desks.a, desks.b
    _patch_member(admin_api, a.principal["user_id"], notifications_enabled=False)
    _measure(qapp, lambda: a.runtime.notification_schedule.notifications_enabled is False, "notifications off -> A")
    assert b.runtime.notification_schedule.notifications_enabled is True

    sent = admin_api.post("/desktop-notifications/push", json={"title": f"E2E off {RUN}", "message": "Only B should see this."})
    assert sent.status_code == 200, sent.text
    _measure(qapp, lambda: any(f"E2E off {RUN}" == c["title"] for c in b.spy.shown), "announcement -> B", budget=20)
    # A hears the same version move and must show nothing: wait as long as B took, and more.
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.05)
    asked = [c for c in a.spy.calls if c["title"] == f"E2E off {RUN}"]
    displayed = [c for c in a.spy.shown if c["title"] == f"E2E off {RUN}"]
    print(f"\nA (switched off) was asked to show the announcement {len(asked)} time(s); displayed {len(displayed)}")
    assert not displayed, "A was switched off and still shown it"

    _patch_member(admin_api, a.principal["user_id"], notifications_enabled=True)
    _measure(qapp, lambda: a.runtime.notification_schedule.notifications_enabled is True, "notifications on -> A")


# ── 2. the announcement ──────────────────────────────────────────────────────

def test_an_announcement_reaches_both_desktops_once_as_the_announcement_card(qapp, desks, admin_api):
    from background_services.notifications.notification_service import NotificationLevel

    a, b = desks.a, desks.b
    sent_at = time.monotonic()
    sent = admin_api.post("/desktop-notifications/push", json={
        "title": f"E2E ann {RUN}", "message": "Please save your work: the server restarts in five minutes.",
    })
    assert sent.status_code == 200, sent.text
    for name, desk in (("A", a), ("B", b)):
        _pump(qapp, lambda d=desk: any(c["title"] == f"E2E ann {RUN}" for c in d.spy.shown), 70,
              f"the announcement to reach {name}")
        shown = [c for c in desk.spy.shown if c["title"] == f"E2E ann {RUN}"]
        print(f"\nPROPAGATION announcement -> {name}: {shown[0]['at'] - sent_at:.2f}s")
        assert shown[0]["level"] == NotificationLevel.ANNOUNCEMENT
        assert shown[0]["native"] is False
        assert shown[0]["key"].startswith("wellbeing:push:")
    # Nothing shows it twice, however many polls and streams follow.
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.05)
    for desk in (a, b):
        assert len([c for c in desk.spy.shown if c["title"] == f"E2E ann {RUN}"]) == 1
    # The card itself carries the heading.
    cards = [entry.card for entry in a.runtime.notifications._cards if entry.title == f"E2E ann {RUN}"]
    if cards:
        assert not cards[0]._eyebrow.isHidden()


# ── 3. size and quality ──────────────────────────────────────────────────────

@pytest.mark.usefixtures("settings_restored")
def test_size_and_quality_reach_the_desktop_and_the_next_real_capture(qapp, desks, admin_api, db):
    from background_services.screenshot import config

    a, b = desks.a, desks.b
    _start(a, qapp, "E2E size")

    def put(width, height, quality):
        response = admin_api.put("/api/v1/screenshot-settings",
                                 json={"image_width": width, "image_height": height, "image_quality": quality})
        assert response.status_code == 200, response.text
        return response.json()

    # A smaller canvas, Automatic quality.
    put(800, 600, None)
    _measure(qapp, lambda: a.runtime.screenshot._current_capture_settings() == config.CaptureSettings(800, 600, None),
             "size 800x600 -> A")
    assert b.runtime.screenshot._current_capture_settings() == config.CaptureSettings(800, 600, None) or \
        _measure(qapp, lambda: b.runtime.screenshot._current_capture_settings() == config.CaptureSettings(800, 600, None),
                 "size 800x600 -> B")
    small = _capture(a, qapp)
    assert small and "window_start" in small, small
    (w, h), fmt = _decoded_size(a, small)
    assert (w, h) == (800, 600) and fmt == "WEBP", (w, h, fmt)
    print(f"\nCAPTURE 800x600 automatic: {w}x{h} {fmt} {small['file_size_bytes']} bytes")

    # A fixed low quality, then a fixed high quality, same screen: the file sizes order.
    put(1280, 720, 30)
    _measure(qapp, lambda: a.runtime.screenshot._current_capture_settings() == config.CaptureSettings(1280, 720, 30),
             "quality 30 -> A")
    low = _capture(a, qapp)
    put(1280, 720, 95)
    _measure(qapp, lambda: a.runtime.screenshot._current_capture_settings() == config.CaptureSettings(1280, 720, 95),
             "quality 95 -> A")
    high = _capture(a, qapp)
    for record in (low, high):
        (w, h), fmt = _decoded_size(a, record)
        assert (w, h) == (1280, 720) and fmt == "WEBP"
    print(f"\nCAPTURE 1280x720 q30: {low['file_size_bytes']} bytes; q95: {high['file_size_bytes']} bytes")
    assert high["file_size_bytes"] > low["file_size_bytes"], "a higher fixed quality of the same screen is a larger file"

    # Two changes before the next capture: the latest wins.
    put(1000, 1000, 60)
    put(1500, 1500, None)
    _measure(qapp, lambda: a.runtime.screenshot._current_capture_settings() == config.CaptureSettings(1500, 1500, None),
             "two changes, latest -> A")
    final = _capture(a, qapp)
    assert _decoded_size(a, final)[0] == (1500, 1500)

    # Invalid values are refused and change nothing.
    for body in ({"image_width": 100, "image_height": 600, "image_quality": None},
                 {"image_width": 800, "image_height": 600, "image_quality": 5},
                 {"image_width": 800, "image_height": 600},
                 {"image_width": "wide", "image_height": 600, "image_quality": None}):
        assert admin_api.put("/api/v1/screenshot-settings", json=body).status_code == 422, body
    assert a.runtime.screenshot._current_capture_settings() == config.CaptureSettings(1500, 1500, None)

    _stop(a, qapp)


# ── 4. the screens ───────────────────────────────────────────────────────────

def test_the_desktop_reports_its_screens_and_the_last_one_cannot_be_switched_off(qapp, desks, admin_api):
    a = desks.a
    _pump(qapp, lambda: any(m["user_id"] == a.principal["user_id"]
                            for m in admin_api.get("/api/v1/screenshot-displays").json().get("members", [])),
          30, "A's screens to be reported")
    members = admin_api.get("/api/v1/screenshot-displays").json()["members"]
    mine = next(m for m in members if m["user_id"] == a.principal["user_id"])
    screens = mine["screens"]
    print(f"\nSCREENS reported by A: {[(s['number'], s['width'], s['height'], s['is_primary'], s['connected']) for s in screens]}")
    assert screens and all(s["capture_enabled"] for s in screens), "every screen is captured by default"
    connected = [s for s in screens if s["connected"]]
    if len(connected) == 1:
        refused = admin_api.put(f"/api/v1/screenshot-displays/{a.principal['user_id']}",
                                json={"screens": [{"number": connected[0]["number"], "capture_enabled": False}]})
        assert refused.status_code == 422, refused.text
        pytest.skip("one physical screen here: per-screen exclusion with a real second monitor is not verifiable on this machine")
    # Two or more: switch the second off and the next capture is the first screen only.
    second = connected[1]
    chosen = admin_api.put(f"/api/v1/screenshot-displays/{a.principal['user_id']}",
                           json={"screens": [{"number": second["number"], "capture_enabled": False}]})
    assert chosen.status_code == 200, chosen.text
    _measure(qapp, lambda: second["number"] in a.runtime.screenshot._excluded_displays(), "screen off -> A")
    assert desks.b.runtime.screenshot._excluded_displays() == frozenset()
    admin_api.put(f"/api/v1/screenshot-displays/{a.principal['user_id']}",
                  json={"screens": [{"number": second["number"], "capture_enabled": True}]})
    _measure(qapp, lambda: a.runtime.screenshot._excluded_displays() == frozenset(), "screen on -> A")


def test_with_two_screens_pretended_only_the_chosen_one_is_captured(qapp, desks, admin_api, monkeypatch):
    """This machine has one panel, so a second monitor cannot be plugged in. Only the
    *enumeration* is substituted (as `test_screenshot_lifecycle_e2e._pretend_displays` does):
    every grab, the compositor, the encoder, the queue, the report to the backend, the
    administrator's choice, the stream and the exclusion are the real ones."""
    from background_services.screenshot import capture as capture_mod
    from background_services.screenshot.displays import Display, enumerate_displays
    import mss

    a = desks.a
    real = enumerate_displays(mss)
    panel = real[0]
    layout = [
        Display(number=n, left=(n - 1) * panel.width, top=0, width=panel.width, height=panel.height, is_primary=(n == 1))
        for n in (1, 2)
    ]
    monkeypatch.setattr(capture_mod, "enumerate_displays", lambda module: layout)
    def two_reported():
        a.runtime.screenshot._report_displays()
        mine = next((m for m in admin_api.get("/api/v1/screenshot-displays").json()["members"]
                     if m["user_id"] == a.principal["user_id"]), {"screens": []})
        return len([s for s in mine["screens"] if s["connected"]]) == 2

    _pump(qapp, two_reported, 30, "two screens to be reported")
    screens = next(m for m in admin_api.get("/api/v1/screenshot-displays").json()["members"]
                   if m["user_id"] == a.principal["user_id"])["screens"]
    print(f"\nSCREENS (pretended) reported by A: {[(s['number'], s['width'], s['height'], s['is_primary'], s['connected']) for s in screens]}")

    _start(a, qapp, "E2E screens")
    both = _capture(a, qapp)
    assert both and both["display_count"] == 2, both
    both_size = _decoded_size(a, both)[0]

    chosen = admin_api.put(f"/api/v1/screenshot-displays/{a.principal['user_id']}",
                           json={"screens": [{"number": 2, "capture_enabled": False}]})
    assert chosen.status_code == 200, chosen.text
    _measure(qapp, lambda: 2 in a.runtime.screenshot._excluded_displays(), "screen 2 off -> A")
    assert desks.b.runtime.screenshot._excluded_displays() == frozenset(), "B's screens are B's"
    one = _capture(a, qapp)
    assert one and one["display_count"] == 1, one
    one_size = _decoded_size(a, one)[0]
    print(f"\nCAPTURE both screens: {both_size}; screen 1 only: {one_size}")
    assert one_size[0] < both_size[0], "leaving a screen out makes a narrower image"
    assert one_size[0] == 1500, "one screen is fitted into the 1500 px box exactly as a single-screen desk"

    # Screen 1 only, then both again: each choice is the next capture's.
    swapped = admin_api.put(f"/api/v1/screenshot-displays/{a.principal['user_id']}",
                            json={"screens": [{"number": 1, "capture_enabled": False}, {"number": 2, "capture_enabled": True}]})
    assert swapped.status_code == 200, swapped.text
    _measure(qapp, lambda: a.runtime.screenshot._excluded_displays() == frozenset({1}), "screen 1 off, 2 on -> A")
    other = _capture(a, qapp)
    assert other and other["display_count"] == 1
    # Both switched off is refused: that is the member's screenshot switch.
    refused = admin_api.put(f"/api/v1/screenshot-displays/{a.principal['user_id']}",
                            json={"screens": [{"number": 2, "capture_enabled": False}]})
    assert refused.status_code == 422, refused.text
    restored = admin_api.put(f"/api/v1/screenshot-displays/{a.principal['user_id']}",
                             json={"screens": [{"number": 1, "capture_enabled": True}]})
    assert restored.status_code == 200
    _measure(qapp, lambda: a.runtime.screenshot._excluded_displays() == frozenset(), "both on -> A")
    again = _capture(a, qapp)
    assert again["display_count"] == 2 and _decoded_size(a, again)[0] == both_size

    # The monitor goes: the real desk again. Its row stays, disconnected, with its choice.
    monkeypatch.setattr(capture_mod, "enumerate_displays", lambda module: layout[:1])

    def reported_disconnected():
        # The report is edge-triggered and de-duplicated on the pool; the service re-sends it
        # at its next refresh, which this stands in for.
        a.runtime.screenshot._report_displays()
        screens = next(m for m in admin_api.get("/api/v1/screenshot-displays").json()["members"]
                       if m["user_id"] == a.principal["user_id"])["screens"]
        return [s["connected"] for s in sorted(screens, key=lambda s: s["number"])] == [True, False]

    _pump(qapp, reported_disconnected, 30, "screen 2 to be reported disconnected")
    alone = _capture(a, qapp)
    assert alone["display_count"] == 1
    _stop(a, qapp)


# ── 5. restart ───────────────────────────────────────────────────────────────

def test_a_restart_without_a_backend_starts_from_what_the_administrator_last_set(qapp, desks, admin_api, member_a):
    from background_services.screenshot import config
    from core.runtime import ApplicationRuntime
    from storage.manager import StorageManager

    a = desks.a
    _patch_member(admin_api, a.principal["user_id"], screenshots_enabled=False)
    _measure(qapp, lambda: not a.runtime.screenshot._screenshots_are_enabled(), "screenshots off -> A (for restart)")
    last_size = a.runtime.screenshot._current_capture_settings()
    _shutdown(a)

    # The same storage, a backend that is not there.
    manager = StorageManager(str(a.path / "a-cache.db"))
    runtime = ApplicationRuntime(storage=manager)
    runtime.api_client.base_url = f"http://127.0.0.1:{_free_port()}"
    try:
        assert runtime.session_manager.restore_session()
        runtime.api_client.access_token = runtime.session_manager.access_token
        runtime.start_services()
        profile = runtime.session_manager.user_info
        assert profile["screenshots_enabled"] is False, "the switch the poll reported is in the persisted profile"
        runtime.screenshot.apply_user_profile(profile)
        runtime.activity.apply_user_profile(profile)
        assert runtime.screenshot._screenshots_are_enabled() is False
        assert runtime.screenshot._current_capture_settings() == last_size != config.DEFAULT_CAPTURE_SETTINGS or \
            runtime.screenshot._current_capture_settings() == last_size
    finally:
        runtime.shutdown(timeout_ms=5000)
        # Put A back for the module's other tests and leave the member switched on.
        _patch_member(admin_api, a.principal["user_id"], screenshots_enabled=True)
        desks.a = _runtime(a.path, os.environ["SMS_API_BASE_URL"], member_a, "a")


# ── 6. authorization ─────────────────────────────────────────────────────────

def test_every_admin_route_refuses_an_employee(a_api, member_b, desks):
    other = member_b["user_id"]
    assert a_api.patch(f"/members/{other}", json={"screenshots_enabled": False}).status_code == 403
    assert a_api.patch(f"/members/{desks.a.principal['user_id']}", json={"screenshots_enabled": False}).status_code == 403
    assert a_api.put("/api/v1/screenshot-settings",
                     json={"image_width": 800, "image_height": 600, "image_quality": None}).status_code == 403
    assert a_api.get("/api/v1/screenshot-displays").status_code == 403
    assert a_api.put(f"/api/v1/screenshot-displays/{other}",
                     json={"screens": [{"number": 1, "capture_enabled": False}]}).status_code == 403
    assert a_api.post("/desktop-notifications/push", json={"title": "Nope", "message": "Not allowed."}).status_code == 403
    assert a_api.get("/api/v1/email-cc").status_code == 403
    assert a_api.put("/api/v1/email-cc", json={"addresses": []}).status_code == 403
    # And nobody at all without a token.
    bare = httpx.Client(base_url=str(a_api.base_url), timeout=10)
    assert bare.get("/screenshots/config").status_code == 401
    assert bare.get("/desktop-notifications/stream?since=0").status_code == 401

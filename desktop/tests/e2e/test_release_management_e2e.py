"""
Release management, end to end: admin API -> PostgreSQL -> the desktop runtime
-> the backend's enforcement -> the admin's statistics.

Opt-in like the other E2E modules (`MONITRA_E2E=1`, a database that
`DATABASE_URL_DEV` names). The real backend is started on a free port, a real
administrator and a real member are provisioned, and the real desktop runtime
is pointed at it. Nothing here is mocked except time: the 24-hour deadline is
crossed by moving the grace row's deadline on the server (the one clock that
decides), not by waiting.

What is proven, in order:

 1. the administrator creates, validates and publishes a mandatory release
    for both platforms, and the announcement is queued for the targeted users
    and delivered by the console provider (no real email);
 2. each platform is answered its own artifact; a Linux claim is held to the
    policy under `unknown`;
 3. the desktop's own update check files the grace period on the server
    clock, offers the prompt as mandatory with Later, and a second check, a
    restart and a second device do not move the deadline;
 4. a timer started inside the grace keeps running through everything below;
 5. once the deadline passes (server side), a start is refused with 426 while
    stop/active/auth stay open; the desktop blocks on its next check, its
    timer service refuses a new start, and the main window shows the
    update-required page;
 6. Update Now against the published (but not yet uploaded) 1.3.4 installer
    fails with a real HTTP 404 from the real website: nothing is installed,
    the failure is reported to the backend, the requirement stands;
 7. the "installed" client (the running version patched to the target) is
    released: the requirement is dropped locally only by the running version,
    the server marks the grace row satisfied, protected routes answer again;
 8. withdrawing the release suspends enforcement for everyone still below the
    floor and keeps their grace rows.

A real installer is deliberately NOT executed here; see the report for what
that leaves to a physical machine.
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone

import httpx
import pytest

import version
from tests.e2e.test_timing_lifecycle_e2e import (  # noqa: F401  (fixtures)
    BACKEND_ROOT, _pump, _row, _running_rows, backend, db, desktop, principal, pytestmark,
)

UTC = timezone.utc
HOST = "https://staff.peakworkos.com/download_app_files"
_MAJOR, _MINOR, _PATCH = (int(p) for p in version.VERSION.split("."))
TARGET = f"{_MAJOR}.{_MINOR}.{_PATCH + 1}"          # strictly newer than the running build


@pytest.fixture(scope="module")
def administrator():
    helper = BACKEND_ROOT / "tests" / "e2e_support.py"
    out = subprocess.run(
        [sys.executable, str(helper), "provision", "administrator"], cwd=str(BACKEND_ROOT),
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


def _client(backend, token, **headers):
    return httpx.Client(base_url=backend, timeout=60,
                        headers={"Authorization": f"Bearer {token}", **headers})


def _desktop_headers(client_version=version.VERSION, platform="win32", arch="AMD64"):
    return {"User-Agent": f"Monitra/{client_version}", "X-Monitra-Platform": platform, "X-Monitra-Arch": arch}


def _sql(db, statement, **params):
    from sqlalchemy import text
    with db.begin() as conn:
        result = conn.execute(text(statement), params)
        return result.fetchall() if result.returns_rows else []


def _artifacts():
    return [
        {"platform": "win32", "architecture": None, "download_url": f"{HOST}/Monitra-Setup-{TARGET}.exe",
         "sha256": "a" * 64, "file_size": 32478743, "signed": False},
        {"platform": "darwin", "architecture": "arm64", "download_url": f"{HOST}/Monitra-macOS-arm64-{TARGET}.dmg",
         "sha256": "b" * 64, "file_size": 55273984, "signed": False},
        {"platform": "darwin", "architecture": "x86_64", "download_url": f"{HOST}/Monitra-macOS-x86_64-{TARGET}.dmg",
         "sha256": "c" * 64, "file_size": 62060924, "signed": False},
    ]


def _forget_release(db):
    """Every row this test writes about TARGET, before and after, so a
    re-run on the same database starts clean and leaves nothing behind."""
    for statement in (
        "DELETE FROM desktop_update_reports WHERE target_version = :v",
        "DELETE FROM desktop_update_requirements WHERE required_version = :v",
        "DELETE FROM email_notifications WHERE notification_type = 'release' AND dedupe_key LIKE :k",
        "DELETE FROM activity_logs WHERE module = 'system' AND action LIKE 'release_%' "
        "AND entity_id IN (SELECT id FROM desktop_release_versions WHERE version = :v)",
        "DELETE FROM desktop_releases WHERE version = :v",
        "DELETE FROM desktop_release_versions WHERE version = :v",
    ):
        _sql(db, statement, v=TARGET, k=f"release:{TARGET}:%")


@pytest.fixture
def clean_release(db):
    _forget_release(db)
    yield
    _forget_release(db)


def test_the_whole_release_journey(qapp, backend, principal, administrator, desktop, db, monkeypatch, tmp_path, clean_release):
    admin = _client(backend, administrator["token"])
    member = _client(backend, principal["token"], **_desktop_headers())
    report = {}

    # ── 1. create -> validate -> publish, announcement queued and delivered ──
    created = admin.post("/desktop/releases/versions", json={
        "version": TARGET, "title": "E2E release", "release_notes": "- Faster startup\n- Fixes",
        "target_platforms": ["win32", "darwin"], "mandatory": True, "grace_hours": 24,
        "artifacts": _artifacts(),
    })
    assert created.status_code == 201, created.text
    assert created.json()["status"] == "draft" and created.json()["ready"] is True
    assert member.get("/desktop/latest-version").json()["update_available"] is False, "a draft affects nobody"

    validated = admin.post(f"/desktop/releases/versions/{TARGET}/validate")
    assert validated.status_code == 200 and validated.json()["status"] == "validated", validated.text

    forbidden = member.post(f"/desktop/releases/versions/{TARGET}/publish")
    assert forbidden.status_code == 403, "a member cannot publish"

    published = admin.post(f"/desktop/releases/versions/{TARGET}/publish")
    assert published.status_code == 200, published.text
    assert len(published.json()["published"]) == 3
    assert published.json()["announcement_queued"] is True
    again = admin.post(f"/desktop/releases/versions/{TARGET}/publish")
    assert again.status_code == 200 and again.json()["announcement_queued"] is False, "publishing twice is a no-op"

    detail = admin.get(f"/desktop/releases/versions/{TARGET}").json()
    assert detail["status"] == "published"
    assert detail["stats"]["email"].get("pending", 0) >= 1
    assert [h["action"] for h in detail["history"]][:1] == ["release_published"]
    report["targeted_users"] = detail["stats"]["targeted_users"]

    dispatched = httpx.post(f"{backend}/internal/email/dispatch", timeout=60,
                            headers={"X-Email-Dispatch-Token": "local-rig-dispatch-token"})
    assert dispatched.status_code == 200, dispatched.text
    detail = admin.get(f"/desktop/releases/versions/{TARGET}").json()
    assert detail["stats"]["email"].get("sent", 0) >= 1 and detail["stats"]["email"].get("pending", 0) == 0
    rows = _sql(db, "SELECT recipients, payload FROM email_notifications WHERE notification_type='release' AND dedupe_key LIKE :k",
                k=f"release:{TARGET}:user:%")
    assert rows, "one announcement row per targeted user"
    payload = json.loads(rows[0][1])
    assert payload["mandatory"] is True and payload["grace_hours"] == 24 and payload["platforms"] == ["win32", "darwin"]
    report["emails"] = len(rows)

    # ── 2. each platform gets its own artifact; a Linux claim is held to the policy ──
    arm = member.get("/desktop/latest-version", headers=_desktop_headers(platform="darwin", arch="arm64")).json()
    intel = member.get("/desktop/latest-version", headers=_desktop_headers(platform="darwin", arch="x86_64")).json()
    win = member.get("/desktop/latest-version").json()
    assert arm["download_url"].endswith(f"Monitra-macOS-arm64-{TARGET}.dmg") and arm["architecture"] == "arm64"
    assert intel["download_url"].endswith(f"Monitra-macOS-x86_64-{TARGET}.dmg")
    assert win["download_url"].endswith(f"Monitra-Setup-{TARGET}.exe") and win["mandatory"] is True
    linux = member.get("/desktop/update-policy", headers=_desktop_headers(platform="linux", arch="x86_64")).json()
    assert linux["update_required"] is True and linux["platform"] == "unknown"

    # ── 3. the desktop's own check: grace on the server clock, Later, no reset ──
    offers, blocked_edges = [], []
    desktop.updates.update_offered.connect(lambda r, m, s: offers.append((r.version, m, s)))
    desktop.updates.blocked_changed.connect(blocked_edges.append)
    desktop.updates.check_now()
    _pump(qapp, lambda: desktop.updates.requirement is not None, 30, "the desktop to receive the requirement")
    requirement = desktop.updates.requirement
    assert requirement.required_version == TARGET
    assert offers and offers[-1] == (TARGET, True, "initial")
    assert desktop.updates.is_blocked is False
    remaining = desktop.updates.requirement_remaining_seconds()
    assert 24 * 3600 - 120 <= remaining <= 24 * 3600, remaining
    policy = member.get("/desktop/update-policy").json()
    deadline = policy["grace"]["deadline_at"]
    first_seen = policy["grace"]["first_seen_at"]
    assert policy["grace"]["active"] is True and policy["blocked"] is False
    # Not a second after first contact: the desktop's check started it.
    assert abs((datetime.fromisoformat(deadline) - datetime.fromisoformat(first_seen)).total_seconds() - 24 * 3600) < 1

    desktop.updates.postpone()
    desktop.updates.check_now()
    _pump(qapp, lambda: len(offers) >= 2, 30, "the reminder after Later")
    assert offers[-1] == (TARGET, True, "reminder")
    assert member.get("/desktop/update-policy").json()["grace"]["deadline_at"] == deadline, "a repeated check does not move it"
    other_device = member.get("/desktop/update-policy", headers=_desktop_headers(client_version="1.3.2")).json()
    assert other_device["grace"]["deadline_at"] == deadline, "a second device shares the deadline"
    assert len(_sql(db, "SELECT id FROM desktop_update_requirements WHERE user_id=:u", u=principal["user_id"])) == 2, \
        "one row for win32, one for the unknown (Linux) claim -- never one per check"

    # A restart: the persisted requirement carries the same deadline.
    from background_services.update.update_service import REQUIREMENT_KEY
    stored = desktop.cache.load_app_state(REQUIREMENT_KEY)
    assert abs(stored["deadline_at"] - datetime.fromisoformat(deadline).timestamp()) < 1

    # ── 4. a timer started inside the grace ──
    timer = desktop.timer
    timer.start_tracking(principal["project_id"], principal["task_id"], "E2E release task")
    _pump(qapp, lambda: timer.entry_id is not None, 30, "the start to be bound")
    entry_id = timer.entry_id
    assert timer.is_running()

    # ── 5. the deadline passes on the server ──
    _sql(db, "UPDATE desktop_update_requirements SET deadline_at = now() - interval '1 minute' WHERE user_id=:u",
         u=principal["user_id"])
    refused = member.post("/time-entries/start", json={"project_id": principal["project_id"], "task_id": principal["task_id"]})
    assert refused.status_code == 426, refused.text
    assert refused.json()["detail"]["code"] == "desktop_update_required"
    assert refused.json()["detail"]["required_version"] == TARGET
    assert member.get("/time-entries/active").status_code == 200
    assert member.get("/auth/me").status_code == 200
    assert member.get("/desktop/update-policy").json()["blocked"] is True
    assert member.post(f"/time-entries/{entry_id}/heartbeat", json={}).status_code != 426
    assert member.get("/api/v1/projects").status_code == 426

    desktop.updates.check_now()
    _pump(qapp, lambda: desktop.updates.is_blocked, 30, "the desktop to block")
    assert blocked_edges == [True]
    assert timer.is_running(), "enforcement never ends the running session"
    errors = []
    timer.timer_error.connect(errors.append)
    timer.switch_tracking(principal["project_id"], principal["task_id"], "another")
    assert errors and "required Monitra update" in errors[-1]
    assert timer.entry_id == entry_id and timer.is_running()

    import main as main_module
    monkeypatch.setattr(main_module.MainWindow, "_on_exit_ready", lambda self: None)
    from PySide6.QtCore import QSettings
    monkeypatch.setattr(main_module, "window_settings",
                        lambda: QSettings(str(tmp_path / "w.ini"), QSettings.Format.IniFormat))
    window = main_module.MainWindow(desktop)
    window.api.notify = lambda *a, **k: None
    try:
        me = member.get("/auth/me").json()
        desktop.session_manager.start_session(principal["token"], me)
        window._enter_dashboard(me, announce=False)
        assert window._stack.currentWidget() is window._update_screen
        assert TARGET in window._update_screen._required_row.value_label.text()
        assert "E2E release" in window._update_screen._title.text()

        # ── 6. Update Now against a published-but-not-uploaded installer: a real 404 ──
        failures = []
        desktop.updates.update_failed.connect(failures.append)
        # This suite runs from a source checkout, which `can_install` refuses
        # before anything is fetched. Lifting that one check lets the real
        # download run; the installer could only be reached after a verified
        # download, and this one ends at the website's 404.
        from background_services.update import update_service as update_module
        monkeypatch.setattr(update_module, "can_install", lambda: None)
        assert desktop.updates.start_update() is True
        _pump(qapp, lambda: failures, 120, "the download to fail against the real website")
        # The website answers a missing installer with its index page (200,
        # text/html), measured here; a 404 would be the other honest answer.
        # Either way nothing is installed and the user is told why.
        lowered = failures[-1].lower()
        assert "web page instead of the installer" in lowered or "404" in lowered, failures[-1]
        assert desktop.updates.requirement is not None and desktop.updates.is_blocked
        assert window._stack.currentWidget() is window._update_screen
        _pump(qapp, lambda: admin.get(f"/desktop/releases/versions/{TARGET}").json()["stats"]["installs"].get("download-failed", 0) >= 1,
              30, "the failure to be reported to the backend")
        report["failure_reported"] = True
    finally:
        window._dashboard.reset_state()
        window.hide()
        window.deleteLater()
        qapp.processEvents()

    # the timer, through all of it: still the same entry, still running
    assert _running_rows(db, principal["user_id"]) and timer.entry_id == entry_id
    finalized = []
    timer.timer_finalized.connect(finalized.append)
    timer.stop_tracking()
    _pump(qapp, lambda: finalized, 60, "the stop to land (stop stays open past the deadline)")
    row = _row(db, entry_id)
    assert row["end_time"] is not None and abs(int(row["total_seconds"]) - int(row["derived"])) <= 1
    assert len(_sql(db, "SELECT id FROM time_entries WHERE user_id=:u", u=principal["user_id"])) == 1, "no duplicate entry"

    # ── 7. the "installed" client (simulated: the running version is the target) ──
    monkeypatch.setattr(version, "VERSION", TARGET)
    from background_services.update import UpdateService
    installed = UpdateService(desktop, desktop.update_api, desktop.cache)
    installed._load_requirement()
    assert installed.requirement is None and installed.is_blocked is False, \
        "the persisted requirement is dropped by the running version, nothing else"
    updated = _client(backend, principal["token"], **_desktop_headers(client_version=TARGET))
    answer = updated.get("/desktop/latest-version").json()
    assert answer["update_available"] is False and answer["blocked"] is False and answer["mandatory"] is False
    satisfied = _sql(db, "SELECT satisfied_version FROM desktop_update_requirements WHERE user_id=:u AND platform='win32'",
                     u=principal["user_id"])
    assert satisfied and satisfied[0][0] == TARGET, "the server marked the grace row satisfied on observing the version"
    assert updated.get("/api/v1/projects").status_code == 200
    detail = admin.get(f"/desktop/releases/versions/{TARGET}").json()
    assert detail["stats"]["requirements"]["satisfied"] >= 1
    report["stats"] = detail["stats"]

    # ── 8. withdrawal keeps the grace rows and suspends enforcement for everyone still below ──
    withdrawn = admin.post(f"/desktop/releases/versions/{TARGET}/withdraw?reason=e2e")
    assert withdrawn.status_code == 200, withdrawn.text
    assert withdrawn.json()["version"]["status"] == "withdrawn"
    assert withdrawn.json()["suspended_requirements"] >= 1
    old_client = member.get("/desktop/update-policy").json()
    assert old_client["update_required"] is False and old_client["blocked"] is False
    assert member.post("/time-entries/start", json={"project_id": principal["project_id"], "task_id": principal["task_id"]}).status_code != 426
    assert _sql(db, "SELECT id FROM desktop_update_requirements WHERE user_id=:u", u=principal["user_id"]), "rows kept"
    # tidy the entry the last start may have created
    for (running_id,) in _running_rows(db, principal["user_id"]):
        member.post(f"/time-entries/{running_id}/stop", json={})

    print("\n  E2E release journey:", json.dumps(report, default=str))

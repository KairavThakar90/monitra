"""
An administrator excludes a signed-in member: the real desktop, against the
real backend and the real (development) database.

    MONITRA_E2E=1 python -m pytest tests/e2e/test_login_exclusion_e2e.py -q -s

The member is the disposable principal the timing suite provisions (an
`@e2e.invalid` account with its own project and task, removed afterwards);
the administrator is a second disposable principal provisioned the same way.
Nothing here touches a real member.

What is pinned, end to end and without any refresh:

* the member has a timer running on the server when the administrator
  excludes them;
* the open desktop window returns to the sign-in screen by itself, within a
  few seconds (the dashboard's sync probe meets the backend's refusal), and
  shows the administrator's message with the contact note in bold;
* the running entry is stopped on the server;
* allowed again, the member's fresh session is accepted.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time

import httpx
import pytest

from app.api.exceptions import LOGIN_DISABLED_MESSAGE, LOGIN_DISABLED_NOTE
from tests.e2e.test_timing_lifecycle_e2e import (  # noqa: F401  (fixtures)
    BACKEND_ROOT, _pump, _running_rows, backend, db, desktop, principal, pytestmark,
)


@pytest.fixture(scope="module")
def administrator():
    """A second disposable principal, with the administrator role."""
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


def test_an_excluded_member_is_signed_out_of_the_open_desktop_and_their_timer_stops(
    qapp, desktop, backend, principal, administrator, db, monkeypatch,
):
    import main as main_module

    member = httpx.Client(base_url=backend, timeout=30,
                          headers={"Authorization": f"Bearer {principal['token']}"})
    admin = httpx.Client(base_url=backend, timeout=30,
                         headers={"Authorization": f"Bearer {administrator['token']}"})

    # A timer is running on the server.
    started = member.post("/time-entries/start", json={
        "project_id": principal["project_id"], "task_id": principal["task_id"],
    })
    assert started.status_code == 201, started.text
    assert _running_rows(db, principal["user_id"])

    # The member's desktop is open on the dashboard.
    monkeypatch.setattr(main_module.MainWindow, "_on_exit_ready", lambda self: None)
    window = main_module.MainWindow(desktop)
    window.api.notify = lambda *a, **k: None
    window.show()
    me = member.get("/auth/me").json()
    desktop.session_manager.start_session(principal["token"], me)
    window._enter_dashboard(me, announce=False)
    _pump(qapp, lambda: window._stack.currentWidget() is window._dashboard, 10, "the dashboard")

    try:
        # The administrator excludes the member.
        excluded_at = time.monotonic()
        response = admin.patch(f"/api/v1/members/{principal['user_id']}", json={"can_login": False})
        assert response.status_code == 200, response.text
        assert response.json()["can_login"] is False

        # The open window signs itself out -- no refresh, no restart.
        _pump(qapp, lambda: window._stack.currentWidget() is window._login, 20,
              "the desktop to return to the sign-in screen")
        elapsed = time.monotonic() - excluded_at
        print(f"\n  desktop signed out {elapsed:.1f}s after the administrator's exclusion")
        assert elapsed < 12, elapsed
        text = window._login.error_label.text()
        assert LOGIN_DISABLED_MESSAGE in text
        assert f"<b>{LOGIN_DISABLED_NOTE}</b>" in text

        # The running entry was stopped on the server.
        assert _running_rows(db, principal["user_id"]) == []

        # Every request from the member is refused with the reason.
        refused = member.get("/auth/me")
        assert refused.status_code == 401
        assert refused.json()["detail"]["code"] == "login_disabled"

        # Allowed again: a fresh session for the member is accepted.
        response = admin.patch(f"/api/v1/members/{principal['user_id']}", json={"can_login": True})
        assert response.status_code == 200 and response.json()["can_login"] is True
        assert member.get("/auth/me").status_code == 200
    finally:
        box = getattr(window._login, "_login_disabled_box", None)
        if box is not None:
            try:
                box.close()
            except RuntimeError:
                pass
        window._dashboard.reset_state()
        window.hide()
        window.deleteLater()
        qapp.processEvents()

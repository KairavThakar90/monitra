"""
The desktop's reminder catalogue and the backend's copy of it must agree.

The backend lets an administrator name, switch, move and restrict each built-in
reminder, so it keeps its own small catalogue
(`backend/app/services/desktop_notification_catalogue.py`). The wording and the
timing logic stay on the desktop; this test is what keeps the copy honest. Add
a reminder, change a cadence, move a default time or reword a body on one side
and it fails until the other side follows.

The backend module is standard-library only and is loaded by path -- the
backend is a separate application that is not on the desktop's `sys.path`
(same approach as `test_validation_framework.py`). If `backend/` is absent, as
in a packaged desktop checkout, the comparison is skipped: absence proves
nothing about the catalogues.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_CATALOGUE = REPO_ROOT / "backend" / "app" / "services" / "desktop_notification_catalogue.py"
DESKTOP_REMINDERS = (
    Path(__file__).resolve().parent.parent / "background_services" / "wellbeing" / "reminders.py"
)


def _load(path: Path, name: str):
    if not path.is_file():
        pytest.skip(f"{path.name} is not present in this checkout")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolves string annotations through sys.modules.
    import sys
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(name, None)
    return module


@pytest.fixture(scope="module")
def backend():
    return _load(BACKEND_CATALOGUE, "_backend_notification_catalogue")


@pytest.fixture(scope="module")
def desktop():
    return _load(DESKTOP_REMINDERS, "_desktop_wellbeing_reminders")


def _desktop_by_key(desktop):
    items = {r.key: r for r in desktop.INTERVAL_REMINDERS}
    daily = {r.key: r for r in desktop.DAILY_REMINDERS}
    assert not set(items) & set(daily), "a key is both an interval and a daily reminder"
    return items, daily


def test_the_same_keys_exist_on_both_sides(backend, desktop):
    interval, daily = _desktop_by_key(desktop)

    assert set(backend.BUILTIN_BY_KEY) == set(interval) | set(daily), (
        "the desktop and backend catalogues name different reminders: "
        f"only backend {sorted(set(backend.BUILTIN_BY_KEY) - set(interval) - set(daily))}, "
        f"only desktop {sorted((set(interval) | set(daily)) - set(backend.BUILTIN_BY_KEY))}"
    )
    assert len(backend.BUILTIN_NOTIFICATIONS) == len(backend.BUILTIN_BY_KEY), (
        "the backend catalogue repeats a key"
    )


def test_the_kinds_agree(backend, desktop):
    interval, daily = _desktop_by_key(desktop)

    for key, item in backend.BUILTIN_BY_KEY.items():
        expected = backend.KIND_INTERVAL if key in interval else backend.KIND_DAILY
        assert item.kind == expected, f"{key}: backend says {item.kind}"


def test_every_interval_cadence_agrees(backend, desktop):
    interval, _ = _desktop_by_key(desktop)

    for key, reminder in interval.items():
        item = backend.BUILTIN_BY_KEY[key]
        assert item.every_minutes == reminder.every_minutes, (
            f"{key}: desktop every {reminder.every_minutes} min, "
            f"backend every {item.every_minutes}"
        )
        assert item.default_time is None, f"{key}: an interval reminder has no time of day"


def test_every_daily_default_time_agrees(backend, desktop):
    _, daily = _desktop_by_key(desktop)

    for key, reminder in daily.items():
        item = backend.BUILTIN_BY_KEY[key]
        assert item.default_time == reminder.at.strftime("%H:%M"), (
            f"{key}: desktop at {reminder.at:%H:%M}, backend default {item.default_time}"
        )
        assert item.every_minutes is None, f"{key}: a daily reminder has no cadence"


def test_every_description_is_the_desktops_own_wording(backend, desktop):
    interval, daily = _desktop_by_key(desktop)

    for key, reminder in {**interval, **daily}.items():
        assert backend.BUILTIN_BY_KEY[key].description == reminder.body, (
            f"{key}: the backend's description is not the desktop's body"
        )

"""The desktop configuration revision: "something an administrator set for a desktop changed".

A member's switches, the screens captured for them, the deployment's screenshot size and
quality and the privacy rules each reach a desktop through a poll it already makes every
three minutes. This pins the accelerator on top of that poll: one counter, raised by every
such change, carried on the notification change stream so a connected desktop re-reads
its configuration within a second or two instead of at the next poll. What matters:

* **Every writer raises it, and names the member it concerns** (or nobody, for a change
  every desktop reads), after its own commit and audit -- never before, never instead.
* **A change for one member reaches that member's desktop and not the fleet's**: the
  stream says whether a change since the revision a desktop last heard concerns it.
* **An older desktop is never harmed**: a client that does not ask about the revision is
  not sent an event for a configuration change alone (it would read the unchanged
  schedule version as the same change signalled twice, and back off its stream).
* **A failure to record it never reaches the administrator**: their change is saved;
  the desktop learns of it at its poll.
"""
import asyncio
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from app.core.database import get_db
from app.core.security import get_current_user
from app.main import app
from app.models.system_setting import SystemSetting, SystemSettingKey
from app.schemas.member import MemberUpdate
from app.schemas.screenshot_settings import ScreenshotSettingsUpdate
from app.services import desktop_notification_push as push
from app.services.desktop_config_revision import MAX_REMEMBERED_CHANGES, DesktopConfigRevisionService as Service
from app.services.member_display import MemberDisplayService
from app.services.member_service import DESKTOP_CONFIG_FIELDS, MemberService
from app.services.screenshot_settings import ScreenshotSettingsService

from tests.test_member_desktop_switches import _add_member, _admin, _sqlite_session
from tests.test_member_displays import (
    _add_member as _add_display_member, _choose, _report, _screen, _sqlite_session as _display_session,
    _user as _display_user,
)


def _stored(db):
    db.expire_all()
    row = db.get(SystemSetting, SystemSettingKey.DESKTOP_CONFIG)
    return row.value if row is not None else None


# ── The counter ─────────────────────────────────────────────────────────────


class BumpTests(unittest.TestCase):

    def setUp(self):
        self.db = _sqlite_session()

    def test_nothing_stored_is_revision_zero_with_nothing_remembered(self):
        self.assertEqual(Service.current(self.db), {"revision": 0, "changes": []})

    def test_each_raise_is_one_more_and_remembers_who_it_concerned(self):
        with mock.patch.object(push, "notify_changed") as woke:
            self.assertEqual(Service.bump(self.db, member_id=42, what="test"), 1)
            self.assertEqual(Service.bump(self.db, member_id=None, what="test"), 2)
        self.assertEqual(woke.call_count, 2)
        self.assertEqual(
            Service.current(self.db),
            {"revision": 2, "changes": [{"revision": 1, "member": 42}, {"revision": 2, "member": None}]},
        )

    def test_only_the_last_few_changes_are_remembered(self):
        with mock.patch.object(push, "notify_changed"):
            for i in range(MAX_REMEMBERED_CHANGES + 5):
                Service.bump(self.db, member_id=i, what="test")
        state = Service.current(self.db)
        self.assertEqual(state["revision"], MAX_REMEMBERED_CHANGES + 5)
        self.assertEqual(len(state["changes"]), MAX_REMEMBERED_CHANGES)
        self.assertEqual(state["changes"][0]["revision"], 6)

    def test_a_damaged_row_reads_as_zero_and_is_raised_from_there(self):
        self.db.add(SystemSetting(key=SystemSettingKey.DESKTOP_CONFIG, value={"revision": "x", "changes": "y"}))
        self.db.commit()
        self.assertEqual(Service.current(self.db), {"revision": 0, "changes": []})
        with mock.patch.object(push, "notify_changed"):
            self.assertEqual(Service.bump(self.db, member_id=None, what="test"), 1)

    def test_a_failure_to_record_is_logged_and_never_raised(self):
        with mock.patch.object(push, "notify_changed") as woke, \
             mock.patch("app.services.desktop_config_revision.SystemSettingRepository.get_for_update",
                        side_effect=RuntimeError("database away")):
            self.assertIsNone(Service.bump(self.db, member_id=1, what="test"))
        woke.assert_not_called()
        self.assertIsNone(_stored(self.db))


class ConcernsTests(unittest.TestCase):

    STATE = {"revision": 12, "changes": [
        {"revision": 10, "member": 42}, {"revision": 11, "member": 7}, {"revision": 12, "member": None},
    ]}

    def test_nothing_after_what_the_desktop_heard_concerns_nobody(self):
        self.assertFalse(Service.concerns(self.STATE, 12, 42))
        self.assertFalse(Service.concerns(self.STATE, 13, 42))

    def test_a_change_for_everyone_concerns_every_desktop(self):
        self.assertTrue(Service.concerns(self.STATE, 11, 42))
        self.assertTrue(Service.concerns(self.STATE, 11, 999))

    def test_a_change_for_one_member_concerns_that_member_only(self):
        state = {"revision": 11, "changes": [{"revision": 10, "member": 42}, {"revision": 11, "member": 7}]}
        self.assertTrue(Service.concerns(state, 9, 42))
        self.assertTrue(Service.concerns(state, 10, 7))
        self.assertFalse(Service.concerns(state, 10, 42))
        self.assertFalse(Service.concerns(state, 9, 999))

    def test_a_desktop_too_far_behind_to_be_sure_is_told_it_concerns_it(self):
        state = {"revision": 60, "changes": [{"revision": r, "member": 5} for r in range(11, 61)]}
        self.assertTrue(Service.concerns(state, 3, 999))     # older than the list: cannot say no
        self.assertFalse(Service.concerns(state, 10, 999))   # exactly at the list's edge: can say no


# ── The writers ─────────────────────────────────────────────────────────────


class WritersRaiseItTests(unittest.TestCase):

    def setUp(self):
        self.db = _sqlite_session()
        self.admin = _admin(user_id=5)
        _add_member(self.db, 40)
        patcher = mock.patch.object(push, "notify_changed")
        self.woke = patcher.start()
        self.addCleanup(patcher.stop)

    def test_each_desktop_field_of_the_member_update_raises_it_for_that_member(self):
        changes = {
            "screenshots_enabled": False, "notifications_enabled": False, "activity_tracking_enabled": False,
            "idle_enabled": False, "idle_minutes": 7, "capture_frequency": 3,
        }
        self.assertEqual(set(changes), set(DESKTOP_CONFIG_FIELDS))
        for i, (field, value) in enumerate(changes.items(), start=1):
            with self.subTest(field=field):
                MemberService.update(self.db, self.admin, 40, MemberUpdate(**{field: value}))
                state = Service.current(self.db)
                self.assertEqual(state["revision"], i)
                self.assertEqual(state["changes"][-1], {"revision": i, "member": 40})

    def test_a_field_the_desktop_does_not_poll_for_does_not_raise_it(self):
        MemberService.update(self.db, self.admin, 40, MemberUpdate(name="Renamed"))
        self.assertEqual(Service.current(self.db)["revision"], 0)
        self.woke.assert_not_called()

    def test_re_sending_the_current_position_does_not_raise_it(self):
        MemberService.update(self.db, self.admin, 40, MemberUpdate(screenshots_enabled=True))
        self.assertEqual(Service.current(self.db)["revision"], 0)

    def test_a_screen_choice_raises_it_for_that_member_and_an_unchanged_choice_does_not(self):
        db = _display_session()  # holds member_displays too
        SystemSetting.__table__.create(db.get_bind())
        _add_display_member(db, 40)
        member = _display_user("employee", user_id=40, username="mia")
        MemberDisplayService.report(db, member, _report(_screen(1), _screen(2)))
        self.assertEqual(Service.current(db)["revision"], 0, "a desktop's report is facts, not a change")
        MemberDisplayService.set_choices(db, self.admin, 40, _choose(s2=False))
        self.assertEqual(Service.current(db)["changes"][-1], {"revision": 1, "member": 40})
        MemberDisplayService.set_choices(db, self.admin, 40, _choose(s2=False))
        self.assertEqual(Service.current(db)["revision"], 1)

    def test_the_screenshot_size_and_quality_raise_it_for_every_desktop(self):
        ScreenshotSettingsService.update_settings(
            self.db, self.admin, ScreenshotSettingsUpdate(image_width=1000, image_height=800, image_quality=None),
        )
        self.assertEqual(Service.current(self.db)["changes"][-1], {"revision": 1, "member": None})
        ScreenshotSettingsService.update_settings(
            self.db, self.admin, ScreenshotSettingsUpdate(image_width=1000, image_height=800, image_quality=None),
        )
        self.assertEqual(Service.current(self.db)["revision"], 1, "an unchanged setting is not a change")


# ── The stream ──────────────────────────────────────────────────────────────


class FakeStore:
    def __init__(self, version=3, config=5, changes=()):
        self.state = push.WatchState(version=version, config=config, changes=tuple(changes))

    def read(self):
        return self.state

    def move_config(self, member):
        config = self.state.config + 1
        self.state = push.WatchState(
            self.state.version, config, self.state.changes + ({"revision": config, "member": member},),
        )

    def move_version(self):
        self.state = push.WatchState(self.state.version + 1, self.state.config, self.state.changes)


class StreamTests(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        for patcher in (
            mock.patch.object(push, "HEARTBEAT_SECONDS", 0.05),
            mock.patch.object(push, "MAX_STREAM_SECONDS", 0.6),
            mock.patch.object(push, "WATCH_INTERVAL_SECONDS", 0.1),
            mock.patch.object(push, "_shutting_down", False),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.store = FakeStore()
        self.watcher = push.VersionWatcher(self.store.read)

    async def collect(self, since, since_config=None, user_id=42, within=8.0):
        chunks = []

        async def read():
            async for chunk in push.event_stream(
                since, source=self.watcher, since_config=since_config, user_id=user_id,
            ):
                chunks.append(chunk)

        await asyncio.wait_for(read(), within)
        return chunks

    def events(self, chunks):
        return [c for c in chunks if c.startswith("event:")]

    async def test_a_desktop_that_missed_a_configuration_change_is_told_at_once_whether_it_concerns_it(self):
        self.store.move_config(member=42)
        for user, concerns in ((42, True), (7, False)):
            with self.subTest(user=user):
                chunks = await self.collect(since=3, since_config=5, user_id=user)
                self.assertEqual(chunks, [push.sse_event(3, 6, concerns)])
                self.assertIn('"config": 6', chunks[0])
                self.assertIn(f'"concerns": {str(concerns).lower()}', chunks[0])

    async def test_a_configuration_change_while_connected_is_one_event_and_then_the_stream_ends(self):
        async def change_soon():
            await asyncio.sleep(0.15)
            self.store.move_config(member=None)
            self.watcher.poke()

        asyncio.ensure_future(change_soon())
        chunks = await self.collect(since=3, since_config=5)
        self.assertEqual(self.events(chunks), [push.sse_event(3, 6, True)])

    async def test_an_older_desktop_hears_nothing_about_a_configuration_change_and_still_hears_the_version(self):
        async def changes_soon():
            await asyncio.sleep(0.15)
            self.store.move_config(member=None)
            self.watcher.poke()
            await asyncio.sleep(0.15)
            self.store.move_version()
            self.watcher.poke()

        asyncio.ensure_future(changes_soon())
        chunks = await self.collect(since=3, since_config=None)
        self.assertEqual(self.events(chunks), [push.sse_event(4)])
        self.assertNotIn("config", chunks[-1])

    async def test_an_older_desktop_that_is_up_to_date_hears_only_pings(self):
        self.store.move_config(member=None)
        chunks = await self.collect(since=3, since_config=None)
        self.assertEqual(self.events(chunks), [])

    async def test_a_version_move_also_carries_the_configuration_revision_for_a_desktop_that_asked(self):
        async def change_soon():
            await asyncio.sleep(0.15)
            self.store.move_version()
            self.watcher.poke()

        asyncio.ensure_future(change_soon())
        chunks = await self.collect(since=3, since_config=5)
        self.assertEqual(self.events(chunks), [push.sse_event(4, 5, False)])


class RouteTests(unittest.TestCase):

    def setUp(self):
        self.db = _sqlite_session()
        self.user = _admin(user_id=5)
        app.dependency_overrides[get_db] = lambda: self.db
        app.dependency_overrides[get_current_user] = lambda: self.user
        self.client = TestClient(app)
        self.addCleanup(app.dependency_overrides.clear)
        for patcher in (
            mock.patch.object(push, "HEARTBEAT_SECONDS", 0.05),
            mock.patch.object(push, "MAX_STREAM_SECONDS", 0.3),
            mock.patch.object(push, "WATCH_INTERVAL_SECONDS", 0.1),
            mock.patch.object(push, "_shutting_down", False),
            mock.patch.object(push.watcher, "_read_version", lambda: push.WatchState(
                version=0, config=Service.current(self.db)["revision"],
                changes=tuple(Service.current(self.db)["changes"]),
            )),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _stream(self, query):
        with self.client.stream("GET", f"/desktop-notifications/stream?{query}") as response:
            return response.status_code, "".join(response.iter_text())

    def test_a_desktop_that_asks_is_told_about_a_change_for_it_and_not_one_for_someone_else(self):
        _add_member(self.db, 40)
        with mock.patch.object(push, "notify_changed"):
            MemberService.update(self.db, self.user, 40, MemberUpdate(screenshots_enabled=False))
        self.user.id = 40
        status, body = self._stream("since=0&since_config=0")
        self.assertEqual(status, 200)
        self.assertIn('"config": 1', body)
        self.assertIn('"concerns": true', body)
        self.user.id = 41
        status, body = self._stream("since=0&since_config=0")
        self.assertIn('"concerns": false', body)
        status, body = self._stream("since=0&since_config=1")
        self.assertNotIn("event:", body)

    def test_an_older_desktop_asking_without_the_revision_is_answered_as_before(self):
        status, body = self._stream("since=0")
        self.assertEqual(status, 200)
        self.assertNotIn("event:", body)
        self.assertIn(": connected", body)

    def test_a_negative_revision_is_422(self):
        self.assertEqual(self.client.get("/desktop-notifications/stream?since=0&since_config=-1").status_code, 422)

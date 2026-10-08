"""Pushing desktop notifications: the event stream and "push now".

What is pinned here, and why each matters:

* **A change reaches an open desktop at once, not at its next poll.** The stream
  tells it the moment the stored ``version`` moves -- immediately when the change
  was made in this process, within the watcher's interval when another worker
  made it (production runs more than one).
* **The stream is cheap and cannot pile up.** One query per interval per process
  however many desktops are connected, none with nobody connected; a stream that
  is closed, cancelled or errored always releases its place.
* **A stream always ends**, by itself after a bounded time and within a heartbeat
  of the process being told to stop -- otherwise a restart waits on it.
* **A pushed message is delivered, then forgotten.** It is in the schedule for
  ten minutes with its age measured by the server, never longer, and the row
  remembers only the last twenty.
* **Only an administrator may push**, with the same title/message rules as a
  custom notification; every push is audited.
"""
import asyncio
import signal
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.database import get_db
from app.core.security import get_current_user
from app.main import app
from app.models.activity_log import ActivityLog, ActivityLogAction
from app.models.system_setting import SystemSetting, SystemSettingKey
from app.schemas.desktop_notifications import BuiltinNotificationUpdate, DesktopPushCreate
from app.services import desktop_notification_push as push
from app.services.desktop_notification_catalogue import MAX_STORED_PUSHES, PUSH_TTL_SECONDS
from app.services.desktop_notifications import DesktopNotificationService as Service

from tests.test_desktop_notifications import _sqlite_session, _user


def _push(title="Server restart", message="Please save your work. The server restarts in five minutes."):
    return DesktopPushCreate(title=title, message=message)


# ── "Push now": the service ──────────────────────────────────────────────────


class PushNowTests(unittest.TestCase):

    def setUp(self):
        self.db = _sqlite_session()
        self.admin = _user("administrator", user_id=5, username="grace")

    def _audit(self):
        return list(self.db.execute(select(ActivityLog).order_by(ActivityLog.id)).scalars())

    def _stored(self):
        return self.db.get(SystemSetting, SystemSettingKey.DESKTOP_NOTIFICATIONS).value

    def test_a_push_is_in_the_schedule_with_its_age_and_bumps_the_version(self):
        before = Service.get_schedule(self.db)
        self.assertEqual((before["version"], before["pushes"]), (0, []))

        Service.push_now(self.db, self.admin, _push())

        after = Service.get_schedule(self.db)
        self.assertEqual(after["version"], 1)
        self.assertEqual(len(after["pushes"]), 1)
        item = after["pushes"][0]
        self.assertEqual((item["title"], item["message"]), ("Server restart", "Please save your work. The server restarts in five minutes."))
        self.assertLessEqual(item["seconds_ago"], 2)
        self.assertTrue(item["id"])

    def test_a_push_changes_nothing_else_in_the_schedule(self):
        Service.update_builtin(self.db, self.admin, "hydrate", BuiltinNotificationUpdate(enabled=False))
        Service.push_now(self.db, self.admin, _push())

        schedule = Service.get_schedule(self.db)
        self.assertFalse(next(r for r in schedule["builtin"] if r["key"] == "hydrate")["enabled"])
        self.assertEqual(schedule["custom"], [])

    def test_it_is_audited_with_who_and_what(self):
        Service.push_now(self.db, self.admin, _push(title="Server restart"))

        rows = self._audit()
        self.assertEqual([r.action for r in rows], [ActivityLogAction.DESKTOP_NOTIFICATION_PUSHED])
        self.assertIn("Server restart", rows[0].description)

    def test_the_same_words_twice_are_two_pushes(self):
        Service.push_now(self.db, self.admin, _push())
        Service.push_now(self.db, self.admin, _push())

        pushes = Service.get_schedule(self.db)["pushes"]
        self.assertEqual(len(pushes), 2)
        self.assertEqual(len({p["id"] for p in pushes}), 2)

    def test_the_row_remembers_only_the_last_few(self):
        for number in range(MAX_STORED_PUSHES + 5):
            Service.push_now(self.db, self.admin, _push(title=f"Push {number}"))

        titles = [p["title"] for p in Service.get_schedule(self.db)["pushes"]]
        self.assertEqual(len(titles), MAX_STORED_PUSHES)
        self.assertEqual(len(self._stored()["pushes"]), MAX_STORED_PUSHES)      # on the row itself
        self.assertEqual(titles[0], "Push 5")                      # the oldest five were dropped
        self.assertEqual(titles[-1], f"Push {MAX_STORED_PUSHES + 4}")

    def test_a_push_older_than_its_lifetime_is_not_in_the_schedule(self):
        now = datetime.now(timezone.utc)
        self.db.add(SystemSetting(key=SystemSettingKey.DESKTOP_NOTIFICATIONS, value={
            "version": 4, "builtin": {}, "custom": [],
            "pushes": [
                {"id": "old", "title": "Old", "message": "m", "sent_at": (now - timedelta(seconds=PUSH_TTL_SECONDS + 30)).isoformat(), "sent_by": "x"},
                {"id": "edge", "title": "Edge", "message": "m", "sent_at": (now - timedelta(seconds=PUSH_TTL_SECONDS - 30)).isoformat(), "sent_by": "x"},
                {"id": "new", "title": "New", "message": "m", "sent_at": (now - timedelta(seconds=90)).isoformat(), "sent_by": "x"},
            ],
        }))
        self.db.commit()

        pushes = Service.get_schedule(self.db)["pushes"]
        self.assertEqual([p["id"] for p in pushes], ["edge", "new"])
        self.assertAlmostEqual(pushes[1]["seconds_ago"], 90, delta=3)   # measured by the server, now

    def test_a_damaged_push_is_skipped_and_cannot_break_the_poll(self):
        now = datetime.now(timezone.utc).isoformat()
        self.db.add(SystemSetting(key=SystemSettingKey.DESKTOP_NOTIFICATIONS, value={
            "version": 2, "builtin": {}, "custom": [],
            "pushes": [
                "junk", {"id": "no-fields"}, {"id": "bad", "title": "t", "message": "m", "sent_at": "not a date"},
                {"id": "naive", "title": "Naive", "message": "m", "sent_at": datetime.now(timezone.utc).replace(tzinfo=None).isoformat()},
                {"id": "good", "title": "Good", "message": "m", "sent_at": now},
            ],
        }))
        self.db.commit()

        pushes = Service.get_schedule(self.db)["pushes"]
        self.assertEqual({p["id"] for p in pushes}, {"naive", "good"})  # a timestamp without a zone is read as UTC

    def test_a_schedule_with_no_pushes_stores_no_key_for_them(self):
        Service.update_builtin(self.db, self.admin, "hydrate", BuiltinNotificationUpdate(enabled=False))
        self.assertNotIn("pushes", self._stored())

    def test_the_stored_version_alone_can_be_read(self):
        self.assertEqual(Service.current_version(self.db), 0)
        Service.push_now(self.db, self.admin, _push())
        Service.push_now(self.db, self.admin, _push())
        self.assertEqual(Service.current_version(self.db), 2)

    def test_everyone_but_an_administrator_is_refused_and_nothing_is_written(self):
        service_principal = _user("release_bot")
        service_principal.is_service_principal = True
        from fastapi import HTTPException
        for who in (_user("hr"), _user("leader"), _user("manager"), _user("employee"), _user("client"), service_principal):
            with self.subTest(role=who.role_name):
                with self.assertRaises(HTTPException) as caught:
                    Service.push_now(self.db, who, _push())
                self.assertEqual(caught.exception.status_code, 403)
        self.assertEqual(Service.get_schedule(self.db)["version"], 0)

    def test_every_administrator_spelling_may_push(self):
        for role in ("administrator", "org_admin", "super_admin"):
            Service.push_now(self.db, _user(role), _push(title=f"From {role}"))
        self.assertEqual(len(Service.get_schedule(self.db)["pushes"]), 3)

    def test_a_real_change_wakes_the_streams_and_a_refused_one_does_not(self):
        with mock.patch.object(push.watcher, "poke") as poke:
            Service.push_now(self.db, self.admin, _push())
            self.assertEqual(poke.call_count, 1)

            Service.update_builtin(self.db, self.admin, "hydrate", BuiltinNotificationUpdate(enabled=False))
            Service.update_builtin(self.db, self.admin, "hydrate", BuiltinNotificationUpdate(enabled=False))  # no change
            self.assertEqual(poke.call_count, 2)

            from fastapi import HTTPException
            with self.assertRaises(HTTPException):
                Service.push_now(self.db, _user("employee"), _push())
            self.assertEqual(poke.call_count, 2)


# ── The watcher and the stream ───────────────────────────────────────────────


class FakeStore:
    """Stands in for the stored version; counts how often it is read."""

    def __init__(self, version=0):
        self.version = version
        self.reads = 0
        self.fail_next = 0

    def read(self):
        self.reads += 1
        if self.fail_next:
            self.fail_next -= 1
            raise RuntimeError("database unavailable")
        return self.version


class StreamTestCase(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        patches = [
            mock.patch.object(push, "HEARTBEAT_SECONDS", 0.05),
            mock.patch.object(push, "MAX_STREAM_SECONDS", 0.6),
            mock.patch.object(push, "WATCH_INTERVAL_SECONDS", 0.1),
            mock.patch.object(push, "_shutting_down", False),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.store = FakeStore(3)
        self.watcher = push.VersionWatcher(self.store.read)

    async def collect(self, since, limit=None, within=8.0):
        """Everything the stream sends until it ends. A stream that never ends fails
        here after `within` seconds instead of hanging the run."""
        chunks = []

        async def read():
            async for chunk in push.event_stream(since, source=self.watcher):
                chunks.append(chunk)
                if limit and len(chunks) >= limit:
                    break

        await asyncio.wait_for(read(), within)
        return chunks


class EventStreamTests(StreamTestCase):

    async def test_a_client_that_is_up_to_date_hears_only_pings_and_the_stream_ends_by_itself(self):
        chunks = await self.collect(since=3)

        self.assertEqual(chunks[0], push.SSE_OPEN)
        self.assertGreater(len(chunks), 3)
        self.assertTrue(all(chunk == push.SSE_PING for chunk in chunks[1:]))
        self.assertEqual(self.watcher.subscribers, 0)

    async def test_a_client_that_missed_a_change_is_told_at_once_and_the_stream_ends(self):
        chunks = await self.collect(since=1)

        self.assertEqual(chunks, [push.sse_event(3)])
        self.assertIn('data: {"version": 3}', chunks[0])
        self.assertTrue(chunks[0].startswith("event: schedule\n"))
        self.assertEqual(self.watcher.subscribers, 0)

    async def test_a_change_while_connected_is_one_event_and_then_the_stream_ends(self):
        async def change_soon():
            await asyncio.sleep(0.15)
            self.store.version = 4

        asyncio.ensure_future(change_soon())
        chunks = await self.collect(since=3)

        self.assertEqual(chunks[-1], push.sse_event(4))
        self.assertEqual(sum(1 for chunk in chunks if "event: schedule" in chunk), 1)

    async def test_a_poke_delivers_a_change_at_once_not_at_the_next_look(self):
        # Neither the heartbeat nor the watcher's own look could deliver this in
        # time: only a poke, waking the stream the instant the version moves.
        with mock.patch.object(push, "WATCH_INTERVAL_SECONDS", 30.0), mock.patch.object(push, "HEARTBEAT_SECONDS", 5.0),                 mock.patch.object(push, "MAX_STREAM_SECONDS", 10.0):
            started = asyncio.get_running_loop().time()

            async def change_and_poke():
                await asyncio.sleep(0.1)
                self.store.version = 9
                self.watcher.poke()

            asyncio.ensure_future(change_and_poke())
            chunks = await self.collect(since=3)

        self.assertEqual(chunks[-1], push.sse_event(9))
        self.assertLess(asyncio.get_running_loop().time() - started, 0.5)

    async def test_a_change_made_elsewhere_is_found_by_the_watchers_own_look(self):
        """Another worker process wrote it: nothing pokes this one."""
        async def change_quietly():
            await asyncio.sleep(0.1)
            self.store.version = 12

        asyncio.ensure_future(change_quietly())
        chunks = await self.collect(since=3)

        self.assertEqual(chunks[-1], push.sse_event(12))

    async def test_every_connected_client_is_told(self):
        async def one():
            return await self.collect(since=3)

        async def change_and_poke():
            await asyncio.sleep(0.15)
            self.store.version = 5
            self.watcher.poke()

        asyncio.ensure_future(change_and_poke())
        results = await asyncio.gather(*[one() for _ in range(10)])

        self.assertTrue(all(chunks[-1] == push.sse_event(5) for chunks in results))
        self.assertEqual(self.watcher.subscribers, 0)

    async def test_the_stream_ends_within_a_heartbeat_of_the_process_being_told_to_stop(self):
        with mock.patch.object(push, "MAX_STREAM_SECONDS", 5.0):           # it would otherwise run for five seconds
            async def stop_soon():
                await asyncio.sleep(0.15)
                push.begin_shutdown()

            asyncio.ensure_future(stop_soon())
            started = asyncio.get_running_loop().time()
            await self.collect(since=3)

        self.assertLess(asyncio.get_running_loop().time() - started, 1.0)

    async def test_a_stream_opened_while_shutting_down_ends_straight_away(self):
        push.begin_shutdown()
        chunks = await self.collect(since=3)
        self.assertEqual(chunks, [push.SSE_OPEN])


class WatcherCostTests(StreamTestCase):

    async def test_nobody_connected_means_no_reads_and_no_task(self):
        await asyncio.sleep(0.3)
        self.assertEqual(self.store.reads, 0)

    async def test_reads_do_not_grow_with_the_number_of_desktops(self):
        """One look per interval for the whole process, plus one per connection."""
        with mock.patch.object(push, "MAX_STREAM_SECONDS", 0.5):
            await asyncio.gather(*[self.collect(since=3) for _ in range(40)])

        # 40 connection reads, plus ~5 looks (0.5 s / 0.1 s) -- not 40 x 5.
        self.assertLess(self.store.reads, 40 + 12)

    async def test_the_watching_stops_when_the_last_stream_goes(self):
        await self.collect(since=3)
        await asyncio.sleep(0.35)
        reads = self.store.reads
        await asyncio.sleep(0.35)

        self.assertEqual(self.store.reads, reads)
        self.assertEqual(self.watcher.subscribers, 0)

    async def test_a_cancelled_stream_releases_its_place(self):
        started = asyncio.Event()

        async def consume():
            async for _ in push.event_stream(3, source=self.watcher):
                started.set()

        task = asyncio.ensure_future(consume())
        await asyncio.wait_for(started.wait(), 1.0)
        self.assertEqual(self.watcher.subscribers, 1)

        task.cancel()                                   # the desktop went away
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.watcher.subscribers, 0)

    async def test_an_unreadable_store_when_connecting_fails_the_stream_and_leaves_no_place_held(self):
        self.store.fail_next = 1
        with self.assertRaises(RuntimeError):
            await self.collect(since=3)
        self.assertEqual(self.watcher.subscribers, 0)

        chunks = await self.collect(since=3)            # and the next connection is fine
        self.assertEqual(chunks[0], push.SSE_OPEN)

    async def test_a_failed_look_does_not_end_the_watching(self):
        async def break_then_change():
            await asyncio.sleep(0.12)
            self.store.fail_next = 2                     # two looks fail ...
            await asyncio.sleep(0.35)
            self.store.version = 7                       # ... then the change is still found

        asyncio.ensure_future(break_then_change())
        with mock.patch.object(push, "MAX_STREAM_SECONDS", 1.5):
            chunks = await self.collect(since=3)

        self.assertEqual(chunks[-1], push.sse_event(7))

    async def test_a_new_event_loop_starts_clean(self):
        """The watcher belongs to one loop; a test client (or a restarted loop)
        must not inherit another's events."""
        await self.collect(since=3)

        def in_another_loop():
            async def go():
                return [chunk async for chunk in push.event_stream(1, source=self.watcher)]
            return asyncio.run(go())

        chunks = await asyncio.get_running_loop().run_in_executor(None, in_another_loop)
        self.assertEqual(chunks, [push.sse_event(3)])


class ShutdownHookTests(unittest.TestCase):

    def setUp(self):
        self.saved = {name: signal.getsignal(getattr(signal, name)) for name in ("SIGINT", "SIGTERM") if hasattr(signal, name)}
        push._hook_installed = False
        push._shutting_down = False
        self.addCleanup(self._restore)

    def _restore(self):
        for name, handler in self.saved.items():
            signal.signal(getattr(signal, name), handler)
        push._hook_installed = False
        push._shutting_down = False

    def test_the_original_handler_still_runs_and_the_streams_are_told_first(self):
        calls = []
        number = signal.SIGTERM if hasattr(signal, "SIGTERM") else signal.SIGINT
        signal.signal(number, lambda signum, frame: calls.append(("original", push.is_shutting_down())))

        push.install_shutdown_hook()
        signal.getsignal(number)(number, None)

        self.assertEqual(calls, [("original", True)])        # the flag was already set when the server's own handler ran

    def test_installing_twice_wraps_once(self):
        calls = []
        number = signal.SIGTERM if hasattr(signal, "SIGTERM") else signal.SIGINT
        signal.signal(number, lambda signum, frame: calls.append(1))

        push.install_shutdown_hook()
        push.install_shutdown_hook()
        signal.getsignal(number)(number, None)

        self.assertEqual(calls, [1])

    def test_from_a_thread_that_is_not_the_main_one_it_does_nothing_and_does_not_raise(self):
        import threading
        errors = []

        def run():
            try:
                push.install_shutdown_hook()
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        thread = threading.Thread(target=run)
        thread.start()
        thread.join()
        self.assertEqual(errors, [])


# ── The routes ───────────────────────────────────────────────────────────────


class RouteTests(unittest.TestCase):

    def setUp(self):
        self.db = _sqlite_session()
        self.user = _user("administrator", user_id=5, username="grace")
        app.dependency_overrides[get_db] = lambda: self.db
        app.dependency_overrides[get_current_user] = lambda: self.user
        self.client = TestClient(app)
        for patcher in (
            mock.patch.object(push, "HEARTBEAT_SECONDS", 0.05),
            mock.patch.object(push, "MAX_STREAM_SECONDS", 0.4),
            mock.patch.object(push, "WATCH_INTERVAL_SECONDS", 0.1),
            mock.patch.object(push, "_shutting_down", False),
            mock.patch.object(push.watcher, "_read_version", lambda: Service.current_version(self.db)),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self):
        app.dependency_overrides.clear()

    def _stream(self, since):
        with self.client.stream("GET", f"/desktop-notifications/stream?since={since}") as response:
            return response.status_code, dict(response.headers), "".join(response.iter_text())

    def test_the_push_route_reaches_the_schedule_under_both_prefixes(self):
        for prefix in ("", "/api/v1"):
            with self.subTest(prefix=prefix):
                sent = self.client.post(f"{prefix}/desktop-notifications/push", json={"title": f"Hello{prefix}", "message": "Please save your work."})
                self.assertEqual(sent.status_code, 200, sent.text)
                polled = self.client.get(f"{prefix}/desktop-notifications/schedule").json()
                self.assertIn(f"Hello{prefix}", [p["title"] for p in polled["pushes"]])

    def test_an_employee_may_not_push_but_may_open_the_stream(self):
        self.user = _user("employee", user_id=9, username="eve")
        self.assertEqual(self.client.post("/desktop-notifications/push", json={"title": "Hi", "message": "There"}).status_code, 403)
        self.assertEqual(Service.get_schedule(self.db)["version"], 0)
        self.assertEqual(self._stream(0)[0], 200)

    def test_bad_input_is_a_422_and_writes_nothing(self):
        for name, body in {
            "empty": {},
            "blank title": {"title": "  ", "message": "m"},
            "punctuation-only title": {"title": "!!!", "message": "m"},
            "markup in the message": {"title": "t", "message": "<script>alert(1)</script>"},
            "title too long": {"title": "x" * 81, "message": "m"},
            "message too long": {"title": "t", "message": "x" * 301},
            "blank message": {"title": "t", "message": " "},
            "null title": {"title": None, "message": "m"},
        }.items():
            with self.subTest(body=name):
                self.assertEqual(self.client.post("/desktop-notifications/push", json=body).status_code, 422)
        self.assertEqual(Service.get_schedule(self.db)["version"], 0)

    def test_the_stream_needs_a_signed_in_client(self):
        app.dependency_overrides.pop(get_current_user)
        self.assertEqual(self.client.get("/desktop-notifications/stream").status_code, 401)

    def test_a_negative_or_non_numeric_since_is_a_422(self):
        self.assertEqual(self.client.get("/desktop-notifications/stream?since=-1").status_code, 422)
        self.assertEqual(self.client.get("/desktop-notifications/stream?since=abc").status_code, 422)

    def test_the_stream_is_an_event_stream_that_no_proxy_or_compressor_may_hold_back(self):
        status, headers, body = self._stream(0)

        self.assertEqual(status, 200)
        self.assertTrue(headers["content-type"].startswith("text/event-stream"))
        self.assertIn("no-cache", headers["cache-control"])
        self.assertIn("no-transform", headers["cache-control"])
        self.assertEqual(headers["x-accel-buffering"], "no")
        self.assertNotIn("content-encoding", headers)
        self.assertTrue(body.startswith(": connected"))
        self.assertIn(": ping", body)

    def test_a_client_that_missed_a_change_gets_the_event_at_once(self):
        Service.push_now(self.db, _user("administrator"), _push())
        Service.push_now(self.db, _user("administrator"), _push())

        status, _, body = self._stream(0)

        self.assertEqual(status, 200)
        self.assertEqual(body, push.sse_event(2))

    def test_a_push_while_a_stream_is_open_reaches_it(self):
        import threading
        results = {}

        def listen():
            results["stream"] = self._stream(0)

        thread = threading.Thread(target=listen)
        thread.start()
        asyncio_sleep = __import__("time").sleep
        asyncio_sleep(0.15)
        self.client.post("/desktop-notifications/push", json={"title": "Live", "message": "A message sent while you were connected."})
        thread.join(timeout=5)

        self.assertIn('event: schedule', results["stream"][2])
        self.assertIn('"version": 1', results["stream"][2])

    def test_the_route_holds_no_database_connection_while_it_waits(self):
        """The dependency's session is closed before the first byte (scope="function"),
        which is what tests/test_db_lifecycle.py enforces for every site."""
        import inspect
        from app.api import desktop_notifications as module
        source = inspect.getsource(module)
        self.assertNotIn("Depends(get_db)", source.replace('Depends(get_db, scope="function")', ""))


if __name__ == "__main__":
    unittest.main()

"""Monitra -> WFPM: a timer started here starts the timer there.

What these tests defend, and the production symptom each maps to:

* **Starting a timer never depends on WFPM.** Nothing about WFPM -- unset
  configuration, an unlinked task, a failing query, WFPM being down -- may
  turn a started timer into an error, or make the start wait.
* **One timer, one event.** A replayed start, a second sweep and a retry all
  collapse onto one `wfpm_timer_events` row, and every attempt carries the
  same `event_id` so WFPM can de-duplicate a request it already handled.
* **The right task.** The event names the task's `wfpm_task_id`; a task WFPM
  does not know starts no WFPM timer at all.
* **Failures are retried, refusals are not.** A timeout or a 5xx is retried
  with backoff; a 4xx that a retry cannot change is parked where it can be
  seen rather than re-sent forever.

The queue runs against real SQLite rows; WFPM itself is an httpx mock
transport, so what is asserted about the request is the request that would
actually leave this process.
"""
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.security import get_current_user
from app.main import app
from app.models.project import Project
from app.models.task import Task
from app.models.time_entry import TimeEntry
from app.models.user import User
from app.WFPM import client as wfpm_client
from app.WFPM import timer_sync
from app.WFPM.client import WfpmDeliveryError
from app.WFPM.models import (
    EVENT_TIMER_START, STATUS_FAILED, STATUS_PENDING, STATUS_REJECTED, STATUS_SENT, WfpmTimerEvent,
)
from app.WFPM.repository import WfpmLinkRepository, WfpmTimerEventRepository
from app.WFPM.timer_sync import WfpmTimerSync
from tests.test_project_hours_summary import ORG, _sqlite_schema

UTC = timezone.utc
URL = "https://wfpm.example.test/api/monitra/timer/start"
USER, PROJECT, LINKED_TASK, UNLINKED_TASK = 54, 7, 70, 71
STARTED = datetime(2026, 9, 30, 10, 0, 0, tzinfo=UTC)


def wfpm_settings(**overrides):
    defaults = {
        "WFPM_TIMER_START_URL": URL, "WFPM_API_TOKEN": "wfpm-test-token",
        "WFPM_REQUEST_TIMEOUT_SECONDS": 5.0, "WFPM_TIMER_MAX_ATTEMPTS": 3,
        "WFPM_TIMER_RETRY_BASE_DELAY_SECONDS": 30, "WFPM_TIMER_RETRY_MAX_DELAY_SECONDS": 900,
        "WFPM_TIMER_DISPATCH_BATCH_SIZE": 50,
    }
    defaults.update(overrides)
    return patch.multiple(settings, **defaults)


class _Db(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        _sqlite_schema(self.engine, User, Project, Task, TimeEntry, WfpmTimerEvent)
        self.db = Session(self.engine)
        self.db.add(User(
            id=USER, organization_id=ORG, username="asha", email="asha@example.com", name="Asha",
            role_name="employee", permissions={}, status="active", is_active=True,
            idle_enabled=True, idle_minutes=5, capture_frequency=10,
        ))
        self.db.add(Project(
            id=PROJECT, organization_id=ORG, project_name="Website rebuild", status="active",
            status_id=5, created_by=USER, wfpm_project_id="55",
        ))
        self.db.add(Task(
            id=LINKED_TASK, organization_id=ORG, project_id=PROJECT, task_name="Design",
            status="todo", status_id=1, created_by=USER, wfpm_task_id="900",
        ))
        self.db.add(Task(
            id=UNLINKED_TASK, organization_id=ORG, project_id=PROJECT, task_name="Internal Discussion",
            status="todo", status_id=1, created_by=USER,
        ))
        self.db.commit()
        configured = wfpm_settings()
        configured.start()
        self.addCleanup(configured.stop)

    def tearDown(self):
        self.db.close()

    def entry(self, entry_id=1, task_id=LINKED_TASK, user_id=USER, **fields) -> TimeEntry:
        row = TimeEntry(
            id=entry_id, organization_id=ORG, user_id=user_id, project_id=PROJECT, task_id=task_id,
            start_time=STARTED, end_time=None, total_seconds=0, status="running",
            is_manual=False, is_billable=False, **fields,
        )
        self.db.add(row)
        self.db.commit()
        return row

    def events(self):
        self.db.expire_all()
        return list(self.db.scalars(select(WfpmTimerEvent).order_by(WfpmTimerEvent.id)).all())

    def make_due(self, event_id):
        """Move a parked row's next attempt into the past, as time passing would."""
        row = self.db.get(WfpmTimerEvent, event_id)
        row.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
        self.db.commit()


# ── queueing ─────────────────────────────────────────────────────────────────

class QueueTests(_Db):
    def test_a_timer_on_a_linked_task_queues_one_event_with_the_wfpm_ids(self):
        event_id = WfpmTimerSync.queue_timer_start(self.db, self.entry())

        [row] = self.events()
        self.assertEqual(event_id, row.id)
        self.assertEqual((row.event_type, row.status, row.attempt_count), (EVENT_TIMER_START, STATUS_PENDING, 0))
        self.assertEqual((row.wfpm_task_id, row.wfpm_project_id), ("900", "55"))
        self.assertEqual((row.time_entry_id, row.task_id, row.project_id, row.user_id), (1, LINKED_TASK, PROJECT, USER))
        self.assertEqual(row.max_attempts, 3)

    def test_a_timer_on_a_task_wfpm_does_not_know_queues_nothing(self):
        self.assertIsNone(WfpmTimerSync.queue_timer_start(self.db, self.entry(task_id=UNLINKED_TASK)))
        self.assertEqual(self.events(), [])

    def test_nothing_is_queued_while_the_integration_is_unconfigured(self):
        """An event queued against a URL that does not exist yet would be
        delivered hours late the moment it did."""
        entry = self.entry()
        with wfpm_settings(WFPM_TIMER_START_URL=""):
            self.assertIsNone(WfpmTimerSync.queue_timer_start(self.db, entry))
        with wfpm_settings(WFPM_TIMER_START_URL="   "):
            self.assertIsNone(WfpmTimerSync.queue_timer_start(self.db, entry))
        self.assertEqual(self.events(), [])

    def test_the_same_entry_is_never_queued_twice(self):
        entry = self.entry()
        first = WfpmTimerSync.queue_timer_start(self.db, entry)
        second = WfpmTimerSync.queue_timer_start(self.db, entry)
        self.assertIsNotNone(first)
        # Nothing new to deliver: the first call's event already exists.
        self.assertIsNone(second)
        self.assertEqual(len(self.events()), 1)

    def test_a_task_linked_under_an_unlinked_project_still_starts_its_timer(self):
        self.db.get(Project, PROJECT).wfpm_project_id = None
        self.db.commit()
        WfpmTimerSync.queue_timer_start(self.db, self.entry())
        [row] = self.events()
        self.assertEqual((row.wfpm_task_id, row.wfpm_project_id), ("900", None))

    def test_a_failure_while_queueing_never_reaches_the_timer_start(self):
        entry = self.entry()
        with patch.object(WfpmLinkRepository, "timer_link", side_effect=RuntimeError("relation does not exist")):
            self.assertIsNone(WfpmTimerSync.queue_timer_start(self.db, entry))
        with patch.object(WfpmTimerEventRepository, "enqueue", side_effect=RuntimeError("database is down")):
            self.assertIsNone(WfpmTimerSync.queue_timer_start(self.db, entry))
        # And with no session at all, which is what a route test hands it.
        self.assertIsNone(WfpmTimerSync.queue_timer_start(None, entry))
        self.assertEqual(self.events(), [])
        # The session is still usable for the response the route builds next.
        self.assertEqual(self.db.get(TimeEntry, 1).task_id, LINKED_TASK)


# ── delivery ─────────────────────────────────────────────────────────────────

class DeliveryTests(_Db):
    def setUp(self):
        super().setUp()
        self.sent = []

    def wfpm(self, outcome=200):
        """Stand in for WFPM: record what it was sent, answer with `outcome`
        (a status code, or an exception to raise)."""
        def post_event(url, *, token, payload, idempotency_key, timeout_seconds):
            self.sent.append({"url": url, "token": token, "payload": payload,
                              "idempotency_key": idempotency_key, "timeout": timeout_seconds})
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        return patch.object(wfpm_client, "post_event", post_event)

    def queued(self, **entry_fields):
        return WfpmTimerSync.queue_timer_start(self.db, self.entry(**entry_fields))

    def test_a_delivered_event_names_the_wfpm_task_and_is_marked_sent(self):
        event_id = self.queued()
        with self.wfpm(201):
            self.assertEqual(WfpmTimerSync.deliver_one(self.db, event_id), "sent")

        [call] = self.sent
        self.assertEqual(call["url"], URL)
        self.assertEqual(call["token"], "wfpm-test-token")
        self.assertEqual(call["timeout"], 5.0)
        self.assertEqual(call["payload"], {
            "event": "timer_start",
            "event_id": "monitra:timer_start:1",
            "wfpm_task_id": "900",
            "wfpm_project_id": "55",
            "started_at": "2026-09-30T10:00:00+00:00",
            "stopped_at": None,
            "user_email": "asha@example.com",
            "user_name": "Asha",
            "monitra_user_id": USER,
            "monitra_time_entry_id": 1,
            "monitra_task_id": LINKED_TASK,
            "monitra_project_id": PROJECT,
        })
        self.assertEqual(call["idempotency_key"], "monitra:timer_start:1")

        [row] = self.events()
        self.assertEqual((row.status, row.attempt_count, row.response_status), (STATUS_SENT, 1, 201))
        self.assertIsNotNone(row.sent_at)
        self.assertIsNone(row.last_error)

    def test_a_sent_event_is_never_sent_again(self):
        event_id = self.queued()
        with self.wfpm():
            WfpmTimerSync.deliver_one(self.db, event_id)
            self.assertEqual(WfpmTimerSync.deliver_one(self.db, event_id), "skipped")
            self.assertEqual(WfpmTimerSync.dispatch_pending(self.db), {"attempted": 0})
        self.assertEqual(len(self.sent), 1)

    def test_a_transient_failure_is_parked_for_a_retry_not_resent_at_once(self):
        event_id = self.queued()
        before = datetime.now(UTC).replace(tzinfo=None)
        with self.wfpm(WfpmDeliveryError("WFPM answered HTTP 503", retryable=True, status_code=503)):
            self.assertEqual(WfpmTimerSync.deliver_one(self.db, event_id), "retrying")
            # Not due yet: an immediate second attempt, or a sweep, leaves it.
            self.assertEqual(WfpmTimerSync.deliver_one(self.db, event_id), "skipped")
            self.assertEqual(WfpmTimerSync.dispatch_pending(self.db), {"attempted": 0})

        [row] = self.events()
        self.assertEqual((row.status, row.attempt_count, row.response_status), (STATUS_PENDING, 1, 503))
        self.assertIn("503", row.last_error)
        self.assertGreater(row.next_attempt_at, before + timedelta(seconds=14))   # base/2 at least
        self.assertEqual(len(self.sent), 1)

    def test_a_retry_carries_the_same_event_id_and_succeeds_when_wfpm_recovers(self):
        event_id = self.queued()
        with self.wfpm(WfpmDeliveryError("WFPM did not answer within 5s", retryable=True)):
            WfpmTimerSync.deliver_one(self.db, event_id)
        self.make_due(event_id)
        with self.wfpm(200):
            self.assertEqual(WfpmTimerSync.dispatch_pending(self.db), {"attempted": 1, "sent": 1})

        self.assertEqual([call["idempotency_key"] for call in self.sent], ["monitra:timer_start:1"] * 2)
        [row] = self.events()
        self.assertEqual((row.status, row.attempt_count), (STATUS_SENT, 2))
        self.assertIsNone(row.last_error)

    def test_a_refusal_is_parked_as_rejected_and_never_retried(self):
        event_id = self.queued()
        with self.wfpm(WfpmDeliveryError("WFPM answered HTTP 404: unknown task", retryable=False, status_code=404)):
            self.assertEqual(WfpmTimerSync.deliver_one(self.db, event_id), "rejected")
            self.make_due(event_id)
            self.assertEqual(WfpmTimerSync.dispatch_pending(self.db), {"attempted": 0})

        [row] = self.events()
        self.assertEqual((row.status, row.attempt_count, row.response_status), (STATUS_REJECTED, 1, 404))
        self.assertIn("unknown task", row.last_error)
        self.assertEqual(len(self.sent), 1)

    def test_an_event_that_runs_out_of_attempts_is_parked_as_failed(self):
        event_id = self.queued()
        outcomes = []
        with self.wfpm(WfpmDeliveryError("WFPM could not be reached (ConnectError)", retryable=True)):
            for _ in range(3):                      # WFPM_TIMER_MAX_ATTEMPTS
                outcomes.append(WfpmTimerSync.deliver_one(self.db, event_id))
                self.make_due(event_id)
            self.assertEqual(WfpmTimerSync.dispatch_pending(self.db), {"attempted": 0})

        self.assertEqual(outcomes, ["retrying", "retrying", "failed"])
        [row] = self.events()
        self.assertEqual((row.status, row.attempt_count), (STATUS_FAILED, 3))
        self.assertEqual(len(self.sent), 3)

    def test_an_unexpected_error_costs_an_attempt_but_does_not_escape(self):
        event_id = self.queued()
        with self.wfpm(RuntimeError("something nobody planned for")):
            self.assertEqual(WfpmTimerSync.deliver_one(self.db, event_id), "retrying")
        [row] = self.events()
        self.assertEqual((row.status, row.attempt_count), (STATUS_PENDING, 1))
        self.assertIn("RuntimeError", row.last_error)

    def test_a_late_retry_reports_that_the_session_has_already_ended(self):
        event_id = self.queued()
        entry = self.db.get(TimeEntry, 1)
        entry.end_time, entry.total_seconds, entry.status = STARTED + timedelta(minutes=25), 1500, "stopped"
        self.db.commit()
        with self.wfpm():
            WfpmTimerSync.deliver_one(self.db, event_id)
        payload = self.sent[0]["payload"]
        self.assertEqual(payload["started_at"], "2026-09-30T10:00:00+00:00")
        self.assertEqual(payload["stopped_at"], "2026-09-30T10:25:00+00:00")

    def test_the_wfpm_ids_are_the_ones_the_timer_started_under(self):
        """Relinking a task afterwards must not redirect an event in flight."""
        event_id = self.queued()
        self.db.get(Task, LINKED_TASK).wfpm_task_id = "901"
        self.db.commit()
        with self.wfpm():
            WfpmTimerSync.deliver_one(self.db, event_id)
        self.assertEqual(self.sent[0]["payload"]["wfpm_task_id"], "900")

    def test_nothing_is_attempted_once_the_url_is_removed(self):
        event_id = self.queued()
        with self.wfpm(), wfpm_settings(WFPM_TIMER_START_URL=""):
            self.assertEqual(WfpmTimerSync.deliver_one(self.db, event_id), "unconfigured")
            self.assertEqual(WfpmTimerSync.dispatch_pending(self.db), {"attempted": 0, "unconfigured": True})
        self.assertEqual(self.sent, [])
        [row] = self.events()
        self.assertEqual((row.status, row.attempt_count), (STATUS_PENDING, 0))   # no attempt spent

    def test_a_sweep_delivers_every_due_event_and_survives_a_bad_one(self):
        users = (USER, 55, 56)
        for user_id in users[1:]:
            self.db.add(User(
                id=user_id, organization_id=ORG, username=f"u{user_id}", email=f"u{user_id}@example.com",
                name=f"User {user_id}", role_name="employee", permissions={}, status="active",
                is_active=True, idle_enabled=True, idle_minutes=5, capture_frequency=10,
            ))
        self.db.commit()
        for entry_id, user_id in enumerate(users, start=1):
            self.queued(entry_id=entry_id, user_id=user_id)

        def post_event(url, *, token, payload, idempotency_key, timeout_seconds):
            if payload["monitra_time_entry_id"] == 2:
                raise WfpmDeliveryError("WFPM answered HTTP 422", retryable=False, status_code=422)
            return 200

        with patch.object(wfpm_client, "post_event", post_event):
            tally = WfpmTimerSync.dispatch_pending(self.db)
        self.assertEqual(tally, {"attempted": 3, "sent": 2, "rejected": 1})
        self.assertEqual([row.status for row in self.events()], [STATUS_SENT, STATUS_REJECTED, STATUS_SENT])


class BackoffTests(unittest.TestCase):
    def test_the_delay_grows_is_capped_and_is_jittered(self):
        with wfpm_settings(WFPM_TIMER_RETRY_BASE_DELAY_SECONDS=30, WFPM_TIMER_RETRY_MAX_DELAY_SECONDS=900):
            for attempt, ceiling in ((1, 30), (2, 60), (3, 120), (6, 900), (40, 900)):
                draws = [timer_sync.retry_delay_seconds(attempt) for _ in range(200)]
                with self.subTest(attempt=attempt):
                    self.assertGreaterEqual(min(draws), 15)
                    self.assertLessEqual(max(draws), ceiling)
            # Jitter: synchronised retries are a self-inflicted load test.
            self.assertGreater(len({round(timer_sync.retry_delay_seconds(4), 3) for _ in range(50)}), 1)


# ── the HTTP request itself ──────────────────────────────────────────────────

class ClientTests(unittest.TestCase):
    """`post_event` against a mock transport: the real request, no network."""

    def post(self, handler, *, token="wfpm-test-token"):
        self.requests = []

        def recording(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return handler(request)

        real_client = httpx.Client
        with patch.object(wfpm_client.httpx, "Client",
                          lambda **kwargs: real_client(transport=httpx.MockTransport(recording), **kwargs)):
            return wfpm_client.post_event(
                URL, token=token, payload={"event": "timer_start", "wfpm_task_id": "900"},
                idempotency_key="monitra:timer_start:1", timeout_seconds=5.0,
            )

    def test_the_request_carries_the_token_the_key_and_the_json_body(self):
        self.assertEqual(self.post(lambda request: httpx.Response(200, json={"ok": True})), 200)
        [request] = self.requests
        self.assertEqual((request.method, str(request.url)), ("POST", URL))
        self.assertEqual(request.headers["Authorization"], "Bearer wfpm-test-token")
        self.assertEqual(request.headers["Idempotency-Key"], "monitra:timer_start:1")
        self.assertEqual(request.headers["Content-Type"], "application/json")
        self.assertNotIn("br", request.headers["Accept-Encoding"])
        self.assertEqual(json.loads(request.content), {"event": "timer_start", "wfpm_task_id": "900"})

    def test_no_authorization_header_is_sent_without_a_token(self):
        self.post(lambda request: httpx.Response(204), token="")
        self.assertNotIn("Authorization", self.requests[0].headers)

    def test_every_2xx_is_accepted(self):
        for status_code in (200, 201, 202, 204):
            with self.subTest(status=status_code):
                self.assertEqual(self.post(lambda request: httpx.Response(status_code)), status_code)

    def test_answers_are_classified_as_retryable_or_not(self):
        cases = {
            500: True, 502: True, 503: True, 504: True,     # WFPM is unwell
            408: True, 429: True,                            # not now
            401: True, 403: True,                            # the token: configuration, fixable
            301: True, 302: True,                            # the URL: configuration, fixable
            400: False, 404: False, 409: False, 422: False,  # this event is refused
        }
        for status_code, retryable in cases.items():
            with self.subTest(status=status_code), self.assertRaises(WfpmDeliveryError) as ctx:
                self.post(lambda request: httpx.Response(status_code, text="nope"))
            self.assertEqual((ctx.exception.retryable, ctx.exception.status_code), (retryable, status_code))

    def test_a_redirect_is_not_followed(self):
        with self.assertRaises(WfpmDeliveryError):
            self.post(lambda request: httpx.Response(302, headers={"Location": "https://elsewhere.test/"}))
        self.assertEqual(len(self.requests), 1)

    def test_a_timeout_and_a_refused_connection_are_retryable_and_carry_no_status(self):
        def timeout(request):
            raise httpx.ReadTimeout("timed out", request=request)

        def refused(request):
            raise httpx.ConnectError("connection refused", request=request)

        for handler in (timeout, refused):
            with self.subTest(handler=handler.__name__), self.assertRaises(WfpmDeliveryError) as ctx:
                self.post(handler)
            self.assertTrue(ctx.exception.retryable)
            self.assertIsNone(ctx.exception.status_code)

    def test_the_token_never_appears_in_an_error(self):
        with self.assertRaises(WfpmDeliveryError) as ctx:
            self.post(lambda request: httpx.Response(500, text="boom"))
        self.assertNotIn("wfpm-test-token", wfpm_client.redact_error(ctx.exception))


# ── the timer-start route ────────────────────────────────────────────────────

class StartRouteTests(unittest.TestCase):
    def setUp(self):
        user = User()
        user.id, user.organization_id, user.role_name = USER, ORG, "employee"
        user.permissions, user.is_active, user.name = {}, True, "Asha"
        app.dependency_overrides[get_current_user] = lambda: user
        app.dependency_overrides[get_db] = lambda: None
        self.client = TestClient(app)
        self.addCleanup(app.dependency_overrides.clear)
        net = patch("app.repositories.time_entry_adjustment.TimeEntryAdjustmentRepository.net_for_entries",
                    return_value={})
        net.start()
        self.addCleanup(net.stop)
        now = datetime.now(UTC)
        self.entry = TimeEntry(
            id=61, organization_id=ORG, user_id=USER, project_id=PROJECT, task_id=LINKED_TASK,
            start_time=now, end_time=None, total_seconds=0, status="running", is_manual=False,
            is_billable=False, description=None, client_op=None, created_at=now, updated_at=now,
        )

    def start(self, *, created, queued):
        with patch("app.api.time_entry.TimeEntryService.start_timer", return_value=(self.entry, created)), \
                patch.object(WfpmTimerSync, "queue_timer_start", return_value=queued) as queue, \
                patch.object(timer_sync, "deliver_in_background") as deliver:
            response = self.client.post("/time-entries/start", json={"project_id": PROJECT, "task_id": LINKED_TASK})
        return response, queue, deliver

    def test_a_new_timer_is_announced_after_the_response(self):
        response, queue, deliver = self.start(created=True, queued=812)
        self.assertEqual(response.status_code, 201, response.text)
        queue.assert_called_once()
        self.assertIs(queue.call_args.args[1], self.entry)
        deliver.assert_called_once_with(812)

    def test_a_timer_wfpm_does_not_know_schedules_no_delivery(self):
        response, queue, deliver = self.start(created=True, queued=None)
        self.assertEqual(response.status_code, 201, response.text)
        queue.assert_called_once()
        deliver.assert_not_called()

    def test_a_replayed_start_announces_nothing(self):
        """The entry already exists, and so does its event: the first start
        queued it."""
        response, queue, deliver = self.start(created=False, queued=812)
        self.assertEqual(response.status_code, 200, response.text)
        queue.assert_not_called()
        deliver.assert_not_called()

    def test_the_start_response_is_unchanged_by_the_integration(self):
        """The desktop reads this body; WFPM must add nothing to it."""
        response, _, _ = self.start(created=True, queued=812)
        self.assertFalse([key for key in response.json() if "wfpm" in key.lower()])


# ── the sweeper endpoint ─────────────────────────────────────────────────────

class DispatchRouteTests(unittest.TestCase):
    PATH = "/internal/wfpm/timer-events/dispatch"

    def setUp(self):
        app.dependency_overrides[get_db] = lambda: None
        self.addCleanup(app.dependency_overrides.clear)
        self.client = TestClient(app)

    def test_it_is_closed_without_the_dispatch_token(self):
        with patch.object(settings, "EMAIL_DISPATCH_TOKEN", "sweeper-test-token"), \
                patch.object(WfpmTimerSync, "dispatch_pending") as dispatch:
            self.assertEqual(self.client.post(self.PATH).status_code, 401)
            self.assertEqual(self.client.post(self.PATH, headers={"X-Email-Dispatch-Token": "wrong"}).status_code, 401)
            self.assertEqual(self.client.get(self.PATH).status_code, 401)
        dispatch.assert_not_called()
        with patch.object(settings, "EMAIL_DISPATCH_TOKEN", ""):
            self.assertEqual(self.client.post(self.PATH).status_code, 503)

    def test_it_reports_the_tally(self):
        tally = {"attempted": 3, "sent": 2, "retrying": 1}
        with patch.object(settings, "EMAIL_DISPATCH_TOKEN", "sweeper-test-token"), \
                patch.object(WfpmTimerSync, "dispatch_pending", return_value=tally) as dispatch:
            response = self.client.post(f"{self.PATH}?limit=10", headers={"X-Email-Dispatch-Token": "sweeper-test-token"})
            by_get = self.client.get(self.PATH, headers={"Authorization": "Bearer sweeper-test-token"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {
            "attempted": 3, "sent": 2, "retrying": 1, "failed": 0, "rejected": 0,
            "skipped": 0, "error": 0, "unconfigured": False,
        })
        self.assertEqual(dispatch.call_args_list[0].kwargs["limit"], 10)
        self.assertEqual(by_get.status_code, 200)

    def test_an_unconfigured_deployment_answers_honestly_and_does_nothing(self):
        with patch.object(settings, "EMAIL_DISPATCH_TOKEN", "sweeper-test-token"), \
                wfpm_settings(WFPM_TIMER_START_URL=""):
            response = self.client.post(self.PATH, headers={"X-Email-Dispatch-Token": "sweeper-test-token"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual((response.json()["unconfigured"], response.json()["attempted"]), (True, 0))


class ConfigurationTests(unittest.TestCase):
    def test_the_description_never_carries_the_url_or_the_token(self):
        with wfpm_settings():
            description = timer_sync.describe_configuration()
        self.assertEqual(description, {"configured": True, "token_present": True})
        with wfpm_settings(WFPM_TIMER_START_URL="", WFPM_API_TOKEN=""):
            description = timer_sync.describe_configuration()
        self.assertEqual(description["configured"], False)
        self.assertIn("WFPM_TIMER_START_URL", description["reason"])

    def test_health_reports_whether_the_integration_is_on(self):
        with wfpm_settings():
            body = TestClient(app).get("/health").json()
        self.assertEqual(body["wfpm_timer_sync"], {"configured": True, "token_present": True})
        self.assertNotIn("wfpm.example.test", json.dumps(body))
        self.assertNotIn("wfpm-test-token", json.dumps(body))


if __name__ == "__main__":
    unittest.main()

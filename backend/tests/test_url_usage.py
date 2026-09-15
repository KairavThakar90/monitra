import unittest
from unittest.mock import MagicMock, patch
from fastapi import HTTPException
from datetime import datetime, timezone, timedelta
from pydantic import ValidationError

from app.models.user import User
from app.models.time_entry import TimeEntry
from app.models.time_entry_url_usage import TimeEntryUrlUsage
from app.schemas.url_usage import URLUsageCreate, URLUsageBatchCreate
from app.services.url_usage_service import URLUsageService, normalize_url

class TestURLUsageService(unittest.TestCase):
    def setUp(self):
        self.db = MagicMock()
        self.current_user = User(id=1, organization_id=10, permissions={"time_entries:view_all": False})
        self.admin_user = User(id=2, organization_id=10, permissions={"time_entries:view_all": True})

        self.active_time_entry = TimeEntry(
            id=100,
            organization_id=10,
            user_id=1,
            status="running",
            end_time=None
        )

    # 1. Create URL usage successfully
    @patch("app.repositories.time_entry.TimeEntryRepository.get_by_id")
    @patch("app.repositories.url_usage_repository.URLUsageRepository.get_by_client_event_id")
    @patch("app.repositories.url_usage_repository.URLUsageRepository.get_latest_record")
    @patch("app.repositories.url_usage_repository.URLUsageRepository.create")
    def test_1_create_url_usage_success(self, mock_create, mock_latest, mock_get_client_id, mock_get_by_id):
        mock_get_by_id.return_value = self.active_time_entry
        mock_get_client_id.return_value = None
        mock_latest.return_value = None
        mock_create.return_value = TimeEntryUrlUsage(
            id=1, organization_id=10, time_entry_id=100,
            browser_name="Google Chrome", domain="github.com",
            url="https://github.com/org/repo", page_title="GitHub",
            duration_seconds=15
        )

        payload = URLUsageCreate(
            time_entry_id=100,
            browser_name="Google Chrome",
            domain="github.com",
            url="https://github.com/org/repo",
            page_title="GitHub",
            duration_seconds=15
        )

        record = URLUsageService.record_usage(self.db, payload, self.current_user)
        self.assertIsNotNone(record)
        mock_create.assert_called_once()

    # 2. Reject invalid duration
    def test_2_reject_invalid_duration(self):
        with self.assertRaises(ValidationError):
            URLUsageCreate(
                time_entry_id=100,
                browser_name="Google Chrome",
                domain="github.com",
                duration_seconds=-5
            )

    # 3. Reject invalid/missing browser name
    def test_3_reject_missing_browser_name(self):
        with self.assertRaises(ValidationError):
            URLUsageCreate(
                time_entry_id=100,
                browser_name="",
                domain="github.com",
                duration_seconds=10
            )

    # 4. Reject non-existent time entry
    @patch("app.repositories.time_entry.TimeEntryRepository.get_by_id")
    def test_4_reject_non_existent_time_entry(self, mock_get_by_id):
        mock_get_by_id.return_value = None
        payload = URLUsageCreate(time_entry_id=999, browser_name="Google Chrome", domain="github.com", duration_seconds=10)

        with self.assertRaises(HTTPException) as ctx:
            URLUsageService.record_usage(self.db, payload, self.current_user)
        self.assertEqual(ctx.exception.status_code, 404)

    # 5. Reject cross-organization time entry
    @patch("app.repositories.time_entry.TimeEntryRepository.get_by_id")
    def test_5_reject_cross_org_time_entry(self, mock_get_by_id):
        other_org_entry = TimeEntry(id=100, organization_id=99, user_id=1, status="running")
        mock_get_by_id.return_value = other_org_entry
        payload = URLUsageCreate(time_entry_id=100, browser_name="Google Chrome", domain="github.com", duration_seconds=10)

        with self.assertRaises(HTTPException) as ctx:
            URLUsageService.record_usage(self.db, payload, self.current_user)
        self.assertEqual(ctx.exception.status_code, 404)

    # 6. Reject unauthorized user submitting for another user's entry
    @patch("app.repositories.time_entry.TimeEntryRepository.get_by_id")
    def test_6_reject_unauthorized_user_submit(self, mock_get_by_id):
        other_user_entry = TimeEntry(id=100, organization_id=10, user_id=99, status="running")
        mock_get_by_id.return_value = other_user_entry
        payload = URLUsageCreate(time_entry_id=100, browser_name="Google Chrome", domain="github.com", duration_seconds=10)

        with self.assertRaises(HTTPException) as ctx:
            URLUsageService.record_usage(self.db, payload, self.current_user)
        self.assertEqual(ctx.exception.status_code, 403)

    # 7 & 8. Batch sync multiple records & transaction behavior
    @patch("app.repositories.time_entry.TimeEntryRepository.get_by_id")
    @patch("app.repositories.url_usage_repository.URLUsageRepository.get_by_client_event_id")
    @patch("app.repositories.url_usage_repository.URLUsageRepository.get_latest_record")
    @patch("app.repositories.url_usage_repository.URLUsageRepository.create")
    def test_7_8_batch_sync_multiple_records(self, mock_create, mock_latest, mock_get_client_id, mock_get_by_id):
        mock_get_by_id.return_value = self.active_time_entry
        mock_get_client_id.return_value = None
        mock_latest.return_value = None

        payload = URLUsageBatchCreate(records=[
            URLUsageCreate(time_entry_id=100, browser_name="Google Chrome", domain="github.com", duration_seconds=10),
            URLUsageCreate(time_entry_id=100, browser_name="Google Chrome", domain="stackoverflow.com", duration_seconds=20)
        ])

        accepted, failed = URLUsageService.batch_record_usage(self.db, payload, self.current_user)
        self.assertEqual(accepted, 2)
        self.assertEqual(failed, 0)
        self.assertEqual(mock_create.call_count, 2)

    # 9. Retry/idempotency behavior
    @patch("app.repositories.time_entry.TimeEntryRepository.get_by_id")
    @patch("app.repositories.url_usage_repository.URLUsageRepository.get_by_client_event_id")
    @patch("app.repositories.url_usage_repository.URLUsageRepository.create")
    def test_9_idempotency_retry_behavior(self, mock_create, mock_get_client_id, mock_get_by_id):
        mock_get_by_id.return_value = self.active_time_entry
        existing_record = TimeEntryUrlUsage(id=5, organization_id=10, time_entry_id=100, client_event_id="uuid-123")
        mock_get_client_id.return_value = existing_record

        payload = URLUsageCreate(
            time_entry_id=100, browser_name="Google Chrome", domain="github.com",
            duration_seconds=15, client_event_id="uuid-123"
        )

        record = URLUsageService.record_usage(self.db, payload, self.current_user)
        self.assertEqual(record.id, 5)
        mock_create.assert_not_called()

    # 10. Consecutive same URL aggregation (updates duration)
    @patch("app.repositories.time_entry.TimeEntryRepository.get_by_id")
    @patch("app.repositories.url_usage_repository.URLUsageRepository.get_by_client_event_id")
    @patch("app.repositories.url_usage_repository.URLUsageRepository.get_latest_record")
    @patch("app.repositories.url_usage_repository.URLUsageRepository.update_duration_and_time")
    def test_10_consecutive_same_url_aggregation(self, mock_update, mock_latest, mock_get_client_id, mock_get_by_id):
        mock_get_by_id.return_value = self.active_time_entry
        mock_get_client_id.return_value = None

        now = datetime.now(timezone.utc)
        latest_record = TimeEntryUrlUsage(
            id=10, organization_id=10, time_entry_id=100,
            browser_name="Google Chrome", domain="github.com", url="https://github.com/project",
            duration_seconds=10, recorded_at=now - timedelta(seconds=10)
        )
        mock_latest.return_value = latest_record
        mock_update.return_value = latest_record

        payload = URLUsageCreate(
            time_entry_id=100, browser_name="Google Chrome", domain="github.com",
            url="https://github.com/project", duration_seconds=5, recorded_at=now
        )

        URLUsageService.record_usage(self.db, payload, self.current_user)
        mock_update.assert_called_once_with(
            db=self.db,
            record=latest_record,
            added_duration=5,
            new_recorded_at=now
        )

    # 11. Different URL creates separate usage record
    @patch("app.repositories.time_entry.TimeEntryRepository.get_by_id")
    @patch("app.repositories.url_usage_repository.URLUsageRepository.get_by_client_event_id")
    @patch("app.repositories.url_usage_repository.URLUsageRepository.get_latest_record")
    @patch("app.repositories.url_usage_repository.URLUsageRepository.create")
    def test_11_different_url_creates_new_record(self, mock_create, mock_latest, mock_get_client_id, mock_get_by_id):
        mock_get_by_id.return_value = self.active_time_entry
        mock_get_client_id.return_value = None

        now = datetime.now(timezone.utc)
        latest_record = TimeEntryUrlUsage(
            id=10, organization_id=10, time_entry_id=100,
            browser_name="Google Chrome", domain="github.com", url="https://github.com/project",
            duration_seconds=10, recorded_at=now - timedelta(seconds=10)
        )
        mock_latest.return_value = latest_record

        payload = URLUsageCreate(
            time_entry_id=100, browser_name="Google Chrome", domain="youtube.com",
            url="https://youtube.com/watch", duration_seconds=15, recorded_at=now
        )

        URLUsageService.record_usage(self.db, payload, self.current_user)
        mock_create.assert_called_once()

    # 12 & 13. Get URL usage by time entry and pagination
    @patch("app.repositories.time_entry.TimeEntryRepository.get_by_id")
    @patch("app.repositories.url_usage_repository.URLUsageRepository.list_by_filters")
    def test_12_13_get_url_usage_pagination(self, mock_list, mock_get_by_id):
        mock_get_by_id.return_value = self.active_time_entry
        mock_list.return_value = ([TimeEntryUrlUsage(id=1, organization_id=10, time_entry_id=100, browser_name="Google Chrome", domain="github.com", duration_seconds=10)], 1)

        items, total = URLUsageService.list_usage_for_entry(
            self.db, 100, domain=None, browser_name=None,
            start_time=None, end_time=None, skip=0, limit=10, current_user=self.current_user
        )
        self.assertEqual(total, 1)
        self.assertEqual(len(items), 1)

    # 14 & 15. Domain filter & date range filter
    @patch("app.repositories.time_entry.TimeEntryRepository.get_by_id")
    @patch("app.repositories.url_usage_repository.URLUsageRepository.list_by_filters")
    def test_14_15_domain_and_date_filter(self, mock_list, mock_get_by_id):
        mock_get_by_id.return_value = self.active_time_entry
        mock_list.return_value = ([], 0)

        start = datetime.now(timezone.utc) - timedelta(days=1)
        end = datetime.now(timezone.utc)

        URLUsageService.list_usage_for_entry(
            self.db, 100, domain="github.com", browser_name=None,
            start_time=start, end_time=end, skip=0, limit=10, current_user=self.current_user
        )

        mock_list.assert_called_once_with(
            db=self.db,
            organization_id=10,
            time_entry_id=100,
            domain="github.com",
            browser_name=None,
            start_time=start,
            end_time=end,
            skip=0,
            limit=10,
            sort_asc=True
        )

    # 16 & 17. Aggregated domain summary & browser summary
    @patch("app.repositories.time_entry.TimeEntryRepository.get_by_id")
    @patch("app.repositories.url_usage_repository.URLUsageRepository.get_domain_summary")
    @patch("app.repositories.url_usage_repository.URLUsageRepository.get_browser_summary")
    def test_16_17_aggregated_summaries(self, mock_browser, mock_domain, mock_get_by_id):
        mock_get_by_id.return_value = self.active_time_entry
        mock_domain.return_value = [("github.com", 1800), ("stackoverflow.com", 900)]
        mock_browser.return_value = [("Google Chrome", 2700)]

        summary = URLUsageService.get_summary_for_entry(self.db, 100, self.current_user)
        self.assertEqual(summary["total_duration_seconds"], 2700)
        self.assertEqual(len(summary["domains"]), 2)
        self.assertEqual(len(summary["browsers"]), 1)
        self.assertEqual(summary["domains"][0]["domain"], "github.com")

    # 18. Cross-organization access prevention
    @patch("app.repositories.time_entry.TimeEntryRepository.get_by_id")
    def test_18_cross_org_access_prevention(self, mock_get_by_id):
        other_org_entry = TimeEntry(id=200, organization_id=99, user_id=1, status="running")
        mock_get_by_id.return_value = other_org_entry

        with self.assertRaises(HTTPException) as ctx:
            URLUsageService.get_summary_for_entry(self.db, 200, self.current_user)
        self.assertEqual(ctx.exception.status_code, 404)

    # Test URL normalization helper
    def test_url_normalization(self):
        d1, u1 = normalize_url("HTTPS://GitHub.com/Test/", "github.com")
        self.assertEqual(d1, "github.com")
        self.assertEqual(u1, "https://github.com/Test")

        d2, u2 = normalize_url("https://github.com/Test?q=1", "github.com")
        self.assertEqual(d2, "github.com")
        self.assertEqual(u2, "https://github.com/Test?q=1")


class TestPrivateBrowsing(unittest.TestCase):
    """`is_private` is three-valued and is part of a run's identity.

    The desktop detects private/incognito windows and records the state
    alongside ordinary URL usage — same table, same idempotency key, same
    batch endpoint. Two properties matter here: an undetermined state must not
    become "not private", and private browsing must not be aggregated into a
    normal-window record.
    """

    def setUp(self):
        self.db = MagicMock()
        self.current_user = User(
            id=1, organization_id=10, permissions={"time_entries:view_all": False}
        )
        self.active_time_entry = TimeEntry(
            id=100, organization_id=10, user_id=1, status="running", end_time=None
        )

    def _payload(self, **overrides):
        base = dict(
            time_entry_id=100, browser_name="Google Chrome", domain="wikipedia.org",
            url="https://wikipedia.org/wiki/Privacy", duration_seconds=15,
        )
        base.update(overrides)
        return URLUsageCreate(**base)

    def test_an_omitted_private_state_is_none_not_false(self):
        # A client that could not determine the state omits the field. NULL is
        # "not observed"; false would be a claim nobody made.
        assert self._payload().is_private is None

    def test_a_private_flag_is_accepted_and_preserved(self):
        assert self._payload(is_private=True).is_private is True
        assert self._payload(is_private=False).is_private is False

    @patch("app.repositories.time_entry.TimeEntryRepository.get_by_id")
    @patch("app.repositories.url_usage_repository.URLUsageRepository.get_by_client_event_id")
    @patch("app.repositories.url_usage_repository.URLUsageRepository.get_latest_record")
    @patch("app.repositories.url_usage_repository.URLUsageRepository.create")
    def test_the_private_state_reaches_the_stored_record(
        self, mock_create, mock_latest, mock_client_id, mock_entry
    ):
        mock_entry.return_value = self.active_time_entry
        mock_client_id.return_value = None
        mock_latest.return_value = None

        URLUsageService.record_usage(self.db, self._payload(is_private=True), self.current_user)
        assert mock_create.call_args.kwargs["is_private"] is True

    @patch("app.repositories.time_entry.TimeEntryRepository.get_by_id")
    @patch("app.repositories.url_usage_repository.URLUsageRepository.get_by_client_event_id")
    @patch("app.repositories.url_usage_repository.URLUsageRepository.get_latest_record")
    @patch("app.repositories.url_usage_repository.URLUsageRepository.create")
    @patch("app.repositories.url_usage_repository.URLUsageRepository.update_duration_and_time")
    def test_private_browsing_is_not_aggregated_into_a_normal_record(
        self, mock_update, mock_create, mock_latest, mock_client_id, mock_entry
    ):
        # The same page, seconds apart, first in a normal window and then in an
        # incognito one. Aggregating them would produce a single row whose
        # is_private described only whichever half was written first.
        mock_entry.return_value = self.active_time_entry
        mock_client_id.return_value = None
        now = datetime.now(timezone.utc)
        mock_latest.return_value = TimeEntryUrlUsage(
            id=10, organization_id=10, time_entry_id=100,
            browser_name="Google Chrome", domain="wikipedia.org",
            url="https://wikipedia.org/wiki/Privacy", is_private=False,
            duration_seconds=10, recorded_at=now - timedelta(seconds=10),
        )

        URLUsageService.record_usage(
            self.db, self._payload(is_private=True, recorded_at=now), self.current_user
        )
        mock_update.assert_not_called()
        mock_create.assert_called_once()
        assert mock_create.call_args.kwargs["is_private"] is True

    @patch("app.repositories.time_entry.TimeEntryRepository.get_by_id")
    @patch("app.repositories.url_usage_repository.URLUsageRepository.get_by_client_event_id")
    @patch("app.repositories.url_usage_repository.URLUsageRepository.get_latest_record")
    @patch("app.repositories.url_usage_repository.URLUsageRepository.update_duration_and_time")
    def test_consecutive_private_browsing_of_one_page_still_aggregates(
        self, mock_update, mock_latest, mock_client_id, mock_entry
    ):
        # Splitting on the state must not stop a continuous private session
        # from being aggregated the way a normal one is.
        mock_entry.return_value = self.active_time_entry
        mock_client_id.return_value = None
        now = datetime.now(timezone.utc)
        latest = TimeEntryUrlUsage(
            id=10, organization_id=10, time_entry_id=100,
            browser_name="Google Chrome", domain="wikipedia.org",
            url="https://wikipedia.org/wiki/Privacy", is_private=True,
            duration_seconds=10, recorded_at=now - timedelta(seconds=10),
        )
        mock_latest.return_value = latest
        mock_update.return_value = latest

        URLUsageService.record_usage(
            self.db, self._payload(is_private=True, recorded_at=now), self.current_user
        )
        mock_update.assert_called_once()

    @patch("app.repositories.time_entry.TimeEntryRepository.get_by_id")
    @patch("app.repositories.url_usage_repository.URLUsageRepository.get_by_client_event_id")
    @patch("app.repositories.url_usage_repository.URLUsageRepository.get_latest_record")
    @patch("app.repositories.url_usage_repository.URLUsageRepository.create")
    def test_a_batch_carries_the_private_state_of_each_record(
        self, mock_create, mock_latest, mock_client_id, mock_entry
    ):
        mock_entry.return_value = self.active_time_entry
        mock_client_id.return_value = None
        mock_latest.return_value = None

        batch = URLUsageBatchCreate(records=[
            self._payload(is_private=True, client_event_id="ev-private"),
            self._payload(domain="example.com", url="https://example.com",
                          is_private=False, client_event_id="ev-normal"),
            self._payload(domain="example.org", url="https://example.org",
                          client_event_id="ev-unknown"),
        ])
        accepted, failed = URLUsageService.batch_record_usage(
            self.db, batch, self.current_user
        )
        assert (accepted, failed) == (3, 0)
        states = [c.kwargs["is_private"] for c in mock_create.call_args_list]
        assert states == [True, False, None]

    def test_a_record_written_by_an_older_client_reads_as_unknown(self):
        # No is_private in the payload at all: the field is absent from the
        # JSON an older desktop sends, and must not default to false.
        payload = URLUsageCreate.model_validate({
            "time_entry_id": 100, "browser_name": "Google Chrome",
            "domain": "example.com", "duration_seconds": 5,
        })
        assert payload.is_private is None

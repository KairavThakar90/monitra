"""The desktop version last reported per member, read for the Active Users list.

`DesktopClientVersionRepository.for_users` is the one grouped read behind the "Desktop" column:
the members asked about come back keyed by id, a member who never reported is simply absent
(shown as unknown, never as a made-up version), duplicates in the ask cost nothing, and an empty
ask touches no database. Run on a real (SQLite) database.
"""
import unittest
from unittest.mock import MagicMock

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.models.desktop_client_version import DesktopClientVersion
from app.repositories.desktop_client_version import DesktopClientVersionRepository


def _session() -> Session:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=[DesktopClientVersion.__table__])
    return Session(engine)


class ForUsersTests(unittest.TestCase):

    def setUp(self):
        self.db = _session()
        self.db.add_all([
            DesktopClientVersion(id=1, organization_id=1, user_id=11, app_version="1.3.2", platform="win32"),
            DesktopClientVersion(id=2, organization_id=1, user_id=12, app_version="1.3.3", platform="darwin"),
            DesktopClientVersion(id=3, organization_id=1, user_id=13, app_version="1.2.0", platform=None),
        ])
        self.db.commit()

    def test_returns_each_asked_member_keyed_by_id(self):
        found = DesktopClientVersionRepository.for_users(self.db, [11, 12])
        self.assertEqual(set(found), {11, 12})
        self.assertEqual((found[11].app_version, found[11].platform), ("1.3.2", "win32"))
        self.assertEqual((found[12].app_version, found[12].platform), ("1.3.3", "darwin"))

    def test_a_member_who_never_reported_is_absent_not_invented(self):
        found = DesktopClientVersionRepository.for_users(self.db, [11, 99])
        self.assertEqual(set(found), {11})

    def test_members_not_asked_about_are_not_returned(self):
        self.assertEqual(set(DesktopClientVersionRepository.for_users(self.db, [13])), {13})

    def test_duplicate_ids_in_the_ask_are_harmless(self):
        self.assertEqual(set(DesktopClientVersionRepository.for_users(self.db, [11, 11, "11"])), {11})

    def test_a_platform_that_was_never_reported_stays_none(self):
        self.assertIsNone(DesktopClientVersionRepository.for_users(self.db, [13])[13].platform)

    def test_an_empty_ask_does_not_touch_the_database(self):
        db = MagicMock()
        self.assertEqual(DesktopClientVersionRepository.for_users(db, []), {})
        db.execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()

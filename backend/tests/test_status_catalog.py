"""The cached `project_statuses` / `task_statuses` reference tables.

These two tables are seeded by migration and written by nothing, yet every
project- or task-shaped response re-read them -- ~85ms of round trip apiece on
the development database, for eight rows that had not changed since deployment.
`StatusCatalog` reads them once per process per TTL instead.

Caching reference data is easy to get subtly wrong in three ways, and each of
them is pinned below: serving rows still bound to a closed session, caching an
*empty* table and so freezing a deployment problem in place for the whole TTL,
and never re-reading at all.
"""
import unittest
from unittest.mock import MagicMock

from app.repositories import status_catalog as module
from app.repositories.status_catalog import StatusCatalog, StatusRow


class _Row:
    """What the database hands back: an ORM row bound to a session."""

    def __init__(self, row_id, name, color="#CBD5E1"):
        self.id = row_id
        self.name = name
        self.color = color


def _session(*rows):
    db = MagicMock()
    db.scalars.return_value.all.return_value = list(rows)
    return db


class StatusCatalogTests(unittest.TestCase):
    def setUp(self):
        StatusCatalog.invalidate()
        self.addCleanup(StatusCatalog.invalidate)

    def test_the_table_is_read_once_and_then_served_from_memory(self):
        db = _session(_Row(1, "Active"), _Row(2, "Pending"))

        first = StatusCatalog.project_statuses(db)
        second = StatusCatalog.project_statuses(db)

        self.assertEqual(db.scalars.call_count, 1, "re-read a table that cannot change")
        self.assertEqual(first, second)
        self.assertEqual({row.name for row in first.values()}, {"Active", "Pending"})

    def test_the_two_tables_are_cached_separately(self):
        db = _session(_Row(1, "Active"))
        StatusCatalog.project_statuses(db)
        StatusCatalog.task_statuses(db)
        self.assertEqual(db.scalars.call_count, 2)

    def test_rows_are_detached_values_not_orm_instances(self):
        """A cached ORM row stays bound to the session that loaded it; the
        first attribute read after that session closed raises
        DetachedInstanceError, on a later request, in another thread."""
        rows = StatusCatalog.project_statuses(_session(_Row(3, "Completed")))
        self.assertIsInstance(rows[3], StatusRow)
        self.assertEqual((rows[3].id, rows[3].name, rows[3].color), (3, "Completed", "#CBD5E1"))

    def test_an_unseeded_table_is_not_cached(self):
        """An empty result is a deployment problem, not an answer. Caching it
        would keep project creation failing with "Todo task status is not
        configured" for the whole TTL after the seed had been applied."""
        empty = _session()
        self.assertEqual(StatusCatalog.task_statuses(empty), {})
        self.assertEqual(StatusCatalog.task_statuses(empty), {})
        self.assertEqual(empty.scalars.call_count, 2, "cached an unseeded table")

        seeded = _session(_Row(1, "Todo"))
        self.assertEqual(StatusCatalog.task_statuses(seeded)[1].name, "Todo")

    def test_the_snapshot_expires(self):
        db = _session(_Row(1, "Active"))
        StatusCatalog.project_statuses(db)

        original = module.TTL_SECONDS
        module.TTL_SECONDS = -1  # everything already held is older than this
        try:
            StatusCatalog.project_statuses(db)
        finally:
            module.TTL_SECONDS = original

        self.assertEqual(db.scalars.call_count, 2, "never re-read the table")

    def test_invalidate_forces_a_re_read(self):
        db = _session(_Row(1, "Active"))
        StatusCatalog.project_statuses(db)
        StatusCatalog.invalidate()
        StatusCatalog.project_statuses(db)
        self.assertEqual(db.scalars.call_count, 2)

    def test_a_single_status_is_looked_up_by_id(self):
        db = _session(_Row(1, "Active"), _Row(2, "Pending"))
        self.assertEqual(StatusCatalog.project_status(db, 2).name, "Pending")
        self.assertIsNone(StatusCatalog.project_status(db, 99))


if __name__ == "__main__":
    unittest.main()

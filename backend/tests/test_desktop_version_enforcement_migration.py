"""Migration and rollback verification for desktop_version_enforcement.

Item 15: Tests that migration b7e2a9c1d4f8:
- Creates the desktop_version_enforcement table with constraints
- Seeds the initial row (id=1, minimum_version='1.3.4')
- Rollback drops the table cleanly
- Re-upgrade creates and seeds correctly
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest

import alembic.op
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.pool import StaticPool

# Dynamically import the migration file
_MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "b7e2a9c1d4f8_add_desktop_version_enforcement.py"
)
_spec = importlib.util.spec_from_file_location("migration_b7e2a9c1d4f8", _MIGRATION_PATH)
migration = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(migration)


class MigrationSafetyTests(unittest.TestCase):

    def setUp(self):
        self.engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        # Create users table stub so foreign key can bind
        with self.engine.connect() as conn:
            conn.execute(
                text(
                    "CREATE TABLE users ("
                    "id INTEGER PRIMARY KEY, "
                    "username VARCHAR(150), "
                    "email VARCHAR(255)"
                    ")"
                )
            )
            conn.commit()

    def tearDown(self):
        self.engine.dispose()

    def test_migration_lifecycle_upgrade_rollback_reupgrade(self):
        """Test complete upgrade -> seed verify -> rollback -> re-upgrade cycle."""
        with self.engine.connect() as conn:
            ctx = MigrationContext.configure(conn)
            op = Operations(ctx)
            alembic.op._forward = op

            # 1. UPGRADE
            with op.get_context().autocommit_block():
                migration.upgrade()

            # Verify table was created
            inspector = inspect(self.engine)
            tables = inspector.get_table_names()
            self.assertIn("desktop_version_enforcement", tables)

            # Verify columns
            columns = {col["name"]: col for col in inspector.get_columns("desktop_version_enforcement")}
            self.assertIn("id", columns)
            self.assertIn("minimum_version", columns)
            self.assertIn("download_url", columns)
            self.assertIn("message", columns)
            self.assertIn("is_active", columns)
            self.assertIn("updated_at", columns)
            self.assertIn("updated_by_user_id", columns)
            self.assertIn("updated_by_username", columns)

            # Verify seeded initial values
            row = conn.execute(
                text("SELECT id, minimum_version, download_url, message, is_active FROM desktop_version_enforcement WHERE id = 1")
            ).mappings().first()
            self.assertIsNotNone(row)
            self.assertEqual(row["id"], 1)
            self.assertEqual(row["minimum_version"], "1.3.4")
            self.assertEqual(row["download_url"], "https://staff.peakworkos.com/download")
            self.assertIn("outdated", row["message"])
            self.assertTrue(bool(row["is_active"]))

            # 2. DOWNGRADE / ROLLBACK
            with op.get_context().autocommit_block():
                migration.downgrade()

            # Verify table was dropped cleanly
            inspector_after = inspect(self.engine)
            tables_after = inspector_after.get_table_names()
            self.assertNotIn("desktop_version_enforcement", tables_after)

            # 3. RE-UPGRADE
            with op.get_context().autocommit_block():
                migration.upgrade()

            # Verify re-upgrade succeeds and table + seeded row exist
            inspector_re = inspect(self.engine)
            self.assertIn("desktop_version_enforcement", inspector_re.get_table_names())
            row_re = conn.execute(
                text("SELECT id, minimum_version FROM desktop_version_enforcement WHERE id = 1")
            ).mappings().first()
            self.assertIsNotNone(row_re)
            self.assertEqual(row_re["minimum_version"], "1.3.4")

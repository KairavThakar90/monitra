"""Grace periods under real concurrency, on a real PostgreSQL.

Opt-in (``MONITRA_PG_TEST_URL``, loopback only -- see ``pg_support``). The
SQLite suite proves the policy; this proves the one thing SQLite cannot: two
connections racing to file the first grace row for the same user, platform
and floor end up with exactly one row and the same deadline, because the
unique constraint decides and the loser reads the winner's row.
"""
from __future__ import annotations

import threading
import unittest
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text

from pg_support import engine_for, pg_url, sessions
from app.models.desktop_release import DesktopRelease
from app.models.desktop_release_version import (
    DesktopReleaseVersion, DesktopUpdateRequirement, ReleaseVersionStatus,
)
from app.models.user import User
from app.services import desktop_update_policy as update_policy

URL = pg_url()
T0 = datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc)
HOST = "https://staff.peakworkos.com/download_app_files"


@unittest.skipUnless(URL, "set MONITRA_PG_TEST_URL to a loopback PostgreSQL to run")
class ConcurrentGraceRowTests(unittest.TestCase):

    def setUp(self):
        from unittest.mock import patch

        self.engine = engine_for(URL)
        self.Session = sessions(self.engine)
        with self.engine.begin() as conn:
            conn.execute(text(
                "TRUNCATE desktop_update_requirements, desktop_update_reports, "
                "desktop_release_versions, desktop_releases, desktop_client_versions, "
                "activity_logs, users RESTART IDENTITY CASCADE"
            ))
        with self.Session() as db:
            user = User(
                organization_id=1, username="ada", email="ada@example.invalid", name="Ada",
                role_name="employee", permissions={}, capture_frequency=0,
            )
            db.add(user)
            db.flush()
            self.user_id = user.id
            db.add(DesktopReleaseVersion(
                version="1.4.0", version_major=1, version_minor=4, version_patch=0,
                title="Monitra 1.4.0", target_platforms="win32,darwin", mandatory=True,
                grace_hours=24, status=ReleaseVersionStatus.PUBLISHED, published_at=T0,
                created_at=T0, updated_at=T0,
            ))
            for platform, arch, name in (
                ("win32", None, "Monitra-Setup-1.4.0.exe"),
                ("darwin", "arm64", "Monitra-macOS-arm64-1.4.0.dmg"),
                ("darwin", "x86_64", "Monitra-macOS-x86_64-1.4.0.dmg"),
            ):
                db.add(DesktopRelease(
                    version="1.4.0", version_major=1, version_minor=4, version_patch=0,
                    platform=platform, architecture=arch, download_url=f"{HOST}/{name}",
                    file_size=10, sha256="a" * 64, status="published", signed=True,
                ))
            db.commit()
        update_policy.invalidate_policy_cache()
        self.addCleanup(update_policy.invalidate_policy_cache)
        patcher = patch.multiple(
            "app.core.config.settings",
            DESKTOP_DOWNLOAD_ALLOWED_HOSTS="staff.peakworkos.com",
            DESKTOP_DOWNLOAD_URL_PREFIXES="",
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.engine.dispose()

    #: Racers. Fewer than the engine's pool (8 + 8 overflow): a racer that
    #: cannot check out a connection never reaches the barrier, and the test
    #: hangs instead of failing.
    RACERS = 8

    def test_racing_first_encounters_file_one_row_with_one_deadline(self):
        barrier = threading.Barrier(self.RACERS)
        verdicts = []
        errors = []

        def encounter(index):
            barrier.wait(timeout=30)
            try:
                with self.Session() as db:
                    user = db.get(User, self.user_id)
                    verdict = update_policy.evaluate(
                        db, user, "1.3.3", "win32", now=T0 + timedelta(seconds=index),
                    )
                    verdicts.append(verdict)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=encounter, args=(i,)) for i in range(self.RACERS)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        self.assertEqual(errors, [])
        self.assertEqual(len(verdicts), self.RACERS)
        self.assertEqual(sum(1 for v in verdicts if v.created), 1, "exactly one request created the row")
        deadlines = {v.deadline_at for v in verdicts}
        self.assertEqual(len(deadlines), 1, "every request saw the same deadline")
        with self.Session() as db:
            rows = db.execute(select(DesktopUpdateRequirement)).scalars().all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].required_version, "1.4.0")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

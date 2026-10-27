"""Desktop release management: versions, platform targeting, the 24-hour grace
period, backend enforcement, targeted announcements, install reports, audit.

Against a real (SQLite) database wherever a row's existence is the claim --
the grace row that must be created exactly once, the audit row, the report
that must not be stored twice -- and with the clock injected, so a 24-hour
deadline is crossed in a test without waiting for it. The policy's time is
always the server's: nothing here ever reads the client's clock.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import BigInteger, create_engine, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.core.permissions import ROLE_PERMISSIONS
from app.core.request_context import (
    CLIENT_DESKTOP, CLIENT_WEB, RequestContext, reset_request_context,
    set_request_context,
)
from app.core.security import create_access_token, get_current_user
from app.main import app
from app.models.activity_log import ActivityLog, ActivityLogAction
from app.models.desktop_client_version import DesktopClientVersion
from app.models.desktop_release import DesktopRelease, ReleaseStatus
from app.models.desktop_release_version import (
    DesktopReleaseVersion, DesktopUpdateReport, DesktopUpdateRequirement,
    ReleaseVersionStatus,
)
from app.models.email_notification import EmailNotification
from app.models.user import User
from app.schemas.desktop_release import (
    ReleaseArtifactInput, ReleaseVersionCreate, ReleaseVersionUpdate,
    UpdateReportCreate,
)
from app.services import desktop_update_policy as update_policy
from app.services.desktop_release import (
    DesktopReleaseService, ReleaseConflict, ReleasePolicyViolation,
)
from app.services.email import messages
from app.services.email.workflows import queue_release_announcements
from test_email_notifications import email_settings

UTC = timezone.utc
T0 = datetime(2026, 11, 2, 10, 0, tzinfo=UTC)          # Monday 10:00
HOST = "https://staff.peakworkos.com/download_app_files"


@compiles(JSONB, "sqlite")
def _jsonb_is_json_on_sqlite(type_, compiler, **kw):  # pragma: no cover - dialect shim
    return "JSON"


@compiles(BigInteger, "sqlite")
def _bigint_is_integer_on_sqlite(type_, compiler, **kw):  # pragma: no cover - dialect shim
    return "INTEGER"


def _sqlite_session() -> Session:
    """A real session over an in-memory database with every table this
    feature reads or writes. One shared connection, so the TestClient's worker
    thread sees the same database."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    tables = [
        User.__table__, DesktopRelease.__table__, DesktopReleaseVersion.__table__,
        DesktopUpdateRequirement.__table__, DesktopUpdateReport.__table__,
        DesktopClientVersion.__table__, ActivityLog.__table__, EmailNotification.__table__,
    ]
    stripped = []
    for table in tables:
        for column in table.columns:
            default = column.server_default
            if default is not None and "::" in str(getattr(default, "arg", "")):
                stripped.append((column, default))
                column.server_default = None
    try:
        Base.metadata.create_all(engine, tables=tables)
    finally:
        for column, default in stripped:
            column.server_default = default
    return Session(engine)


def _user(role_name="employee", user_id=1, username="ada", email=None, organization_id=7) -> User:
    user = User()
    user.id = user_id
    user.organization_id = organization_id
    user.username = username
    user.email = email or f"{username}@example.com"
    user.name = username.title()
    user.role_name = role_name
    user.permissions = {p: True for p in ROLE_PERMISSIONS.get(role_name, set())}
    user.is_active = True
    user.status = "active"
    user.capture_frequency = 0
    user.idle_minutes = 5
    user.idle_enabled = False
    return user


def _persist_user(db: Session, **kwargs) -> User:
    user = _user(**kwargs)
    db.add(user)
    db.commit()
    return user


def _artifact(version, platform="win32", arch=None, status=ReleaseStatus.DRAFT, **overrides):
    names = {
        ("win32", None): f"Monitra-Setup-{version}.exe",
        ("darwin", "arm64"): f"Monitra-macOS-arm64-{version}.dmg",
        ("darwin", "x86_64"): f"Monitra-macOS-x86_64-{version}.dmg",
    }
    major, minor, patch = (int(p) for p in version.split("."))
    row = DesktopRelease(
        version=version, version_major=major, version_minor=minor, version_patch=patch,
        platform=platform, architecture=arch,
        download_url=f"{HOST}/{names[(platform, arch)]}",
        file_size=31_000_000, sha256="a" * 64, release_notes="Notes.",
        status=status, force_update=False, min_supported_version=None,
        signed=True, signer="Monitra",
    )
    for key, value in overrides.items():
        setattr(row, key, value)
    return row


def _full_set(version, status=ReleaseStatus.DRAFT):
    return [
        _artifact(version, "win32", None, status),
        _artifact(version, "darwin", "arm64", status),
        _artifact(version, "darwin", "x86_64", status),
    ]


class _Base(unittest.TestCase):
    """A database, an administrator, a settled clock and an empty policy cache."""

    def setUp(self):
        self.db = _sqlite_session()
        self.admin = _persist_user(self.db, role_name="administrator", user_id=5, username="grace")
        self.user = _persist_user(self.db, role_name="employee", user_id=42, username="ada")
        update_policy.invalidate_policy_cache()
        self.addCleanup(update_policy.invalidate_policy_cache)
        # Hosting policy: the website folder, signing not required for these rows.
        patcher = patch.multiple(
            "app.core.config.settings",
            DESKTOP_DOWNLOAD_ALLOWED_HOSTS="staff.peakworkos.com",
            DESKTOP_DOWNLOAD_URL_PREFIXES="",
            DESKTOP_REQUIRED_ARTIFACTS="win32,darwin:arm64,darwin:x86_64",
            DESKTOP_REQUIRE_SIGNED_RELEASES=False,
            DESKTOP_AUTO_UPDATE_MIN_CLIENT_VERSION="",
            RELEASE_EMAIL_ENABLED=False,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.db.close()

    # ── helpers ───────────────────────────────────────────────────────────

    def create(self, version, *, targets=("win32", "darwin"), mandatory=False,
               grace_hours=24, floor=None, title=None, artifacts=True, effective_at=None):
        payload = ReleaseVersionCreate(
            version=version, title=title or f"Monitra {version}",
            release_notes="- Faster startup", target_platforms=list(targets),
            mandatory=mandatory, grace_hours=grace_hours, required_min_version=floor,
            effective_at=effective_at,
        )
        record = DesktopReleaseService.create_version(self.db, payload, actor=self.admin)
        if artifacts:
            for row in _full_set(version):
                if row.platform in targets:
                    self.db.add(row)
            self.db.commit()
        return record

    def publish(self, version):
        published, queued = DesktopReleaseService.publish_version(self.db, version, actor=self.admin)
        update_policy.invalidate_policy_cache()
        return published, queued

    def evaluate(self, client_version, platform="win32", now=T0, user=None, settle=False):
        return update_policy.evaluate(
            self.db, user or self.user, client_version, platform, now=now, settle=settle,
        )

    def requirements(self):
        return list(self.db.execute(select(DesktopUpdateRequirement)).scalars())

    def audit(self):
        return list(self.db.execute(select(ActivityLog).order_by(ActivityLog.id)).scalars())


# ═══════════════════════════════════════════════════════════════════════════
# Version records and the lifecycle
# ═══════════════════════════════════════════════════════════════════════════


class VersionLifecycleTests(_Base):

    def test_a_created_version_is_a_draft_that_affects_nobody(self):
        self.create("1.4.0", mandatory=True)
        self.assertEqual(self.evaluate("1.3.3").required, False)
        detail = DesktopReleaseService.version_detail(self.db, "1.4.0")
        self.assertEqual(detail.status, ReleaseVersionStatus.DRAFT)
        self.assertTrue(detail.ready)
        self.assertEqual([a.action for a in self.audit()], [ActivityLogAction.RELEASE_CREATED])

    def test_creating_the_same_version_twice_is_refused(self):
        self.create("1.4.0")
        with self.assertRaises(ReleaseConflict):
            self.create("1.4.0", artifacts=False)

    def test_an_invalid_target_platform_is_refused(self):
        with self.assertRaises(ReleasePolicyViolation):
            self.create("1.4.0", targets=("linux",), artifacts=False)
        with self.assertRaises(ReleasePolicyViolation):
            self.create("1.4.1", targets=(), artifacts=False)

    def test_a_floor_newer_than_the_release_is_refused(self):
        with self.assertRaises(ReleasePolicyViolation):
            self.create("1.4.0", mandatory=True, floor="1.5.0", artifacts=False)

    def test_an_artifact_for_a_platform_the_release_does_not_target_is_refused(self):
        payload = ReleaseVersionCreate(
            version="1.4.0", target_platforms=["win32"],
            artifacts=[ReleaseArtifactInput(
                platform="darwin", architecture="arm64",
                download_url=f"{HOST}/Monitra-macOS-arm64-1.4.0.dmg", sha256="a" * 64,
                file_size=10,
            )],
        )
        with self.assertRaises(ReleasePolicyViolation):
            DesktopReleaseService.create_version(self.db, payload, actor=self.admin)

    def test_artifacts_given_with_the_version_go_through_the_hosting_policy(self):
        payload = ReleaseVersionCreate(
            version="1.4.0", target_platforms=["win32"],
            artifacts=[ReleaseArtifactInput(
                platform="win32", download_url="https://evil.example/Monitra-Setup-1.4.0.exe",
                sha256="a" * 64, file_size=10,
            )],
        )
        with self.assertRaises(ReleasePolicyViolation) as caught:
            DesktopReleaseService.create_version(self.db, payload, actor=self.admin)
        self.assertEqual(caught.exception.problems[0].code, "url")

    def test_a_valid_hand_entered_artifact_is_registered_as_a_draft_row(self):
        payload = ReleaseVersionCreate(
            version="1.4.0", target_platforms=["win32"],
            artifacts=[ReleaseArtifactInput(
                platform="win32", download_url=f"{HOST}/Monitra-Setup-1.4.0.exe",
                sha256="b" * 64, file_size=10, signed=True, signer="Monitra",
            )],
        )
        DesktopReleaseService.create_version(self.db, payload, actor=self.admin)
        detail = DesktopReleaseService.version_detail(self.db, "1.4.0")
        self.assertEqual([a.platform for a in detail.artifacts], ["win32"])
        self.assertEqual(detail.artifacts[0].status, ReleaseStatus.DRAFT)
        self.assertTrue(detail.ready)

    def test_validate_records_a_passing_readiness_check(self):
        self.create("1.4.0")
        record, report = DesktopReleaseService.validate_version(self.db, "1.4.0", actor=self.admin)
        self.assertTrue(report.ready)
        self.assertEqual(record.status, ReleaseVersionStatus.VALIDATED)
        self.assertIsNotNone(record.validated_at)
        self.assertIn(ActivityLogAction.RELEASE_VALIDATED, [a.action for a in self.audit()])

    def test_validate_refuses_an_incomplete_set_and_lists_what_is_missing(self):
        self.create("1.4.0", artifacts=False)
        self.db.add(_artifact("1.4.0", "win32"))
        self.db.commit()
        record, report = DesktopReleaseService.validate_version(self.db, "1.4.0", actor=self.admin)
        self.assertFalse(report.ready)
        self.assertEqual(record.status, ReleaseVersionStatus.DRAFT)
        self.assertEqual(sorted(p.artifact for p in report.problems), ["darwin/arm64", "darwin/x86_64"])

    def test_publishing_moves_the_rows_and_the_record_in_one_step_with_an_audit_row(self):
        self.create("1.4.0", mandatory=True)
        published, _queued = self.publish("1.4.0")
        self.assertEqual(len(published), 3)
        detail = DesktopReleaseService.version_detail(self.db, "1.4.0")
        self.assertEqual(detail.status, ReleaseVersionStatus.PUBLISHED)
        self.assertEqual(detail.published_by_user_id, self.admin.id)
        self.assertIsNotNone(detail.published_at)
        published_rows = [a for a in self.audit() if a.action == ActivityLogAction.RELEASE_PUBLISHED]
        self.assertEqual(len(published_rows), 1)
        self.assertEqual(published_rows[0].user_id, self.admin.id)
        self.assertEqual(published_rows[0].entity_id, detail.id)
        self.assertIn("mandatory", published_rows[0].description)
        self.assertIn("24 h grace", published_rows[0].description)

    def test_publishing_twice_changes_nothing_and_records_nothing_more(self):
        self.create("1.4.0")
        self.publish("1.4.0")
        before = len(self.audit())
        published, queued = self.publish("1.4.0")
        self.assertEqual(len(published), 3)
        self.assertFalse(queued)
        self.assertEqual(len(self.audit()), before)

    def test_publishing_refuses_an_incomplete_set_for_the_targets(self):
        self.create("1.4.0", artifacts=False)
        self.db.add(_artifact("1.4.0", "win32"))
        self.db.commit()
        with self.assertRaises(ReleasePolicyViolation):
            self.publish("1.4.0")
        self.assertEqual(
            DesktopReleaseService.version_detail(self.db, "1.4.0").status, ReleaseVersionStatus.DRAFT,
        )

    def test_a_windows_only_release_needs_only_the_windows_installer(self):
        self.create("1.4.0", targets=("win32",))
        published, _ = self.publish("1.4.0")
        self.assertEqual([r.platform for r in published], ["win32"])

    def test_a_macos_only_release_needs_both_mac_builds_and_no_windows_one(self):
        self.create("1.4.0", targets=("darwin",), artifacts=False)
        self.db.add(_artifact("1.4.0", "darwin", "arm64"))
        self.db.commit()
        with self.assertRaises(ReleasePolicyViolation) as caught:
            self.publish("1.4.0")
        self.assertEqual([p.artifact for p in caught.exception.problems], ["darwin/x86_64"])
        self.db.add(_artifact("1.4.0", "darwin", "x86_64"))
        self.db.commit()
        published, _ = self.publish("1.4.0")
        self.assertEqual(sorted(r.architecture for r in published), ["arm64", "x86_64"])

    def test_withdrawing_rolls_back_every_row_and_records_who_did_it(self):
        self.create("1.4.0")
        self.publish("1.4.0")
        record, waiting = DesktopReleaseService.withdraw_version(
            self.db, "1.4.0", actor=self.admin, reason="installer crashes on start",
        )
        self.assertEqual(record.status, ReleaseVersionStatus.WITHDRAWN)
        self.assertEqual(record.withdrawn_by_user_id, self.admin.id)
        self.assertEqual(waiting, 0)
        rows = self.db.execute(select(DesktopRelease)).scalars().all()
        self.assertEqual({r.status for r in rows}, {ReleaseStatus.ROLLED_BACK})
        entry = [a for a in self.audit() if a.action == ActivityLogAction.RELEASE_WITHDRAWN][0]
        self.assertIn("installer crashes on start", entry.description)

    def test_a_withdrawn_version_cannot_be_published_again(self):
        self.create("1.4.0")
        self.publish("1.4.0")
        DesktopReleaseService.withdraw_version(self.db, "1.4.0", actor=self.admin)
        with self.assertRaises(ReleasePolicyViolation):
            self.publish("1.4.0")

    def test_the_platforms_of_a_published_release_cannot_be_changed(self):
        self.create("1.4.0")
        self.publish("1.4.0")
        with self.assertRaises(ReleasePolicyViolation):
            DesktopReleaseService.update_version(
                self.db, "1.4.0", ReleaseVersionUpdate(target_platforms=["win32"]), actor=self.admin,
            )

    def test_an_update_is_audited_with_what_changed(self):
        self.create("1.4.0")
        DesktopReleaseService.update_version(
            self.db, "1.4.0",
            ReleaseVersionUpdate(mandatory=True, grace_hours=48, title="Autumn release"),
            actor=self.admin,
        )
        entry = [a for a in self.audit() if a.action == ActivityLogAction.RELEASE_UPDATED][0]
        self.assertIn("mandatory=yes", entry.description)
        self.assertIn("grace=48h", entry.description)
        self.assertIn("title", entry.description)
        detail = DesktopReleaseService.version_detail(self.db, "1.4.0")
        self.assertEqual(detail.enforced_floor, "1.4.0")

    def test_superseded_is_derived_from_newer_published_coverage(self):
        self.create("1.4.0")
        self.publish("1.4.0")
        self.create("1.4.1", targets=("win32",))
        self.publish("1.4.1")
        statuses = {v.version: v.status for v in DesktopReleaseService.list_versions(self.db)}
        # 1.4.1 covers Windows only, so 1.4.0 is still the macOS release.
        self.assertEqual(statuses["1.4.0"], ReleaseVersionStatus.PUBLISHED)
        self.create("1.4.2")
        self.publish("1.4.2")
        statuses = {v.version: v.status for v in DesktopReleaseService.list_versions(self.db)}
        self.assertEqual(statuses["1.4.0"], "superseded")
        self.assertEqual(statuses["1.4.1"], "superseded")
        self.assertEqual(statuses["1.4.2"], ReleaseVersionStatus.PUBLISHED)

    def test_a_pipeline_registration_gets_a_draft_record_for_every_platform(self):
        from app.schemas.desktop_release import DesktopReleaseCreate

        DesktopReleaseService.create_release(self.db, DesktopReleaseCreate(
            version="1.4.0", platform="win32", download_url=f"{HOST}/Monitra-Setup-1.4.0.exe",
            sha256="a" * 64, file_size=10,
        ), actor=self.admin)
        detail = DesktopReleaseService.version_detail(self.db, "1.4.0")
        self.assertEqual(detail.status, ReleaseVersionStatus.DRAFT)
        self.assertEqual(detail.target_platforms, ["win32", "darwin"])
        self.assertFalse(detail.mandatory)


# ═══════════════════════════════════════════════════════════════════════════
# The policy engine: who is required, by when
# ═══════════════════════════════════════════════════════════════════════════


class PolicyEngineTests(_Base):

    def test_no_published_mandatory_release_means_nothing_is_required(self):
        self.create("1.4.0")
        self.publish("1.4.0")
        verdict = self.evaluate("1.3.3")
        self.assertFalse(verdict.required)
        self.assertFalse(verdict.blocked)
        self.assertEqual(self.requirements(), [])

    def test_an_optional_release_blocks_nobody_however_old_the_client(self):
        self.create("1.4.0")
        self.publish("1.4.0")
        self.assertFalse(self.evaluate("1.0.0", now=T0 + timedelta(days=30)).required)

    def test_first_encounter_files_the_grace_row_on_the_server_clock(self):
        self.create("1.4.0", mandatory=True)
        self.publish("1.4.0")
        verdict = self.evaluate("1.3.3", now=T0)
        self.assertTrue(verdict.required)
        self.assertTrue(verdict.created)
        self.assertTrue(verdict.grace_active)
        self.assertFalse(verdict.blocked)
        self.assertEqual(verdict.required_version, "1.4.0")
        self.assertEqual(verdict.first_seen_at, T0)
        self.assertEqual(verdict.deadline_at, T0 + timedelta(hours=24))
        self.assertEqual(verdict.remaining_seconds, 24 * 3600)
        rows = self.requirements()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].first_seen_client_version, "1.3.3")

    def test_a_client_at_or_above_the_floor_owes_nothing(self):
        self.create("1.4.0", mandatory=True)
        self.publish("1.4.0")
        self.assertFalse(self.evaluate("1.4.0").required)
        self.assertFalse(self.evaluate("1.5.0").required)
        self.assertEqual(self.requirements(), [])

    def test_the_deadline_never_moves_however_often_the_client_asks(self):
        self.create("1.4.0", mandatory=True)
        self.publish("1.4.0")
        first = self.evaluate("1.3.3", now=T0)
        for later in (T0 + timedelta(hours=1), T0 + timedelta(hours=12), T0 + timedelta(hours=23, minutes=59)):
            again = self.evaluate("1.3.3", now=later)
            self.assertFalse(again.created)
            self.assertEqual(again.deadline_at, first.deadline_at)
            self.assertTrue(again.grace_active)
        self.assertEqual(len(self.requirements()), 1)

    def test_just_before_at_and_after_the_deadline(self):
        self.create("1.4.0", mandatory=True)
        self.publish("1.4.0")
        self.evaluate("1.3.3", now=T0)
        deadline = T0 + timedelta(hours=24)
        self.assertFalse(self.evaluate("1.3.3", now=deadline - timedelta(seconds=1)).blocked)
        self.assertTrue(self.evaluate("1.3.3", now=deadline).blocked)
        after = self.evaluate("1.3.3", now=deadline + timedelta(days=3))
        self.assertTrue(after.blocked)
        self.assertEqual(after.remaining_seconds, 0)

    def test_a_restart_or_a_moved_client_clock_cannot_reset_the_deadline(self):
        # The client's clock is never an input: `evaluate` takes only the
        # server's `now`. A "restart" is simply another call; a client that
        # set its clock back a week sends the same request.
        self.create("1.4.0", mandatory=True)
        self.publish("1.4.0")
        first = self.evaluate("1.3.3", now=T0)
        self.assertEqual(self.evaluate("1.3.3", now=T0 + timedelta(hours=25)).blocked, True)
        self.assertEqual(self.evaluate("1.3.3", now=T0 + timedelta(hours=25)).deadline_at, first.deadline_at)

    def test_two_devices_of_one_user_share_one_grace_row_per_platform(self):
        self.create("1.4.0", mandatory=True)
        self.publish("1.4.0")
        self.evaluate("1.3.3", platform="win32", now=T0)
        self.evaluate("1.3.2", platform="win32", now=T0 + timedelta(hours=2))
        self.assertEqual(len(self.requirements()), 1)
        # A Mac of the same user is its own grace period: the policy is per platform.
        mac = self.evaluate("1.3.3", platform="darwin", now=T0 + timedelta(hours=2))
        self.assertTrue(mac.created)
        self.assertEqual(mac.deadline_at, T0 + timedelta(hours=26))
        self.assertEqual(len(self.requirements()), 2)

    def test_concurrent_first_encounters_create_exactly_one_row(self):
        """Two requests racing on the insert: the unique constraint lets one
        through and the other reads it. Simulated on one session by inserting
        between the existence check and the insert, which is the interleaving
        two connections produce."""
        self.create("1.4.0", mandatory=True)
        self.publish("1.4.0")
        from app.repositories.desktop_release_version import DesktopReleaseVersionRepository

        original = DesktopReleaseVersionRepository.get_requirement
        calls = {"n": 0}

        def racing_get(db, **kwargs):
            calls["n"] += 1
            row = original(db, **kwargs)
            if calls["n"] == 1 and row is None:
                # The other request wins the race between our check and our insert.
                other = DesktopUpdateRequirement(
                    user_id=kwargs["user_id"], platform=kwargs["platform"],
                    required_version=kwargs["required_version"],
                    required_major=1, required_minor=4, required_patch=0,
                    release_version_id=None, first_seen_at=T0 - timedelta(hours=3),
                    deadline_at=T0 + timedelta(hours=21), first_seen_client_version="1.3.2",
                )
                db.add(other)
                db.flush()
                return None
            return row

        with patch.object(DesktopReleaseVersionRepository, "get_requirement", staticmethod(racing_get)):
            verdict = self.evaluate("1.3.3", now=T0)
        self.assertFalse(verdict.created)
        self.assertEqual(verdict.deadline_at, T0 + timedelta(hours=21))
        self.assertEqual(len(self.requirements()), 1)

    def test_the_grace_period_is_the_release_record_s_own(self):
        self.create("1.4.0", mandatory=True, grace_hours=6)
        self.publish("1.4.0")
        verdict = self.evaluate("1.3.3", now=T0)
        self.assertEqual(verdict.deadline_at, T0 + timedelta(hours=6))
        self.assertEqual(verdict.grace_hours, 6)

    def test_a_zero_hour_grace_blocks_at_first_sight(self):
        self.create("1.4.0", mandatory=True, grace_hours=0)
        self.publish("1.4.0")
        self.assertTrue(self.evaluate("1.3.3", now=T0).blocked)

    def test_a_floor_below_the_release_requires_only_the_clients_below_it(self):
        self.create("1.4.0", mandatory=True, floor="1.3.0")
        self.publish("1.4.0")
        self.assertTrue(self.evaluate("1.2.9").required)
        self.assertFalse(self.evaluate("1.3.0").required)
        self.assertFalse(self.evaluate("1.3.3").required)

    def test_an_optional_release_with_a_floor_requires_only_the_clients_below_the_floor(self):
        self.create("1.4.0", mandatory=False, floor="1.2.0")
        self.publish("1.4.0")
        self.assertTrue(self.evaluate("1.1.0").required)
        self.assertFalse(self.evaluate("1.2.0").required)
        self.assertFalse(self.evaluate("1.3.3").required)

    def test_an_effective_time_in_the_future_holds_the_policy_back(self):
        self.create("1.4.0", mandatory=True, effective_at=T0 + timedelta(days=2))
        self.publish("1.4.0")
        self.assertFalse(self.evaluate("1.3.3", now=T0).required)
        verdict = self.evaluate("1.3.3", now=T0 + timedelta(days=2))
        self.assertTrue(verdict.required)
        self.assertEqual(verdict.first_seen_at, T0 + timedelta(days=2))

    def test_an_unreadable_client_version_is_never_required(self):
        self.create("1.4.0", mandatory=True)
        self.publish("1.4.0")
        for bad in (None, "", "garbage", "1.0", "v1.0.0"):
            with self.subTest(version=bad):
                verdict = self.evaluate(bad)
                self.assertFalse(verdict.required)
        self.assertEqual(self.requirements(), [])

    # ── platforms ──────────────────────────────────────────────────────────

    def test_a_windows_only_mandatory_release_leaves_macs_alone(self):
        self.create("1.4.0", mandatory=True, targets=("win32",))
        self.publish("1.4.0")
        self.assertTrue(self.evaluate("1.3.3", platform="win32").required)
        self.assertFalse(self.evaluate("1.3.3", platform="darwin").required)
        self.assertFalse(self.evaluate("1.3.3", platform="macos").required)

    def test_a_macos_only_mandatory_release_leaves_windows_alone(self):
        self.create("1.4.0", mandatory=True, targets=("darwin",))
        self.publish("1.4.0")
        self.assertTrue(self.evaluate("1.3.3", platform="darwin").required)
        self.assertFalse(self.evaluate("1.3.3", platform="win32").required)

    def test_a_higher_windows_version_does_not_satisfy_or_invalidate_a_macos_policy(self):
        self.create("1.4.0", mandatory=True, targets=("darwin",))
        self.publish("1.4.0")
        self.create("1.5.0", targets=("win32",))
        self.publish("1.5.0")
        policies = update_policy.active_policies(self.db, T0)
        self.assertIsNone(policies["win32"])
        self.assertEqual(policies["darwin"].floor, "1.4.0")
        self.assertEqual(policies["darwin"].installable_version, "1.4.0")
        self.assertTrue(self.evaluate("1.3.3", platform="darwin").required)
        self.assertFalse(self.evaluate("1.3.3", platform="win32").required)

    def test_the_strictest_floor_wins_when_several_policies_apply(self):
        self.create("1.4.0", mandatory=True, floor="1.3.0")
        self.publish("1.4.0")
        self.create("1.4.1", mandatory=True)
        self.publish("1.4.1")
        policies = update_policy.active_policies(self.db, T0)
        self.assertEqual(policies["win32"].floor, "1.4.1")
        verdict = self.evaluate("1.3.5", now=T0)
        self.assertTrue(verdict.required)
        self.assertEqual(verdict.required_version, "1.4.1")
        self.assertEqual(verdict.release_version, "1.4.1")

    def test_a_client_claiming_no_supported_platform_is_held_to_every_policy(self):
        self.create("1.4.0", mandatory=True, targets=("win32",))
        self.publish("1.4.0")
        for claim in (None, "", "linux", "freebsd", "toaster"):
            with self.subTest(claim=claim):
                verdict = self.evaluate("1.3.3", platform=claim, now=T0)
                self.assertTrue(verdict.required)
                self.assertEqual(verdict.platform, "unknown")
        self.assertEqual(len(self.requirements()), 1)

    # ── withdrawal and corrective releases ─────────────────────────────────

    def test_withdrawing_the_only_installable_release_suspends_enforcement_but_keeps_the_grace(self):
        self.create("1.4.0", mandatory=True)
        self.publish("1.4.0")
        self.evaluate("1.3.3", now=T0)
        _record, waiting = DesktopReleaseService.withdraw_version(self.db, "1.4.0", actor=self.admin)
        self.assertEqual(waiting, 1)
        update_policy.invalidate_policy_cache()
        # Past the deadline, nothing is enforced: there is nothing to install.
        verdict = self.evaluate("1.3.3", now=T0 + timedelta(days=2))
        self.assertFalse(verdict.required)
        self.assertFalse(verdict.blocked)
        # The grace row is still there, untouched.
        rows = self.requirements()
        self.assertEqual(len(rows), 1)
        self.assertEqual(update_policy.aware(rows[0].deadline_at), T0 + timedelta(hours=24))
        self.assertIsNone(rows[0].satisfied_at)

    def test_a_corrective_release_with_the_same_floor_resumes_the_original_deadline(self):
        self.create("1.4.0", mandatory=True)
        self.publish("1.4.0")
        self.evaluate("1.3.3", now=T0)
        DesktopReleaseService.withdraw_version(self.db, "1.4.0", actor=self.admin)
        update_policy.invalidate_policy_cache()
        # 1.4.1 carries the floor 1.4.0: the same requirement, not a new one.
        self.create("1.4.1", mandatory=True, floor="1.4.0")
        self.publish("1.4.1")
        verdict = self.evaluate("1.3.3", now=T0 + timedelta(hours=30))
        self.assertTrue(verdict.required)
        self.assertTrue(verdict.blocked)
        self.assertEqual(verdict.deadline_at, T0 + timedelta(hours=24))
        self.assertEqual(verdict.release_version, "1.4.1")
        self.assertEqual(len(self.requirements()), 1)

    def test_a_corrective_release_with_a_new_floor_starts_a_new_grace_period(self):
        self.create("1.4.0", mandatory=True)
        self.publish("1.4.0")
        self.evaluate("1.3.3", now=T0)
        DesktopReleaseService.withdraw_version(self.db, "1.4.0", actor=self.admin)
        update_policy.invalidate_policy_cache()
        self.create("1.4.1", mandatory=True)
        self.publish("1.4.1")
        verdict = self.evaluate("1.3.3", now=T0 + timedelta(hours=30))
        self.assertTrue(verdict.required)
        self.assertFalse(verdict.blocked)
        self.assertEqual(verdict.deadline_at, T0 + timedelta(hours=54))
        self.assertEqual(len(self.requirements()), 2)

    def test_a_published_row_with_a_refused_url_is_not_enforced(self):
        self.create("1.4.0", mandatory=True)
        self.publish("1.4.0")
        for row in self.db.execute(select(DesktopRelease)).scalars():
            row.download_url = row.download_url.replace("staff.peakworkos.com", "evil.example")
        self.db.commit()
        update_policy.invalidate_policy_cache()
        self.assertFalse(self.evaluate("1.3.3").required)

    # ── satisfaction ───────────────────────────────────────────────────────

    def test_the_server_marks_a_requirement_satisfied_when_it_sees_the_new_version(self):
        self.create("1.4.0", mandatory=True)
        self.publish("1.4.0")
        self.evaluate("1.3.3", now=T0)
        self.evaluate("1.4.0", now=T0 + timedelta(hours=3), settle=True)
        row = self.requirements()[0]
        self.assertEqual(update_policy.aware(row.satisfied_at), T0 + timedelta(hours=3))
        self.assertEqual(row.satisfied_version, "1.4.0")
        summary = update_policy.requirement_summary(self.requirements(), now=T0 + timedelta(hours=4))
        self.assertEqual(summary, {"pending": 0, "expired": 0, "satisfied": 1})

    def test_a_client_still_below_the_floor_does_not_satisfy_anything(self):
        self.create("1.4.0", mandatory=True)
        self.publish("1.4.0")
        self.evaluate("1.3.3", now=T0)
        self.evaluate("1.3.4", now=T0 + timedelta(hours=3), settle=True)
        self.assertIsNone(self.requirements()[0].satisfied_at)

    def test_the_policy_cache_is_dropped_by_publish_and_withdraw(self):
        self.create("1.4.0", mandatory=True)
        self.assertIsNone(update_policy.active_policies(self.db, T0)["win32"])
        DesktopReleaseService.publish_version(self.db, "1.4.0", actor=self.admin)   # no manual invalidate
        self.assertIsNotNone(update_policy.active_policies(self.db, T0)["win32"])
        DesktopReleaseService.withdraw_version(self.db, "1.4.0", actor=self.admin)
        self.assertIsNone(update_policy.active_policies(self.db, T0)["win32"])


# ═══════════════════════════════════════════════════════════════════════════
# The update check's answer and the policy endpoint
# ═══════════════════════════════════════════════════════════════════════════


class UpdateCheckAnswerTests(_Base):

    def _latest(self, client_version, platform="win32", now=T0):
        with patch.object(update_policy, "now_utc", return_value=now):
            return DesktopReleaseService.latest_version(
                self.db, self.user, client_version, platform, None,
            )

    def test_an_optional_release_is_offered_without_any_policy_fields(self):
        self.create("1.4.0")
        self.publish("1.4.0")
        answer = self._latest("1.3.3")
        self.assertTrue(answer.update_available)
        self.assertFalse(answer.mandatory)
        self.assertFalse(answer.force_update)
        self.assertIsNone(answer.deadline_at)
        self.assertEqual(answer.release_title, "Monitra 1.4.0")
        self.assertEqual(answer.release_notes, "- Faster startup")

    def test_a_mandatory_release_inside_the_grace_is_mandatory_but_not_forced(self):
        """A client that predates grace periods reads `force_update` as "lock
        out now"; it must see false until the deadline. A new client reads
        `mandatory`, `deadline_at` and `blocked`."""
        self.create("1.4.0", mandatory=True)
        self.publish("1.4.0")
        answer = self._latest("1.3.3", now=T0)
        self.assertTrue(answer.update_available)
        self.assertTrue(answer.mandatory)
        self.assertFalse(answer.force_update)
        self.assertFalse(answer.blocked)
        self.assertTrue(answer.grace_active)
        self.assertEqual(answer.required_version, "1.4.0")
        self.assertEqual(answer.deadline_at, T0 + timedelta(hours=24))
        self.assertEqual(answer.server_time, T0)
        self.assertEqual(answer.grace_hours, 24)

    def test_after_the_deadline_the_answer_is_forced_and_blocked(self):
        self.create("1.4.0", mandatory=True)
        self.publish("1.4.0")
        self._latest("1.3.3", now=T0)
        answer = self._latest("1.3.3", now=T0 + timedelta(hours=25))
        self.assertTrue(answer.force_update)
        self.assertTrue(answer.blocked)
        self.assertFalse(answer.grace_active)

    def test_a_current_client_is_told_nothing_is_pending_and_its_grace_is_settled(self):
        self.create("1.4.0", mandatory=True)
        self.publish("1.4.0")
        self._latest("1.3.3", now=T0)
        answer = self._latest("1.4.0", now=T0 + timedelta(hours=1))
        self.assertFalse(answer.update_available)
        self.assertFalse(answer.mandatory)
        self.assertFalse(answer.blocked)
        self.assertIsNotNone(self.requirements()[0].satisfied_at)

    def test_a_legacy_row_without_a_record_still_forces_by_its_own_flags(self):
        self.db.add_all(_full_set("1.4.0", ReleaseStatus.PUBLISHED))
        for row in self.db.execute(select(DesktopRelease)).scalars():
            row.force_update = True
        self.db.commit()
        answer = self._latest("1.3.3")
        self.assertTrue(answer.force_update)
        self.assertTrue(answer.mandatory)
        self.assertIsNone(answer.deadline_at)   # no record, no grace period

    def test_the_policy_view_carries_the_grace_and_the_release(self):
        self.create("1.4.0", mandatory=True)
        self.publish("1.4.0")
        with patch.object(update_policy, "now_utc", return_value=T0):
            view = DesktopReleaseService.update_policy_view(self.db, self.user, "1.3.3", "win32", "AMD64")
        self.assertTrue(view.update_required)
        self.assertEqual(view.required_version, "1.4.0")
        self.assertEqual(view.grace.deadline_at, T0 + timedelta(hours=24))
        self.assertEqual(view.grace.remaining_seconds, 24 * 3600)
        self.assertTrue(view.grace.active)
        self.assertFalse(view.blocked)
        self.assertEqual(view.release.latest_version, "1.4.0")
        self.assertEqual(view.release.sha256, "a" * 64)


# ═══════════════════════════════════════════════════════════════════════════
# Backend enforcement: the gate
# ═══════════════════════════════════════════════════════════════════════════


def _context(path, *, client=CLIENT_DESKTOP, version="1.3.3", platform="win32"):
    return RequestContext(
        client=client, client_version=version, ip_address="10.0.0.1",
        path=path, platform=platform, architecture="AMD64",
    )


class GateTests(_Base):

    def _enforce(self, path, *, now=T0, user=None, **context):
        token = set_request_context(_context(path, **context))
        try:
            with patch.object(update_policy, "now_utc", return_value=now):
                update_policy.enforce(self.db, user or self.user)
        finally:
            reset_request_context(token)

    def _expired(self):
        self.create("1.4.0", mandatory=True)
        self.publish("1.4.0")
        self.evaluate("1.3.3", now=T0)
        return T0 + timedelta(hours=25)

    def test_no_active_policy_lets_everything_through(self):
        self.create("1.4.0")
        self.publish("1.4.0")
        self._enforce("/time-entries/start", now=T0 + timedelta(days=9))
        self.assertEqual(self.requirements(), [])

    def test_inside_the_grace_period_nothing_is_refused(self):
        self.create("1.4.0", mandatory=True)
        self.publish("1.4.0")
        self._enforce("/time-entries/start", now=T0)
        self._enforce("/api/v1/projects", now=T0 + timedelta(hours=23))
        self.assertEqual(len(self.requirements()), 1, "the gate itself starts the grace period")

    def test_after_the_deadline_a_protected_request_is_refused_with_426(self):
        later = self._expired()
        with self.assertRaises(HTTPException) as caught:
            self._enforce("/time-entries/start", now=later)
        self.assertEqual(caught.exception.status_code, 426)
        detail = caught.exception.detail
        self.assertEqual(detail["code"], "desktop_update_required")
        self.assertEqual(detail["required_version"], "1.4.0")
        self.assertEqual(detail["latest_version"], "1.4.0")
        self.assertEqual(detail["installed_version"], "1.3.3")
        self.assertEqual(detail["platform"], "win32")
        self.assertFalse(detail["grace_active"])
        self.assertEqual(detail["deadline_at"], (T0 + timedelta(hours=24)).isoformat())
        self.assertEqual(detail["policy_url"], "/desktop/update-policy")

    def test_the_recovery_paths_stay_open_after_the_deadline(self):
        later = self._expired()
        for path in (
            "/auth/me", "/auth/refresh", "/auth/logout",
            "/desktop/latest-version", "/desktop/update-policy", "/desktop/update-reports",
            "/system/maintenance-status",
            "/time-entries/active", "/time-entries/17/stop", "/time-entries/17/heartbeat",
            "/time-entries/17/activity/batch", "/time-entries/17/app-usage/batch",
            "/time-entries/17/app-usage", "/time-entries/17/screenshots",
            "/time-entries/17/unwanted-activity", "/time-entries/17/adjustments",
            "/time-entry-activities/batch", "/url-usage/batch",
            "/time-entry-screenshots/capture-events",
            "/idle-periods", "/idle-periods/active", "/idle-periods/3/resolve",
            "/api/v1/activity-logs/client-events", "/api/v1/time-entries/17/stop",
            "/api/v1/auth/me",
            # The running session's capture reads (found by the release E2E:
            # refused, the screenshot gate held every capture of a timer the
            # policy had left running).
            "/api/v1/screenshot/privacy-config", "/screenshots/config",
        ):
            with self.subTest(path=path):
                self._enforce(path, now=later)

    def test_the_normal_paths_are_refused_after_the_deadline(self):
        later = self._expired()
        for path in (
            "/time-entries/start", "/api/v1/projects", "/api/v1/projects/3/tasks",
            "/api/v1/sync/revision", "/manual-time-entries", "/feedback",
            "/time-entries", "/desktop-notifications/schedule", "/api/v1/task-statuses",
        ):
            with self.subTest(path=path):
                with self.assertRaises(HTTPException) as caught:
                    self._enforce(path, now=later)
                self.assertEqual(caught.exception.status_code, 426)

    def test_a_web_client_is_never_judged(self):
        later = self._expired()
        self._enforce("/time-entries/start", now=later, client=CLIENT_WEB, version=None, platform=None)

    def test_a_service_principal_is_never_judged(self):
        later = self._expired()
        bot = SimpleNamespace(id=900, organization_id=1, is_service_principal=True)
        self._enforce("/desktop/releases", now=later, user=bot)

    def test_a_legacy_client_that_cannot_show_the_screen_is_still_refused(self):
        # A 1.3.2 client sends the same headers; it simply does not know the
        # 426 body. The policy holds regardless of what the client can render.
        later = self._expired()
        with self.assertRaises(HTTPException) as caught:
            self._enforce("/time-entries/start", now=later, version="1.3.2")
        self.assertEqual(caught.exception.status_code, 426)
        self.assertIn("1.4.0", caught.exception.detail["message"])

    def test_a_client_that_updated_is_let_through_at_once(self):
        later = self._expired()
        self._enforce("/time-entries/start", now=later, version="1.4.0")

    def test_changing_the_claimed_platform_does_not_escape_a_single_platform_policy(self):
        self.create("1.4.0", mandatory=True, targets=("win32",))
        self.publish("1.4.0")
        self.evaluate("1.3.3", platform="win32", now=T0)
        later = T0 + timedelta(hours=25)
        with self.assertRaises(HTTPException):
            self._enforce("/time-entries/start", now=later, platform="linux")
        with self.assertRaises(HTTPException):
            self._enforce("/time-entries/start", now=later, platform=None)
        # Claiming the other supported platform is the one claim the server
        # cannot refute; it is subject to that platform's own policy (none).
        self._enforce("/time-entries/start", now=later, platform="darwin")

    def test_an_error_inside_the_evaluation_fails_open(self):
        later = self._expired()
        with patch.object(update_policy, "active_policies", side_effect=RuntimeError("boom")):
            self._enforce("/time-entries/start", now=later)

    def test_exempt_path_matching_is_exact_not_prefix(self):
        self.assertTrue(update_policy.is_exempt_path("/time-entries/17/stop"))
        self.assertFalse(update_policy.is_exempt_path("/time-entries/17/stop/extra"))
        self.assertFalse(update_policy.is_exempt_path("/time-entries/start"))
        self.assertTrue(update_policy.is_exempt_path("/api/v1/idle-periods/9/reassign"))
        self.assertFalse(update_policy.is_exempt_path("/desktop/releases"))


class GateOverHttpTests(_Base):
    """The gate inside the real `get_current_user`, reached over HTTP with a
    real access token: a bypass attempt that skips the desktop UI entirely."""

    def setUp(self):
        super().setUp()
        self.create("1.4.0", mandatory=True)
        self.publish("1.4.0")
        self.evaluate("1.3.3", now=T0)
        app.dependency_overrides[get_db] = lambda: self.db
        self.addCleanup(app.dependency_overrides.clear)
        patcher = patch("app.core.security.UserRepository.get_by_id", return_value=self.user)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.token = create_access_token({"user_id": self.user.id})
        self.client = TestClient(app)

    def _headers(self, version="1.3.3"):
        return {
            "Authorization": f"Bearer {self.token}",
            "User-Agent": f"Monitra/{version}",
            "X-Monitra-Platform": "win32",
            "X-Monitra-Arch": "AMD64",
        }

    def test_an_outdated_desktop_past_its_deadline_cannot_start_a_timer(self):
        with patch.object(update_policy, "now_utc", return_value=T0 + timedelta(hours=25)):
            response = self.client.post(
                "/time-entries/start", headers=self._headers(),
                json={"project_id": 1, "task_id": 1},
            )
        self.assertEqual(response.status_code, 426, response.text)
        self.assertEqual(response.json()["detail"]["code"], "desktop_update_required")

    def test_the_same_desktop_may_still_read_its_policy_and_stop_its_timer(self):
        with patch.object(update_policy, "now_utc", return_value=T0 + timedelta(hours=25)):
            policy = self.client.get("/desktop/update-policy", headers=self._headers())
            self.assertEqual(policy.status_code, 200, policy.text)
            self.assertTrue(policy.json()["blocked"])
            self.assertEqual(policy.json()["required_version"], "1.4.0")
            me = self.client.get("/auth/me", headers=self._headers())
            self.assertEqual(me.status_code, 200, me.text)
            stop = self.client.post("/time-entries/99/stop", headers=self._headers(), json={})
            self.assertNotEqual(stop.status_code, 426)

    def test_inside_the_grace_the_same_request_is_answered_normally(self):
        with patch.object(update_policy, "now_utc", return_value=T0 + timedelta(hours=2)):
            response = self.client.post(
                "/time-entries/start", headers=self._headers(),
                json={"project_id": 1, "task_id": 1},
            )
        self.assertNotEqual(response.status_code, 426, response.text)

    def test_a_web_session_of_the_same_user_is_untouched(self):
        with patch.object(update_policy, "now_utc", return_value=T0 + timedelta(hours=25)):
            response = self.client.get(
                "/auth/me",
                headers={"Authorization": f"Bearer {self.token}", "User-Agent": "Mozilla/5.0"},
            )
        self.assertEqual(response.status_code, 200)


# ═══════════════════════════════════════════════════════════════════════════
# Email: targeting and the policy wording
# ═══════════════════════════════════════════════════════════════════════════


class AnnouncementTargetingTests(_Base):

    def setUp(self):
        super().setUp()
        self.win = _persist_user(self.db, user_id=11, username="winnie", email="win@example.com")
        self.mac = _persist_user(self.db, user_id=12, username="mac", email="mac@example.com")
        self.unknown = _persist_user(self.db, user_id=13, username="newbie", email="new@example.com")
        self.db.add_all([
            DesktopClientVersion(organization_id=7, user_id=11, app_version="1.3.3", platform="win32"),
            DesktopClientVersion(organization_id=7, user_id=12, app_version="1.3.3", platform="darwin"),
        ])
        self.db.commit()

    def _recipients(self):
        rows = self.db.execute(select(EmailNotification)).scalars().all()
        return sorted(row.user_id for row in rows)

    def _announce(self, version, **policy):
        with email_settings(RELEASE_EMAIL_ENABLED=True, RELEASE_EMAIL_TEST_RECIPIENTS=""):
            return queue_release_announcements(
                self.db, SimpleNamespace(version=version, release_notes="x", release_notes_url=None),
                **policy,
            )

    def test_a_windows_only_release_reaches_windows_users_only(self):
        queued = self._announce("1.4.0", platforms=["win32"])
        self.assertEqual(len(queued), 1)
        self.assertEqual(self._recipients(), [11])

    def test_a_macos_only_release_reaches_mac_users_only(self):
        self._announce("1.4.0", platforms=["darwin"])
        self.assertEqual(self._recipients(), [12])

    def test_a_both_platform_release_reaches_everyone_including_the_unknown(self):
        self._announce("1.4.0", platforms=["win32", "darwin"])
        self.assertEqual(self._recipients(), [5, 11, 12, 13, 42])

    def test_publishing_a_windows_only_release_announces_to_windows_users(self):
        self.create("1.4.0", targets=("win32",), mandatory=True)
        with email_settings(RELEASE_EMAIL_ENABLED=True, RELEASE_EMAIL_TEST_RECIPIENTS=""):
            _published, queued = DesktopReleaseService.publish_version(self.db, "1.4.0", actor=self.admin)
        self.assertTrue(queued)
        self.assertEqual(self._recipients(), [11])
        row = self.db.execute(select(EmailNotification)).scalars().first()
        self.assertIn('"mandatory": true', row.payload)
        self.assertIn('"grace_hours": 24', row.payload)

    def test_the_same_release_is_announced_once_per_user_however_often_it_is_queued(self):
        self._announce("1.4.0", platforms=["win32"])
        self._announce("1.4.0", platforms=["win32"])
        self.assertEqual(self._recipients(), [11])

    def test_a_draft_is_never_announced(self):
        self.create("1.4.0")
        self.assertEqual(self._recipients(), [])

    def test_the_outbox_counts_for_a_version_read_only_that_version(self):
        from app.repositories.desktop_release_version import DesktopReleaseVersionRepository

        self._announce("1.4.0", platforms=["win32", "darwin"])
        self._announce("1.4.1", platforms=["win32"])
        counts = DesktopReleaseVersionRepository.email_counts(self.db, version="1.4.1")
        self.assertEqual(counts, {"pending": 1})


class AnnouncementWordingTests(unittest.TestCase):

    def _payload(self, **overrides):
        payload = {
            "version": "1.4.0", "release_notes": "- Faster startup", "release_notes_url": None,
            "platforms": ["win32"], "mandatory": False, "grace_hours": 24,
            "required_version": None, "title": None, "user_id": 42, "name": "Ada",
        }
        payload.update(overrides)
        return payload

    def test_a_mandatory_release_states_the_grace_rule_and_the_platform(self):
        with email_settings(MONITRA_APP_URL="https://staff.example.com"):
            mail = messages.build_release_email(self._payload(mandatory=True), ["ada@example.com"])
        self.assertEqual(mail.subject, "Required update: Monitra 1.4.0")
        self.assertIn("Required update", mail.html)
        self.assertIn("Windows", mail.html)
        self.assertIn("postpone it for up to 24 hours", mail.html)
        self.assertIn("cannot be used until the update is installed", mail.html)
        self.assertIn("REQUIRED UPDATE - Windows", mail.text)
        self.assertIn("24 hours", mail.text)

    def test_an_optional_release_says_it_blocks_nothing(self):
        with email_settings(MONITRA_APP_URL="https://staff.example.com"):
            mail = messages.build_release_email(self._payload(platforms=["win32", "darwin"]), ["a@b.c"])
        self.assertEqual(mail.subject, "Monitra 1.4.0 is available — what's new")
        self.assertIn("Optional update", mail.html)
        self.assertIn("Windows and macOS", mail.html)
        self.assertIn("keeps working whether or not you install it", mail.html)

    def test_a_floor_below_the_release_is_named(self):
        with email_settings(MONITRA_APP_URL="https://staff.example.com"):
            mail = messages.build_release_email(
                self._payload(mandatory=True, required_version="1.3.0", platforms=["darwin"]), ["a@b.c"],
            )
        self.assertIn("applies to Monitra 1.3.0 and older", mail.html)
        self.assertIn("macOS", mail.html)

    def test_the_download_link_is_the_web_page_never_an_artifact(self):
        with email_settings(MONITRA_APP_URL="https://staff.example.com"):
            mail = messages.build_release_email(self._payload(mandatory=True), ["a@b.c"])
        self.assertIn("https://staff.example.com/download", mail.text)
        self.assertNotIn(".exe", mail.text)


# ═══════════════════════════════════════════════════════════════════════════
# Install reports and the history view
# ═══════════════════════════════════════════════════════════════════════════


class InstallReportTests(_Base):

    def _report(self, event="evt-1", outcome="success", target="1.4.0", **overrides):
        payload = UpdateReportCreate(
            client_event_id=event, target_version=target, from_version="1.3.3",
            outcome=outcome, category=overrides.get("category"), detail=overrides.get("detail"),
            occurred_at=overrides.get("occurred_at", T0), client_time=overrides.get("client_time"),
        )
        with patch.object(update_policy, "now_utc", return_value=overrides.get("now", T0 + timedelta(minutes=5))):
            return DesktopReleaseService.record_update_report(self.db, self.user, payload, "win32", "AMD64")

    def test_a_report_is_stored_once_per_client_event(self):
        row, created = self._report()
        again, created_again = self._report()
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(row.id, again.id)
        self.assertEqual(len(self.db.execute(select(DesktopUpdateReport)).scalars().all()), 1)

    def test_an_unknown_outcome_is_refused(self):
        with self.assertRaises(ValueError):
            self._report(outcome="exploded")

    def test_the_event_is_placed_on_the_server_clock_by_its_age(self):
        # The client's clock is an hour fast: the event is 10 minutes old on
        # its clock, so it is 10 minutes old on ours.
        now = T0 + timedelta(minutes=5)
        row, _ = self._report(
            occurred_at=T0 + timedelta(hours=1) - timedelta(minutes=10),
            client_time=T0 + timedelta(hours=1), now=now,
        )
        self.assertEqual(update_policy.aware(row.occurred_at), now - timedelta(minutes=10))

    def test_a_report_never_satisfies_a_requirement_by_itself(self):
        self.create("1.4.0", mandatory=True)
        self.publish("1.4.0")
        self.evaluate("1.3.3", now=T0)
        self._report(outcome="success")
        self.assertIsNone(self.requirements()[0].satisfied_at)

    def test_the_history_counts_installs_grace_rows_and_adoption(self):
        self.create("1.4.0", mandatory=True)
        self.publish("1.4.0")
        other = _persist_user(self.db, user_id=77, username="bob")
        self.db.add_all([
            DesktopClientVersion(organization_id=7, user_id=42, app_version="1.3.3", platform="win32"),
            DesktopClientVersion(organization_id=7, user_id=77, app_version="1.4.0", platform="darwin"),
        ])
        self.db.commit()
        self.evaluate("1.3.3", now=T0)
        self.evaluate("1.3.3", now=T0, user=other, platform="darwin")
        self.evaluate("1.4.0", now=T0 + timedelta(hours=1), user=other, platform="darwin", settle=True)
        self._report(outcome="success")
        self._report(event="evt-2", outcome="failed", category="checksum-failed")
        with patch.object(update_policy, "now_utc", return_value=T0 + timedelta(hours=2)):
            detail = DesktopReleaseService.version_detail(self.db, "1.4.0")
        self.assertEqual(detail.stats.installs, {"success": 1, "failed": 1})
        self.assertEqual(detail.stats.requirements, {"pending": 1, "expired": 0, "satisfied": 1})
        self.assertEqual(detail.stats.adoption, {"on_or_above": 1, "below": 1})
        # grace (no platform known, both platforms targeted), ada (win32), bob (darwin)
        self.assertEqual(detail.stats.targeted_users, 3)
        self.assertEqual([h.action for h in detail.history], [
            ActivityLogAction.RELEASE_PUBLISHED, ActivityLogAction.RELEASE_CREATED,
        ])


# ═══════════════════════════════════════════════════════════════════════════
# The management routes: who may do what
# ═══════════════════════════════════════════════════════════════════════════


class ManagementRouteTests(_Base):

    def setUp(self):
        super().setUp()
        self.create("1.4.0", mandatory=True)
        app.dependency_overrides[get_db] = lambda: self.db
        self.addCleanup(app.dependency_overrides.clear)
        self.client = TestClient(app)

    def _as(self, user):
        app.dependency_overrides[get_current_user] = lambda: user

    def test_an_employee_cannot_read_or_change_releases(self):
        self._as(self.user)
        self.assertEqual(self.client.get("/desktop/releases/versions").status_code, 403)
        self.assertEqual(self.client.post("/desktop/releases/versions/1.4.0/publish").status_code, 403)
        self.assertEqual(self.client.post("/desktop/releases/versions/1.4.0/withdraw").status_code, 403)
        self.assertEqual(self.client.post(
            "/desktop/releases/versions", json={"version": "1.5.0"},
        ).status_code, 403)

    def test_a_release_credential_may_create_but_not_publish_or_withdraw(self):
        bot = _user(role_name="release_bot", user_id=900, username="release_bot")
        bot.is_service_principal = True
        self._as(bot)
        self.assertEqual(self.client.post(
            "/desktop/releases/versions", json={"version": "1.5.0", "target_platforms": ["win32"]},
        ).status_code, 201)
        self.assertEqual(self.client.post("/desktop/releases/versions/1.4.0/publish").status_code, 403)
        self.assertEqual(self.client.post("/desktop/releases/versions/1.4.0/withdraw").status_code, 403)

    def test_an_administrator_walks_the_whole_lifecycle(self):
        self._as(self.admin)
        listing = self.client.get("/desktop/releases/versions")
        self.assertEqual(listing.status_code, 200, listing.text)
        self.assertEqual([v["version"] for v in listing.json()["versions"]], ["1.4.0"])

        validated = self.client.post("/desktop/releases/versions/1.4.0/validate")
        self.assertEqual(validated.status_code, 200, validated.text)
        self.assertEqual(validated.json()["status"], "validated")

        edited = self.client.patch("/desktop/releases/versions/1.4.0", json={"grace_hours": 48})
        self.assertEqual(edited.status_code, 200, edited.text)
        self.assertEqual(edited.json()["grace_hours"], 48)

        published = self.client.post("/desktop/releases/versions/1.4.0/publish")
        self.assertEqual(published.status_code, 200, published.text)
        self.assertEqual(len(published.json()["published"]), 3)

        detail = self.client.get("/desktop/releases/versions/1.4.0")
        self.assertEqual(detail.status_code, 200)
        body = detail.json()
        self.assertEqual(body["status"], "published")
        self.assertEqual(body["published_by_user_id"], self.admin.id)
        self.assertEqual(
            [h["action"] for h in body["history"]],
            ["release_published", "release_updated", "release_validated", "release_created"],
        )
        self.assertNotIn("password", detail.text)

        withdrawn = self.client.post("/desktop/releases/versions/1.4.0/withdraw?reason=bad+build")
        self.assertEqual(withdrawn.status_code, 200, withdrawn.text)
        self.assertEqual(withdrawn.json()["version"]["status"], "withdrawn")
        self.assertEqual(withdrawn.json()["suspended_requirements"], 0)

    def test_an_invalid_version_record_is_refused_with_the_problems_listed(self):
        self._as(self.admin)
        response = self.client.post(
            "/desktop/releases/versions",
            json={"version": "1.5.0", "target_platforms": ["linux"]},
        )
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(response.json()["detail"]["problems"][0]["code"], "platform")
        self.assertEqual(self.client.post(
            "/desktop/releases/versions", json={"version": "1.4.0"},
        ).status_code, 409)
        self.assertEqual(self.client.post(
            "/desktop/releases/versions", json={"version": "v1.5"},
        ).status_code, 422)

    def test_the_desktop_reports_an_install_and_a_retry_is_one_row(self):
        self._as(self.user)
        body = {
            "client_event_id": "evt-9", "target_version": "1.4.0", "from_version": "1.3.3",
            "outcome": "success", "occurred_at": T0.isoformat(), "client_time": T0.isoformat(),
        }
        first = self.client.post("/desktop/update-reports", json=body, headers={"X-Monitra-Platform": "win32"})
        self.assertEqual(first.status_code, 201, first.text)
        second = self.client.post("/desktop/update-reports", json=body, headers={"X-Monitra-Platform": "win32"})
        self.assertEqual(second.status_code, 201)
        self.assertEqual(first.json()["id"], second.json()["id"])
        bad = self.client.post("/desktop/update-reports", json={**body, "outcome": "exploded"})
        self.assertEqual(bad.status_code, 422)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

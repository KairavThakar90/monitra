"""Comprehensive tests for independent mandatory desktop version enforcement.

Covers:
1. Client version exactly matches minimum (1.3.4 == 1.3.4)
2. Client version is newer than minimum (1.3.5, 1.4.0, 2.0.0)
3. Client version is older than minimum (1.3.3, 1.2.0, 0.9.0)
4. Multi-digit version components (1.10.0 > 1.9.9, 1.3.10 > 1.3.4)
5. Missing version information on desktop client
6. Malformed version information on desktop client
7. Unauthenticated requests and exempt paths
8. Authenticated requests from supported clients
9. Authenticated requests from outdated clients (HTTP 426 structured response)
10. Token refresh handling (outdated desktop client blocked, supported client allowed)
11. Protected API endpoint coverage
12. Web-dashboard and non-desktop client compatibility
13. Updating the required version from 1.3.4 to 1.3.5
14. Changing download URL without rebuilding desktop client
15. Timer and time-entry integrity (exempt stop endpoint)
16. Existing authentication and authorization tests
17. Administrative authorization and configuration validation
"""
from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from sqlalchemy import BigInteger, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.core.permissions import ROLE_PERMISSIONS
from app.core.request_context import (
    CLIENT_DESKTOP,
    CLIENT_WEB,
    RequestContext,
    reset_request_context,
    set_request_context,
)
from app.core.security import create_access_token
from app.main import app
from app.models.activity_log import ActivityLog, ActivityLogModule
from app.models.desktop_version_enforcement import (
    DEFAULT_DOWNLOAD_URL,
    DEFAULT_MESSAGE,
    DEFAULT_MINIMUM_VERSION,
    DesktopVersionEnforcement,
)
from app.models.user import User
from app.services.desktop_version_enforcement import (
    UPGRADE_REQUIRED_CODE,
    compare_versions,
    enforce,
    enforce_request,
    invalidate_policy_cache,
    parse_version_tuple,
    validate_download_url,
    DesktopVersionEnforcementService,
)

# SQLite dialect shims for testing
@compiles(JSONB, "sqlite")
def _jsonb_is_json_on_sqlite(type_, compiler, **kw):  # pragma: no cover
    return "JSON"


@compiles(BigInteger, "sqlite")
def _bigint_is_integer_on_sqlite(type_, compiler, **kw):  # pragma: no cover
    return "INTEGER"


def _sqlite_session() -> Session:
    """A real session over an in-memory SQLite DB with required tables.
    
    Strips Postgres-specific '::jsonb' defaults so SQLite can create tables.
    """
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    tables = [
        User.__table__,
        ActivityLog.__table__,
        DesktopVersionEnforcement.__table__,
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


class VersionComparisonUnitTests(unittest.TestCase):
    """Semantic version parsing and comparison tests (Items 1, 2, 3, 4, 6)."""

    def test_parses_standard_semver(self):
        self.assertEqual(parse_version_tuple("1.3.4"), (1, 3, 4))
        self.assertEqual(parse_version_tuple("v1.3.4"), (1, 3, 4))
        self.assertEqual(parse_version_tuple(" 1.3.4 "), (1, 3, 4))

    def test_parses_multi_digit_components(self):
        self.assertEqual(parse_version_tuple("1.10.2"), (1, 10, 2))
        self.assertEqual(parse_version_tuple("12.34.56"), (12, 34, 56))
        self.assertEqual(parse_version_tuple("1.3.10"), (1, 3, 10))

    def test_handles_prerelease_as_strictly_older(self):
        # 1.3.4-beta is strictly older than 1.3.4 final
        t_pre = parse_version_tuple("1.3.4-beta.1")
        t_final = parse_version_tuple("1.3.4")
        self.assertIsNotNone(t_pre)
        self.assertIsNotNone(t_final)
        self.assertLess(t_pre, t_final)

    def test_rejects_malformed_version_strings(self):
        self.assertIsNone(parse_version_tuple(None))
        self.assertIsNone(parse_version_tuple(""))
        self.assertIsNone(parse_version_tuple("abc"))
        self.assertIsNone(parse_version_tuple("1"))
        self.assertIsNone(parse_version_tuple("1.3"))
        self.assertIsNone(parse_version_tuple("1.3.4.5"))
        self.assertIsNone(parse_version_tuple("-1.3.4"))
        self.assertIsNone(parse_version_tuple("1.3.-4"))

    def test_version_equality(self):
        # Item 1: Exactly matches minimum
        self.assertEqual(compare_versions("1.3.4", "1.3.4"), 0)
        self.assertEqual(compare_versions("v1.3.4", "1.3.4"), 0)

    def test_version_newer(self):
        # Item 2: Newer than minimum
        self.assertEqual(compare_versions("1.3.5", "1.3.4"), 1)
        self.assertEqual(compare_versions("1.4.0", "1.3.4"), 1)
        self.assertEqual(compare_versions("2.0.0", "1.3.4"), 1)

    def test_version_older(self):
        # Item 3: Older than minimum
        self.assertEqual(compare_versions("1.3.3", "1.3.4"), -1)
        self.assertEqual(compare_versions("1.2.0", "1.3.4"), -1)
        self.assertEqual(compare_versions("0.9.9", "1.3.4"), -1)

    def test_multi_digit_comparison_correctness(self):
        # Item 4: Multi-digit comparison (1.10.0 > 1.9.9, 1.3.10 > 1.3.4)
        self.assertEqual(compare_versions("1.10.0", "1.9.9"), 1)
        self.assertEqual(compare_versions("1.9.9", "1.10.0"), -1)
        self.assertEqual(compare_versions("1.3.10", "1.3.4"), 1)
        self.assertEqual(compare_versions("1.3.4", "1.3.10"), -1)
        self.assertEqual(compare_versions("10.0.0", "9.9.9"), 1)

    def test_validate_download_url(self):
        self.assertTrue(validate_download_url("https://staff.peakworkos.com/download"))
        self.assertTrue(validate_download_url("https://monitra.ai/download"))
        self.assertFalse(validate_download_url("http://insecure.com/download"))
        self.assertFalse(validate_download_url("ftp://files.com"))
        self.assertFalse(validate_download_url("https://user:pass@host.com"))
        self.assertFalse(validate_download_url(""))
        self.assertFalse(validate_download_url(None))


class DesktopVersionEnforcementIntegrationTests(unittest.TestCase):
    """End-to-end integration tests using SQLite in-memory DB and TestClient."""

    def setUp(self):
        invalidate_policy_cache()
        self.db = _sqlite_session()

        # Seed admin user
        self.admin = User(
            id=1,
            organization_id=1,
            username="admin@monitra.local",
            email="admin@monitra.local",
            name="Admin User",
            role_name="administrator",
            permissions=dict.fromkeys(ROLE_PERMISSIONS.get("administrator", ()), True),
            is_active=True,
            can_login=True,
            idle_minutes=5,
            capture_frequency=600,
        )
        self.db.add(self.admin)

        # Seed regular member user
        self.member = User(
            id=2,
            organization_id=1,
            username="member@monitra.local",
            email="member@monitra.local",
            name="Regular Member",
            role_name="employee",
            permissions=dict.fromkeys(ROLE_PERMISSIONS.get("employee", ()), True),
            is_active=True,
            can_login=True,
            idle_minutes=5,
            capture_frequency=600,
        )
        self.db.add(self.member)

        # Seed initial enforcement configuration (1.3.4)
        self.enforcement = DesktopVersionEnforcement(
            id=1,
            minimum_version="1.3.4",
            download_url="https://staff.peakworkos.com/download",
            message="Your Monitra application version is outdated. Please install the latest version to continue using Monitra.",
            is_active=True,
        )
        self.db.add(self.enforcement)
        self.db.commit()

        # Mint tokens
        self.admin_token = create_access_token({"user_id": self.admin.id})
        self.member_token = create_access_token({"user_id": self.member.id})

        # App client with DB dependency override
        app.dependency_overrides[get_db] = lambda: self.db
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()
        invalidate_policy_cache()
        self.db.close()

    # ── Test Cases ─────────────────────────────────────────────────────────────

    def test_item1_client_version_exactly_matches_minimum(self):
        """Item 1: Client version exactly 1.3.4 continues working normally."""
        headers = {
            "Authorization": f"Bearer {self.member_token}",
            "User-Agent": "Monitra/1.3.4",
            "X-Monitra-Platform": "win32",
            "X-Monitra-Arch": "x86_64",
        }
        res = self.client.get("/auth/me", headers=headers)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["id"], self.member.id)

    def test_item2_client_version_newer_than_minimum(self):
        """Item 2: Client versions 1.3.5 and 2.0.0 continue working normally."""
        for newer_ver in ("1.3.5", "1.4.0", "2.0.0"):
            headers = {
                "Authorization": f"Bearer {self.member_token}",
                "User-Agent": f"Monitra/{newer_ver}",
                "X-Monitra-Platform": "darwin",
                "X-Monitra-Arch": "arm64",
            }
            res = self.client.get("/auth/me", headers=headers)
            self.assertEqual(res.status_code, 200, f"Expected 200 for version {newer_ver}")

    def test_item3_client_version_older_than_minimum(self):
        """Item 3: Client versions older than 1.3.4 are rejected with HTTP 426."""
        for older_ver in ("1.3.3", "1.2.0", "1.0.0", "0.9.1"):
            headers = {
                "Authorization": f"Bearer {self.member_token}",
                "User-Agent": f"Monitra/{older_ver}",
                "X-Monitra-Platform": "win32",
                "X-Monitra-Arch": "x86_64",
            }
            res = self.client.get("/auth/me", headers=headers)
            self.assertEqual(res.status_code, 426, f"Expected 426 for older version {older_ver}")
            body = res.json()
            self.assertIn("detail", body)
            detail = body["detail"]
            self.assertEqual(detail["code"], UPGRADE_REQUIRED_CODE)
            self.assertEqual(detail["minimum_required_version"], "1.3.4")
            self.assertEqual(detail["download_url"], "https://staff.peakworkos.com/download")
            self.assertIn("outdated", detail["message"])

    def test_item4_multi_digit_version_handling(self):
        """Item 4: Multi-digit version components (e.g. 1.10.0 > 1.3.4)."""
        headers = {
            "Authorization": f"Bearer {self.member_token}",
            "User-Agent": "Monitra/1.10.0",
            "X-Monitra-Platform": "win32",
        }
        res = self.client.get("/auth/me", headers=headers)
        self.assertEqual(res.status_code, 200)

        # 1.3.10 is newer than 1.3.4
        headers["User-Agent"] = "Monitra/1.3.10"
        res = self.client.get("/auth/me", headers=headers)
        self.assertEqual(res.status_code, 200)

    def test_item5_missing_version_information(self):
        """Item 5: Desktop client headers present but version missing -> rejected with 426."""
        # Desktop headers present without User-Agent version
        headers = {
            "Authorization": f"Bearer {self.member_token}",
            "X-Monitra-Platform": "win32",
            "X-Monitra-Arch": "x86_64",
        }
        res = self.client.get("/auth/me", headers=headers)
        self.assertEqual(res.status_code, 426)
        self.assertEqual(res.json()["detail"]["code"], UPGRADE_REQUIRED_CODE)

    def test_item6_malformed_version_information(self):
        """Item 6: Malformed version strings fail safely with HTTP 426."""
        for malformed in ("Monitra/", "Monitra/abc", "Monitra/1.0", "Monitra/invalid-version"):
            headers = {
                "Authorization": f"Bearer {self.member_token}",
                "User-Agent": malformed,
                "X-Monitra-Platform": "win32",
            }
            res = self.client.get("/auth/me", headers=headers)
            self.assertEqual(res.status_code, 426, f"Expected 426 for malformed User-Agent {malformed}")

    def test_item7_unauthenticated_and_exempt_requests(self):
        """Item 7: Unauthenticated and exempt paths are not blocked."""
        # Health check is public and exempt
        res = self.client.get("/health")
        self.assertEqual(res.status_code, 200)

        # Version enforcement public status is open
        res = self.client.get("/system/desktop-version-enforcement/status")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["minimum_version"], "1.3.4")

    def test_item8_item9_authenticated_supported_vs_outdated(self):
        """Items 8 & 9: Authenticated requests from supported vs outdated desktop clients."""
        supported_headers = {
            "Authorization": f"Bearer {self.member_token}",
            "User-Agent": "Monitra/1.3.4",
            "X-Monitra-Platform": "win32",
        }
        outdated_headers = {
            "Authorization": f"Bearer {self.member_token}",
            "User-Agent": "Monitra/1.3.3",
            "X-Monitra-Platform": "win32",
        }

        # Supported client gets 200
        res = self.client.get("/auth/me", headers=supported_headers)
        self.assertEqual(res.status_code, 200)

        # Outdated client gets 426
        res = self.client.get("/auth/me", headers=outdated_headers)
        self.assertEqual(res.status_code, 426)
        detail = res.json()["detail"]
        self.assertEqual(detail["code"], UPGRADE_REQUIRED_CODE)
        self.assertEqual(detail["minimum_required_version"], "1.3.4")

    def test_item10_token_refresh_and_session_handling(self):
        """Item 10: Outdated desktop client cannot refresh session; supported client can."""
        with patch("app.services.auth.AuthService.refresh_session") as mock_refresh:
            mock_refresh.return_value = {
                "access_token": "new-access-token",
                "refresh_token": "new-refresh-token",
                "token_type": "bearer",
            }

            # Outdated desktop client refresh attempt -> 426
            outdated_headers = {
                "User-Agent": "Monitra/1.3.2",
                "X-Monitra-Platform": "win32",
            }
            res = self.client.post(
                "/auth/refresh",
                json={"refresh_token": "dummy-token"},
                headers=outdated_headers,
            )
            self.assertEqual(res.status_code, 426)
            self.assertEqual(res.json()["detail"]["code"], UPGRADE_REQUIRED_CODE)
            mock_refresh.assert_not_called()

            # Supported desktop client refresh attempt -> 200
            supported_headers = {
                "User-Agent": "Monitra/1.3.4",
                "X-Monitra-Platform": "win32",
            }
            res = self.client.post(
                "/auth/refresh",
                json={"refresh_token": "dummy-token"},
                headers=supported_headers,
            )
            self.assertEqual(res.status_code, 200)
            mock_refresh.assert_called_once()

    def test_item11_protected_endpoint_coverage(self):
        """Item 11: Protected endpoints reject outdated desktop clients."""
        outdated_headers = {
            "Authorization": f"Bearer {self.member_token}",
            "User-Agent": "Monitra/1.3.1",
            "X-Monitra-Platform": "win32",
        }
        # /auth/me
        res = self.client.get("/auth/me", headers=outdated_headers)
        self.assertEqual(res.status_code, 426)

        # /system/maintenance-mode (protected admin endpoint)
        res = self.client.get("/system/maintenance-mode", headers=outdated_headers)
        self.assertEqual(res.status_code, 426)

    def test_item12_web_dashboard_compatibility(self):
        """Item 12: Web dashboard users and non-desktop API clients are never blocked."""
        # Browser user agent
        web_headers = {
            "Authorization": f"Bearer {self.member_token}",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        }
        res = self.client.get("/auth/me", headers=web_headers)
        self.assertEqual(res.status_code, 200)

        # Generic curl or API client without desktop headers
        api_headers = {
            "Authorization": f"Bearer {self.member_token}",
            "User-Agent": "curl/8.4.0",
        }
        res = self.client.get("/auth/me", headers=api_headers)
        self.assertEqual(res.status_code, 200)

    def test_item13_updating_required_version_from_1_3_4_to_1_3_5(self):
        """Item 13: Administrator bumps version to 1.3.5 without desktop rebuild."""
        admin_headers = {
            "Authorization": f"Bearer {self.admin_token}",
            "User-Agent": "Mozilla/5.0",
        }

        # Client on 1.3.4 works initially
        client_134_headers = {
            "Authorization": f"Bearer {self.member_token}",
            "User-Agent": "Monitra/1.3.4",
            "X-Monitra-Platform": "win32",
        }
        res = self.client.get("/auth/me", headers=client_134_headers)
        self.assertEqual(res.status_code, 200)

        # Admin updates minimum version to 1.3.5
        update_res = self.client.put(
            "/system/desktop-version-enforcement",
            json={"minimum_version": "1.3.5"},
            headers=admin_headers,
        )
        self.assertEqual(update_res.status_code, 200)
        self.assertEqual(update_res.json()["minimum_version"], "1.3.5")

        # Now client on 1.3.4 is outdated and rejected with 426
        res = self.client.get("/auth/me", headers=client_134_headers)
        self.assertEqual(res.status_code, 426)
        self.assertEqual(res.json()["detail"]["minimum_required_version"], "1.3.5")

        # Client on 1.3.5 is supported and allowed
        client_135_headers = {
            "Authorization": f"Bearer {self.member_token}",
            "User-Agent": "Monitra/1.3.5",
            "X-Monitra-Platform": "win32",
        }
        res = self.client.get("/auth/me", headers=client_135_headers)
        self.assertEqual(res.status_code, 200)

    def test_item14_changing_download_url_at_runtime(self):
        """Item 14: Administrator changes download URL; takes effect immediately."""
        admin_headers = {
            "Authorization": f"Bearer {self.admin_token}",
            "User-Agent": "Mozilla/5.0",
        }
        new_url = "https://monitra.ai/downloads/v2"

        # Admin updates download URL
        res = self.client.put(
            "/system/desktop-version-enforcement",
            json={"download_url": new_url},
            headers=admin_headers,
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["download_url"], new_url)

        # Outdated client gets 426 with the new download URL
        outdated_headers = {
            "Authorization": f"Bearer {self.member_token}",
            "User-Agent": "Monitra/1.3.0",
            "X-Monitra-Platform": "win32",
        }
        res = self.client.get("/auth/me", headers=outdated_headers)
        self.assertEqual(res.status_code, 426)
        self.assertEqual(res.json()["detail"]["download_url"], new_url)

    def test_item16_timer_and_time_entry_integrity(self):
        """Item 16: Exempt stop/heartbeat endpoints allow outdated clients to save state."""
        outdated_headers = {
            "Authorization": f"Bearer {self.member_token}",
            "User-Agent": "Monitra/1.3.2",
            "X-Monitra-Platform": "win32",
        }

        # When an exempt endpoint is hit (e.g. stop timer pattern), enforce does not raise 426
        # Even if the underlying record doesn't exist (returning 404 or 422), it is NOT 426
        res = self.client.post("/time-entries/999/stop", headers=outdated_headers)
        self.assertNotEqual(res.status_code, 426)

    def test_item17_item18_administrative_authorization_and_validation(self):
        """Items 17 & 18: Admin authorization, input validation, and audit trail."""
        # Non-admin user cannot read admin details (403)
        non_admin_headers = {
            "Authorization": f"Bearer {self.member_token}",
            "User-Agent": "Mozilla/5.0",
        }
        res = self.client.get("/system/desktop-version-enforcement", headers=non_admin_headers)
        self.assertEqual(res.status_code, 403)

        # Non-admin user cannot update settings (403)
        res = self.client.put(
            "/system/desktop-version-enforcement",
            json={"minimum_version": "1.3.5"},
            headers=non_admin_headers,
        )
        self.assertEqual(res.status_code, 403)

        # Admin with invalid semver gets 422
        admin_headers = {
            "Authorization": f"Bearer {self.admin_token}",
            "User-Agent": "Mozilla/5.0",
        }
        res = self.client.put(
            "/system/desktop-version-enforcement",
            json={"minimum_version": "not-a-semver"},
            headers=admin_headers,
        )
        self.assertEqual(res.status_code, 422)

        # Admin with invalid download URL gets 422
        res = self.client.put(
            "/system/desktop-version-enforcement",
            json={"download_url": "http://insecure-http.com"},
            headers=admin_headers,
        )
        self.assertEqual(res.status_code, 422)

        # Valid update records audit row in activity_logs
        res = self.client.put(
            "/system/desktop-version-enforcement",
            json={"minimum_version": "1.3.5", "message": "Custom update required message"},
            headers=admin_headers,
        )
        self.assertEqual(res.status_code, 200)

        # Check audit row
        audit_log = self.db.query(ActivityLog).filter(
            ActivityLog.action == "desktop_version_enforcement_updated"
        ).first()
        self.assertIsNotNone(audit_log)
        self.assertEqual(audit_log.user_id, self.admin.id)
        self.assertIn("1.3.5", audit_log.description)

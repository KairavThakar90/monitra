"""End-to-end proof that the release credential works and is contained.

Unit tests pin the logic; this drives the real HTTP API against a real database
so the deployment-shaped claims can be *shown* rather than asserted: that a
service key registers a release, that it is refused everywhere else, that
people still sign in, and that all of it holds with ``ENV=production`` — the
setting the release pipeline used to make impossible.

Usage, from backend/ (two terminals):

    # terminal 1 — the server, in production mode, against the DEV database
    ENV=production DATABASE_URL="$DATABASE_URL_DEV" \\
        python -m uvicorn app.main:app --port 8010

    # terminal 2
    python scripts/smoke_release_credential.py
    python scripts/smoke_release_credential.py --cleanup

It refuses to touch anything but the development database unless
``--i-am-sure`` is passed, and every row it creates is removed by ``--cleanup``.
The key it mints is revoked at the end of every run.

Options:
    --base-url URL   API root (default http://127.0.0.1:8010)
    --cleanup        Delete this script's account, keys and releases, then exit.
    --i-am-sure      Permit a target other than the development database.
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx
from sqlalchemy import delete, select

from app.core.database import describe_url, get_database_url, get_session_local
from app.core.security import create_access_token
from app.models.desktop_release import DesktopRelease
from app.models.service_credential import ServiceCredential
from app.models.user import User
from app.services.service_credential import ServiceCredentialService

#: Never a real version. Any row this script writes carries it, so cleanup can
#: find its work and nothing else.
SMOKE_VERSION = "99.99.99"
SMOKE_EMAIL = "release-bot-smoke@monitra.invalid"
SMOKE_KEY_NAME = "release-credential-smoke"

results: list[tuple[str, bool, str]] = []


def check(name: str, passed: bool, detail: str = "") -> bool:
    results.append((name, passed, detail))
    print(f"  {'PASS' if passed else 'FAIL'}  {name}{f'  — {detail}' if detail else ''}")
    return passed


def expect_status(name: str, response: httpx.Response, expected: int) -> bool:
    ok = response.status_code == expected
    detail = f"got {response.status_code}, expected {expected}"
    return check(name, ok, "" if ok else f"{detail}: {response.text[:160]}")


def _is_development_target() -> bool:
    from app.core.config import settings

    return bool(settings.DATABASE_URL_DEV) and get_database_url() == settings.DATABASE_URL_DEV


def _smoke_account(db) -> User | None:
    return db.scalar(select(User).where(User.email == SMOKE_EMAIL))


def _create_smoke_account(db) -> User:
    user = User(
        organization_id=1,
        username="release_bot_smoke",
        email=SMOKE_EMAIL,
        name="Release Credential Smoke",
        role_name="release_bot",
        password_hash=None,
        permissions={"manage_desktop_releases": True},
        is_active=True,
        status="active",
        capture_frequency=0,
        idle_enabled=False,
        idle_minutes=5,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


#: Every spelling of "administrator" this deployment actually stores. The
#: provider returns `administrator`, which the alias table renames to `admin`
#: at sign-in; both spellings therefore exist on real rows.
ADMIN_ROLES = ("admin", "org_admin", "administrator")


def _admins(db) -> list[User]:
    return list(db.scalars(
        select(User)
        .where(User.role_name.in_(ADMIN_ROLES), User.is_active.is_(True))
        .order_by(User.id)
    ).all())


def _a_person(db, role_names: tuple[str, ...]) -> User | None:
    """Any active human account in one of these roles, to sign in as."""
    return db.scalar(
        select(User)
        .where(User.role_name.in_(role_names), User.is_active.is_(True))
        .order_by(User.id)
    )


def cleanup(db) -> int:
    deleted = db.execute(
        delete(DesktopRelease).where(DesktopRelease.version == SMOKE_VERSION)
    ).rowcount
    user = _smoke_account(db)
    if user is not None:
        db.execute(delete(ServiceCredential).where(ServiceCredential.user_id == user.id))
        db.delete(user)
    db.commit()
    print(f"Removed {deleted} smoke release row(s) and the smoke account.")
    return 0


def run(db, base_url: str, use_existing: bool = False) -> int:
    """Drive the checks. `use_existing` is the production shape of this run.

    Against a real deployment there must be no second release account and no
    disposable key: the credential under test is the one CI actually holds, it
    is read from MONITRA_RELEASE_CREDENTIAL, and this script must not revoke it
    on the way out. What it registers is still removed before it returns, so a
    verified deployment is left exactly as it was found.
    """
    client = httpx.Client(base_url=base_url, timeout=30.0)

    print(f"\nDatabase: {describe_url(get_database_url())}")
    print(f"API:      {base_url}\n")

    # ── The deployment is in production mode ──────────────────────────────
    print("Deployment mode")
    root = client.get("/")
    environment = root.json().get("environment") if root.status_code == 200 else None
    check(
        "ENV=production",
        environment == "production",
        "" if environment == "production" else f"server reports environment={environment!r}",
    )
    dev_login = client.post(
        "/auth/dev-login", json={"email": SMOKE_EMAIL, "password": "irrelevant"}
    )
    expect_status("/auth/dev-login unavailable in production", dev_login, 404)

    # ── The credential under test ─────────────────────────────────────────
    print("\nCredential")
    if use_existing:
        key = os.environ.get("MONITRA_RELEASE_CREDENTIAL", "").strip()
        if not key:
            print(
                "--use-existing needs the credential in MONITRA_RELEASE_CREDENTIAL.",
                file=sys.stderr,
            )
            return 2
        row = None
        check(
            "using the credential CI holds (nothing minted)", True,
            "read from MONITRA_RELEASE_CREDENTIAL",
        )
    else:
        user = _smoke_account(db) or _create_smoke_account(db)
        # Any previous run's keys are retired before this one mints its own.
        for stale in db.scalars(
            select(ServiceCredential).where(
                ServiceCredential.user_id == user.id,
                ServiceCredential.revoked_at.is_(None),
            )
        ).all():
            ServiceCredentialService.revoke(db, stale.key_id)
        key, row = ServiceCredentialService.issue(db, user, name=SMOKE_KEY_NAME)
        check("a key was issued to the release account", True, f"key_id={row.key_id}")
    auth = {"Authorization": f"Bearer {key}"}

    # ── What it is for ────────────────────────────────────────────────────
    print("\nThe release credential can do its job")
    created = client.post(
        "/desktop/releases",
        headers=auth,
        json={
            "version": SMOKE_VERSION,
            "platform": "win32",
            "download_url": f"https://example.invalid/Monitra-Setup-{SMOKE_VERSION}.exe",
            "sha256": "c" * 64,
            "file_size": 1234,
            "release_notes": "Smoke test row. Not a real release.",
        },
    )
    if created.status_code == 409:
        # A previous run left the row behind; re-registering is a no-op, so
        # find it and carry on rather than reporting a failure that is not one.
        listing = client.get(f"/desktop/releases?status=draft", headers=auth)
        existing = [
            r for r in listing.json().get("releases", [])
            if r["version"] == SMOKE_VERSION
        ]
        release_id = existing[0]["id"] if existing else None
        check("register a desktop release", release_id is not None, "already registered")
    else:
        expect_status("register a desktop release", created, 201)
        release_id = created.json().get("id") if created.status_code == 201 else None
        if created.status_code == 201:
            check(
                "it is registered as a draft, not published",
                created.json().get("status") == "draft",
                f"status={created.json().get('status')}",
            )

    if release_id:
        amended = client.patch(
            f"/desktop/releases/{release_id}",
            headers=auth,
            json={"release_notes": "Smoke test row, amended."},
        )
        expect_status("update a desktop release", amended, 200)

    listing = client.get("/desktop/releases", headers=auth)
    expect_status("read the release list (drafts included)", listing, 200)
    if listing.status_code == 200:
        check(
            "the registered row is readable back",
            any(r["version"] == SMOKE_VERSION for r in listing.json()["releases"]),
        )

    identity = client.get("/auth/me", headers=auth)
    if expect_status("read its own identity", identity, 200):
        granted = {p for p, on in (identity.json().get("permissions") or {}).items() if on}
        check(
            "it holds exactly manage_desktop_releases",
            granted == {"manage_desktop_releases"},
            f"holds {sorted(granted)}",
        )

    # ── And nothing else ──────────────────────────────────────────────────
    print("\nThe release credential is refused everywhere else")
    for label, path in (
        ("member directory", "/api/v1/members"),
        ("employee list", "/employees"),
        ("fleet client versions", "/desktop/client-versions"),
    ):
        expect_status(f"403 on {label}", client.get(path, headers=auth), 403)
    expect_status(
        "403 on the interactive-session handoff",
        client.post("/auth/sso/handoff", headers=auth),
        403,
    )

    # ── People are unaffected ─────────────────────────────────────────────
    print("\nOrdinary authentication still works")
    admins = _admins(db)
    # Deliberately the admin who actually *holds* the permission. Picking any
    # admin conflates two different questions: "did this change break release
    # management for people?" and "is that particular account's stored
    # permission row up to date?". Only the first is this script's business.
    release_manager = next(
        (u for u in admins if (u.permissions or {}) and
         isinstance(u.permissions, dict) and
         u.permissions.get("manage_desktop_releases")),
        None,
    )
    if release_manager is None:
        check(
            "normal admin authentication", False,
            "no admin account in this database holds manage_desktop_releases",
        )
    else:
        admin_auth = {
            "Authorization": f"Bearer {create_access_token({'user_id': release_manager.id})}"
        }
        expect_status(
            "an admin can read the release list",
            client.get("/desktop/releases", headers=admin_auth), 200,
        )
        expect_status(
            "an admin can still open a web session",
            client.post("/auth/sso/handoff", headers=admin_auth),
            200,
        )

    # Not a failure of this arrangement, but worth surfacing: an admin-role
    # account whose stored permissions predate `manage_desktop_releases` cannot
    # publish a release until its next sign-in refreshes them from the role
    # table. Reported, never repaired -- rewriting somebody's permission row is
    # not something a verification script gets to do.
    stale = [
        u for u in admins
        if u is not release_manager
        and not (isinstance(u.permissions, dict)
                 and u.permissions.get("manage_desktop_releases"))
    ]
    if stale:
        print(
            f"  NOTE  {len(stale)} admin-role account(s) carry stale permission "
            "rows and cannot manage releases until their next sign-in:"
        )
        for u in stale:
            shape = type(u.permissions).__name__
            size = len(u.permissions) if isinstance(u.permissions, (dict, list)) else 0
            print(f"          id={u.id} role={u.role_name} permissions={shape}({size})")

    employee = _a_person(db, ("employee",))
    if employee is None:
        check("normal employee authentication", False, "no employee account in this database")
    else:
        staff_auth = {"Authorization": f"Bearer {create_access_token({'user_id': employee.id})}"}
        expect_status("an employee is authenticated", client.get("/auth/me", headers=staff_auth), 200)
        expect_status(
            "an employee cannot manage releases",
            client.get("/desktop/releases", headers=staff_auth),
            403,
        )

    # ── Revocation actually closes the door ───────────────────────────────
    print("\nRevocation")
    if row is not None:
        ServiceCredentialService.revoke(db, row.key_id)
        expect_status(
            "a revoked key is refused", client.get("/desktop/releases", headers=auth), 401
        )
    else:
        # The credential under test is the live one. Revoking it here would
        # break the next release to prove a property the unit tests already
        # pin, so this run does not touch it.
        print("  SKIP  revoking the live credential (it is the one CI holds)")
    expect_status(
        "a forged key is refused",
        client.get("/desktop/releases", headers={"Authorization": "Bearer msk_deadbeef_forged"}),
        401,
    )

    if use_existing:
        # Leave the deployment as it was found: the row registered above was
        # written only to prove the credential can write.
        removed = db.execute(
            delete(DesktopRelease).where(DesktopRelease.version == SMOKE_VERSION)
        ).rowcount
        db.commit()
        check(
            "the verification release row was removed again",
            db.scalar(
                select(DesktopRelease).where(DesktopRelease.version == SMOKE_VERSION)
            ) is None,
            f"deleted {removed}",
        )

    client.close()

    failed = [name for name, passed, _ in results if not passed]
    print("\n" + "-" * 66)
    print(f"{len(results) - len(failed)}/{len(results)} checks passed.")
    if failed:
        print("FAILED:")
        for name in failed:
            print(f"  - {name}")
        return 1
    print("PASS — the release credential registers releases and reaches nothing else.")
    if not use_existing:
        print(
            f"\nThe smoke release row ({SMOKE_VERSION}) is still in the database. "
            "Remove it with --cleanup."
        )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8010")
    parser.add_argument("--cleanup", action="store_true")
    parser.add_argument("--i-am-sure", action="store_true")
    parser.add_argument(
        "--use-existing", action="store_true",
        help="Test the credential in MONITRA_RELEASE_CREDENTIAL instead of "
             "minting a disposable one. Creates no account, revokes nothing, "
             "and removes the release row it registers. This is the mode to "
             "use against a real deployment.",
    )
    args = parser.parse_args()

    if not _is_development_target() and not args.i_am_sure:
        print(
            f"Target {describe_url(get_database_url())} is not the development "
            "database. This script writes rows; re-run with --i-am-sure if that "
            "is really what you want.",
            file=sys.stderr,
        )
        return 2

    db = get_session_local()()
    try:
        if args.cleanup:
            return cleanup(db)
        return run(db, args.base_url.rstrip("/"), use_existing=args.use_existing)
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())

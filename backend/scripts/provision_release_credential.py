"""Provision, rotate, list and revoke the release pipeline's credential.

This is the only supported way to create the `release_bot` service account and
its API key. It is run by a person, on purpose, against a named database — it
is not part of any deployment or CI job.

Usage, from backend/::

    # See what exists. Safe, read-only, says which database it read.
    python scripts/provision_release_credential.py show

    # Create the account (if absent) and mint a key. Prints the key ONCE.
    python scripts/provision_release_credential.py issue \\
        --email release-bot@example.com --name github-actions-release

    # Replace the key: mint the new one, then revoke the old.
    python scripts/provision_release_credential.py rotate --name github-actions-release

    # Retire one key by its public id.
    python scripts/provision_release_credential.py revoke --key-id 0a1b2c3d4e5f

Targeting a database
--------------------
The same resolver the application uses picks the target (see
`app.core.database.get_database_url`), and every run prints which host and
database it resolved before it does anything. To act on production from a
machine whose ENV says development, export DATABASE_URL for that one command —
an explicit export outranks everything else. Any command that writes also
requires `--i-am-sure` when the target is not the development database.

About the account
-----------------
The account is created with **no password hash at all**. It cannot sign in
through `/auth/login`, `/auth/dev-login`, or single sign-on; the API key is its
only credential. Its role is `release_bot`, whose entire authority is
`manage_desktop_releases`.

About the key
-------------
It is shown once, on stdout, and stored only as a SHA-256. Nothing can read it
back. Paste it straight into the GitHub repository secret
`MONITRA_RELEASE_CREDENTIAL`; if it is lost, rotate.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select

from app.core.database import describe_url, get_database_url, get_session_local
from app.core.permissions import ROLE_PERMISSIONS, SERVICE_ROLE_NAMES
from app.models.service_credential import ServiceCredential
from app.models.user import User
from app.services.service_credential import ServiceCredentialService

#: The role the release pipeline authenticates as. One permission, and the
#: tests in tests/test_desktop_release.py fail if that ever changes.
RELEASE_ROLE = "release_bot"

DEFAULT_KEY_NAME = "github-actions-release"


def _resolved_target() -> str:
    return describe_url(get_database_url())


def _is_development_target() -> bool:
    from app.core.config import settings

    dev_url = settings.DATABASE_URL_DEV
    return bool(dev_url) and get_database_url() == dev_url


def _guard_write(args) -> None:
    """A write to anything but the development database must be deliberate."""
    if _is_development_target() or getattr(args, "i_am_sure", False):
        return
    print(
        f"\nTarget {_resolved_target()} is not the development database.\n"
        "Re-run with --i-am-sure if that is what you intend.",
        file=sys.stderr,
    )
    raise SystemExit(2)


def _find_release_account(db) -> User | None:
    return db.scalar(select(User).where(User.role_name == RELEASE_ROLE))


def _create_release_account(db, email: str, username: str, organization_id: int) -> User:
    """The service account: one role, one permission, and no password.

    `permissions` is written from ROLE_PERMISSIONS rather than by hand, so this
    account can never be provisioned with authority the role does not define.
    """
    user = User(
        organization_id=organization_id,
        username=username,
        email=email,
        name="Monitra Release Pipeline",
        designation="Automation",
        role_name=RELEASE_ROLE,
        # No password hash. This account has no interactive sign-in at all.
        password_hash=None,
        permissions={p: True for p in ROLE_PERMISSIONS[RELEASE_ROLE]},
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


def _print_account(db, user: User) -> None:
    print(f"  account   id={user.id} email={user.email} role={user.role_name}")
    print(f"  active    is_active={user.is_active} status={user.status}")
    print(f"  password  {'SET (unexpected)' if user.password_hash else 'none (correct)'}")
    granted = sorted(p for p, on in (user.permissions or {}).items() if on)
    print(f"  grants    {', '.join(granted) or '(none)'}")

    rows = db.scalars(
        select(ServiceCredential)
        .where(ServiceCredential.user_id == user.id)
        .order_by(ServiceCredential.id)
    ).all()
    if not rows:
        print("  keys      (none issued)")
        return
    print("  keys:")
    for row in rows:
        state = "revoked" if row.revoked_at else "live"
        if row.expires_at and row.expires_at.replace(tzinfo=timezone.utc) <= datetime.now(timezone.utc):
            state = "expired"
        print(
            f"    key_id={row.key_id}  {state:8} name={row.name}  "
            f"issued={row.created_at:%Y-%m-%d}  last_used="
            f"{row.last_used_at or 'never'}"
        )


def _emit_key(token: str) -> None:
    print("\n" + "=" * 70)
    print("The key below is shown ONCE and is not recoverable.")
    print("Paste it into the GitHub secret MONITRA_RELEASE_CREDENTIAL, then clear")
    print("your terminal scrollback. Do not commit it or paste it into chat.")
    print("=" * 70)
    print(f"\n{token}\n")
    print("=" * 70)


def cmd_show(db, args) -> int:
    user = _find_release_account(db)
    if user is None:
        print(f"No account with role {RELEASE_ROLE!r} exists in {_resolved_target()}.")
        return 0
    print(f"Release account in {_resolved_target()}:")
    _print_account(db, user)
    return 0


def cmd_issue(db, args) -> int:
    _guard_write(args)
    user = _find_release_account(db)
    if user is None:
        if not args.email:
            print(
                "No release account exists yet; pass --email to create one.",
                file=sys.stderr,
            )
            return 2
        user = _create_release_account(
            db, args.email, args.username or "release_bot", args.organization_id
        )
        print(f"Created release account id={user.id} ({user.email}).")
    else:
        print(f"Using existing release account id={user.id} ({user.email}).")

    token, row = ServiceCredentialService.issue(db, user, name=args.name)
    print(f"Issued credential key_id={row.key_id} name={row.name}.")
    _emit_key(token)
    return 0


def cmd_rotate(db, args) -> int:
    _guard_write(args)
    user = _find_release_account(db)
    if user is None:
        print("No release account exists; run `issue` first.", file=sys.stderr)
        return 2

    live = db.scalars(
        select(ServiceCredential).where(
            ServiceCredential.user_id == user.id,
            ServiceCredential.revoked_at.is_(None),
        )
    ).all()

    # New key first, old keys second. The other order leaves a window in which
    # a release cannot be registered at all.
    token, row = ServiceCredentialService.issue(db, user, name=args.name)
    print(f"Issued replacement key_id={row.key_id}.")
    _emit_key(token)

    print("Update the GitHub secret with the key above BEFORE continuing.")
    if not args.revoke_old:
        print(
            "Old keys were left live. Re-run with --revoke-old (or use "
            "`revoke --key-id ...`) once the secret is updated:"
        )
        for old in live:
            print(f"    key_id={old.key_id} name={old.name}")
        return 0

    for old in live:
        ServiceCredentialService.revoke(db, old.key_id)
        print(f"Revoked key_id={old.key_id}.")
    return 0


def cmd_revoke(db, args) -> int:
    _guard_write(args)
    if ServiceCredentialService.revoke(db, args.key_id):
        print(f"Revoked key_id={args.key_id}.")
    else:
        print(f"No live credential with key_id={args.key_id}; nothing to do.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    show = sub.add_parser("show", help="Print the account and its keys. Read-only.")
    show.set_defaults(func=cmd_show)

    issue = sub.add_parser("issue", help="Create the account if needed and mint a key.")
    issue.add_argument("--email", help="Email for the account, if it must be created.")
    issue.add_argument("--username", default=None)
    issue.add_argument("--organization-id", type=int, default=1)
    issue.add_argument("--name", default=DEFAULT_KEY_NAME, help="Label for the key.")
    issue.add_argument("--i-am-sure", action="store_true")
    issue.set_defaults(func=cmd_issue)

    rotate = sub.add_parser("rotate", help="Mint a replacement key.")
    rotate.add_argument("--name", default=DEFAULT_KEY_NAME)
    rotate.add_argument(
        "--revoke-old", action="store_true",
        help="Revoke the previous keys in the same run. Only do this once the "
             "new key is already in the GitHub secret.",
    )
    rotate.add_argument("--i-am-sure", action="store_true")
    rotate.set_defaults(func=cmd_rotate)

    revoke = sub.add_parser("revoke", help="Retire one key by its public id.")
    revoke.add_argument("--key-id", required=True)
    revoke.add_argument("--i-am-sure", action="store_true")
    revoke.set_defaults(func=cmd_revoke)

    args = parser.parse_args()

    print(f"Database target: {_resolved_target()}")
    if RELEASE_ROLE not in SERVICE_ROLE_NAMES:
        print(
            f"{RELEASE_ROLE!r} is not in SERVICE_ROLE_NAMES; a key issued here "
            "would be refused at authentication time.",
            file=sys.stderr,
        )
        return 2

    session = get_session_local()()
    try:
        return args.func(session, args)
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())

"""Service credentials: how a machine authenticates to this API.

One caller needs this today — the GitHub Actions job that registers a desktop
release — and the shape of the problem is the reason this module exists at all.
CI has to authenticate as *something*, and every option that already existed
was wrong for it:

* the provider sign-in (`/auth/login`) is a person's Hubstaff password;
* `/auth/dev-login` is 404 in production, by design, and keeping production in
  development mode so a build step can sign in is a bad trade;
* an access token is valid for thirty minutes, so one pasted into a repository
  secret is dead long before the next release.

So a service credential is a long-lived random key, stored only as a hash,
bound to an account whose role is in `SERVICE_ROLE_NAMES`. It is presented the
same way an access token is — `Authorization: Bearer <key>` — so nothing
downstream of authentication has to know or care which kind of principal it is
serving. `require_permission` keeps working unchanged, and that is deliberate:
this adds a way to *become* a principal, and changes nothing about what any
principal is allowed to do.

Key format::

    msk_<key_id>_<secret>

`key_id` is public: it identifies the row, it is what lookups are keyed on, and
it is safe in a log line. `secret` is never stored anywhere, in any form, and
is shown exactly once — at the moment the key is minted.
"""
from __future__ import annotations

import hmac
import logging
import secrets
from datetime import datetime, timezone
from typing import Optional, Tuple

from fastapi import HTTPException, status
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.permissions import ROLE_PERMISSIONS, SERVICE_ROLE_NAMES
from app.core.security import hash_token
from app.models.service_credential import ServiceCredential
from app.models.user import User
from app.repositories.user import UserRepository

logger = logging.getLogger("uvicorn.error")

#: Marks a bearer token as a service credential rather than a JWT. A JWT never
#: starts with this, so `get_current_user` can route the two apart without
#: attempting to decode one as the other.
SERVICE_KEY_PREFIX = "msk_"

#: Bytes of randomness behind the secret half. 32 bytes is 256 bits; this key
#: does not expire, so it is sized for that.
_SECRET_BYTES = 32
_KEY_ID_BYTES = 6

#: One 401 for every reason a key can be refused. Which reason it was goes to
#: the log, never to the caller: an attacker holding a guess must not be able
#: to learn whether the key exists, whether it is revoked, or whose it is.
_REFUSED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Not authenticated",
)


def looks_like_service_key(token: str) -> bool:
    """Whether this bearer token is a service credential.

    Cheap and total: it decides which verifier runs, not whether the caller is
    authentic. A forged prefix simply gets refused by the service path instead
    of the JWT path.
    """
    return isinstance(token, str) and token.startswith(SERVICE_KEY_PREFIX)


def _split(token: str) -> Optional[str]:
    """The `key_id` inside a well-formed key, or None."""
    body = token[len(SERVICE_KEY_PREFIX):]
    key_id, separator, secret = body.partition("_")
    if not separator or not key_id or not secret:
        return None
    return key_id


class ServiceCredentialService:

    @staticmethod
    def issue(
        db: Session,
        user: User,
        name: str,
        expires_at: Optional[datetime] = None,
    ) -> Tuple[str, ServiceCredential]:
        """Mint a key for a service account. Returns (key, row).

        The plaintext key is returned to the caller and never persisted. There
        is no code path anywhere in this application that can read it back: a
        lost key is rotated, not recovered.

        The account's role is checked here as well as at authentication time.
        Refusing to *create* a key for an administrator is what makes the
        provisioning script safe to hand to someone; refusing to *accept* one
        is what makes the system safe if a row is ever written another way.
        """
        if user.role_name not in SERVICE_ROLE_NAMES:
            raise ValueError(
                f"{user.role_name!r} is not a service role. A service "
                f"credential may only be issued to: "
                f"{', '.join(sorted(SERVICE_ROLE_NAMES))}."
            )

        key_id = secrets.token_hex(_KEY_ID_BYTES)
        token = f"{SERVICE_KEY_PREFIX}{key_id}_{secrets.token_urlsafe(_SECRET_BYTES)}"
        row = ServiceCredential(
            user_id=user.id,
            name=name,
            key_id=key_id,
            token_hash=hash_token(token),
            expires_at=expires_at,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        logger.info(
            "SERVICE_CREDENTIAL_ISSUED: key_id=%s user=%s role=%s name=%s",
            key_id, user.id, user.role_name, name,
        )
        return token, row

    @staticmethod
    def revoke(db: Session, key_id: str) -> bool:
        """Retire one key. Returns whether a live key was actually retired.

        Idempotent, and it never deletes: the row is the record of what was
        trusted and when, and rotation should leave that history intact.
        """
        now = datetime.now(timezone.utc)
        revoked = db.execute(
            update(ServiceCredential)
            .where(
                ServiceCredential.key_id == key_id,
                ServiceCredential.revoked_at.is_(None),
            )
            .values(revoked_at=now)
            .returning(ServiceCredential.id)
        ).scalar()
        db.commit()
        if revoked:
            logger.info("SERVICE_CREDENTIAL_REVOKED: key_id=%s", key_id)
        return revoked is not None

    @staticmethod
    def authenticate(db: Session, token: str) -> User:
        """The principal behind a service key, or 401.

        Every check here fails closed, and the order matters less than the fact
        that all of them run before anything is returned.
        """
        key_id = _split(token)
        if not key_id:
            logger.warning("SERVICE_CREDENTIAL_REFUSED: malformed key")
            raise _REFUSED

        row = db.scalar(
            select(ServiceCredential).where(ServiceCredential.key_id == key_id)
        )
        if row is None:
            logger.warning("SERVICE_CREDENTIAL_REFUSED: unknown key_id=%s", key_id)
            raise _REFUSED

        # Constant-time: the stored value is a hash, but comparing it with `==`
        # still leaks position-of-first-difference to a caller who can time it.
        if not hmac.compare_digest(row.token_hash, hash_token(token)):
            logger.warning("SERVICE_CREDENTIAL_REFUSED: bad secret key_id=%s", key_id)
            raise _REFUSED

        if row.revoked_at is not None:
            logger.warning("SERVICE_CREDENTIAL_REFUSED: revoked key_id=%s", key_id)
            raise _REFUSED

        now = datetime.now(timezone.utc)
        if row.expires_at is not None and _as_utc(row.expires_at) <= now:
            logger.warning("SERVICE_CREDENTIAL_REFUSED: expired key_id=%s", key_id)
            raise _REFUSED

        user = UserRepository.get_by_id(db, row.user_id)
        if user is None or not user.is_active or user.status != "active":
            logger.warning(
                "SERVICE_CREDENTIAL_REFUSED: account unavailable key_id=%s", key_id
            )
            raise _REFUSED

        # The hard cap. A key is only ever as strong as the account behind it,
        # and this is what stops that account from being an administrator --
        # even if somebody inserts the row by hand, and even if the account's
        # role is changed after the key was minted.
        if user.role_name not in SERVICE_ROLE_NAMES:
            logger.warning(
                "SERVICE_CREDENTIAL_REFUSED: role %r is not a service role "
                "(key_id=%s, user=%s)",
                user.role_name, key_id, user.id,
            )
            raise _REFUSED

        # Detach first, and only then write anything. Two reasons, and the
        # order below is load-bearing for both:
        #
        # * Nothing set on this instance may reach the database. The principal
        #   is shaped in memory just below, and a later `commit()` anywhere in
        #   the request must not persist that shaping onto the user row.
        # * `commit()` expires every instance still in the session. Expunging
        #   *after* the audit write left a detached instance with expired
        #   attributes, and the next attribute read raised
        #   DetachedInstanceError -- a 500 on every authenticated call.
        role_name = user.role_name
        if user in db:
            db.expunge(user)

        # `permissions` is re-derived from the role table rather than read from
        # the stored column: a service principal's authority must come from the
        # role definition that tests pin, not from a JSONB value that some
        # other code path could have widened.
        user.permissions = {
            permission: True for permission in ROLE_PERMISSIONS[role_name]
        }
        # Marks this principal as a machine. Endpoints that mint interactive
        # sessions refuse it -- see `forbid_service_principal`.
        user.is_service_principal = True

        ServiceCredentialService._touch(db, row.id, now)
        return user

    @staticmethod
    def _touch(db: Session, credential_id: int, now: datetime) -> None:
        """Record the use. Never fails the request if it cannot."""
        try:
            db.execute(
                update(ServiceCredential)
                .where(ServiceCredential.id == credential_id)
                .values(last_used_at=now)
            )
            db.commit()
        except Exception:  # noqa: BLE001 - an audit write must not deny service
            db.rollback()
            logger.warning(
                "SERVICE_CREDENTIAL_TOUCH_FAILED: id=%s", credential_id, exc_info=True
            )


def _as_utc(value: datetime) -> datetime:
    """A stored timestamp made comparable with `now()`.

    SQLite hands back naive datetimes for TIMESTAMP columns even when Postgres
    would not, and comparing a naive one raises rather than returning False --
    which would turn "is this key expired?" into a 500.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value

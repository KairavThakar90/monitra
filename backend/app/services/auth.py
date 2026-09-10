from sqlalchemy.orm import Session
from sqlalchemy import text, select
from sqlalchemy.exc import IntegrityError
from datetime import datetime, timedelta, timezone
import logging
import re
import secrets
import uuid
import httpx
from app.repositories.user import UserRepository
from app.schemas.user import DEFAULT_LOGIN_FOR, LoginFor, UserCreate, UserUpdate, UserRead
from app.schemas.token import TokenPair
from app.core.security import create_access_token, generate_refresh_token, hash_token, verify_password
from app.core.config import settings
from app.models.user import User
from app.models.refresh_token import RefreshToken
from app.models.sso_handoff_token import SsoHandoffToken
from app.core.permissions import ROLE_PERMISSIONS, resolve_role_alias
from fastapi import HTTPException
from app.services.external_auth_service import ExternalAuthService

logger = logging.getLogger("uvicorn.error")

#: Marks a token as one this backend minted for the desktop → web handoff.
#: A provider JWT never starts with it, so `/auth/sso/token` can tell the two
#: apart without asking the provider about a token it never issued.
HANDOFF_TOKEN_PREFIX = "mh_"


def _provider_block_diagnostics(response: httpx.Response) -> tuple[dict[str, str], str | None]:
    """Return non-sensitive details that identify an upstream WAF block."""
    relevant_headers = (
        "server",
        "cf-ray",
        "x-sucuri-id",
        "x-sucuri-cache",
        "x-pantheon-styx-hostname",
        "x-request-id",
        "x-cache",
    )
    headers = {
        name: value
        for name in relevant_headers
        if (value := response.headers.get(name))
    }
    body = response.text if isinstance(response.text, str) else ""
    title_match = re.search(r"<title[^>]*>\s*(.*?)\s*</title>", body, flags=re.IGNORECASE | re.DOTALL)
    title = re.sub(r"\s+", " ", title_match.group(1)).strip()[:160] if title_match else None
    return headers, title

def _normalize_role(value) -> str | None:
    """A provider role name reduced to the form ROLE_PERMISSIONS is keyed on.

    Trimmed and lower-cased, so "HR", " hr " and "hr" are one role. Non-strings
    are rejected rather than coerced -- a role is a name, not whatever `str()`
    makes of an object.
    """
    if isinstance(value, str) and (normalized := value.strip().lower()):
        return normalized
    return None


def _provider_roles(wp_user: dict) -> list[str]:
    """The normalised contents of the provider's `user.roles` array."""
    roles = wp_user.get("roles")
    if not isinstance(roles, list):
        return []
    return [normalized for entry in roles if (normalized := _normalize_role(entry))]


def _resolve_provider_role(wp_user: dict, permission_schema: dict) -> tuple[str | None, str, list[str]]:
    """Decide the Monitra role for a provider identity.

    `user.roles` is the source of truth. When the provider names any role
    there, the account's role is one of those roles or the login fails --
    `permission_schema.name` is never consulted as a second opinion.

    That precedence is the whole point, not a detail. The two fields can
    disagree: an HR account came back as roles: ["hr"] alongside
    permission_schema.name: "employee". Preferring the schema name, or falling
    through to it when the roles array named nothing supported, silently signed
    that user in as an employee -- and because login_exchange writes the
    resolved role back with `user.role_name = role_name`, it also rewrote their
    stored role in the database. The user was demoted by logging in, and each
    later login re-applied it. Granting a role the provider did not give is
    exactly the kind of fabricated data this codebase refuses; a login that
    cannot be resolved honestly must fail loudly instead.

    `permission_schema.name` remains the fallback for responses that carry no
    `roles` array at all, which is how this resolved before and must keep
    working.

    Provider slugs are mapped through resolve_role_alias first, so a role the
    provider spells differently (WordPress's `administrator` for Monitra's
    `admin`) resolves to its Monitra name and is stored as that name. Aliasing
    only renames; the resolved role is still required to exist in
    ROLE_PERMISSIONS, so no authority is invented.

    Returns the Monitra role (or None), the field it came from, and the raw
    provider candidates, so the caller can log precisely why a login was
    refused.
    """
    provider_roles = _provider_roles(wp_user)
    if provider_roles:
        return (
            next((aliased for role in provider_roles
                  if (aliased := resolve_role_alias(role)) in ROLE_PERMISSIONS), None),
            "user.roles",
            provider_roles,
        )

    schema_role = _normalize_role(permission_schema.get("name"))
    if schema_role:
        aliased = resolve_role_alias(schema_role)
        return (
            aliased if aliased in ROLE_PERMISSIONS else None,
            "permission_schema.name",
            [schema_role],
        )

    return None, "none", []


#: What the client is told when a session can no longer be renewed. Deliberately
#: one message for "expired", "revoked" and "unknown": the client's only correct
#: response to any of them is to clear local state and ask for credentials, and
#: telling an unauthenticated caller which of the three it hit only helps someone
#: probing for valid token hashes.
SESSION_ENDED_DETAIL = "Your login session has expired. Please sign in again to continue."


def _as_utc(value: datetime | None) -> datetime | None:
    """A timestamp read back from the database, as an aware UTC datetime.

    Drivers differ on whether they return tz-aware values for TIMESTAMP WITH
    TIME ZONE (SQLite, used by the tests, returns naive). Comparing a naive
    value against `datetime.now(timezone.utc)` raises, which would turn an
    ordinary refresh into a 500, so every read is normalised here rather than
    at each comparison.
    """
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


class AuthService:
    @staticmethod
    def _access_claims(user: User) -> dict:
        """The claim set every Monitra access token carries."""
        return {
            "user_id": user.id,
            "organization_id": user.organization_id,
            "role_name": user.role_name,
            "permissions": user.permissions,
            "exp": datetime.now(timezone.utc) + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
        }

    @staticmethod
    def _persist_session_token(
        db: Session,
        user_id: int,
        session_started_at: datetime | None = None,
    ) -> tuple[str, datetime, datetime]:
        """Mint and store one refresh token, returning (plaintext, started, expires).

        `session_started_at` is passed only when rotating an existing session.
        Carrying it forward -- rather than restarting it -- is what keeps the
        session window a hard ceiling: expiry is always measured from the
        original sign-in, so refreshing forever cannot outlive it.
        """
        started_at = session_started_at or datetime.now(timezone.utc)
        expires_at = started_at + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)
        refresh_token_plain = generate_refresh_token()
        try:
            db.add(RefreshToken(
                user_id=user_id,
                token_hash=hash_token(refresh_token_plain),
                session_started_at=started_at,
                expires_at=expires_at,
            ))
            db.commit()
        except Exception:
            db.rollback()
            logger.exception("Failed to persist authentication session for local user %s", user_id)
            raise HTTPException(status_code=500, detail="Unable to create authentication session")
        return refresh_token_plain, started_at, expires_at

    @staticmethod
    def refresh_session(db: Session, refresh_token: str) -> TokenPair:
        """Renew the access token for a still-valid session.

        The refresh token is single-use: the presented row is revoked and a new
        one issued in its place, so a leaked token stops working as soon as the
        real client refreshes. The session window is copied, never extended.
        """
        row = db.scalar(
            select(RefreshToken).where(RefreshToken.token_hash == hash_token(refresh_token))
        )
        if row is None or row.revoked_at is not None:
            logger.info("AUTH_REFRESH_REJECTED: presented refresh token is unknown or revoked")
            raise HTTPException(status_code=401, detail=SESSION_ENDED_DETAIL)

        now = datetime.now(timezone.utc)
        if (expires_at := _as_utc(row.expires_at)) is None or expires_at <= now:
            logger.info("AUTH_REFRESH_EXPIRED: session for user %s reached its window", row.user_id)
            raise HTTPException(status_code=401, detail=SESSION_ENDED_DETAIL)

        user = UserRepository.get_by_id(db, row.user_id)
        if not user or not user.is_active or user.status != "active":
            logger.info("AUTH_REFRESH_REJECTED: user %s is no longer active", row.user_id)
            raise HTTPException(status_code=401, detail=SESSION_ENDED_DETAIL)

        started_at = _as_utc(row.session_started_at) or _as_utc(row.created_at) or now
        row.revoked_at = now
        try:
            db.commit()
        except Exception:
            db.rollback()
            logger.exception("Failed to rotate authentication session for user %s", user.id)
            raise HTTPException(status_code=500, detail="Unable to refresh authentication session")

        refresh_token_plain, started_at, expires_at = AuthService._persist_session_token(
            db, user.id, session_started_at=started_at
        )
        logger.info("AUTH_REFRESH_SUCCESS: access token renewed for user %s", user.id)
        return TokenPair(
            access_token=create_access_token(AuthService._access_claims(user)),
            refresh_token=refresh_token_plain,
            token_type="bearer",
            user=UserRead.model_validate(user),
            session_created_at=started_at,
            session_expires_at=expires_at,
        )

    @staticmethod
    def revoke_session(db: Session, refresh_token: str | None) -> None:
        """End a session on explicit logout. Idempotent by design.

        A logout that fails because the token was already gone would only push
        clients into retrying something that has already had its effect, so an
        unknown token is a no-op rather than an error.
        """
        if not refresh_token:
            return
        row = db.scalar(
            select(RefreshToken).where(RefreshToken.token_hash == hash_token(refresh_token))
        )
        if row is None or row.revoked_at is not None:
            return
        row.revoked_at = datetime.now(timezone.utc)
        try:
            db.commit()
            logger.info("AUTH_LOGOUT: session revoked for user %s", row.user_id)
        except Exception:
            db.rollback()
            logger.exception("Failed to revoke authentication session")

    @staticmethod
    async def login_exchange(
        db: Session,
        username: str,
        password: str,
        login_for: LoginFor = DEFAULT_LOGIN_FOR,
    ) -> TokenPair:
        """Verify credentials with the provider and issue a local session.

        `login_for` identifies the client the sign-in is for and is forwarded to
        the provider. It changes nothing locally: the resolved role, permissions
        and session window are the same whichever client asked.
        """
        normalized_username = username.strip()

        try:
            wp_user = await ExternalAuthService.authenticate(username, password, login_for)
        except HTTPException as he:
            logger.error("AUTH_LOGIN_FAILED: Authentication failed for user %s: %s", normalized_username, he.detail)
            raise he
        except Exception as e:
            logger.error("AUTH_LOGIN_FAILED: Unexpected error during login exchange for %s: %s", normalized_username, str(e))
            raise HTTPException(status_code=500, detail=f"Authentication exchange failed: {str(e)}")

        hubstaff_user_id = wp_user.get("hubstaff_user_id")
        hubstaff_user_id = str(hubstaff_user_id).strip() if hubstaff_user_id is not None else None
        email = str(wp_user.get("email", "")).strip().lower()
        name = str(wp_user.get("name", "")).strip()
        if not email or not name:
            missing = [
                field for field, value in (("email", email), ("name", name)) if not value
            ]
            logger.error(
                "AUTH_PROVIDER_INVALID_RESPONSE: Response omitted required identity field(s): %s",
                ", ".join(missing),
            )
            raise HTTPException(status_code=502, detail="Invalid authentication provider response")
        hubstaff_designation = wp_user.get("hubstaff_designation")
        idle_enabled = wp_user.get("idle_enabled", True)
        idle_minutes = wp_user.get("idle_minutes", 5)
        capture_frequency = wp_user.get("capture_frequency", 300)

        permission_schema = wp_user.get("permission_schema") or {}
        if not isinstance(permission_schema, dict):
            logger.error(
                "AUTH_PROVIDER_INVALID_RESPONSE: permission_schema is a %s, not an object",
                type(permission_schema).__name__,
            )
            raise HTTPException(status_code=502, detail="Invalid authentication provider response")
        wp_capabilities = permission_schema.get("permissions")

        role_name, role_source, candidates = _resolve_provider_role(wp_user, permission_schema)

        if not role_name:
            # Name the values that were rejected. Without them the log said only
            # that *a* role was unrecognised, which is not enough to tell an
            # unmapped role apart from a provider that sent none -- and left the
            # only way to diagnose a 502 as reproducing it with the user's own
            # credentials. Role names are not sensitive; tokens and passwords
            # are never logged here.
            if not candidates:
                logger.error(
                    "AUTH_PROVIDER_ROLE_MISSING: Provider returned no role for %s "
                    "(user.roles=%r, permission_schema.name=%r)",
                    email, wp_user.get("roles"), permission_schema.get("name"),
                )
            else:
                logger.error(
                    "AUTH_PROVIDER_ROLE_UNSUPPORTED: Provider role(s) %s from %s for %s match "
                    "no Monitra role; supported roles are %s",
                    candidates, role_source, email, sorted(ROLE_PERMISSIONS),
                )
            raise HTTPException(status_code=502, detail="Invalid authentication provider response")

        logger.info(
            "AUTH_PROVIDER_ROLE_RESOLVED: Provider role(s) %s from %s resolved to role_name %r",
            candidates, role_source, role_name,
        )

        resolved_permissions = {p: True for p in ROLE_PERMISSIONS[role_name]}

        user = UserRepository.get_by_hubstaff_id(db, hubstaff_user_id) if hubstaff_user_id else None
        email_user = UserRepository.get_by_normalized_email(db, email)

        if user and email_user and user.id != email_user.id:
            db.rollback()
            logger.error("External identity/email conflict for email %s", email)
            raise HTTPException(status_code=409, detail="Authenticated identity conflicts with a local account")
        if user is None:
            user = email_user

        if user:
            logger.info("LOCAL_USER_FOUND: Local user found with id %s", user.id)
            if hubstaff_user_id and user.hubstaff_user_id and user.hubstaff_user_id != hubstaff_user_id:
                logger.error("Email identity conflict for local user %s", user.id)
                raise HTTPException(status_code=409, detail="Authenticated identity conflicts with a local account")
            # Sync identity-provider fields, but preserve local profile fields when
            # the provider does not return them.
            if hubstaff_user_id:
                user.hubstaff_user_id = hubstaff_user_id
            user.email = email
            user.name = name
            if hubstaff_designation:
                user.designation = hubstaff_designation
            user.idle_enabled = idle_enabled
            user.idle_minutes = idle_minutes
            user.capture_frequency = capture_frequency
            user.wp_capabilities = wp_capabilities
            user.role_name = role_name
            user.permissions = resolved_permissions
            user.status = "active"
            user.is_active = True
            try:
                db.commit()
                db.refresh(user)
            except IntegrityError:
                db.rollback()
                logger.error("Local user synchronization conflicted for email %s", email)
                raise HTTPException(status_code=409, detail="Authenticated identity conflicts with a local account")
        else:
            logger.info("LOCAL_USER_CREATED: Local user not found; provisioning started")
            # TEMPORARY: WordPress login response does not include organization_id. Every user is currently assigned
            # DEFAULT_ORGANIZATION_ID until a real mapping is defined. Confirmed with senior lead as acceptable short-term.
            # See docs/rbac.md.
            organization_id = settings.DEFAULT_ORGANIZATION_ID

            # Ensure organization exists
            org_exists = db.execute(
                text("SELECT id FROM organizations WHERE id = :org_id"),
                {"org_id": organization_id}
            ).scalar()

            if not org_exists:
                db.execute(
                    text("INSERT INTO organizations (id, name, slug) VALUES (:org_id, 'Default Org', 'default-org') ON CONFLICT DO NOTHING"),
                    {"org_id": organization_id}
                )
                db.commit()

            # Ensure username is unique and valid
            username_val = wp_user.get("username") or email.split("@")[0]

            user_create = UserCreate(
                organization_id=organization_id,
                hubstaff_user_id=hubstaff_user_id,
                username=username_val[:255],
                email=email,
                name=name,
                designation=hubstaff_designation,
                role_name=role_name,
                permissions=resolved_permissions,
                wp_capabilities=wp_capabilities,
                idle_enabled=idle_enabled,
                idle_minutes=idle_minutes,
                capture_frequency=capture_frequency,
                status="active"
            )
            try:
                user = UserRepository.create(db, user_create)
            except IntegrityError:
                db.rollback()
                user = (
                    UserRepository.get_by_hubstaff_id(db, hubstaff_user_id)
                    if hubstaff_user_id else None
                ) or UserRepository.get_by_normalized_email(db, email)
                if not user:
                    logger.exception("Local user provisioning failed")
                    raise HTTPException(status_code=500, detail="Unable to provision authenticated user")
                logger.info("Local user provisioning race resolved to id %s", user.id)

        logger.info("Local authentication identity ready with id %s", user.id)
        # Issue the SMS JWT only after local provisioning succeeds.
        access_token = create_access_token(AuthService._access_claims(user))
        logger.info("JWT_GENERATED: Local access token generated for user id %s", user.id)

        # A successful credential check is the only thing that starts a new
        # session window.
        refresh_token_plain, started_at, expires_at = AuthService._persist_session_token(db, user.id)

        logger.info("AUTH_LOGIN_SUCCESS: Authentication completed for local user %s", user.id)
        return TokenPair(
            access_token=access_token,
            refresh_token=refresh_token_plain,
            token_type="bearer",
            user=UserRead.model_validate(user),
            session_created_at=started_at,
            session_expires_at=expires_at,
        )

    @staticmethod
    def dev_login(db: Session, email: str, password: str) -> TokenPair:
        # We explicitly resolve/force permissions from ROLE_PERMISSIONS server-side to ensure
        # consistency and enforce that permissions are derived purely from the role_name in the database.
        user = db.scalar(
            select(User).where(User.email == email)
        )

        if not user or not user.password_hash or not verify_password(password, user.password_hash):
            raise HTTPException(status_code=401, detail="Invalid email or password")

        resolved_permissions = {p: True for p in ROLE_PERMISSIONS.get(user.role_name, {})}
        if user.permissions != resolved_permissions:
            user.permissions = resolved_permissions
            db.commit()
            db.refresh(user)

        return AuthService._issue_token_pair(db, user)

    @staticmethod
    def _issue_token_pair(db: Session, user: User) -> TokenPair:
        """Mint the local access/refresh pair for an already-authenticated user."""
        access_token = create_access_token(AuthService._access_claims(user))
        refresh_token_plain, started_at, expires_at = AuthService._persist_session_token(db, user.id)
        return TokenPair(
            access_token=access_token,
            refresh_token=refresh_token_plain,
            token_type="bearer",
            user=UserRead.model_validate(user),
            session_created_at=started_at,
            session_expires_at=expires_at,
        )

    @staticmethod
    def issue_handoff_token(db: Session, user: User) -> tuple[str, datetime]:
        """Mint a single-use handoff token for an already-authenticated user.

        The desktop client holds a local session, not a provider token, so it
        cannot use the portal handoff path. It asks for one of these instead
        and opens the web client with it, which exchanges it for a real
        session through the same `/auth/sso/token` endpoint the portal uses.

        The token is a random secret, not a JWT: it is stored hashed and
        redeemed by claiming its row, which is what makes it usable exactly
        once. Its lifetime is seconds because the only thing it has to outlive
        is the browser launch.
        """
        token = f"{HANDOFF_TOKEN_PREFIX}{secrets.token_urlsafe(32)}"
        expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=settings.SSO_HANDOFF_TOKEN_EXPIRE_SECONDS
        )
        try:
            db.add(SsoHandoffToken(
                user_id=user.id,
                token_hash=hash_token(token),
                expires_at=expires_at,
            ))
            db.commit()
        except Exception:
            db.rollback()
            logger.exception("AUTH_SSO_HANDOFF_PERSIST_FAILED: user %s", user.id)
            raise HTTPException(status_code=500, detail="Unable to start the web sign-in handoff")

        logger.info("AUTH_SSO_HANDOFF_ISSUED: handoff token minted for user %s", user.id)
        return token, expires_at

    @staticmethod
    def _redeem_handoff_token(db: Session, token: str) -> TokenPair:
        """Turn a desktop handoff token into a local session, or fail.

        The row is claimed before anything is issued: `used_at` is set under a
        condition that only matches an unspent, unexpired row, so two browsers
        racing on the same URL cannot both be signed in.
        """
        now = datetime.now(timezone.utc)
        claimed = db.execute(
            SsoHandoffToken.__table__.update()
            .where(
                SsoHandoffToken.token_hash == hash_token(token),
                SsoHandoffToken.used_at.is_(None),
                SsoHandoffToken.expires_at > now,
            )
            .values(used_at=now)
            .returning(SsoHandoffToken.user_id)
        ).scalar()
        if claimed is None:
            db.rollback()
            logger.error("AUTH_SSO_HANDOFF_REJECTED: token is unknown, expired or already used")
            raise HTTPException(status_code=401, detail="Invalid or expired sign-in link")
        db.commit()

        user = UserRepository.get_by_id(db, claimed)
        if not user or not user.is_active or user.status != "active":
            logger.error("AUTH_SSO_HANDOFF_INACTIVE_ACCOUNT: local user %s is not active", claimed)
            raise HTTPException(status_code=403, detail="This account is not active")

        logger.info("AUTH_SSO_HANDOFF_SUCCESS: desktop handoff signed in user %s", user.id)
        return AuthService._issue_token_pair(db, user)

    @staticmethod
    async def sso_exchange(db: Session, provider_token: str) -> TokenPair:
        """
        Exchange a provider-issued JWT (the ?token=... handoff from the performance
        portal) for a local session, so a user arriving from that portal lands on the
        dashboard without typing credentials again.

        The provider token itself is never accepted as a local credential: it is
        verified with the provider, and the local session is issued only for the
        identity the provider reports behind it.

        The same endpoint also redeems the desktop client's handoff token, which
        this backend minted itself for a user who is already signed in there.
        The two are told apart by prefix, so a desktop handoff is never sent to
        the provider and a provider token is never matched against local rows.
        """
        if (provider_token or "").strip().startswith(HANDOFF_TOKEN_PREFIX):
            return AuthService._redeem_handoff_token(db, provider_token.strip())

        profile = await ExternalAuthService.authenticate_token(provider_token)

        email = str(profile.get("email", "")).strip().lower()
        name = (
            str(profile.get("display_name") or "").strip()
            or " ".join(part for part in (
                str(profile.get("first_name") or "").strip(),
                str(profile.get("last_name") or "").strip(),
            ) if part).strip()
            or str(profile.get("username") or "").strip()
        )
        if not email or not name:
            logger.error("AUTH_SSO_INVALID_RESPONSE: Provider profile omitted required identity fields")
            raise HTTPException(status_code=502, detail="Invalid authentication provider response")

        user = UserRepository.get_by_normalized_email(db, email)

        if user is None:
            # The provider profile carries no Hubstaff identity, organisation or
            # permission schema, so a role is only accepted when the provider's own
            # role names one this system already defines. Anything else is refused
            # rather than guessed - a fabricated role would silently grant or deny
            # access the provider never authorised.
            provider_roles = profile.get("roles") if isinstance(profile.get("roles"), list) else []
            role_name = next((str(r) for r in provider_roles if str(r) in ROLE_PERMISSIONS), None)
            if not role_name:
                logger.error(
                    "AUTH_SSO_NO_LOCAL_ACCOUNT: No local user for %s and provider roles %s are not mapped",
                    email,
                    provider_roles,
                )
                raise HTTPException(
                    status_code=403,
                    detail="No Monitra account exists for this user yet. Sign in once with your credentials to set it up.",
                )

            organization_id = settings.DEFAULT_ORGANIZATION_ID
            org_exists = db.execute(
                text("SELECT id FROM organizations WHERE id = :org_id"),
                {"org_id": organization_id},
            ).scalar()
            if not org_exists:
                db.execute(
                    text("INSERT INTO organizations (id, name, slug) VALUES (:org_id, 'Default Org', 'default-org') ON CONFLICT DO NOTHING"),
                    {"org_id": organization_id},
                )
                db.commit()

            username_val = str(profile.get("username") or email.split("@")[0])
            user_create = UserCreate(
                organization_id=organization_id,
                username=username_val[:255],
                email=email,
                name=name,
                role_name=role_name,
                permissions={p: True for p in ROLE_PERMISSIONS[role_name]},
                capture_frequency=300,
                status="active",
            )
            try:
                user = UserRepository.create(db, user_create)
            except IntegrityError:
                db.rollback()
                user = UserRepository.get_by_normalized_email(db, email)
                if not user:
                    logger.exception("AUTH_SSO_PROVISIONING_FAILED: Unable to provision %s", email)
                    raise HTTPException(status_code=500, detail="Unable to provision authenticated user")
            logger.info("AUTH_SSO_USER_PROVISIONED: Local user %s created from provider identity", user.id)

        if not user.is_active or user.status != "active":
            logger.error("AUTH_SSO_INACTIVE_ACCOUNT: Local user %s is not active", user.id)
            raise HTTPException(status_code=403, detail="This account is not active")

        logger.info("AUTH_SSO_SUCCESS: Provider token exchanged for a local session for user %s", user.id)
        return AuthService._issue_token_pair(db, user)

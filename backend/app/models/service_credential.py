from sqlalchemy import BigInteger, String, TIMESTAMP, Identity, ForeignKeyConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from datetime import datetime
from typing import Optional
from app.core.database import Base


class ServiceCredential(Base):
    """A long-lived API key belonging to a machine, not a person.

    This is what GitHub Actions presents to register a desktop release. It
    exists because the two obvious alternatives are both worse:

    * A *password* in a CI secret means the pipeline signs in through
      `/auth/dev-login`, and that route is deliberately 404 in production.
      Keeping production in development mode so CI can sign in trades the whole
      deployment's posture for one build step.
    * An *access token* in a CI secret is dead thirty minutes after it is
      minted (`ACCESS_TOKEN_EXPIRE_MINUTES`), so every release after the first
      fails with a 401 nobody can explain.

    A key here does not expire unless someone gives it an `expires_at`, carries
    no password, and can be revoked on its own without touching the account.

    Only the SHA-256 of the key is stored, for the same reason refresh tokens
    and handoff tokens store only a hash: a database dump must not hand anyone
    a working credential. `key_id` is the non-secret half of the key, so a log
    line, a support question or a revocation can name exactly which credential
    is meant without anyone ever quoting the secret.

    The authority of a key is *not* stored here. It comes entirely from the
    account it belongs to, and `ServiceCredentialService.authenticate` refuses
    any account whose role is not in `SERVICE_ROLE_NAMES`. A key can therefore
    never be minted into more authority than its service role holds.
    """

    __tablename__ = 'service_credentials'

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    #: What this credential is for, in words -- "github-actions-release".
    #: Read by a person deciding whether a key is still needed.
    name: Mapped[str] = mapped_column(String, nullable=False)
    #: The public half of the key, carried in the key itself. Safe to log, and
    #: it is what the lookup is keyed on so verification never has to scan.
    key_id: Mapped[str] = mapped_column(String, nullable=False, unique=True, index=True)
    #: SHA-256 of the whole key. Compared with `hmac.compare_digest`.
    token_hash: Mapped[str] = mapped_column(String, nullable=False, unique=True, index=True)
    #: Null means the key does not expire. That is the normal case for CI: a
    #: credential that dies unattended turns every release into an outage.
    expires_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    #: Set by revocation, and never unset. A revoked row is kept so that "which
    #: key was in CI in March" stays answerable.
    revoked_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    #: Best-effort audit trail: updated on successful use, and a failure to
    #: record it never fails the request.
    last_used_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ['user_id'], ['users.id'],
            name='fk_service_credentials_user', ondelete='CASCADE',
        ),
    )

    user: Mapped["User"] = relationship("User")

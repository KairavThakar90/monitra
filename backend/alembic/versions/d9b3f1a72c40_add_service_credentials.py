"""add service credentials

Machine identities for automated callers -- today, the GitHub Actions job that
registers a desktop release. See app/services/service_credential.py for why
this exists rather than CI holding a password or an access token.

Revision ID: d9b3f1a72c40
Revises: a7c41d9e60b3
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d9b3f1a72c40"
down_revision: Union[str, None] = "a7c41d9e60b3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "service_credentials",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("key_id", sa.String(), nullable=False),
        sa.Column("token_hash", sa.String(), nullable=False),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_service_credentials"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_service_credentials_user",
            ondelete="CASCADE",
        ),
    )
    # Unique on both halves. `key_id` is what verification looks the row up by,
    # so two rows sharing one would make the lookup ambiguous; `token_hash` is
    # unique because two credentials must never be the same secret.
    op.create_index(
        "idx_service_credentials_key_id",
        "service_credentials",
        ["key_id"],
        unique=True,
    )
    op.create_index(
        "idx_service_credentials_token_hash",
        "service_credentials",
        ["token_hash"],
        unique=True,
    )
    op.create_index(
        "idx_service_credentials_user_id",
        "service_credentials",
        ["user_id"],
    )


def downgrade() -> None:
    op.drop_index("idx_service_credentials_user_id", table_name="service_credentials")
    op.drop_index("idx_service_credentials_token_hash", table_name="service_credentials")
    op.drop_index("idx_service_credentials_key_id", table_name="service_credentials")
    op.drop_table("service_credentials")

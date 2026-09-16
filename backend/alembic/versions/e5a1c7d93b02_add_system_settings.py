"""add system_settings

One row per product-wide setting an administrator can change at runtime. The
first key is `maintenance_mode`, a purely informational flag every client
polls so it can show -- and later remove -- the "Monitra is under maintenance"
notice. Nothing in the backend reads it to refuse or alter a request.

The row is seeded here, switched off, so the service can always lock an
existing row (`SELECT ... FOR UPDATE`) rather than racing two first-time
inserts against the primary key.

`activity_logs`, where each change is also recorded, is part of the base
schema and is not touched.

Revision ID: e5a1c7d93b02
Revises: d3f5a7c91e24
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "e5a1c7d93b02"
down_revision: Union[str, None] = "d3f5a7c91e24"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "system_settings",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column(
            "value",
            postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), "sqlite"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("updated_by_user_id", sa.BigInteger(), nullable=True),
        sa.Column("updated_by_username", sa.String(length=150), nullable=True),
        sa.PrimaryKeyConstraint("key", name="pk_system_settings"),
        sa.ForeignKeyConstraint(
            ["updated_by_user_id"],
            ["users.id"],
            name="fk_system_settings_updated_by_user_id_users",
            ondelete="SET NULL",
        ),
    )

    # Seed the one setting this build knows, off. The service updates this
    # row under a row lock; it never has to create it.
    op.execute(
        "INSERT INTO system_settings (key, value) "
        "VALUES ('maintenance_mode', '{\"enabled\": false}') "
        "ON CONFLICT (key) DO NOTHING"
    )


def downgrade() -> None:
    op.drop_table("system_settings")

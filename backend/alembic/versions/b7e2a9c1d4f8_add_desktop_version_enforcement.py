"""add desktop_version_enforcement table

Independent production-ready mandatory version enforcement for Monitra.
Holds a single row (id=1) representing the global minimum required desktop
application version, official download URL, enforcement message, and active
status.

Revision ID: b7e2a9c1d4f8
Revises: a1c4e8b2d6f0
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b7e2a9c1d4f8"
down_revision: Union[str, None] = "a1c4e8b2d6f0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "desktop_version_enforcement",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("minimum_version", sa.String(length=32), nullable=False),
        sa.Column("download_url", sa.String(length=500), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("updated_by_user_id", sa.BigInteger(), nullable=True),
        sa.Column("updated_by_username", sa.String(length=150), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_desktop_version_enforcement"),
        sa.CheckConstraint(
            "id = 1",
            name="ck_desktop_version_enforcement_single_row",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_user_id"],
            ["users.id"],
            name="fk_desktop_version_enforcement_updated_by_user_id_users",
            ondelete="SET NULL",
        ),
    )

    # Seed the initial enforcement configuration: minimum required version 1.3.4
    op.execute(
        "INSERT INTO desktop_version_enforcement "
        "(id, minimum_version, download_url, message, is_active) "
        "VALUES (1, '1.3.4', 'https://staff.peakworkos.com/download', "
        "'Your Monitra application version is outdated. Please install the latest version to continue using Monitra.', true) "
        "ON CONFLICT (id) DO NOTHING"
    )


def downgrade() -> None:
    op.drop_table("desktop_version_enforcement")


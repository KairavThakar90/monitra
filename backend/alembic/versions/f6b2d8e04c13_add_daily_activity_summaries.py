"""add daily_activity_summaries

One row per user per calendar day (reporting timezone), written by the
end-of-day roll-up from that day's `time_entry_activity` windows: the
duration-weighted average activity and the counts it was made from. It is
recomputed idempotently, so a late upload updates the row rather than adding
a second one; the unique constraint is what enforces that.

Revision ID: f6b2d8e04c13
Revises: e5a1c7d93b02
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f6b2d8e04c13"
down_revision: Union[str, None] = "e5a1c7d93b02"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "daily_activity_summaries",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("organization_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("windows", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("measured_seconds", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("weighted_sum", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column("average_activity", sa.Float(), nullable=True),
        sa.Column("keyboard_strokes", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("mouse_clicks", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("mouse_movements", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "computed_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_daily_activity_summaries"),
        sa.UniqueConstraint("user_id", "day", name="uq_daily_activity_summaries_user_day"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"],
            name="fk_daily_activity_summaries_user_id_users", ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_daily_activity_summaries_org_day",
        "daily_activity_summaries",
        ["organization_id", "day"],
    )


def downgrade() -> None:
    op.drop_index("ix_daily_activity_summaries_org_day", table_name="daily_activity_summaries")
    op.drop_table("daily_activity_summaries")

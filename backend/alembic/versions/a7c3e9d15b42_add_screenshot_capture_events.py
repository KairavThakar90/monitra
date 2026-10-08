"""add time_entry_screenshot_events

What happened to an expected screenshot that has no image: the capture failed,
the OS or a privacy rule held it back, or the finished image is stuck on the
desktop failing to upload. Until now the backend could record only a screenshot
that arrived, so a window with tracked time and activity and no screenshot was
always "No capture", whatever the reason. The desktop reports these through its
durable queue; `(organization_id, client_event_id)` is unique so a retry
records nothing twice.

Additive and reversible: a new table, nothing existing is altered.

Revision ID: a7c3e9d15b42
Revises: d4a7e1c93b58
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a7c3e9d15b42"
down_revision: Union[str, None] = "d4a7e1c93b58"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "time_entry_screenshot_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("organization_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("time_entry_id", sa.BigInteger(), nullable=True),
        sa.Column("client_event_id", sa.String(length=255), nullable=False),
        sa.Column("client_screenshot_id", sa.String(length=255), nullable=True),
        sa.Column("window_start", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("occurred_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("state", sa.String(length=20), nullable=False),
        sa.Column("reason", sa.String(length=255), nullable=True),
        sa.Column("attempts", sa.SmallInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "created_at", sa.TIMESTAMP(timezone=True), nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_time_entry_screenshot_events"),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"],
            name="fk_screenshot_events_org", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"],
            name="fk_screenshot_events_user", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["time_entry_id"], ["time_entries.id"],
            name="fk_screenshot_events_entry", ondelete="SET NULL",
        ),
        sa.UniqueConstraint(
            "organization_id", "client_event_id",
            name="uq_screenshot_events_org_client_event",
        ),
        sa.CheckConstraint(
            "state IN ('failed', 'blocked', 'excluded', 'unavailable', "
            "'upload_retrying', 'upload_parked')",
            name="ck_screenshot_events_state",
        ),
    )
    op.create_index(
        "ix_screenshot_events_user_window", "time_entry_screenshot_events",
        ["user_id", "window_start"],
    )
    op.create_index(
        "ix_screenshot_events_org_window", "time_entry_screenshot_events",
        ["organization_id", "window_start"],
    )


def downgrade() -> None:
    op.drop_index("ix_screenshot_events_org_window", table_name="time_entry_screenshot_events")
    op.drop_index("ix_screenshot_events_user_window", table_name="time_entry_screenshot_events")
    op.drop_table("time_entry_screenshot_events")

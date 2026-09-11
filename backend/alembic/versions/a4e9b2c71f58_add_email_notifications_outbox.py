"""add email notifications outbox

The transactional email outbox behind the welcome and feedback-notification
workflows. Purely additive: it creates one new table and touches nothing that
already exists, so an older deployment keeps working against a database that
has already been upgraded.

The unique constraint on (notification_type, dedupe_key) is the whole point of
the table — it is what makes "send this email once" a database guarantee
rather than an application convention.

Revision ID: a4e9b2c71f58
Revises: d9b3f1a72c40
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a4e9b2c71f58"
down_revision: Union[str, None] = "d9b3f1a72c40"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "email_notifications",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("notification_type", sa.String(length=40), nullable=False),
        sa.Column("dedupe_key", sa.String(length=120), nullable=False),
        sa.Column("recipients", sa.Text(), nullable=False),
        sa.Column("subject", sa.String(length=255), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column(
            "attempt_count", sa.Integer(), nullable=False, server_default=sa.text("0"),
        ),
        sa.Column(
            "max_attempts", sa.Integer(), nullable=False, server_default=sa.text("6"),
        ),
        sa.Column(
            "next_attempt_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("last_attempt_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("sent_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(length=500), nullable=True),
        sa.Column("organization_id", sa.BigInteger(), nullable=True),
        sa.Column("user_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_email_notifications"),
        sa.UniqueConstraint(
            "notification_type", "dedupe_key", name="uq_email_notifications_event",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_email_notifications_organization",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_email_notifications_user",
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "idx_email_notifications_due",
        "email_notifications",
        ["status", "next_attempt_at"],
    )


def downgrade() -> None:
    op.drop_index("idx_email_notifications_due", table_name="email_notifications")
    op.drop_table("email_notifications")

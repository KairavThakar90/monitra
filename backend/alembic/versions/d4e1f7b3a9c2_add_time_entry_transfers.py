"""add time_entry_transfers (project/task reassignment audit trail)

Revision ID: d4e1f7b3a9c2
Revises: 2aebccda4c09

One row per transfer of an already-recorded time entry to a different
project/task. `time_entries.start_time`, `end_time` and `total_seconds` are
never written by a transfer -- only `project_id`/`task_id` on the entry
itself change, and this table is the durable record of what it used to be.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "d4e1f7b3a9c2"
down_revision: Union[str, None] = "2aebccda4c09"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "time_entry_transfers",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("organization_id", sa.BigInteger(), nullable=False),
        sa.Column("time_entry_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("transferred_by_user_id", sa.BigInteger(), nullable=False),
        sa.Column("from_project_id", sa.BigInteger(), nullable=False),
        sa.Column("from_task_id", sa.BigInteger(), nullable=False),
        sa.Column("to_project_id", sa.BigInteger(), nullable=False),
        sa.Column("to_task_id", sa.BigInteger(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("transferred_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], name="fk_time_entry_transfers_org", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["time_entry_id"], ["time_entries.id"], name="fk_time_entry_transfers_entry", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_time_entry_transfers_user", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["transferred_by_user_id"], ["users.id"], name="fk_time_entry_transfers_by_user", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["from_project_id"], ["projects.id"], name="fk_time_entry_transfers_from_project", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["from_task_id"], ["tasks.id"], name="fk_time_entry_transfers_from_task", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["to_project_id"], ["projects.id"], name="fk_time_entry_transfers_to_project", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["to_task_id"], ["tasks.id"], name="fk_time_entry_transfers_to_task", ondelete="CASCADE"),
    )
    op.create_index("ix_time_entry_transfers_time_entry_id", "time_entry_transfers", ["time_entry_id"])
    op.create_index("ix_time_entry_transfers_organization_id", "time_entry_transfers", ["organization_id"])


def downgrade() -> None:
    op.drop_index("ix_time_entry_transfers_organization_id", table_name="time_entry_transfers")
    op.drop_index("ix_time_entry_transfers_time_entry_id", table_name="time_entry_transfers")
    op.drop_table("time_entry_transfers")

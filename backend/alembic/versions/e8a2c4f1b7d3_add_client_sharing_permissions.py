"""add client sharing permissions

Revision ID: e8a2c4f1b7d3
Revises: c7f4a1b9e6d2

Lets an admin choose, per client, which of four categories of project
information they can see: member details, screenshots, tasks, and timing
(working hours). Defaults preserve current behaviour for already-approved
clients -- everything they could already see stays visible -- except
screenshots, which is a brand-new capability and defaults off until an
admin explicitly turns it on for a client.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "e8a2c4f1b7d3"
down_revision: Union[str, None] = "c7f4a1b9e6d2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("clients", sa.Column("share_member_details", sa.Boolean(), nullable=False, server_default=sa.text("true")))
    op.add_column("clients", sa.Column("share_screenshots", sa.Boolean(), nullable=False, server_default=sa.text("false")))
    op.add_column("clients", sa.Column("share_tasks", sa.Boolean(), nullable=False, server_default=sa.text("true")))
    op.add_column("clients", sa.Column("share_timing", sa.Boolean(), nullable=False, server_default=sa.text("true")))


def downgrade() -> None:
    op.drop_column("clients", "share_timing")
    op.drop_column("clients", "share_tasks")
    op.drop_column("clients", "share_screenshots")
    op.drop_column("clients", "share_member_details")

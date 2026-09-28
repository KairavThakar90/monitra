"""add can_add_tasks to users

Revision ID: f9a1c3e5b7d2
Revises: e8a2c4f1b7d3

A per-member switch an administrator flips from the Members directory to
withdraw task creation from one person without changing their role. It is a
column of its own rather than a key in `users.permissions` because that map
is a cache of ROLE_PERMISSIONS and is rebuilt on every sign-in, so anything
written into it by hand is lost at the next login. Defaults to true: every
existing member keeps the ability they have today.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "f9a1c3e5b7d2"
down_revision: Union[str, None] = "e8a2c4f1b7d3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("can_add_tasks", sa.Boolean(), nullable=False, server_default=sa.text("true")),
    )


def downgrade() -> None:
    op.drop_column("users", "can_add_tasks")

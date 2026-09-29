"""add can_login to users

Revision ID: b3c5d7e9f1a2
Revises: b3d5f7a9c1e2

The Members directory's Allow / Exclude switch for signing in (see
app/core/login_access.py). Defaults to true, so every existing member keeps
signing in exactly as today.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "b3c5d7e9f1a2"
down_revision: Union[str, None] = "b3d5f7a9c1e2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("can_login", sa.Boolean(), nullable=False, server_default=sa.text("true")),
    )


def downgrade() -> None:
    op.drop_column("users", "can_login")

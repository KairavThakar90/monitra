"""add floor to members

Revision ID: c3f7a1d9e2b4
Revises: b6d1f8a4c2e9
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "c3f7a1d9e2b4"
down_revision: Union[str, None] = "b6d1f8a4c2e9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("floor", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "floor")
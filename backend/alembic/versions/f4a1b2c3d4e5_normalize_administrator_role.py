"""Normalize the administrator role name."""

from typing import Sequence, Union

from alembic import op


revision: str = "f4a1b2c3d4e5"
down_revision: Union[str, None] = "f2b060359157"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("UPDATE users SET role_name = 'administrator' WHERE role_name = 'admin'")


def downgrade() -> None:
    op.execute("UPDATE users SET role_name = 'admin' WHERE role_name = 'administrator'")
"""add screenshots_enabled to users

Revision ID: c9e2a7d41b36
Revises: e9f5a3b7c2d4

A per-member switch an administrator turns off from Screenshot Management to
stop the desktop capturing that person's screen. It is a column of its own for
the same reason `can_login` and `can_add_tasks` are: `users.permissions` is a
cache of ROLE_PERMISSIONS rebuilt at every sign-in, so anything written into it
by hand is lost.

Defaults to true, so every existing member keeps capturing exactly as before and
deploying this changes nothing for anyone until an administrator switches
someone off. Switching it off never removes a screenshot that already exists.

Additive and reversible. The column is added only when it is missing, because a
database restored from a dump can already carry it while its `alembic_version`
still names the previous revision (see 7c65a7896bab).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "c9e2a7d41b36"
down_revision: Union[str, None] = "e9f5a3b7c2d4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _user_columns() -> set:
    # Offline (`--sql`) mode cannot inspect, and renders the column as before.
    if op.get_context().as_sql:
        return set()
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns("users")}


def upgrade() -> None:
    if "screenshots_enabled" not in _user_columns():
        op.add_column(
            "users",
            sa.Column(
                "screenshots_enabled", sa.Boolean(), nullable=False,
                server_default=sa.text("true"),
            ),
        )


def downgrade() -> None:
    op.drop_column("users", "screenshots_enabled")

"""add notifications_enabled and activity_tracking_enabled to users

Revision ID: d1a8f3c5e7b9
Revises: c9e2a7d41b36

Two more per-member switches, set from User Management beside
`screenshots_enabled`:

* `notifications_enabled` -- whether the desktop shows this member the
  administrator-managed notifications (built-in reminders, custom ones, a
  pushed message).
* `activity_tracking_enabled` -- whether the desktop measures this member's
  keyboard/mouse activity and the applications and websites they use.

Each is a column of its own for the reason `can_login` and `can_add_tasks` are:
`users.permissions` is rebuilt from the role at every sign-in, so anything
written into it by hand is lost.

Both default to true, so deploying this changes nothing for any existing member
until an administrator switches someone off.

Additive and reversible, and each column is added only when it is missing,
because a database restored from a dump can already carry it while its
`alembic_version` still names the previous revision (see 7c65a7896bab).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "d1a8f3c5e7b9"
down_revision: Union[str, None] = "c9e2a7d41b36"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_COLUMNS = ("notifications_enabled", "activity_tracking_enabled")


def _user_columns() -> set:
    # Offline (`--sql`) mode cannot inspect, and renders every column.
    if op.get_context().as_sql:
        return set()
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns("users")}


def upgrade() -> None:
    present = _user_columns()
    for name in _COLUMNS:
        if name not in present:
            op.add_column(
                "users",
                sa.Column(name, sa.Boolean(), nullable=False, server_default=sa.text("true")),
            )


def downgrade() -> None:
    for name in reversed(_COLUMNS):
        op.drop_column("users", name)

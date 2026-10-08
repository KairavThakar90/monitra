"""add can_add_nonbillable_tasks to users

Revision ID: b6d1f8a4c2e9
Revises: a7c3e9d15b42

A per-member switch an administrator turns on from the Members directory to
let one person create *Non billable* tasks -- the desktop's second Add button,
whose tasks carry " - Non billable" on the end of their name -- independently
of `can_add_tasks`. It is a column of its own for the same reason
`can_add_tasks` is: `users.permissions` is a cache of ROLE_PERMISSIONS rebuilt
at every sign-in, so anything written into it by hand is lost.

Defaults to false, the opposite of `can_add_tasks`: this is a new capability
that has to be granted, so every existing member keeps exactly what they can do
today and nobody sees the new button until an administrator allows it.

Additive and reversible. The column is added only when it is missing, because a
database restored from a dump can already carry it while its `alembic_version`
still names the previous revision (see 7c65a7896bab).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "b6d1f8a4c2e9"
down_revision: Union[str, None] = "a7c3e9d15b42"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _user_columns() -> set:
    # Offline (`--sql`) mode cannot inspect, and renders the column as before.
    if op.get_context().as_sql:
        return set()
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns("users")}


def upgrade() -> None:
    if "can_add_nonbillable_tasks" not in _user_columns():
        op.add_column(
            "users",
            sa.Column(
                "can_add_nonbillable_tasks", sa.Boolean(), nullable=False,
                server_default=sa.text("false"),
            ),
        )


def downgrade() -> None:
    op.drop_column("users", "can_add_nonbillable_tasks")

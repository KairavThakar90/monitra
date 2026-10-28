"""add member_displays

Revision ID: e3b7c1a9d5f2
Revises: d1a8f3c5e7b9

The screens a member's computer has, as the desktop last reported them, and
which of them an administrator has switched screenshot capture off for. One row
per member per screen number; see `app/models/member_display.py`.

Additive and reversible. The table is created only when it is missing, because a
database restored from a dump can already carry it while its `alembic_version`
still names the previous revision (see 7c65a7896bab). Nothing changes for any
member until a desktop reports or an administrator chooses: with no row a screen
is captured, as every screen was before this existed.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "e3b7c1a9d5f2"
down_revision: Union[str, None] = "d1a8f3c5e7b9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "member_displays"


def _tables() -> set:
    # Offline (`--sql`) mode cannot inspect, and renders the table.
    if op.get_context().as_sql:
        return set()
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    if _TABLE in _tables():
        return
    op.create_table(
        _TABLE,
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("organization_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("number", sa.SmallInteger(), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("left", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("top", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("is_primary", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("connected", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("capture_enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("last_seen_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_by_user_id", sa.BigInteger(), nullable=True),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], name="fk_member_displays_organization", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_member_displays_user", ondelete="CASCADE"),
        sa.UniqueConstraint("user_id", "number", name="uq_member_displays_user_number"),
    )
    op.create_index("ix_member_displays_organization", _TABLE, ["organization_id"])


def downgrade() -> None:
    op.drop_index("ix_member_displays_organization", table_name=_TABLE)
    op.drop_table(_TABLE)

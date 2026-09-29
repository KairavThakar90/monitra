"""add share_billing to clients

Revision ID: a7c9e2f4b6d8
Revises: f9a1c3e5b7d2

A fifth per-client sharing flag: whether the client portal shows billing
details (each fixed-billing project's budgeted, used and remaining hours,
broken down by task). Defaults to false, unlike the other share_* flags:
billing is the most sensitive section, and no existing client should start
seeing budget figures because a column was added -- an admin turns it on
per client, deliberately.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "a7c9e2f4b6d8"
down_revision: Union[str, None] = "f9a1c3e5b7d2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "clients",
        sa.Column("share_billing", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )


def downgrade() -> None:
    op.drop_column("clients", "share_billing")

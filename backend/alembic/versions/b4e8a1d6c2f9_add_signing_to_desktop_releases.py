"""add signing attestation to desktop_releases

Revision ID: b4e8a1d6c2f9
Revises: d7f3a9c1e5b8

The release pipeline verifies an artifact's code signature itself, after
signing, and reports the outcome when it registers the row. These two columns
carry that outcome so a release can be refused at publish time if it was
registered unsigned.

This is a process gate, not the security boundary: the desktop verifies the
real signature before it runs anything. The column exists so that an operator
cannot publish an unsigned build by mistake, and so a support report can say
who signed a given build.

Additive and reversible. `signed` defaults to false, so every row that exists
today reads as "not known to be signed" -- which is the truth for every build
shipped so far -- and nothing already published changes behaviour (the gate
applies at publish time, and those rows are already published).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "b4e8a1d6c2f9"
down_revision: Union[str, None] = "d7f3a9c1e5b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "desktop_releases",
        sa.Column("signed", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "desktop_releases",
        sa.Column("signer", sa.String(length=255), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("desktop_releases", "signer")
    op.drop_column("desktop_releases", "signed")

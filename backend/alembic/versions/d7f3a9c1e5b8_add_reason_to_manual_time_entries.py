"""add reason to manual_time_entries

Revision ID: d7f3a9c1e5b8
Revises: c4e6a8b0d2f4

Why a manual time request was filed: forgot the timer, tracked against the
wrong task or project, or something else. The desktop's Request dialog asks
for it in place of the Billable box, which it no longer shows -- whether the
time is billable is taken from the project (`billing_type`), as it already
was whenever a client left `is_billable` out.

Nullable, with no default and no backfill. A request filed before this
column existed was never asked for a reason, and neither is one from a client
that does not send it (the web form, older desktop builds); "not given" is
the true value for those rows, and any default would be a reason nobody
chose. The permitted values are enforced at the API (`ManualEntryReason`).

Additive and reversible: one nullable column, dropped on downgrade.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "d7f3a9c1e5b8"
down_revision: Union[str, None] = "c4e6a8b0d2f4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "manual_time_entries",
        sa.Column("reason", sa.String(length=40), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("manual_time_entries", "reason")

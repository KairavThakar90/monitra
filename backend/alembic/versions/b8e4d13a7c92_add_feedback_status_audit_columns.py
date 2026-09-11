"""add feedback status audit columns

Records who moved a piece of feedback through the support workflow and when.

Both columns are nullable with no backfill, and that is the honest shape: rows
that already exist were never moved by anybody, so there is no administrator
and no timestamp to write. Defaulting them to `created_at` and the submitter
would invent an audit entry for a transition that never happened.

Nothing here records *whether an email went out*. That lives in
`email_notifications`, keyed `feedback:<id>:<status>`, and its unique
constraint is the duplicate-send guarantee. A column here would be a second
copy of that fact and a second thing to keep in step.

Revision ID: b8e4d13a7c92
Revises: a4e9b2c71f58
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b8e4d13a7c92"
down_revision: Union[str, None] = "a4e9b2c71f58"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "feedback_requests",
        sa.Column("status_changed_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.add_column(
        "feedback_requests",
        sa.Column("status_changed_by", sa.BigInteger(), nullable=True),
    )
    # SET NULL rather than CASCADE: removing an administrator must not remove
    # the feedback other people submitted. The trail keeps the timestamp and
    # loses only the name.
    op.create_foreign_key(
        "fk_feedback_requests_status_changed_by",
        "feedback_requests",
        "users",
        ["status_changed_by"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_feedback_requests_status_changed_by",
        "feedback_requests",
        type_="foreignkey",
    )
    op.drop_column("feedback_requests", "status_changed_by")
    op.drop_column("feedback_requests", "status_changed_at")

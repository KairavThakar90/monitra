"""add scheduled_job_runs

Revision ID: e9f5a3b7c2d4
Revises: d8e4f2a6b9c1

One row per execution of a scheduled job (email dispatch, the reports, budget
alerts, the roll-up, the WFPM sweeper, the timer reconciler). Until now the
only evidence that a scheduler was calling this backend was an in-memory
counter that a restart erased -- which is how every scheduled email could stop
for weeks without anything saying so. The ledger is what /health?deep=1,
/internal/jobs/status and scripts/email_preflight.py read.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "e9f5a3b7c2d4"
down_revision: Union[str, None] = "d8e4f2a6b9c1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "scheduled_job_runs",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("job", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default=sa.text("'running'")),
        sa.Column("dry_run", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("finished_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("error", sa.String(length=500), nullable=True),
        sa.Column("host", sa.String(length=120), nullable=True),
    )
    op.create_index("idx_scheduled_job_runs_job_started", "scheduled_job_runs", ["job", "started_at"])


def downgrade() -> None:
    op.drop_index("idx_scheduled_job_runs_job_started", table_name="scheduled_job_runs")
    op.drop_table("scheduled_job_runs")

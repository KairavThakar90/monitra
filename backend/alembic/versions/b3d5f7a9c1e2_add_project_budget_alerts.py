"""add project budget alerts

Revision ID: b3d5f7a9c1e2
Revises: 067560e19c4d

Fixed-hours budget alerts (50% / 20% / 10% remaining, and budget exhausted).

* ``projects.budget_version`` — which allocation an alert belongs to. Every
  project that exists when this migration runs is set to **0**: "the budget it
  already had when alerts went live". The first evaluation of a version-0
  budget records thresholds already passed silently and notifies only a
  project that is already over budget — so going live never sends a burst of
  historical 50/20/10 emails. Projects created afterwards start at 1 (every
  crossing is new), and a budget change moves to 2+.
* ``project_budget_alerts`` — one row per (project, budget version, event),
  unique, so concurrent evaluations can never both claim the same alert.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b3d5f7a9c1e2"
down_revision: Union[str, None] = "067560e19c4d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "projects",
        sa.Column("budget_version", sa.Integer(), nullable=False, server_default=sa.text("1")),
    )
    # Existing projects: their current budget predates alerts.
    op.execute("UPDATE projects SET budget_version = 0")

    op.create_table(
        "project_budget_alerts",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("organization_id", sa.BigInteger(), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("budget_version", sa.Integer(), nullable=False),
        sa.Column("event", sa.String(length=20), nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("allocation_seconds", sa.BigInteger(), nullable=False),
        sa.Column("used_seconds", sa.BigInteger(), nullable=False),
        sa.Column("emails_queued_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], name="fk_project_budget_alerts_org", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], name="fk_project_budget_alerts_project", ondelete="CASCADE"),
        sa.UniqueConstraint("project_id", "budget_version", "event", name="uq_project_budget_alerts_event"),
        sa.CheckConstraint(
            "event IN ('remaining_50', 'remaining_20', 'remaining_10', 'exhausted', 'start')",
            name="ck_project_budget_alerts_event",
        ),
        sa.CheckConstraint("outcome IN ('notified', 'baseline')", name="ck_project_budget_alerts_outcome"),
    )
    op.create_index(
        "idx_project_budget_alerts_unqueued", "project_budget_alerts", ["emails_queued_at", "outcome"],
    )


def downgrade() -> None:
    op.drop_index("idx_project_budget_alerts_unqueued", table_name="project_budget_alerts")
    op.drop_table("project_budget_alerts")
    op.drop_column("projects", "budget_version")

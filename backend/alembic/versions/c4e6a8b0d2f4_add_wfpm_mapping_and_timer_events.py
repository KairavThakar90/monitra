"""add the WFPM mapping columns and the WFPM timer-event queue

Revision ID: c4e6a8b0d2f4
Revises: b3c5d7e9f1a2

The WFPM integration (backend/app/WFPM, docs/WFPM_INTEGRATION.md) needs three
things from the schema.

``projects.wfpm_project_id`` / ``tasks.wfpm_task_id``
    The id the same record has in WFPM. They are what lets WFPM address a
    Monitra project or task by its own id, and what a timer started in Monitra
    sends back so WFPM starts the timer against the right task. Text rather
    than a number because the ids belong to another system and are treated as
    opaque. Nullable: every row that exists today, and every project or task
    created in Monitra itself, simply has no WFPM counterpart.

    Each has a partial unique index per organization, so one WFPM record can
    only ever be linked to one Monitra row -- a retried create finds the row
    the first attempt made instead of producing a second one, even when two
    retries race.

``wfpm_timer_events``
    The durable queue behind "a timer started in Monitra starts the timer in
    WFPM". One row per started time entry, unique on the entry, delivered
    after the response and retried by a scheduled sweep.

Additive only: no existing column is changed and nothing is backfilled.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "c4e6a8b0d2f4"
down_revision: Union[str, None] = "b3c5d7e9f1a2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("projects", sa.Column("wfpm_project_id", sa.String(length=255), nullable=True))
    op.create_index(
        "uq_projects_org_wfpm_project_id",
        "projects",
        ["organization_id", "wfpm_project_id"],
        unique=True,
        postgresql_where=sa.text("wfpm_project_id IS NOT NULL"),
    )

    op.add_column("tasks", sa.Column("wfpm_task_id", sa.String(length=255), nullable=True))
    op.create_index(
        "uq_tasks_org_wfpm_task_id",
        "tasks",
        ["organization_id", "wfpm_task_id"],
        unique=True,
        postgresql_where=sa.text("wfpm_task_id IS NOT NULL"),
    )

    op.create_table(
        "wfpm_timer_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("organization_id", sa.BigInteger(), nullable=False),
        sa.Column("event_type", sa.String(length=24), nullable=False),
        sa.Column("time_entry_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("task_id", sa.BigInteger(), nullable=False),
        sa.Column("wfpm_task_id", sa.String(length=255), nullable=False),
        sa.Column("wfpm_project_id", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default=sa.text("6")),
        sa.Column("next_attempt_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("last_attempt_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("sent_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("response_status", sa.Integer(), nullable=True),
        sa.Column("last_error", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("event_type", "time_entry_id", name="uq_wfpm_timer_events_event"),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"],
            name="fk_wfpm_timer_events_organization", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["time_entry_id"], ["time_entries.id"],
            name="fk_wfpm_timer_events_time_entry", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"],
            name="fk_wfpm_timer_events_user", ondelete="CASCADE",
        ),
    )
    op.create_index(
        "idx_wfpm_timer_events_due", "wfpm_timer_events", ["status", "next_attempt_at"],
    )


def downgrade() -> None:
    op.drop_index("idx_wfpm_timer_events_due", table_name="wfpm_timer_events")
    op.drop_table("wfpm_timer_events")
    op.drop_index("uq_tasks_org_wfpm_task_id", table_name="tasks")
    op.drop_column("tasks", "wfpm_task_id")
    op.drop_index("uq_projects_org_wfpm_project_id", table_name="projects")
    op.drop_column("projects", "wfpm_project_id")

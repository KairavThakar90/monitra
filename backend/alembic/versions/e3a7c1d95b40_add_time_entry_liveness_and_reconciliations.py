"""add time_entries.last_seen_at and time_entry_reconciliations

Revision ID: e3a7c1d95b40
Revises: b6d1f8a4c2e9
Create Date: 2026-10-12

Why this migration is required
------------------------------
A running time entry (``end_time IS NULL``) is the server's only statement
that somebody is working. Nothing on the server could ever tell a desktop that
is working from one that is switched off, asleep, crashed, uninstalled or
never relaunched, so such an entry stayed open for as long as nobody stopped
it -- Active Users showed 65 hours over a weekend, and every report that
measures a running entry measured the whole stretch.

``last_seen_at`` is the missing fact: the last instant the desktop that owns
the entry proved it was alive (a heartbeat, or any upload it makes for the
entry). ``time_entry_reconciliations`` is the audit trail for the one thing
the server now does about silence -- closing the entry at the last instant the
evidence supports -- so that every such decision records what was decided,
why, from what evidence and under which thresholds.

Both are additive and safe against every client in the field:

* ``last_seen_at`` is nullable and carries no default. A row that never
  reports keeps NULL, which the application reads as "liveness unknown" and
  handles exactly as it always did. No existing row is touched by this
  migration; in particular no running entry is closed or altered.
* The new table is only ever written by the reconciler.

Downgrade drops both. Run against a database holding data it forgets which
entries were closed by the server and why, but loses no time: the closed
entries keep the ``end_time`` and ``total_seconds`` they were given.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'e3a7c1d95b40'
down_revision: Union[str, None] = 'b6d1f8a4c2e9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'time_entries',
        sa.Column('last_seen_at', sa.TIMESTAMP(timezone=True), nullable=True),
    )

    op.create_table(
        'time_entry_reconciliations',
        sa.Column('id', sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column('organization_id', sa.BigInteger(), nullable=False),
        sa.Column('time_entry_id', sa.BigInteger(), nullable=False),
        sa.Column('user_id', sa.BigInteger(), nullable=False),
        sa.Column('reason', sa.String(length=32), nullable=False),
        sa.Column('decided_by', sa.String(length=64), nullable=False),
        sa.Column('entry_start_time', sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column('last_seen_at', sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column('evidence_at', sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column('silent_for_seconds', sa.Integer(), nullable=True),
        sa.Column('ended_at', sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column('total_seconds', sa.Integer(), nullable=False),
        sa.Column('idle_period_id', sa.BigInteger(), nullable=True),
        sa.Column('policy', sa.JSON(), nullable=True),
        sa.Column('decided_at', sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], name='fk_time_entry_reconciliations_org', ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['time_entry_id'], ['time_entries.id'], name='fk_time_entry_reconciliations_entry', ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], name='fk_time_entry_reconciliations_user', ondelete='CASCADE'),
        sa.CheckConstraint(
            "reason IN ('no_signal', 'idle_unanswered', 'replaced_by_start', 'operator')",
            name='ck_time_entry_reconciliations_reason',
        ),
        sa.CheckConstraint('total_seconds >= 0', name='ck_time_entry_reconciliations_total_nonneg'),
    )
    op.create_index('uq_time_entry_reconciliations_entry', 'time_entry_reconciliations', ['time_entry_id'], unique=True)
    op.create_index('ix_time_entry_reconciliations_user_id', 'time_entry_reconciliations', ['user_id'])
    op.create_index('ix_time_entry_reconciliations_decided_at', 'time_entry_reconciliations', ['decided_at'])


def downgrade() -> None:
    op.drop_index('ix_time_entry_reconciliations_decided_at', table_name='time_entry_reconciliations')
    op.drop_index('ix_time_entry_reconciliations_user_id', table_name='time_entry_reconciliations')
    op.drop_index('uq_time_entry_reconciliations_entry', table_name='time_entry_reconciliations')
    op.drop_table('time_entry_reconciliations')
    op.drop_column('time_entries', 'last_seen_at')

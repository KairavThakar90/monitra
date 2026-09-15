"""add client_op to tasks (idempotent create) and join the two heads

Revision ID: b7c1d2e3f4a5
Revises: a1b2c3d4e5f6, f4a1b2c3d4e5
Create Date: 2026-09-15

Why this migration is required
------------------------------
``POST /api/v1/projects/{id}/tasks`` had no idempotency key. The desktop
creates a task with one request and shows the server's answer; when that
answer is lost -- a timeout, a dropped connection after the server committed
-- the client cannot tell "not created" from "created, reply lost", and the
user's retry created the task a second time. Two rows with the same name,
each a real task the team could track time against.

``client_op`` is the desktop's own key for one submission. A create that
carries a key the organization has already seen is answered with the task
that key produced (200), and the partial unique index makes a second row for
the same key impossible even when two retries race. Nullable and unindexed for
NULL, so the web client and older desktop builds are unaffected.

Two heads
---------
``a1b2c3d4e5f6`` (time-entry ``client_op``) and ``f4a1b2c3d4e5`` (the
administrator role rename) were both descended from the same parent, which
left the migration graph with two heads and made ``alembic upgrade head``
refuse to run at all. This revision names both as its parents, so it is also
the merge point; nothing in either is changed.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'b7c1d2e3f4a5'
down_revision: Union[str, Sequence[str], None] = ('a1b2c3d4e5f6', 'f4a1b2c3d4e5')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'tasks',
        sa.Column('client_op', sa.String(length=255), nullable=True),
    )
    op.create_index(
        'uq_tasks_org_client_op',
        'tasks',
        ['organization_id', 'client_op'],
        unique=True,
        postgresql_where=sa.text('client_op IS NOT NULL'),
    )


def downgrade() -> None:
    op.drop_index('uq_tasks_org_client_op', table_name='tasks')
    op.drop_column('tasks', 'client_op')

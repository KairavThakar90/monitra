"""add client_op to time_entries (idempotent start)

Revision ID: a1b2c3d4e5f6
Revises: c3f7a2d81b64
Create Date: 2026-09-15

Why this migration is required
------------------------------
``POST /time-entries/start`` had no idempotency key. The desktop starts a
timer optimistically and, when the request times out or the response is lost,
re-sends the same start through its durable queue. The backend had already
created the entry, so the retry was refused with 409 "already has an active
timer" -- and nothing in that answer told the client which entry was its own.
The queued stop for that session therefore never learned an entry id, was
cancelled as unresolvable, and the entry stayed ``running`` on the server
until the next launch adopted it as "a timer is still running" with a start
time hours in the past. That is the "tracked time suddenly jumps" report.

Nothing in the existing row identifies the client's intent, so a column is
needed: ``client_op`` is the desktop's own session key. A retried start with
the same key is answered with the existing entry (200) instead of a conflict,
and the partial unique index makes a second row for the same key impossible
even when two retries race.

Nullable and unindexed for NULL, so the web client, older desktop builds and
server-created entries (idle-time reassignment) are unaffected.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, None] = 'c3f7a2d81b64'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'time_entries',
        sa.Column('client_op', sa.String(length=255), nullable=True),
    )
    op.create_index(
        'uq_time_entries_user_client_op',
        'time_entries',
        ['user_id', 'client_op'],
        unique=True,
        postgresql_where=sa.text('client_op IS NOT NULL'),
    )


def downgrade() -> None:
    op.drop_index('uq_time_entries_user_client_op', table_name='time_entries')
    op.drop_column('time_entries', 'client_op')

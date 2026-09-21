"""consolidate project statuses to Active / Paused / Completed

Revision ID: c1a2b3d4e5f6
Revises: f6b2d8e04c13
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "c1a2b3d4e5f6"
down_revision: Union[str, None] = "f6b2d8e04c13"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # The picker offered four project statuses (Active, Pending, To Do,
    # Completed) where the product only wants three: Active, Paused,
    # Completed. Any project left on "To Do" moves to Active -- there is no
    # separate "not started" bucket left to hold it, and a project that
    # exists is being worked on rather than paused.
    op.execute("UPDATE projects SET status_id = 1, status = 'active' WHERE status_id = 3")
    # "Pending" becomes "Paused" in place (same id, same legacy `status`
    # value of 'pending') so ProjectManagementService.PROJECT_STATUS_NAMES
    # only needs its display-facing key renamed, not the constrained legacy
    # column value.
    op.execute("UPDATE project_statuses SET name = 'Paused' WHERE id = 2")
    op.execute("DELETE FROM project_statuses WHERE id = 3")


def downgrade() -> None:
    op.execute("UPDATE project_statuses SET name = 'Pending' WHERE id = 2")
    op.execute(
        "INSERT INTO project_statuses (id, name, color) VALUES (3, 'To Do', '#CBD5E1') "
        "ON CONFLICT (id) DO NOTHING"
    )
    # Projects moved off "To Do" by the upgrade are not restored to it: which
    # ones they were is not recoverable once merged into Active.

"""index task_assignees for visible_task_condition

Revision ID: a3d7c05e1b92
Revises: f6592ada084a

`visible_task_condition` (app/services/task_scope.py) runs on every task
list/detail read for a task-scoped caller (everyone outside
TASK_MANAGER_ROLES) -- including the reload the desktop triggers right
after creating a task. It issues two subqueries against `task_assignees`:
one filtered by `user_id`, one unfiltered projecting `task_id` for a
`NOT IN`. `task_assignees` had no index with `user_id` leading -- only the
unique constraint on `(task_id, user_id)`, `task_id` first -- so both
subqueries forced a scan of a table that grows with every assignment across
the whole deployment. Same pattern as `f6592ada084a` on `project_members`,
just on the sibling table, and exactly what would make a task appear to
"load sometime then show" right after being created: the create response
itself is fast and already indexed, but the list reload that follows it
runs through this condition.

`(user_id, task_id)` serves both subqueries: an index scan for the
`user_id = ?` filter, and an index-only scan for the unfiltered `task_id`
projection.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "a3d7c05e1b92"
down_revision: Union[str, None] = "f6592ada084a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "idx_task_assignees_user_task", "task_assignees", ["user_id", "task_id"]
    )


def downgrade() -> None:
    op.drop_index("idx_task_assignees_user_task", table_name="task_assignees")

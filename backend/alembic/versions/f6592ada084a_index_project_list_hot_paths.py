"""index the project-list hot paths

Revision ID: f6592ada084a
Revises: c1a2b3d4e5f6

`GET /api/v1/projects` (ProjectManagementService.list) filters projects by
`organization_id` + legacy `status`, sorted by `created_at desc, id desc`,
and -- for an employee or a leader's staffed projects -- narrows by an
`IN (SELECT project_id FROM project_members WHERE user_id = ...)` subquery.

`project_members` had no index with `user_id` leading (only the unique
constraint on `(project_id, user_id)`, `project_id` first), so that subquery
was a full scan of a table shared by the whole deployment -- fine with a
handful of rows in dev, a real cost once production accumulates enough
membership rows across every organization. `projects` likewise had indexes
on `(organization_id, status_id)` and `(organization_id, leader_id)` but
nothing covering the string `status` column or the `created_at`/`id` sort
this endpoint actually uses, so Postgres had to sort the filtered set
without index support as a live org's project count grew.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "f6592ada084a"
down_revision: Union[str, None] = "c1a2b3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "idx_project_members_user_org", "project_members", ["user_id", "organization_id"]
    )
    op.create_index(
        "idx_projects_org_status_created", "projects",
        ["organization_id", "status", "created_at", "id"],
    )


def downgrade() -> None:
    op.drop_index("idx_projects_org_status_created", table_name="projects")
    op.drop_index("idx_project_members_user_org", table_name="project_members")

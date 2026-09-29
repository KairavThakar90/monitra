"""add project owner and owner eligibility

Revision ID: 7c65a7896bab
Revises: f9a1c3e5b7d2

Two columns, one concept: a project's Owner is a *project-level*
relationship, not a global role.

* `projects.owner_id` -> `users.id`: who is responsible for this project.
  Nullable, and deliberately left NULL for every existing project -- nobody
  has been assigned as the owner of those, and inventing an assignment would
  be recording a decision that was never made. New projects created through
  `/api/v1/projects` must name one (enforced by `ProjectManagementService`);
  the FK and the `(organization_id, owner_id)` index mirror
  `fk_projects_leader` / `idx_projects_org_leader`.

* `users.can_own_projects`: whether a member may be chosen as a project
  owner. A column of its own for the same reason as `can_add_tasks`
  (f9a1c3e5b7d2) -- `users.permissions` is rebuilt from the role at every
  sign-in -- and a capability rather than a role because owners keep their
  existing roles. Defaults to false: eligibility is granted, never assumed.

The one-time grant below records the business decision that designated the
initial project owners. It matches on username only and changes nothing
where those accounts do not exist (the development database, for one), so
it is safe to run everywhere; afterwards eligibility is managed per member
through `PATCH /members/{id}` like any other member switch.

Downgrade drops both columns, their FK and index -- owner assignments made
since the upgrade are lost with them, which is the only way to undo adding
a column.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "7c65a7896bab"
down_revision: Union[str, None] = "f9a1c3e5b7d2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: The project owners designated when this feature was introduced. Lower-cased
#: because stored usernames do not share one casing ("Deepak", "piyush").
INITIAL_PROJECT_OWNER_USERNAMES = ("deepak", "piyush", "bharat")


def _existing():
    """What of this migration is already in the database.

    A database restored from a dump can carry these objects while its
    `alembic_version` still names the previous revision (the development
    database did, and production was seeded from it). A plain re-run then
    fails on "column already exists" and blocks every later deploy, so each
    object is created only when it is missing. Offline (`--sql`) mode cannot
    inspect, and renders everything as before.
    """
    if op.get_context().as_sql:
        return set(), set(), set(), set()
    inspector = sa.inspect(op.get_bind())
    return (
        {c["name"] for c in inspector.get_columns("users")},
        {c["name"] for c in inspector.get_columns("projects")},
        {fk["name"] for fk in inspector.get_foreign_keys("projects")},
        {ix["name"] for ix in inspector.get_indexes("projects")},
    )


def upgrade() -> None:
    user_columns, project_columns, project_fks, project_indexes = _existing()
    grant_initial_owners = "can_own_projects" not in user_columns
    if "can_own_projects" not in user_columns:
        op.add_column(
            "users",
            sa.Column("can_own_projects", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        )
    if "owner_id" not in project_columns:
        op.add_column("projects", sa.Column("owner_id", sa.BigInteger(), nullable=True))
    if "fk_projects_owner" not in project_fks:
        op.create_foreign_key("fk_projects_owner", "projects", "users", ["owner_id"], ["id"])
    if "idx_projects_org_owner" not in project_indexes:
        op.create_index("idx_projects_org_owner", "projects", ["organization_id", "owner_id"])

    # The one-time grant runs only when the column is being created now: on a
    # database where it already existed, eligibility has since been managed by
    # administrators, and re-granting would silently undo a revocation.
    # A literal list rather than an expanding bind parameter, so the statement
    # also renders in offline (`alembic upgrade --sql`) mode. The values are
    # the constants above, never input.
    if grant_initial_owners:
        usernames = ", ".join(f"'{name}'" for name in INITIAL_PROJECT_OWNER_USERNAMES)
        op.execute(f"UPDATE users SET can_own_projects = true WHERE lower(username) IN ({usernames})")


def downgrade() -> None:
    op.drop_index("idx_projects_org_owner", table_name="projects")
    op.drop_constraint("fk_projects_owner", "projects", type_="foreignkey")
    op.drop_column("projects", "owner_id")
    op.drop_column("users", "can_own_projects")

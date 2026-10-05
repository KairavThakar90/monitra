"""add an optional category to projects

Revision ID: c8d2f6a4b1e7
Revises: b4e8a1d6c2f9

`projects.category` lets whoever creates a project tag it as a Kyle project or
an ST project. It is optional: the column is nullable and carries no default,
so every project that exists today -- and every project created without
choosing one -- reads as "uncategorised" rather than being assigned a
category nobody picked.

The allowed values ('kyle', 'st') are enforced by `ProjectCategory` in
`app/schemas/project_management.py`, the one place that defines them, not by a
database constraint: a later category is then a schema change, not another
migration.

Additive and reversible. The column is added only when it is missing, because
a database restored from a dump can already carry it while its
`alembic_version` still names the previous revision (see 7c65a7896bab).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "c8d2f6a4b1e7"
down_revision: Union[str, None] = "b4e8a1d6c2f9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _project_columns() -> set:
    # Offline (`--sql`) mode cannot inspect, and renders the column as before.
    if op.get_context().as_sql:
        return set()
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns("projects")}


def upgrade() -> None:
    if "category" not in _project_columns():
        op.add_column("projects", sa.Column("category", sa.String(length=20), nullable=True))


def downgrade() -> None:
    op.drop_column("projects", "category")

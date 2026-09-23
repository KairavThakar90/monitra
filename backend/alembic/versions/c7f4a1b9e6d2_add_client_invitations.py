"""add clients, client_invitations, client_projects

Revision ID: c7f4a1b9e6d2
Revises: d4e1f7b3a9c2

Supports the client invitation / project-sharing feature: an admin invites an
external client by email, grants them read-only visibility into a chosen set
of projects, and the client signs in (passwordlessly, via the existing SSO
handoff mechanism) to see only what was shared with them.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "c7f4a1b9e6d2"
down_revision: Union[str, None] = "d4e1f7b3a9c2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "clients",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("organization_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("invited_by", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], name="fk_clients_organization", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_clients_user", ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["invited_by"], ["users.id"], name="fk_clients_invited_by", ondelete="CASCADE"),
    )
    op.create_index("ix_clients_organization_id", "clients", ["organization_id"])
    op.create_index("ix_clients_email", "clients", ["email"])

    op.create_table(
        "client_invitations",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("client_id", sa.BigInteger(), nullable=False),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("token_hash", sa.String(), nullable=False),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("invited_by", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("approved_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("rejected_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"], name="fk_client_invitations_client", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["invited_by"], ["users.id"], name="fk_client_invitations_invited_by", ondelete="CASCADE"),
        sa.UniqueConstraint("token_hash", name="uq_client_invitations_token_hash"),
    )
    op.create_index("ix_client_invitations_token_hash", "client_invitations", ["token_hash"])
    op.create_index("ix_client_invitations_client_id", "client_invitations", ["client_id"])

    op.create_table(
        "client_projects",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("client_id", sa.BigInteger(), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"], name="fk_client_projects_client", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], name="fk_client_projects_project", ondelete="CASCADE"),
        sa.UniqueConstraint("client_id", "project_id", name="uq_client_project"),
    )
    op.create_index("ix_client_projects_client_id", "client_projects", ["client_id"])
    op.create_index("ix_client_projects_project_id", "client_projects", ["project_id"])


def downgrade() -> None:
    op.drop_index("ix_client_projects_project_id", table_name="client_projects")
    op.drop_index("ix_client_projects_client_id", table_name="client_projects")
    op.drop_table("client_projects")

    op.drop_index("ix_client_invitations_client_id", table_name="client_invitations")
    op.drop_index("ix_client_invitations_token_hash", table_name="client_invitations")
    op.drop_table("client_invitations")

    op.drop_index("ix_clients_email", table_name="clients")
    op.drop_index("ix_clients_organization_id", table_name="clients")
    op.drop_table("clients")

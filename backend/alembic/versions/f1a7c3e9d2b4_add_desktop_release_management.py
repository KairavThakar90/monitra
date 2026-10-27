"""add desktop release management: version records, grace periods, install reports

Revision ID: f1a7c3e9d2b4
Revises: e9f5a3b7c2d4

Three tables beside ``desktop_releases`` (which is unchanged):

* ``desktop_release_versions`` -- what an administrator decided about one
  version: title, target platforms, mandatory, grace hours, required floor,
  effective time, lifecycle and who did it. One row per version.
* ``desktop_update_requirements`` -- the grace period: one row per user, per
  platform, per required floor, created by the server on first sight and never
  moved. The unique constraint is the idempotency.
* ``desktop_update_reports`` -- what a desktop reported about an update it
  ran, keyed on its own event id so a retry is one row.

Rollback: ``downgrade`` drops the three tables. Nothing in ``desktop_releases``
depends on them, so the update check keeps answering from the artifact rows
(with their own ``force_update`` / ``min_supported_version``) exactly as before
this revision. A grace period in progress is lost with the table; that is the
documented cost of rolling this revision back.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "f1a7c3e9d2b4"
down_revision: Union[str, None] = "e9f5a3b7c2d4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "desktop_release_versions",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("version", sa.String(length=32), nullable=False),
        sa.Column("version_major", sa.Integer(), nullable=False),
        sa.Column("version_minor", sa.Integer(), nullable=False),
        sa.Column("version_patch", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=120), nullable=True),
        sa.Column("release_notes", sa.Text(), nullable=True),
        sa.Column(
            "target_platforms", sa.String(length=64), nullable=False,
            server_default=sa.text("'win32,darwin'"),
        ),
        sa.Column("mandatory", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("grace_hours", sa.Integer(), nullable=False, server_default=sa.text("24")),
        sa.Column("required_min_version", sa.String(length=32), nullable=True),
        sa.Column("effective_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default=sa.text("'draft'")),
        sa.Column("created_by_user_id", sa.BigInteger(), nullable=True),
        sa.Column("published_by_user_id", sa.BigInteger(), nullable=True),
        sa.Column("withdrawn_by_user_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("validated_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("published_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("withdrawn_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.UniqueConstraint("version", name="uq_desktop_release_versions_version"),
        sa.CheckConstraint(
            "status IN ('draft', 'validated', 'published', 'withdrawn')",
            name="ck_desktop_release_versions_status",
        ),
        sa.CheckConstraint(
            "grace_hours >= 0 AND grace_hours <= 720",
            name="ck_desktop_release_versions_grace_hours",
        ),
    )
    op.create_index(
        "ix_desktop_release_versions_order", "desktop_release_versions",
        ["status", "version_major", "version_minor", "version_patch"],
    )

    op.create_table(
        "desktop_update_requirements",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("platform", sa.String(length=32), nullable=False),
        sa.Column("required_version", sa.String(length=32), nullable=False),
        sa.Column("required_major", sa.Integer(), nullable=False),
        sa.Column("required_minor", sa.Integer(), nullable=False),
        sa.Column("required_patch", sa.Integer(), nullable=False),
        sa.Column("release_version_id", sa.BigInteger(), nullable=True),
        sa.Column("first_seen_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("deadline_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("first_seen_client_version", sa.String(length=32), nullable=True),
        sa.Column("satisfied_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("satisfied_version", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"],
            name="fk_desktop_update_requirements_user", ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "user_id", "platform", "required_version",
            name="uq_desktop_update_requirements_user_platform_floor",
        ),
    )
    op.create_index(
        "ix_desktop_update_requirements_user", "desktop_update_requirements",
        ["user_id", "platform"],
    )
    op.create_index(
        "ix_desktop_update_requirements_release", "desktop_update_requirements",
        ["release_version_id"],
    )

    op.create_table(
        "desktop_update_reports",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("organization_id", sa.BigInteger(), nullable=True),
        sa.Column("platform", sa.String(length=32), nullable=True),
        sa.Column("architecture", sa.String(length=32), nullable=True),
        sa.Column("from_version", sa.String(length=32), nullable=True),
        sa.Column("target_version", sa.String(length=32), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("category", sa.String(length=40), nullable=True),
        sa.Column("detail", sa.String(length=500), nullable=True),
        sa.Column("occurred_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("reported_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("client_event_id", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"],
            name="fk_desktop_update_reports_user", ondelete="CASCADE",
        ),
        sa.UniqueConstraint("user_id", "client_event_id", name="uq_desktop_update_reports_event"),
        sa.CheckConstraint(
            "outcome IN ('success', 'failed', 'not-applied', 'interrupted', "
            "'download-failed', 'verification-failed', 'install-unavailable', 'cancelled')",
            name="ck_desktop_update_reports_outcome",
        ),
    )
    op.create_index(
        "ix_desktop_update_reports_target", "desktop_update_reports",
        ["target_version", "outcome"],
    )


def downgrade() -> None:
    op.drop_index("ix_desktop_update_reports_target", table_name="desktop_update_reports")
    op.drop_table("desktop_update_reports")
    op.drop_index("ix_desktop_update_requirements_release", table_name="desktop_update_requirements")
    op.drop_index("ix_desktop_update_requirements_user", table_name="desktop_update_requirements")
    op.drop_table("desktop_update_requirements")
    op.drop_index("ix_desktop_release_versions_order", table_name="desktop_release_versions")
    op.drop_table("desktop_release_versions")

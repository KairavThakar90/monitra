"""add feedback attachments

Lets a piece of feedback carry optional files (screenshots), and makes a
submission that carries them idempotent.

Two changes, both additive:

* `feedback_attachments` -- one row per attached file, pointing at the object in
  the private Google Drive store. The bytes are not in the database. CASCADE
  from `feedback_requests`, so removing a feedback (or the user or organisation
  it cascades from) removes its attachment rows with it.
* `feedback_requests.client_op` -- nullable, with a *partial* unique index on
  `(user_id, client_op)`. Every feedback that exists today has no key and
  keeps none: nothing is backfilled, nothing is rewritten, and a feedback with
  zero attachments reads exactly as before.

Nothing is dropped or altered destructively. Each object is created only when
it is missing, because a database restored from a dump can already carry it
while its `alembic_version` still names the previous revision (see
7c65a7896bab). `downgrade` removes only what this revision added; it does not
touch the files in Drive, which the orphan sweep
(`scripts/feedback_attachment_sweep.py`) cleans up.

Revision ID: d4a7e1c93b58
Revises: c8d2f6a4b1e7
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "d4a7e1c93b58"
down_revision: Union[str, None] = "c8d2f6a4b1e7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _inspector():
    # Offline (`--sql`) mode cannot inspect; None means "render everything".
    if op.get_context().as_sql:
        return None
    return sa.inspect(op.get_bind())


def upgrade() -> None:
    inspector = _inspector()

    request_columns = (
        {c["name"] for c in inspector.get_columns("feedback_requests")} if inspector else set()
    )
    if "client_op" not in request_columns:
        op.add_column(
            "feedback_requests",
            sa.Column("client_op", sa.String(length=64), nullable=True),
        )

    request_indexes = (
        {i["name"] for i in inspector.get_indexes("feedback_requests")} if inspector else set()
    )
    if "uq_feedback_requests_user_client_op" not in request_indexes:
        op.create_index(
            "uq_feedback_requests_user_client_op",
            "feedback_requests",
            ["user_id", "client_op"],
            unique=True,
            postgresql_where=sa.text("client_op IS NOT NULL"),
        )

    if inspector is None or not inspector.has_table("feedback_attachments"):
        op.create_table(
            "feedback_attachments",
            sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
            sa.Column("feedback_id", sa.BigInteger(), nullable=False),
            sa.Column("position", sa.SmallInteger(), nullable=False),
            sa.Column("original_filename", sa.String(length=255), nullable=False),
            sa.Column("content_type", sa.String(length=100), nullable=False),
            sa.Column("file_size", sa.Integer(), nullable=False),
            sa.Column("file_name", sa.String(length=255), nullable=False),
            sa.Column("file_path", sa.String(length=500), nullable=True),
            sa.Column("google_drive_file_id", sa.String(length=255), nullable=False),
            sa.Column("google_drive_folder_id", sa.String(length=255), nullable=True),
            sa.Column(
                "created_at", sa.TIMESTAMP(timezone=True),
                server_default=sa.func.now(), nullable=False,
            ),
            sa.ForeignKeyConstraint(
                ["feedback_id"], ["feedback_requests.id"],
                name="fk_feedback_attachments_feedback", ondelete="CASCADE",
            ),
            sa.UniqueConstraint("feedback_id", "position", name="uq_feedback_attachments_position"),
        )
        op.create_index(
            "idx_feedback_attachments_feedback", "feedback_attachments", ["feedback_id"],
        )


def downgrade() -> None:
    inspector = _inspector()

    if inspector is None or inspector.has_table("feedback_attachments"):
        op.drop_index("idx_feedback_attachments_feedback", table_name="feedback_attachments")
        op.drop_table("feedback_attachments")

    request_indexes = (
        {i["name"] for i in inspector.get_indexes("feedback_requests")} if inspector else set()
    )
    if inspector is None or "uq_feedback_requests_user_client_op" in request_indexes:
        op.drop_index("uq_feedback_requests_user_client_op", table_name="feedback_requests")

    request_columns = (
        {c["name"] for c in inspector.get_columns("feedback_requests")} if inspector else set()
    )
    if inspector is None or "client_op" in request_columns:
        op.drop_column("feedback_requests", "client_op")

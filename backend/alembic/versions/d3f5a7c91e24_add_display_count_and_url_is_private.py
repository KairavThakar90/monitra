"""add display_count to screenshots and is_private to URL usage

Revision ID: d3f5a7c91e24
Revises: b7c1d2e3f4a5
Create Date: 2026-09-15

Why this migration is required
------------------------------
Two desktop capabilities need one column each, and neither changes the shape of
anything already stored.

``time_entry_screenshots.display_count``
    The desktop now captures every attached display and composites them into a
    single merged image. It remains **one screenshot event: one row, one Drive
    object**, so this is metadata describing that single image, not a count of
    screenshots and never a reason to write more than one row. NOT NULL with a
    server default of 1, because a row written before merged capture existed
    genuinely does contain exactly one display — this is a statement of fact
    about those rows, not a placeholder standing in for an unknown.

``time_entry_url_usage.is_private``
    Whether a browsing segment happened in a private/incognito window.
    Deliberately **nullable**, and the three states are distinct: true and
    false are findings the client actually made, NULL means it could not
    determine the state — a platform with no UI Automation, a browser that
    exposes no marker, or a client older than the detector. Backfilling
    existing rows to false would assert that windows nobody ever inspected
    were not private, which is a claim this codebase does not make about
    unobserved things.

Safety
------
Both are additive. ``display_count`` takes a server default so the ALTER does
not rewrite existing rows on PostgreSQL 11+, and ``is_private`` is nullable so
it needs no default at all. Nothing is backfilled, no existing column changes
type, and no index is added — neither column is queried on its own. Both are
reversible without data loss beyond the new columns themselves.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d3f5a7c91e24"
down_revision: Union[str, None] = "b7c1d2e3f4a5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


SCREENSHOTS_TABLE = "time_entry_screenshots"
URL_USAGE_TABLE = "time_entry_url_usage"


def _has_column(table: str, column: str) -> bool:
    """Whether the column is already present.

    Checked rather than assumed so this revision is safe to re-run against a
    database where it was applied by hand, and so it does not fail on an
    environment that is ahead of its stamped revision.
    """
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if table not in inspector.get_table_names():
        return True  # nothing to add to a table that is not there
    return column in {col["name"] for col in inspector.get_columns(table)}


def upgrade() -> None:
    if not _has_column(SCREENSHOTS_TABLE, "display_count"):
        op.add_column(
            SCREENSHOTS_TABLE,
            sa.Column(
                "display_count",
                sa.SmallInteger(),
                nullable=False,
                server_default="1",
                comment=(
                    "Physical displays composited into this one image. One "
                    "screenshot event is always one row; this never implies "
                    "more than one screenshot."
                ),
            ),
        )

    if not _has_column(URL_USAGE_TABLE, "is_private"):
        op.add_column(
            URL_USAGE_TABLE,
            sa.Column(
                "is_private",
                sa.Boolean(),
                nullable=True,
                comment=(
                    "Private/incognito window. NULL means the client could "
                    "not determine it, which is distinct from false."
                ),
            ),
        )


def downgrade() -> None:
    if _has_column(URL_USAGE_TABLE, "is_private"):
        op.drop_column(URL_USAGE_TABLE, "is_private")
    if _has_column(SCREENSHOTS_TABLE, "display_count"):
        op.drop_column(SCREENSHOTS_TABLE, "display_count")

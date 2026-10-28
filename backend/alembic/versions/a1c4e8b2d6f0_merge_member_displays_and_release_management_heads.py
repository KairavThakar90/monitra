"""merge the member_displays and desktop release management heads

Revision ID: a1c4e8b2d6f0
Revises: e3b7c1a9d5f2, f1a7c3e9d2b4

The per-member switches, the member_displays table (e3b7c1a9d5f2) and desktop
release management (f1a7c3e9d2b4) were written off the same parent
(e9f5a3b7c2d4) on different branches; merging the branches left the chain with
two heads, and ``alembic upgrade head`` -- the step every deploy runs -- refuses
with "Multiple head revisions are present". This merge revision joins them and
changes nothing itself. A database that already carries both or only one of
them upgrades cleanly through it.
"""
from typing import Sequence, Union

revision: str = "a1c4e8b2d6f0"
down_revision: Union[str, Sequence[str], None] = ("e3b7c1a9d5f2", "f1a7c3e9d2b4")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass

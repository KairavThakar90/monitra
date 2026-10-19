"""merge the floor and liveness heads

Revision ID: d8e4f2a6b9c1
Revises: c3f7a1d9e2b4, e3a7c1d95b40

Two migrations were written off the same parent (b6d1f8a4c2e9) on different
branches, so the chain had two heads and ``alembic upgrade head`` -- the step
every deploy runs -- refused with "Multiple head revisions are present". This
merge revision joins them and changes nothing itself. A database that already
carries both (production, applied by hand) or only one (development) upgrades
cleanly through it.
"""
from typing import Sequence, Union

revision: str = "d8e4f2a6b9c1"
down_revision: Union[str, Sequence[str], None] = ("c3f7a1d9e2b4", "e3a7c1d95b40")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass

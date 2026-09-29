"""merge project owner and share billing heads

Revision ID: 067560e19c4d
Revises: ('7c65a7896bab', 'a7c9e2f4b6d8')
Create Date: 2026-09-29 13:15:24.525896

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
# Check if imports is defined in context

# revision identifiers, used by Alembic.
revision: str = '067560e19c4d'
down_revision: Union[str, None] = ('7c65a7896bab', 'a7c9e2f4b6d8')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass

from sqlalchemy import BigInteger, TIMESTAMP, Identity, ForeignKeyConstraint, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column
from datetime import datetime
from app.core.database import Base


class ClientProject(Base):
    """One project shared with one client. The entire access boundary for the
    client portal rests on rows in this table existing -- see
    `ClientProjectRepository.exists` and `ClientPortalService`."""

    __tablename__ = 'client_projects'

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    client_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    project_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        ForeignKeyConstraint(['client_id'], ['clients.id'], name='fk_client_projects_client', ondelete='CASCADE'),
        ForeignKeyConstraint(['project_id'], ['projects.id'], name='fk_client_projects_project', ondelete='CASCADE'),
        UniqueConstraint('client_id', 'project_id', name='uq_client_project'),
    )

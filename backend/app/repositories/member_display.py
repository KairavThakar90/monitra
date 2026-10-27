from typing import Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.member_display import MemberDisplay
from app.models.user import User


class MemberDisplayRepository:
    """Reads and writes of `member_displays`. No commit here: the service owns the transaction."""

    @staticmethod
    def for_user(db: Session, user_id: int, *, for_update: bool = False) -> Dict[int, MemberDisplay]:
        """Every screen known for one member, keyed by screen number."""
        statement = select(MemberDisplay).where(MemberDisplay.user_id == user_id)
        if for_update:
            statement = statement.with_for_update()
        return {row.number: row for row in db.scalars(statement).all()}

    @staticmethod
    def excluded_numbers(db: Session, user_id: int) -> List[int]:
        """The screen numbers an administrator has switched capture off for, ascending."""
        rows = db.scalars(
            select(MemberDisplay.number)
            .where(MemberDisplay.user_id == user_id, MemberDisplay.capture_enabled.is_(False))
            .order_by(MemberDisplay.number)
        ).all()
        return [int(number) for number in rows]

    @staticmethod
    def for_organization(db: Session, organization_id: int) -> List[tuple]:
        """(screen, member) for every screen of an active member of the organization."""
        return list(
            db.execute(
                select(MemberDisplay, User)
                .join(User, User.id == MemberDisplay.user_id)
                .where(
                    MemberDisplay.organization_id == organization_id,
                    User.organization_id == organization_id,
                    User.status == "active",
                )
                .order_by(User.name, MemberDisplay.user_id, MemberDisplay.number)
            ).all()
        )

    @staticmethod
    def add(db: Session, row: MemberDisplay) -> MemberDisplay:
        db.add(row)
        return row

    @staticmethod
    def find_member(db: Session, organization_id: int, user_id: int) -> Optional[User]:
        return db.scalar(
            select(User).where(User.id == user_id, User.organization_id == organization_id)
        )

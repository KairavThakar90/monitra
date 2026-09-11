from sqlalchemy.orm import Session
from sqlalchemy import func, select
from typing import Optional
from app.models.user import User
from app.schemas.user import UserCreate, UserUpdate

class UserRepository:
    @staticmethod
    def get_by_id(db: Session, user_id: int) -> Optional[User]:
        return db.scalar(select(User).where(User.id == user_id))

    @staticmethod
    def list_announcement_recipients(db: Session) -> list[User]:
        """Every account that should be told about a new desktop release.

        Active accounts with an address, ordered by id so a run is repeatable.
        Deliberately not scoped to an organisation: a desktop release is the
        same software for everyone, exactly as `desktop_releases` has no
        `organization_id`, and announcing it per tenant would mean one customer
        being told about a build another was silently not.

        Only `id`, `name` and `email` are ever used by the caller; the whole row
        is returned because that is what every other method here returns and a
        second shape would be one more thing to keep in step.
        """
        return list(
            db.scalars(
                select(User)
                .where(
                    User.is_active.is_(True),
                    User.status == "active",
                    User.email.isnot(None),
                    User.email != "",
                )
                .order_by(User.id)
            ).all()
        )

    @staticmethod
    def get_by_email(db: Session, email: str) -> Optional[User]:
        return db.scalar(select(User).where(User.email == email))

    @staticmethod
    def get_by_normalized_email(db: Session, email: str) -> Optional[User]:
        return db.scalar(select(User).where(func.lower(User.email) == email.lower()))

    @staticmethod
    def get_by_hubstaff_id(db: Session, hubstaff_user_id: str) -> Optional[User]:
        return db.scalar(select(User).where(User.hubstaff_user_id == hubstaff_user_id))

    @staticmethod
    def create(db: Session, user_in: UserCreate) -> User:
        db_user = User(
            organization_id=user_in.organization_id,
            hubstaff_user_id=user_in.hubstaff_user_id,
            username=user_in.username,
            email=user_in.email,
            name=user_in.name,
            designation=user_in.designation,
            role_name=user_in.role_name,
            permissions=user_in.permissions,
            wp_capabilities=user_in.wp_capabilities,
            idle_enabled=user_in.idle_enabled,
            idle_minutes=user_in.idle_minutes,
            capture_frequency=user_in.capture_frequency,
            status=user_in.status
        )
        db.add(db_user)
        db.commit()
        db.refresh(db_user)
        return db_user

    @staticmethod
    def update(db: Session, db_user: User, user_in: UserUpdate) -> User:
        update_data = user_in.model_dump(exclude_unset=True)
        for field, value in update_data.items():
            setattr(db_user, field, value)
        db.add(db_user)
        db.commit()
        db.refresh(db_user)
        return db_user

    @staticmethod
    def list_by_organization(db: Session, organization_id: int, limit: int = 100, offset: int = 0) -> list[User]:
        return list(db.scalars(
            select(User)
            .where(User.organization_id == organization_id)
            .order_by(User.name.asc())
            .offset(offset)
            .limit(limit)
        ).all())

    @staticmethod
    def get_by_id_and_organization(db: Session, user_id: int, organization_id: int) -> Optional[User]:
        return db.scalar(
            select(User)
            .where(User.id == user_id, User.organization_id == organization_id)
        )

    @staticmethod
    def update_active_status(db: Session, db_user: User, is_active: bool) -> User:
        db_user.is_active = is_active
        db.add(db_user)
        db.commit()
        db.refresh(db_user)
        return db_user

from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.client import Client
from app.models.client_project import ClientProject
from app.models.project import Project
from app.models.user import User


class ClientRepository:
    @staticmethod
    def get_by_id(db: Session, client_id: int, organization_id: int) -> Optional[Client]:
        return db.scalar(
            select(Client).where(Client.id == client_id, Client.organization_id == organization_id)
        )

    @staticmethod
    def get_by_email(db: Session, email: str, organization_id: int) -> Optional[Client]:
        return db.scalar(
            select(Client).where(Client.email == email, Client.organization_id == organization_id)
        )

    @staticmethod
    def get_by_user_id(db: Session, user_id: int) -> Optional[Client]:
        return db.scalar(select(Client).where(Client.user_id == user_id))

    @staticmethod
    def create(db: Session, *, organization_id: int, email: str, name: str, invited_by: int) -> Client:
        client = Client(
            organization_id=organization_id,
            email=email,
            name=name,
            status="pending",
            invited_by=invited_by,
        )
        db.add(client)
        db.commit()
        db.refresh(client)
        return client

    @staticmethod
    def create_client_user(db: Session, *, organization_id: int, email: str, name: str) -> User:
        """A client's sign-in account: created inactive at invite time and
        activated only once the invitation is approved. No password -- a
        client authenticates through the passwordless magic-link flow."""
        user = User(
            organization_id=organization_id,
            username=email.split("@")[0][:255],
            email=email,
            name=name,
            role_name="client",
            permissions={"clients:view_shared": True},
            is_active=False,
            status="pending",
            capture_frequency=10,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        return user

    @staticmethod
    def set_status(db: Session, client: Client, status: str) -> Client:
        client.status = status
        db.commit()
        db.refresh(client)
        return client

    @staticmethod
    def list_for_organization(
        db: Session, organization_id: int, page: int, limit: int
    ) -> tuple[list[Client], int]:
        base = select(Client).where(Client.organization_id == organization_id)
        total = db.scalar(select(func.count()).select_from(base.subquery()))
        rows = list(
            db.scalars(
                base.order_by(Client.created_at.desc()).offset((page - 1) * limit).limit(limit)
            ).all()
        )
        return rows, total or 0

    @staticmethod
    def projects_for_clients(db: Session, client_ids: list[int]) -> dict[int, list[dict]]:
        """Shared projects (id + name) for a set of clients, for the admin
        table -- the id is what "Edit Projects" needs to pre-select the
        client's current checkboxes; the name is what the table displays."""
        if not client_ids:
            return {}
        rows = db.execute(
            select(ClientProject.client_id, Project.id, Project.project_name)
            .join(Project, Project.id == ClientProject.project_id)
            .where(ClientProject.client_id.in_(client_ids))
        ).all()
        result: dict[int, list[dict]] = {cid: [] for cid in client_ids}
        for client_id, project_id, project_name in rows:
            result.setdefault(client_id, []).append({"id": project_id, "project_name": project_name})
        return result

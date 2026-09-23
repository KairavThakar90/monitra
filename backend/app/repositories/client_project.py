from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models.client_project import ClientProject
from app.models.project import Project


class ClientProjectRepository:
    @staticmethod
    def exists(db: Session, client_id: int, project_id: int) -> bool:
        return (
            db.scalar(
                select(ClientProject.id).where(
                    ClientProject.client_id == client_id,
                    ClientProject.project_id == project_id,
                )
            )
            is not None
        )

    @staticmethod
    def list_project_ids_for_client(db: Session, client_id: int) -> list[int]:
        return list(
            db.scalars(
                select(ClientProject.project_id).where(ClientProject.client_id == client_id)
            ).all()
        )

    @staticmethod
    def list_projects_for_client(db: Session, client_id: int) -> list[Project]:
        return list(
            db.scalars(
                select(Project)
                .join(ClientProject, ClientProject.project_id == Project.id)
                .where(ClientProject.client_id == client_id)
            ).all()
        )

    @staticmethod
    def replace_for_client(db: Session, client_id: int, project_ids: list[int]) -> None:
        """The client's entire shared-project set, replaced atomically.

        Delete-then-insert rather than a diff: the set is small (an admin
        picks a handful of projects from a checkbox list), so there is no
        performance reason to diff it, and a full replace cannot leave a
        stale row behind if a caller's `project_ids` omits one that used to
        be there.
        """
        db.execute(delete(ClientProject).where(ClientProject.client_id == client_id))
        if project_ids:
            db.add_all([
                ClientProject(client_id=client_id, project_id=project_id)
                for project_id in sorted(set(project_ids))
            ])
        db.commit()

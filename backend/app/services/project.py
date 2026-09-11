from sqlalchemy.orm import Session
from fastapi import HTTPException, status
from typing import List
from app.models.project import Project
from app.models.user import User
from app.schemas.project import ProjectCreate, ProjectUpdate
from sqlalchemy import select
from app.models.project_member import ProjectMember
from app.repositories.project_member import ProjectMemberRepository
from app.repositories.project import ProjectRepository
from app.services.project_scope import may_view_project, visible_project_ids

class ProjectService:
    @staticmethod
    def create_project(db: Session, project_in: ProjectCreate, current_user: User) -> Project:
        return ProjectRepository.create(
            db=db,
            project_in=project_in,
            organization_id=current_user.organization_id,
            created_by_user_id=current_user.id
        )

    @staticmethod
    def list_projects(db: Session, current_user: User) -> List[Project]:
        # Admins, managers, and super_admins can see all active projects in their organization
        if current_user.role_name in ["org_admin", "administrator", "super_admin", "manager"]:
            return list(db.scalars(
                select(Project)
                .where(Project.organization_id == current_user.organization_id)
                .where(Project.status != "archived")
            ).all())
        elif current_user.role_name == "employee":
            # Employees can only see active projects in their organization where they are a member
            return list(db.scalars(
                select(Project)
                .join(ProjectMember, Project.id == ProjectMember.project_id)
                .where(Project.organization_id == current_user.organization_id)
                .where(ProjectMember.user_id == current_user.id)
                .where(Project.status != "archived")
            ).all())

        # Anyone else -- in practice a leader, in either spelling -- sees the
        # projects they lead or were staffed onto. This branch used to return
        # an empty list for every unlisted role, so a leader's project list
        # came back empty on every client that reads this route.
        allowed = visible_project_ids(db, current_user)
        if allowed is None:
            return []
        return list(db.scalars(
            select(Project)
            .where(Project.organization_id == current_user.organization_id)
            .where(Project.id.in_(allowed))
            .where(Project.status != "archived")
        ).all())

    @staticmethod
    def get_project(db: Session, project_id: int, current_user: User) -> Project:
        project = ProjectRepository.get_by_id(db, project_id)
        if not project or project.organization_id != current_user.organization_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Project not found"
            )

        # Employees must be a member of the project to retrieve it
        if current_user.role_name == "employee":
            member = ProjectMemberRepository.get_by_project_and_user(db, project_id, current_user.id)
            if not member or member.organization_id != current_user.organization_id:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Project not found"
                )
        # A leader reads their own projects. Same rule, same helper, same 404 as
        # the /api/v1 project routes -- the two must not disagree about what a
        # leader can open.
        elif not may_view_project(db, current_user, project_id):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Project not found"
            )

        return project

    @staticmethod
    def update_project(db: Session, project_id: int, project_in: ProjectUpdate, current_user: User) -> Project:
        project = ProjectRepository.get_by_id(db, project_id)
        if not project or project.organization_id != current_user.organization_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Project not found"
            )
        return ProjectRepository.update(db, project, project_in)

    @staticmethod
    def archive_project(db: Session, project_id: int, current_user: User) -> Project:
        project = ProjectRepository.get_by_id(db, project_id)
        if not project or project.organization_id != current_user.organization_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Project not found"
            )
        return ProjectRepository.archive(db, project)

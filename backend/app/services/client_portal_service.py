from collections import defaultdict
from datetime import date, datetime, timezone
from typing import Optional

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.time_format import to_ist
from app.models.client import Client
from app.models.project import Project
from app.models.project_member import ProjectMember
from app.models.user import User
from app.repositories.client import ClientRepository
from app.repositories.client_project import ClientProjectRepository
from app.repositories.reports import ReportsRepository
from app.repositories.time_entry_screenshot import TimeEntryScreenshotRepository
from app.services.google_drive_service import GoogleDriveError, drive_service
from app.services.time_entry_screenshot import TimeEntryScreenshotService, _build_windows, _overlap_seconds
from app.services.time_tracking import TimeTrackingService

_utc_start = TimeTrackingService._utc_start
_utc_end = TimeTrackingService._utc_end


def _today_ist() -> date:
    """"Today" as the client portal's default day -- the IST calendar date,
    matching every other day-wise figure this system displays."""
    return to_ist(datetime.now(timezone.utc)).date()


def _resolve_range(start_date: Optional[date], end_date: Optional[date]) -> tuple[date, date]:
    """Both ends default to today, giving "just today" when neither is
    supplied -- the same default every other day-wise read in this system
    opens on. A caller picking a wider span (the same 7d/30d/custom presets
    the staff dashboards use) simply supplies both."""
    today = _today_ist()
    resolved_start = start_date if start_date is not None else today
    resolved_end = end_date if end_date is not None else today
    if resolved_start > resolved_end:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "start_date cannot be after end_date.")
    return resolved_start, resolved_end


def _permissions_payload(client: Client) -> dict:
    return {
        "share_member_details": client.share_member_details,
        "share_screenshots": client.share_screenshots,
        "share_tasks": client.share_tasks,
        "share_timing": client.share_timing,
    }


class ClientPortalService:
    """What a signed-in client may read: exactly the projects an admin shared
    with them, and nothing scoped by the staff-facing helpers in
    `project_scope.py`/`member_scope.py`, which know nothing about the client
    role and would answer "no restriction" for it. The one check that matters
    is `ClientProjectRepository.exists`, applied before anything else runs.

    Every read takes an optional `(start_date, end_date)` range (each end
    defaulting to today, in IST) -- the same range shape and default the
    staff/member dashboards use, so the client portal's date filter is the
    same `DateRangeFilter` component rather than a bespoke one.

    Beyond project scoping, every response also respects the four
    `Client.share_*` flags an admin sets per client (see that model): a
    section a client was not granted comes back empty with `permissions`
    saying so, never zeroed-out or fabricated data pretending the section is
    simply quiet.
    """

    @staticmethod
    def _client_for(db: Session, user: User) -> Client:
        client = ClientRepository.get_by_user_id(db, user.id)
        if client is None or client.status != "active":
            raise HTTPException(status.HTTP_403_FORBIDDEN, "This account has no active client access.")
        return client

    @staticmethod
    def _shared_project_ids(db: Session, client: Client) -> list[int]:
        return ClientProjectRepository.list_project_ids_for_client(db, client.id)

    @staticmethod
    def get_my_profile(db: Session, user: User) -> dict:
        """Just enough to drive the portal's own navigation: the client's
        name and which of the four sections they were granted. Deliberately
        its own cheap read rather than folded into `list_my_projects` --
        every client-portal page needs this to decide what to show in its
        shell, and none of them should have to fetch a project list just to
        find out."""
        client = ClientPortalService._client_for(db, user)
        return {"name": client.name, "email": client.email, "permissions": _permissions_payload(client)}

    @staticmethod
    def _scope_project_ids(requested: Optional[list[int]], allowed: list[int]) -> list[int]:
        """`requested`, narrowed to what this client may actually see.

        `None` (no filter picked) means every shared project. A `requested`
        list is intersected with `allowed`, never unioned with it -- a
        project id that is not this client's cannot be smuggled in through
        the filter, and a request that matches nothing returns nothing
        rather than silently falling back to "everything"."""
        if requested is None:
            return allowed
        return sorted(set(requested) & set(allowed))

    @staticmethod
    def list_my_projects(
        db: Session,
        user: User,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        project_ids: Optional[list[int]] = None,
    ) -> dict:
        client = ClientPortalService._client_for(db, user)
        start, end = _resolve_range(start_date, end_date)
        all_projects = ClientProjectRepository.list_projects_for_client(db, client.id)
        allowed_ids = {project.id for project in all_projects}
        scoped_ids = set(ClientPortalService._scope_project_ids(project_ids, sorted(allowed_ids)))
        projects = [project for project in all_projects if project.id in scoped_ids]
        permissions = _permissions_payload(client)
        if not projects:
            return {"start_date": start.isoformat(), "end_date": end.isoformat(), "permissions": permissions, "items": []}

        project_ids = [project.id for project in projects]
        start_time, end_time = _utc_start(start), _utc_end(end)
        seconds_by_project = ReportsRepository.session_seconds_by(
            db, client.organization_id, project_ids, None, start_time, end_time, start, end, "project_id",
        )
        triples = ReportsRepository.session_triples(
            db, client.organization_id, project_ids, None, start_time, end_time, start, end,
        )
        members_by_project: dict[int, set[int]] = defaultdict(set)
        for pid, uid, _tid in triples:
            members_by_project[pid].add(uid)

        items = [
            {
                "id": project.id,
                "project_name": project.project_name,
                "description": project.description,
                "status": project.status,
                "total_tracked_seconds": seconds_by_project.get(project.id, 0) if client.share_timing else None,
                "total_tracked_hours": (
                    round(seconds_by_project.get(project.id, 0) / 3600, 2) if client.share_timing else None
                ),
                "member_count": len(members_by_project.get(project.id, set())) if client.share_member_details else None,
            }
            for project in projects
        ]
        return {"start_date": start.isoformat(), "end_date": end.isoformat(), "permissions": permissions, "items": items}

    @staticmethod
    def list_member_hours(
        db: Session,
        user: User,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        project_ids: Optional[list[int]] = None,
        member_ids: Optional[list[int]] = None,
    ) -> dict:
        """Every member's tracked time across every project shared with this
        client (or a subset of them, and/or a subset of members, when the
        caller filters). Nobody outside those projects' rosters appears here
        -- a client never sees the organization's member directory, only the
        people staffed on the work it was shown. Empty (with `permissions`
        saying so) when this client was not granted member details at all."""
        client = ClientPortalService._client_for(db, user)
        start, end = _resolve_range(start_date, end_date)
        permissions = _permissions_payload(client)
        if not client.share_member_details:
            return {"start_date": start.isoformat(), "end_date": end.isoformat(), "permissions": permissions, "items": []}

        scoped_project_ids = ClientPortalService._scope_project_ids(
            project_ids, ClientPortalService._shared_project_ids(db, client),
        )
        if not scoped_project_ids:
            return {"start_date": start.isoformat(), "end_date": end.isoformat(), "permissions": permissions, "items": []}

        start_time, end_time = _utc_start(start), _utc_end(end)
        seconds_by_member = ReportsRepository.session_seconds_by(
            db, client.organization_id, scoped_project_ids, member_ids, start_time, end_time, start, end, "user_id",
        )
        triples = ReportsRepository.session_triples(
            db, client.organization_id, scoped_project_ids, member_ids, start_time, end_time, start, end,
        )
        projects_by_member: dict[int, set[int]] = defaultdict(set)
        for pid, uid, _tid in triples:
            projects_by_member[uid].add(pid)

        member_ids = [uid for uid, secs in seconds_by_member.items() if secs > 0]
        users = ReportsRepository.users_lookup(db, client.organization_id, member_ids)

        items = [
            {
                "id": member_id,
                "name": (users.get(member_id) or (f"Member {member_id}", None))[0],
                "total_tracked_seconds": seconds if client.share_timing else None,
                "total_tracked_hours": round(seconds / 3600, 2) if client.share_timing else None,
                "project_count": len(projects_by_member.get(member_id, set())),
            }
            for member_id, seconds in seconds_by_member.items()
            if seconds > 0
        ]
        items.sort(key=lambda item: -(item["total_tracked_seconds"] or 0))
        return {"start_date": start.isoformat(), "end_date": end.isoformat(), "permissions": permissions, "items": items}

    @staticmethod
    def list_task_hours(
        db: Session,
        user: User,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        project_ids: Optional[list[int]] = None,
        member_ids: Optional[list[int]] = None,
    ) -> dict:
        """Every task's tracked time across every project shared with this
        client (or a subset of them, and/or a subset of members, when the
        caller filters). Empty (with `permissions` saying so) when this
        client was not granted task visibility at all."""
        client = ClientPortalService._client_for(db, user)
        start, end = _resolve_range(start_date, end_date)
        permissions = _permissions_payload(client)
        if not client.share_tasks:
            return {"start_date": start.isoformat(), "end_date": end.isoformat(), "permissions": permissions, "items": []}

        scoped_project_ids = ClientPortalService._scope_project_ids(
            project_ids, ClientPortalService._shared_project_ids(db, client),
        )
        if not scoped_project_ids:
            return {"start_date": start.isoformat(), "end_date": end.isoformat(), "permissions": permissions, "items": []}

        start_time, end_time = _utc_start(start), _utc_end(end)
        seconds_by_task = ReportsRepository.session_seconds_by(
            db, client.organization_id, scoped_project_ids, member_ids, start_time, end_time, start, end, "task_id",
        )
        task_ids = [tid for tid, secs in seconds_by_task.items() if secs > 0]
        tasks = ReportsRepository.tasks_lookup(db, client.organization_id, task_ids)

        items = [
            {
                "id": task_id,
                "task_name": tasks.get(task_id, (f"Task {task_id}", None, "Unknown project"))[0],
                "project_name": tasks.get(task_id, (None, None, "Unknown project"))[2],
                "total_tracked_seconds": seconds if client.share_timing else None,
                "total_tracked_hours": round(seconds / 3600, 2) if client.share_timing else None,
            }
            for task_id, seconds in seconds_by_task.items()
            if seconds > 0
        ]
        items.sort(key=lambda item: -(item["total_tracked_seconds"] or 0))
        return {"start_date": start.isoformat(), "end_date": end.isoformat(), "permissions": permissions, "items": items}

    @staticmethod
    def get_project_detail(
        db: Session,
        user: User,
        project_id: int,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
    ) -> dict:
        client = ClientPortalService._client_for(db, user)
        if not ClientProjectRepository.exists(db, client.id, project_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found.")

        project = db.get(Project, project_id)
        if project is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found.")

        start, end = _resolve_range(start_date, end_date)
        organization_id = client.organization_id
        start_time, end_time = _utc_start(start), _utc_end(end)

        tasks: list[dict] = []
        if client.share_tasks:
            tasks_by_project = ReportsRepository.active_tasks_by_project(db, organization_id, [project_id])
            task_seconds = ReportsRepository.session_seconds_by(
                db, organization_id, [project_id], None, start_time, end_time, start, end, "task_id",
            )
            tasks = [
                {
                    "id": task.id,
                    "task_name": task.task_name,
                    "status": task.status,
                    "total_tracked_seconds": task_seconds.get(task.id, 0) if client.share_timing else None,
                    "total_tracked_hours": (
                        round(task_seconds.get(task.id, 0) / 3600, 2) if client.share_timing else None
                    ),
                }
                for task in tasks_by_project.get(project_id, [])
            ]
            tasks.sort(key=lambda item: -(item["total_tracked_seconds"] or 0))

        member_items: list[dict] = []
        if client.share_member_details:
            members = list(
                db.execute(
                    select(User.id, User.name, User.designation)
                    .join(ProjectMember, ProjectMember.user_id == User.id)
                    .where(ProjectMember.project_id == project_id)
                    .order_by(User.name)
                ).all()
            )
            member_seconds = ReportsRepository.session_seconds_by(
                db, organization_id, [project_id], None, start_time, end_time, start, end, "user_id",
            )
            member_items = [
                {
                    "id": member_id,
                    "name": name,
                    "designation": designation,
                    "total_tracked_seconds": member_seconds.get(member_id, 0) if client.share_timing else None,
                    "total_tracked_hours": (
                        round(member_seconds.get(member_id, 0) / 3600, 2) if client.share_timing else None
                    ),
                }
                for member_id, name, designation in members
            ]
            member_items.sort(key=lambda item: -(item["total_tracked_seconds"] or 0))

        project_seconds = None
        if client.share_timing:
            project_seconds = ReportsRepository.session_seconds_by(
                db, organization_id, [project_id], None, start_time, end_time, start, end, "project_id",
            ).get(project_id, 0)

        return {
            "id": project.id,
            "project_name": project.project_name,
            "description": project.description,
            "status": project.status,
            "deadline": project.deadline,
            "project_start_date": project.start_date,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "permissions": _permissions_payload(client),
            "total_tracked_seconds": project_seconds,
            "total_tracked_hours": round(project_seconds / 3600, 2) if project_seconds is not None else None,
            "total_members": len(member_items),
            "tasks": tasks,
            "members": member_items,
        }

    # ------------------------------------------------------------ screenshots

    @staticmethod
    def list_project_screenshots(
        db: Session,
        user: User,
        project_id: int,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
    ) -> dict:
        """Screenshots captured while tracking time against this shared
        project, in a date range. Empty (with `permissions` saying so) when
        this client was not granted screenshot access -- never a 403, so
        this behaves exactly like the other three sections when disabled
        rather than singling itself out."""
        client = ClientPortalService._client_for(db, user)
        if not ClientProjectRepository.exists(db, client.id, project_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found.")

        start, end = _resolve_range(start_date, end_date)
        permissions = _permissions_payload(client)
        if not client.share_screenshots:
            return {"start_date": start.isoformat(), "end_date": end.isoformat(), "permissions": permissions, "items": []}

        start_time, end_time = _utc_start(start), _utc_end(end)
        rows = TimeEntryScreenshotRepository.list_screenshots_by_project(
            db, client.organization_id, project_id, start_time, end_time,
        )
        items = [
            {
                "id": row.id,
                "captured_at": row.captured_at,
                "width": row.width,
                "height": row.height,
            }
            for row in rows
        ]
        return {"start_date": start.isoformat(), "end_date": end.isoformat(), "permissions": permissions, "items": items}

    @staticmethod
    def get_project_screenshot_bytes(
        db: Session, user: User, project_id: int, screenshot_id: int,
    ) -> tuple[bytes, str, str]:
        """Stream one screenshot's image bytes, authorised the same way every
        other client-portal read is: the project must be shared with this
        client, and screenshots must be a permission this client was
        granted. The screenshot itself must belong to a time entry tracked
        against *that* project -- not merely to the same organization.

        Proxied through this backend rather than a Drive link, and a
        screenshot outside this client's access answers 404 rather than
        403, for the same reason the staff-facing view endpoint does: a 403
        on a guessed id confirms the id exists.
        """
        client = ClientPortalService._client_for(db, user)
        if not client.share_screenshots or not ClientProjectRepository.exists(db, client.id, project_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Screenshot not found.")

        screenshot, entry = TimeEntryScreenshotRepository.get_with_entry(db, screenshot_id)
        if (
            not screenshot
            or not entry
            or entry.project_id != project_id
            or screenshot.organization_id != client.organization_id
        ):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Screenshot not found.")
        if not screenshot.google_drive_file_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "This screenshot has no stored image")

        try:
            content = drive_service.download_file(screenshot.google_drive_file_id)
        except GoogleDriveError:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Screenshot storage is temporarily unavailable")
        return content, screenshot.mime_type, screenshot.file_name or f"screenshot-{screenshot.id}.webp"

    @staticmethod
    def get_screenshots_grid(
        db: Session,
        user: User,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        project_ids: Optional[list[int]] = None,
    ) -> dict:
        """Every member's captures across every project shared with this
        client (or a filtered subset), grouped member-then-day-then-window --
        the client portal's own Screenshots page, mirroring the shape and
        the grouping `TimeEntryScreenshotService.get_day_grid` builds for
        staff, scoped to shared projects instead of visible members.

        Empty (with `permissions` saying so) when this client was not
        granted screenshot access at all -- the same "empty, never 403"
        contract every other section here follows.

        A capture is included only when it was taken while tracking time
        against one of these projects: each member's "time worked" here is
        their tracked time on the shared project, never their whole day, so
        this can never describe work the client was not shown elsewhere.
        """
        client = ClientPortalService._client_for(db, user)
        start, end = _resolve_range(start_date, end_date)
        permissions = _permissions_payload(client)
        if not client.share_screenshots:
            return {
                "start_date": start.isoformat(), "end_date": end.isoformat(),
                "permissions": permissions, "window_minutes": 10, "members": [],
            }

        span = (end - start).days + 1
        if span > TimeEntryScreenshotService.MAX_GRID_DAYS:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"A screenshot range may cover at most {TimeEntryScreenshotService.MAX_GRID_DAYS} days",
            )

        scoped_project_ids = ClientPortalService._scope_project_ids(
            project_ids, ClientPortalService._shared_project_ids(db, client),
        )
        window_minutes = TimeEntryScreenshotService._window_minutes_for(None)
        if not scoped_project_ids:
            return {
                "start_date": start.isoformat(), "end_date": end.isoformat(),
                "permissions": permissions, "window_minutes": window_minutes, "members": [],
            }

        start_time, end_time = _utc_start(start), _utc_end(end)
        organization_id = client.organization_id

        tagged = TimeEntryScreenshotRepository.list_screenshots_by_projects(
            db, organization_id, scoped_project_ids, start_time, end_time,
        )
        if not tagged:
            return {
                "start_date": start.isoformat(), "end_date": end.isoformat(),
                "permissions": permissions, "window_minutes": window_minutes, "members": [],
            }

        shots: dict[int, dict] = {}
        for member_id, shot in tagged:
            day = TimeEntryScreenshotService._ist_day_of(shot.captured_at)
            shots.setdefault(member_id, {}).setdefault(day, []).append(shot)

        activity: dict[int, dict] = {}
        for member_id, recorded_at, percentage, seconds in (
            TimeEntryScreenshotRepository.get_activity_totals_by_projects(
                db, organization_id, scoped_project_ids, start_time, end_time,
            )
        ):
            if member_id not in shots:
                continue
            day = TimeEntryScreenshotService._ist_day_of(recorded_at)
            activity.setdefault(member_id, {}).setdefault(day, []).append(
                (recorded_at, percentage, seconds)
            )

        intervals: dict[int, list] = {}
        for member_id, began, ended in (
            TimeEntryScreenshotRepository.list_tracked_intervals_by_projects(
                db, organization_id, scoped_project_ids, start_time, end_time,
            )
        ):
            if member_id not in shots:
                continue
            intervals.setdefault(member_id, []).append((began, ended))

        names = {}
        capture_frequency_by_user = {}
        for member in db.query(User).filter(User.id.in_(shots.keys())).all():
            names[member.id] = member.name
            capture_frequency_by_user[member.id] = member.capture_frequency

        task_project_by_entry = TimeEntryScreenshotRepository.get_task_project_names_for_entries(
            db=db, entry_ids={shot.time_entry_id for _, shot in tagged},
        )

        members: list[dict] = []
        for member_id, by_day in shots.items():
            member_intervals = intervals.get(member_id, [])
            member_window_seconds = TimeEntryScreenshotService._window_minutes_for(
                capture_frequency_by_user.get(member_id)
            ) * 60
            days = [
                {
                    "date": day,
                    "windows": _build_windows(
                        member_window_seconds,
                        day_shots,
                        activity.get(member_id, {}).get(day, []),
                        member_intervals,
                        task_project_by_entry,
                    ),
                    "screenshot_count": len(day_shots),
                    "tracked_seconds": _overlap_seconds(
                        member_intervals, _utc_start(day), _utc_end(day),
                    ),
                }
                for day, day_shots in sorted(by_day.items(), reverse=True)
            ]
            members.append({
                "user_id": member_id,
                "user_name": names.get(member_id) or f"Member {member_id}",
                "days": days,
                "screenshot_count": sum(d["screenshot_count"] for d in days),
                "tracked_seconds": sum(d["tracked_seconds"] for d in days),
            })

        members.sort(key=lambda m: (m["user_name"].lower(), m["user_id"]))
        # `_build_windows` stamps the staff view route onto every screenshot;
        # a client cannot use it (it is authorised against staff visibility,
        # not against `Client.share_screenshots`), so every capture here is
        # repointed at this portal's own project-agnostic view endpoint.
        for member in members:
            for day in member["days"]:
                for window in day["windows"]:
                    for shot in window["screenshots"]:
                        shot["view_url"] = f"/api/v1/clients/me/screenshots/{shot['id']}/view"
        return {
            "start_date": start.isoformat(), "end_date": end.isoformat(),
            "permissions": permissions, "window_minutes": window_minutes, "members": members,
        }

    @staticmethod
    def get_screenshot_bytes(db: Session, user: User, screenshot_id: int) -> tuple[bytes, str, str]:
        """Stream one screenshot's image bytes for the Screenshots page --
        the project-agnostic counterpart of `get_project_screenshot_bytes`,
        for a grid that spans every shared project rather than one. Access
        is still authorised the same way: screenshots must be a permission
        this client was granted, and the screenshot's own time entry must
        have been tracked against a project actually shared with them."""
        client = ClientPortalService._client_for(db, user)
        if not client.share_screenshots:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Screenshot not found.")

        screenshot, entry = TimeEntryScreenshotRepository.get_with_entry(db, screenshot_id)
        shared_project_ids = set(ClientPortalService._shared_project_ids(db, client))
        if (
            not screenshot
            or not entry
            or entry.project_id not in shared_project_ids
            or screenshot.organization_id != client.organization_id
        ):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Screenshot not found.")
        if not screenshot.google_drive_file_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "This screenshot has no stored image")

        try:
            content = drive_service.download_file(screenshot.google_drive_file_id)
        except GoogleDriveError:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Screenshot storage is temporarily unavailable")
        return content, screenshot.mime_type, screenshot.file_name or f"screenshot-{screenshot.id}.webp"

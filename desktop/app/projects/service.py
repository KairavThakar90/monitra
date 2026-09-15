from typing import Any, Dict, List, Optional

from app.api.client import ApiClient
from app.api.exceptions import ApiConnectionError, ApiError, ApiHttpError
from core.logging_setup import get_logger

log = get_logger("projects")

#: The largest page the backend will serve (`limit` is capped at 100 on
#: `GET /api/v1/projects`). Asking for the maximum keeps the number of round
#: trips to the minimum the backend allows.
PROJECTS_PAGE_SIZE = 100

#: Upper bound on pages fetched in one call. A guard against a backend that
#: kept answering "there is another page", not a limit anyone is expected to
#: reach: 50 pages is 5,000 projects for one person.
MAX_PROJECT_PAGES = 50


class ProjectService:
    """Service responsible for fetching projects from the backend API."""

    def __init__(self, api_client: ApiClient) -> None:
        """
        Initialize ProjectService.

        :param api_client: Shared instance of ApiClient.
        """
        self.api_client = api_client

    def get_projects(self) -> List[Dict[str, Any]]:
        """
        Fetch every active project scoped to the current user's organization and membership.

        Every page, not the first one. This used to request
        `page=1&limit=20` and stop, so anyone with more than twenty projects
        never saw the rest -- and because the sidebar falls back to the first
        project in the list, the project they were last working in could
        appear to vanish. The backend pages at 100; the pages are walked
        until `pagination.total_pages` is reached.

        `include_tasks=false` because this client never reads the embedded
        array: the sidebar shows project names, and a project's tasks are
        fetched separately from `/projects/{id}/tasks` when one is selected.
        Asking for them meant every project's every active task crossed the
        wire on each of these calls -- and this runs on login and again on
        every refresh round. `task_count` is unaffected.

        :raises ApiError: On session expiry (401), server error, or connection issues.
        :return: List of project dictionaries.
        """
        try:
            items: List[Dict[str, Any]] = []
            page = 1
            while True:
                response = self.api_client.get(
                    f"/api/v1/projects?page={page}&limit={PROJECTS_PAGE_SIZE}&include_tasks=false"
                )
                data = response.json()
                if not (isinstance(data, dict) and "items" in data):
                    # An older backend answering with a bare list has no
                    # pages to walk.
                    return data if isinstance(data, list) else []
                items.extend(data.get("items") or [])
                pagination = data.get("pagination") or {}
                total_pages = int(pagination.get("total_pages") or 1)
                if page >= total_pages or not data.get("items"):
                    break
                page += 1
                if page > MAX_PROJECT_PAGES:
                    log.error(
                        "projects: stopping after %d pages (backend reports %d); "
                        "the list on screen is incomplete", MAX_PROJECT_PAGES, total_pages,
                    )
                    break
            if page > 1:
                log.info("projects: %d loaded across %d pages", len(items), page)
            return items
        except ApiHttpError as e:
            if e.status_code == 401:
                raise ApiError("Session expired. Please log in again.", status_code=401)
            raise ApiError(f"Failed to load projects: Server error (HTTP {e.status_code}).", status_code=e.status_code)
        except ApiConnectionError:
            raise ApiError("Failed to load projects: Network connection error.")
        except Exception as e:
            raise ApiError(f"Failed to load projects: {str(e)}")

    def get_sync_revision(self) -> Optional[Dict[str, Any]]:
        """
        The backend's fingerprint of everything this user can see.

        `GET /api/v1/sync/revision` answers with an opaque `revision` that
        changes whenever a project, task, membership, assignment or one of
        the user's own time entries changes. The dashboard polls this instead
        of the lists themselves and re-reads them only when it moves.

        Returns None when the backend does not have the endpoint (an older
        deployment answers 404), so the caller can fall back to its periodic
        full refresh rather than treat the absence as an error.

        :raises ApiError: On session expiry (401), server error, or connection issues.
        """
        try:
            response = self.api_client.get("/api/v1/sync/revision")
            data = response.json()
            return data if isinstance(data, dict) and data.get("revision") else None
        except ApiHttpError as e:
            if e.status_code == 404:
                return None
            if e.status_code == 401:
                raise ApiError("Session expired. Please log in again.", status_code=401)
            raise ApiError(f"Failed to check for changes: Server error (HTTP {e.status_code}).", status_code=e.status_code)
        except ApiConnectionError:
            raise ApiError("Failed to check for changes: Network connection error.")
        except Exception as e:
            raise ApiError(f"Failed to check for changes: {str(e)}")

from typing import List, Dict, Any, Optional
from app.api.client import ApiClient
from app.api.exceptions import ApiError, ApiHttpError, ApiConnectionError, error_detail
from core.logging_setup import get_logger

log = get_logger("tasks")


def _explain(action: str, exc: ApiHttpError) -> ApiError:
    """
    Turn an HTTP error into a message that says what actually went wrong.

    The backend already explains its refusals ("Task assignee must be
    assigned to this project"); the desktop used to replace that with
    "Server error (HTTP 400)" and log nothing, so the only way to find out
    why a task would not save was to reproduce it against the API by hand.
    The status code is preserved for callers that branch on it, the full
    response body goes to the log for developers, and the user sees the
    backend's own sentence.
    """
    detail = error_detail(exc.response_body)
    log.warning(
        "%s failed: HTTP %s%s", action, exc.status_code,
        f" -- {exc.response_body}" if exc.response_body else "",
    )
    if detail:
        return ApiError(f"{action} failed: {detail}", status_code=exc.status_code)
    return ApiError(
        f"{action} failed: the server rejected the request (HTTP {exc.status_code}).",
        status_code=exc.status_code,
    )


class TaskService:
    """Service responsible for managing project tasks via the backend API."""

    def __init__(self, api_client: ApiClient) -> None:
        """
        Initialize TaskService.
        
        :param api_client: Shared instance of ApiClient.
        """
        self.api_client = api_client

    def get_tasks_for_project(self, project_id: int) -> List[Dict[str, Any]]:
        """
        Fetch tasks nested under the specified project.
        
        :param project_id: Project identifier.
        :raises ApiError: On session expiry (401), missing project (404), or network failures.
        :return: List of task dictionaries.
        """
        try:
            response = self.api_client.get(f"/api/v1/projects/{project_id}/tasks")
            return response.json()
        except ApiHttpError as e:
            if e.status_code == 401:
                raise ApiError("Session expired. Please log in again.", status_code=401)
            if e.status_code == 404:
                raise ApiError("Project not found or access denied.", status_code=404)
            raise ApiError(f"Failed to load tasks: Server error (HTTP {e.status_code}).", status_code=e.status_code)
        except ApiConnectionError:
            raise ApiError("Failed to load tasks: Network connection error.")
        except Exception as e:
            raise ApiError(f"Failed to load tasks: {str(e)}")

    def create_task(
        self,
        project_id: int,
        task_name: str,
        assignee_id: Optional[int] = None,
        status_id: int = 1,
        client_op: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Create a new task nested under the specified project.

        `assignee_id` is optional and is omitted from the payload when it is
        None, which creates an unassigned task. It must never be defaulted to
        the signed-in user: the backend only accepts an active employee who
        is a member of the project as an assignee, so self-assignment is
        refused with HTTP 400 for every admin and leader -- which is what
        made task creation look account-specific.

        `client_op` is this submission's own key. A create whose reply was
        lost -- the server committed it, the response never arrived -- is
        retried with the same key and answered with the task it already
        created, instead of a second task with the same name. An older
        backend ignores the field.

        The creating user is never sent -- the backend derives it from the
        bearer token, which is the only identity that can be trusted.
        """
        payload: Dict[str, Any] = {
            "name": task_name,
            "status_id": status_id
        }
        if assignee_id is not None:
            payload["assignee_id"] = assignee_id
        if client_op:
            payload["client_op"] = client_op
        try:
            response = self.api_client.post(f"/api/v1/projects/{project_id}/tasks", json_data=payload)
            return response.json()
        except ApiHttpError as e:
            if e.status_code == 401:
                raise ApiError("Session expired. Please log in again.", status_code=401)
            if e.status_code == 403:
                raise ApiError("You do not have permission to create tasks.", status_code=403)
            raise _explain("Creating the task", e)
        except ApiConnectionError:
            raise ApiError("Failed to create task: Network connection error.")
        except Exception as e:
            raise ApiError(f"Failed to create task: {str(e)}")
 
    def update_task(self, project_id: int, task_id: int, task_name: str, status_id: int, assignee_id: Optional[int] = None) -> Dict[str, Any]:
        """
        Update an existing task.
        """
        payload = {
            "name": task_name,
            "status_id": status_id
        }
        if assignee_id is not None:
            payload["assignee_id"] = assignee_id
        try:
            response = self.api_client.patch(f"/api/v1/projects/{project_id}/tasks/{task_id}", json_data=payload)
            return response.json()
        except ApiHttpError as e:
            if e.status_code == 401:
                raise ApiError("Session expired. Please log in again.", status_code=401)
            if e.status_code == 403:
                raise ApiError("You do not have permission to update tasks.", status_code=403)
            raise _explain("Updating the task", e)
        except ApiConnectionError:
            raise ApiError("Failed to update task: Network connection error.")
        except Exception as e:
            raise ApiError(f"Failed to update task: {str(e)}")
 
    def delete_task(self, project_id: int, task_id: int) -> Dict[str, Any]:
        """
        Archive (delete) an existing task.
        """
        try:
            response = self.api_client.delete(f"/api/v1/projects/{project_id}/tasks/{task_id}")
            return response.json()
        except ApiHttpError as e:
            if e.status_code == 401:
                raise ApiError("Session expired. Please log in again.", status_code=401)
            if e.status_code == 403:
                raise ApiError("You do not have permission to delete tasks.", status_code=403)
            raise _explain("Deleting the task", e)
        except ApiConnectionError:
            raise ApiError("Failed to delete task: Network connection error.")
        except Exception as e:
            raise ApiError(f"Failed to delete task: {str(e)}")

    def get_task_statuses(self) -> List[Dict[str, Any]]:
        """
        Fetch task status definitions (id, name, color) from the backend.
        """
        try:
            response = self.api_client.get("/api/v1/task-statuses")
            return response.json()
        except ApiHttpError as e:
            raise ApiError(f"Failed to load task statuses: HTTP {e.status_code}")
        except ApiConnectionError:
            raise ApiError("Failed to load task statuses: Network connection error.")
        except Exception as e:
            raise ApiError(f"Failed to load task statuses: {str(e)}")

import unittest
from unittest.mock import MagicMock
import sys
import os

# Inject current desktop directory to sys.path so app module can be found
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.api.client import ApiClient
from app.api.exceptions import ApiHttpError, ApiConnectionError, ApiError
from app.projects.service import ProjectService
from app.tasks.service import TaskService

class TestSelectionServices(unittest.TestCase):
    """Unit test suite for the ProjectService and TaskService layers."""

    def setUp(self) -> None:
        # Mock ApiClient to isolate service tests from real HTTP calls
        self.api_client = MagicMock(spec=ApiClient)
        self.project_service = ProjectService(self.api_client)
        self.task_service = TaskService(self.api_client)

    # ==================================================
    # PROJECTS SERVICE TESTS
    # ==================================================

    def test_successful_project_retrieval(self) -> None:
        mock_response = MagicMock()
        mock_response.json.return_value = [
            {"id": 1, "project_name": "Project Alpha", "status": "active"},
            {"id": 2, "project_name": "Project Beta", "status": "active"}
        ]
        self.api_client.get.return_value = mock_response

        projects = self.project_service.get_projects()
        
        self.api_client.get.assert_called_once_with(
            "/api/v1/projects?page=1&limit=100&include_tasks=false"
        )
        self.assertEqual(len(projects), 2)
        self.assertEqual(projects[0]["project_name"], "Project Alpha")
        self.assertEqual(projects[1]["id"], 2)

    def test_empty_project_response(self) -> None:
        mock_response = MagicMock()
        mock_response.json.return_value = []
        self.api_client.get.return_value = mock_response

        projects = self.project_service.get_projects()
        
        self.assertEqual(len(projects), 0)

    def test_projects_api_error_handling(self) -> None:
        self.api_client.get.side_effect = ApiHttpError(
            status_code=500,
            response_body="Internal Server Error",
            message="Server Error"
        )
        
        with self.assertRaises(ApiError) as context:
            self.project_service.get_projects()
            
        self.assertIn("Server error (HTTP 500)", str(context.exception))

    def test_projects_unauthorized_handling(self) -> None:
        self.api_client.get.side_effect = ApiHttpError(
            status_code=401,
            response_body="Unauthorized",
            message="Unauthorized"
        )
        
        with self.assertRaises(ApiError) as context:
            self.project_service.get_projects()
            
        self.assertIn("Session expired. Please log in again.", str(context.exception))

    # ==================================================
    # TASKS SERVICE TESTS
    # ==================================================

    def test_successful_task_retrieval(self) -> None:
        mock_response = MagicMock()
        mock_response.json.return_value = [
            {"id": 10, "project_id": 1, "task_name": "Task One", "status": "todo"},
            {"id": 11, "project_id": 1, "task_name": "Task Two", "status": "todo"}
        ]
        self.api_client.get.return_value = mock_response

        tasks = self.task_service.get_tasks_for_project(1)
        
        self.api_client.get.assert_called_once_with("/api/v1/projects/1/tasks")
        self.assertEqual(len(tasks), 2)
        self.assertEqual(tasks[0]["task_name"], "Task One")
        self.assertEqual(tasks[1]["project_id"], 1)

    def test_empty_task_response(self) -> None:
        mock_response = MagicMock()
        mock_response.json.return_value = []
        self.api_client.get.return_value = mock_response

        tasks = self.task_service.get_tasks_for_project(1)
        
        self.assertEqual(len(tasks), 0)

    def test_tasks_api_error_handling(self) -> None:
        self.api_client.get.side_effect = ApiHttpError(
            status_code=500,
            response_body="Internal Server Error",
            message="Server Error"
        )
        
        with self.assertRaises(ApiError) as context:
            self.task_service.get_tasks_for_project(1)
            
        self.assertIn("Server error (HTTP 500)", str(context.exception))

    def test_tasks_unauthorized_handling(self) -> None:
        self.api_client.get.side_effect = ApiHttpError(
            status_code=401,
            response_body="Unauthorized",
            message="Unauthorized"
        )
        
        with self.assertRaises(ApiError) as context:
            self.task_service.get_tasks_for_project(1)
            
        self.assertIn("Session expired. Please log in again.", str(context.exception))

    def test_tasks_project_not_found(self) -> None:
        self.api_client.get.side_effect = ApiHttpError(
            status_code=404,
            response_body="Not Found",
            message="Not Found"
        )
        
        with self.assertRaises(ApiError) as context:
            self.task_service.get_tasks_for_project(999)
            
        self.assertIn("Project not found or access denied.", str(context.exception))

# End of tests


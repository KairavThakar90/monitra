import unittest
from unittest.mock import MagicMock
import sys
import os

# Inject desktop dir to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.api.client import ApiClient
from app.projects.service import ProjectService
from app.tasks.service import TaskService
from sync.local_cache import LocalCache

class TestProjectsTasksIntegration(unittest.TestCase):
    def setUp(self):
        self.api_client = MagicMock(spec=ApiClient)
        self.project_service = ProjectService(self.api_client)
        self.task_service = TaskService(self.api_client)
        
        # In-memory LocalCache for testing SQLite caching
        self.local_cache = LocalCache(":memory:")

    def tearDown(self):
        self.local_cache.close()

    def test_project_service_list_extraction(self):
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "items": [
                {"id": 1, "project_name": "Test Project 1", "status": {"id": 1, "name": "Active", "color": "#3B82F6"}},
                {"id": 2, "project_name": "Test Project 2", "status": {"id": 2, "name": "Pending", "color": "#F59E0B"}}
            ],
            "pagination": {"page": 1, "limit": 20, "total": 2}
        }
        self.api_client.get.return_value = mock_response

        projects = self.project_service.get_projects()
        
        # include_tasks=false: this client fetches a project's tasks from
        # /projects/{id}/tasks when one is selected and never reads the array
        # embedded in the list.
        self.api_client.get.assert_called_once_with(
            "/api/v1/projects?page=1&limit=20&include_tasks=false"
        )
        self.assertEqual(len(projects), 2)
        self.assertEqual(projects[0]["project_name"], "Test Project 1")
        self.assertEqual(projects[0]["status"]["name"], "Active")

    def test_task_service_routes_and_payloads(self):
        # 1. Fetch tasks
        mock_get_response = MagicMock()
        mock_get_response.json.return_value = [
            {"id": 10, "project_id": 1, "name": "Task 1", "status": {"id": 1, "name": "Todo", "color": "#64748B"}}
        ]
        self.api_client.get.return_value = mock_get_response
        tasks = self.task_service.get_tasks_for_project(1)
        self.api_client.get.assert_any_call("/api/v1/projects/1/tasks")
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["name"], "Task 1")

        # 2. Update task PATCH payload
        mock_patch_response = MagicMock()
        mock_patch_response.json.return_value = {"id": 10, "name": "Updated Task", "status_id": 2}
        self.api_client.patch.return_value = mock_patch_response
        updated_task = self.task_service.update_task(1, 10, "Updated Task", 2)
        self.api_client.patch.assert_called_once_with(
            "/api/v1/projects/1/tasks/10",
            json_data={"name": "Updated Task", "status_id": 2}
        )
        self.assertEqual(updated_task["name"], "Updated Task")

        # 3. Create task POST payload
        mock_post_response = MagicMock()
        mock_post_response.json.return_value = {"id": 11, "name": "New Task", "assignee_id": 5, "status_id": 1}
        self.api_client.post.return_value = mock_post_response
        new_task = self.task_service.create_task(1, "New Task", 5)
        self.api_client.post.assert_called_once_with(
            "/api/v1/projects/1/tasks",
            json_data={"name": "New Task", "assignee_id": 5, "status_id": 1}
        )
        self.assertEqual(new_task["name"], "New Task")

        # 4. Fetch status definitions
        mock_status_response = MagicMock()
        mock_status_response.json.return_value = [
            {"id": 1, "name": "Todo", "color": "#64748B"},
            {"id": 2, "name": "In Progress", "color": "#3B82F6"}
        ]
        self.api_client.get.return_value = mock_status_response
        statuses = self.task_service.get_task_statuses()
        self.api_client.get.assert_any_call("/api/v1/task-statuses")
        self.assertEqual(len(statuses), 2)

    def test_local_cache_status_caching(self):
        statuses = [
            {"id": 1, "name": "Todo", "color": "#64748B"},
            {"id": 2, "name": "In Progress", "color": "#3B82F6"}
        ]
        
        # Verify initial state is empty
        cached = self.local_cache.get_cached_task_statuses()
        self.assertIsNone(cached)
        
        # Save to database
        self.local_cache.cache_task_statuses(statuses)
        
        # Retrieve and verify
        cached = self.local_cache.get_cached_task_statuses()
        self.assertIsNotNone(cached)
        self.assertEqual(len(cached), 2)
        self.assertEqual(cached[0]["name"], "Todo")
        self.assertEqual(cached[0]["color"], "#64748B")
        self.assertEqual(cached[1]["name"], "In Progress")

if __name__ == "__main__":
    unittest.main()

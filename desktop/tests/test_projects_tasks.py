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
            "pagination": {"page": 1, "limit": 100, "total": 2, "total_pages": 1}
        }
        self.api_client.get.return_value = mock_response

        projects = self.project_service.get_projects()

        # include_tasks=false: this client fetches a project's tasks from
        # /projects/{id}/tasks when one is selected and never reads the array
        # embedded in the list. limit=100 is the largest page the backend
        # serves, so one page covers most users in one round trip.
        self.api_client.get.assert_called_once_with(
            "/api/v1/projects?page=1&limit=100&include_tasks=false"
        )
        self.assertEqual(len(projects), 2)
        self.assertEqual(projects[0]["project_name"], "Test Project 1")
        self.assertEqual(projects[0]["status"]["name"], "Active")

    def test_project_service_walks_every_page(self):
        """A user with more projects than one page never saw the rest: the
        request was `page=1&limit=20` and stopped, and because the dashboard
        falls back to the first project in the list, the one they were last
        in could appear to vanish."""
        def page(items, page_number, total_pages):
            response = MagicMock()
            response.json.return_value = {
                "items": items,
                "pagination": {"page": page_number, "limit": 100, "total": 250, "total_pages": total_pages},
            }
            return response

        self.api_client.get.side_effect = [
            page([{"id": i} for i in range(1, 101)], 1, 3),
            page([{"id": i} for i in range(101, 201)], 2, 3),
            page([{"id": i} for i in range(201, 251)], 3, 3),
        ]

        projects = self.project_service.get_projects()

        self.assertEqual(len(projects), 250)
        self.assertEqual([p["id"] for p in projects][:3], [1, 2, 3])
        self.assertEqual(projects[-1]["id"], 250)
        self.assertEqual(
            [call.args[0] for call in self.api_client.get.call_args_list],
            [f"/api/v1/projects?page={n}&limit=100&include_tasks=false" for n in (1, 2, 3)],
        )

    def test_project_service_stops_on_an_empty_page(self):
        """A backend that miscounts pages must not be walked for ever."""
        first = MagicMock()
        first.json.return_value = {"items": [{"id": 1}], "pagination": {"total_pages": 5}}
        empty = MagicMock()
        empty.json.return_value = {"items": [], "pagination": {"total_pages": 5}}
        self.api_client.get.side_effect = [first, empty]

        self.assertEqual(self.project_service.get_projects(), [{"id": 1}])
        self.assertEqual(self.api_client.get.call_count, 2)

    def test_sync_revision_is_none_on_an_older_backend(self):
        from app.api.exceptions import ApiHttpError

        self.api_client.get.side_effect = ApiHttpError(404, "Not Found")
        self.assertIsNone(self.project_service.get_sync_revision())

    def test_sync_revision_returns_the_fingerprint(self):
        response = MagicMock()
        response.json.return_value = {"revision": "abc123", "components": {"projects": "1:x:1"}}
        self.api_client.get.return_value = response

        self.assertEqual(self.project_service.get_sync_revision()["revision"], "abc123")
        self.api_client.get.assert_called_once_with("/api/v1/sync/revision")

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

import { formatApiError } from "./utils";

export interface TaskRead {
  task_name: string;
  description?: string;
  status: string;
  start_date?: string;
  due_date?: string;
  estimated_hours?: number;
  time_tracked_seconds: number;
  completed_at?: string;
  completed_by?: number;
  id: number;
  organization_id: number;
  project_id: number;
  created_by: number;
  created_at: string;
  updated_at: string;
}

export interface TaskCreate {
  task_name: string;
  description?: string;
  start_date?: string;
  due_date?: string;
  estimated_hours?: number;
  assignee_id?: number;
}

export interface TaskUpdate {
  task_name?: string;
  description?: string;
  status?: string;
  start_date?: string;
  due_date?: string;
  estimated_hours?: number;
  completed_at?: string;
  completed_by?: number;
}

const API_BASE_URL = import.meta.env.VITE_API_BASE || import.meta.env.VITE_API_URL || "https://monitra-lvzq.vercel.app/api/v1";

export async function listTasksAPI(token: string, projectId: number): Promise<TaskRead[]> {
  const response = await fetch(`${API_BASE_URL}/projects/${projectId}/tasks`, {
    method: "GET",
    headers: {
      "Authorization": `Bearer ${token}`,
      "Content-Type": "application/json",
    },
  });

  if (!response.ok) {
    if (response.status === 401) {
      throw new Error("Unauthorized");
    }
    let errorDetail = "Failed to fetch tasks";
    try {
      const errorData = await response.json();
      errorDetail = formatApiError(errorData, errorDetail);
    } catch {
      // Ignore
    }
    throw new Error(errorDetail);
  }

  return response.json();
}

export async function createTaskAPI(token: string, projectId: number, task: TaskCreate): Promise<TaskRead> {
  const response = await fetch(`${API_BASE_URL}/projects/${projectId}/tasks`, {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${token}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(task),
  });

  if (!response.ok) {
    if (response.status === 401) {
      throw new Error("Unauthorized");
    }
    let errorDetail = "Failed to create task";
    try {
      const errorData = await response.json();
      errorDetail = formatApiError(errorData, errorDetail);
    } catch {
      // Ignore
    }
    throw new Error(errorDetail);
  }

  return response.json();
}

export async function updateTaskAPI(token: string, projectId: number, taskId: number, task: TaskUpdate): Promise<TaskRead> {
  const response = await fetch(`${API_BASE_URL}/projects/${projectId}/tasks/${taskId}`, {
    method: "PUT",
    headers: {
      "Authorization": `Bearer ${token}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(task),
  });

  if (!response.ok) {
    if (response.status === 401) {
      throw new Error("Unauthorized");
    }
    let errorDetail = "Failed to update task";
    try {
      const errorData = await response.json();
      errorDetail = formatApiError(errorData, errorDetail);
    } catch {
      // Ignore
    }
    throw new Error(errorDetail);
  }

  return response.json();
}

export async function archiveTaskAPI(token: string, projectId: number, taskId: number): Promise<TaskRead> {
  const response = await fetch(`${API_BASE_URL}/projects/${projectId}/tasks/${taskId}/archive`, {
    method: "PATCH",
    headers: {
      "Authorization": `Bearer ${token}`,
      "Content-Type": "application/json",
    },
  });

  if (!response.ok) {
    if (response.status === 401) {
      throw new Error("Unauthorized");
    }
    let errorDetail = "Failed to archive task";
    try {
      const errorData = await response.json();
      errorDetail = formatApiError(errorData, errorDetail);
    } catch {
      // Ignore
    }
    throw new Error(errorDetail);
  }

  return response.json();
}

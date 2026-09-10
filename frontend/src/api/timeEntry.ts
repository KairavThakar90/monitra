import { formatApiError } from "./utils";

const API_BASE_URL = import.meta.env.VITE_API_BASE || import.meta.env.VITE_API_URL || "https://monitra-lvzq.vercel.app/api/v1";

export interface TimeEntryRead {
  id: number;
  organization_id: number;
  user_id: number;
  project_id: number;
  task_id: number;
  start_time: string;
  end_time: string | null;
  total_seconds: number;
  status: string;
  is_manual: boolean;
  is_billable: boolean;
  description: string | null;
  created_at: string;
  updated_at: string;
}

export async function startTimerAPI(
  token: string,
  projectId: number,
  taskId: number,
  isBillable: boolean = true
): Promise<TimeEntryRead> {
  const response = await fetch(`${API_BASE_URL}/time-entries/start`, {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${token}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      project_id: projectId,
      task_id: taskId,
      is_billable: isBillable,
    }),
  });

  if (!response.ok) {
    if (response.status === 401) {
      throw new Error("Unauthorized");
    }
    if (response.status === 409) {
      throw new Error("Conflict: You already have a timer running on another task");
    }
    if (response.status === 403) {
      throw new Error("Forbidden: You don't have permission to track time on this task");
    }
    let errorDetail = "Failed to start timer";
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

export async function stopTimerAPI(token: string, entryId: number): Promise<TimeEntryRead> {
  const response = await fetch(`${API_BASE_URL}/time-entries/${entryId}/stop`, {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${token}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({}),
  });

  if (!response.ok) {
    if (response.status === 401) {
      throw new Error("Unauthorized");
    }
    if (response.status === 409) {
      throw new Error("Conflict: Already stopped");
    }
    if (response.status === 403) {
      throw new Error("Forbidden: You don't have permission to track time on this task");
    }
    let errorDetail = "Failed to stop timer";
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

export async function listTimeEntriesAPI(
  token: string,
  taskId: number,
  status?: string
): Promise<TimeEntryRead[]> {
  let url = `${API_BASE_URL}/time-entries?task_id=${taskId}`;
  if (status) {
    url += `&status=${status}`;
  }
  const response = await fetch(url, {
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
    if (response.status === 403) {
      throw new Error("Forbidden: You don't have permission to track time on this task");
    }
    let errorDetail = "Failed to fetch time entries";
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

// TODO: This only fetches the first 100 entries. Once any project exceeds
// 100 time entries, totals will be silently incorrect. Needs real pagination
// (loop until a response returns fewer than `limit` results) before this
// ships to real users.
export async function listProjectTimeEntriesAPI(
  token: string,
  projectId: number
): Promise<TimeEntryRead[]> {
  const response = await fetch(`${API_BASE_URL}/time-entries?project_id=${projectId}&limit=100`, {
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
    let errorDetail = "Failed to fetch project time entries";
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

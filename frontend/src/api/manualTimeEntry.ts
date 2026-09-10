import { formatApiError } from "./utils";

const API_BASE_URL = import.meta.env.VITE_API_BASE || import.meta.env.VITE_API_URL || "https://monitra-lvzq.vercel.app/api/v1";
export interface ManualTimeEntryCreate {
  project_id: number;
  task_id: number;
  work_date: string;
  total_seconds: number;
  is_billable: boolean;
  description?: string;
}

export interface ManualTimeEntryRead {
  id: number;
  organization_id: number;
  user_id: number;
  project_id: number;
  task_id: number;
  work_date: string;
  start_time: string;
  end_time: string;
  total_seconds: number;
  description: string | null;
  is_billable: boolean;
  approval_status: string;
  approved_by: number | null;
  approved_at: string | null;
  created_at: string;
  updated_at: string;
}

export async function createManualTimeEntryAPI(
  token: string,
  payload: ManualTimeEntryCreate
): Promise<ManualTimeEntryRead> {
  const response = await fetch(`${API_BASE_URL}/manual-time-entries`, {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${token}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(payload),
  });

  if (!response.ok) {
    if (response.status === 401) {
      throw new Error("Unauthorized");
    }
    let errorDetail = "Failed to log manual time";
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

export async function listManualTimeEntriesAPI(
  token: string,
  taskId: number
): Promise<ManualTimeEntryRead[]> {
  const response = await fetch(`${API_BASE_URL}/manual-time-entries?task_id=${taskId}`, {
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
    let errorDetail = "Failed to fetch manual time entries";
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
export async function listProjectManualTimeEntriesAPI(
  token: string,
  projectId: number
): Promise<ManualTimeEntryRead[]> {
  const response = await fetch(`${API_BASE_URL}/manual-time-entries?project_id=${projectId}&limit=100`, {
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
    let errorDetail = "Failed to fetch project manual time entries";
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

import { formatApiError } from "./utils";

export interface ProjectRead {
  project_name: string;
  description?: string;
  status: string;
  start_date?: string;
  completed_at?: string;
  is_billable: boolean;
  time_tracked_seconds: number;
  id: number;
  organization_id: number;
  created_by: number;
  created_at: string;
  updated_at: string;
}

const API_BASE_URL = import.meta.env.VITE_API_BASE || import.meta.env.VITE_API_URL || "https://monitra-lvzq.vercel.app/api/v1";

export async function listProjectsAPI(token: string): Promise<ProjectRead[]> {
  const response = await fetch(`${API_BASE_URL}/projects`, {
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
    let errorDetail = "Failed to fetch projects";
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

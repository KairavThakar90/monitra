import { formatApiError } from "./utils";

const API_BASE_URL = import.meta.env.VITE_API_BASE || import.meta.env.VITE_API_URL || "https://monitra-lvzq.vercel.app/api/v1";

export interface TimeEntryScreenshotRead {
  id: number;
  organization_id: number;
  time_entry_id: number;
  captured_at: string;
  file_path: string;
  monitor_number: number;
  created_at: string;
}

export async function listScreenshotsAPI(
  token: string,
  timeEntryId?: number,
  userId?: number,
  limit: number = 8
): Promise<TimeEntryScreenshotRead[]> {
  let url = `${API_BASE_URL}/time-entry-screenshots?limit=${limit}`;
  if (timeEntryId) {
    url += `&time_entry_id=${timeEntryId}`;
  }
  if (userId) {
    url += `&user_id=${userId}`;
  }
  const response = await fetch(url, {
    method: "GET",
    headers: {
      "Authorization": `Bearer ${token}`,
      "Content-Type": "application/json",
    },
  });

  if (!response.ok) {
    let errorDetail = "Failed to fetch screenshots";
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

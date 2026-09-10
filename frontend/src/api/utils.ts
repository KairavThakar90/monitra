const DEFAULT_API_BASE_URL = "https://monitra-lvzq.vercel.app/api/v1";

const configuredApiBaseUrl = import.meta.env.VITE_API_BASE || import.meta.env.VITE_API_URL;

export const API_BASE_URL = configuredApiBaseUrl && /^https?:\/\//i.test(configuredApiBaseUrl)
  ? configuredApiBaseUrl.replace(/\/+$/, "")
  : import.meta.env.DEV && configuredApiBaseUrl
    ? configuredApiBaseUrl.replace(/\/+$/, "")
    : DEFAULT_API_BASE_URL;

export function formatApiError(errorData: any, defaultMessage: string = "Request failed"): string {
  if (!errorData || !errorData.detail) {
    return defaultMessage;
  }

  const detail = errorData.detail;

  if (typeof detail === "string") {
    return detail;
  }

  if (Array.isArray(detail)) {
    return detail
      .map((err: any) => {
        const field = err.loc ? err.loc.slice(1).join(".") : "";
        const fieldPrefix = field ? `${field}: ` : "";
        return `${fieldPrefix}${err.msg}`;
      })
      .join("; ");
  }

  if (typeof detail === "object" && detail !== null) {
    return detail.message || detail.error || JSON.stringify(detail);
  }

  return defaultMessage;
}

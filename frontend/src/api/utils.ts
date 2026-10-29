// api.peakworkos.com is the GCP-hosted backend's own domain. The old
// default, monitra-lvzq.vercel.app, was a separate deployment with its own
// database; it is retired and must not be reintroduced here.
const DEFAULT_API_BASE_URL = "https://api.peakworkos.com/api/v1";

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
    const lines = detail
      .map((err: any) => {
        if (typeof err === "string") return err;
        if (!err || typeof err.msg !== "string") return "";
        const field = Array.isArray(err.loc) ? err.loc.slice(1).join(".") : "";
        return `${field ? `${field}: ` : ""}${err.msg}`;
      })
      .filter(Boolean);
    return lines.length ? lines.join("; ") : defaultMessage;
  }

  if (typeof detail === "object" && detail !== null) {
    // Only a sentence the server wrote for a person is shown. An object with no
    // such field used to be `JSON.stringify`-ed onto the screen -- raw JSON in a toast
    // or a banner -- and is now the caller's default message instead.
    const text = [detail.message, detail.error].find((value) => typeof value === "string" && value.trim());
    return (text as string | undefined) ?? defaultMessage;
  }

  return defaultMessage;
}

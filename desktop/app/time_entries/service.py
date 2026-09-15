import json
from datetime import datetime, timezone
from typing import Optional, Dict, Any
from app.api.client import ApiClient
from app.api.exceptions import ApiError, ApiHttpError, ApiConnectionError
from core.validation.rules import IDEMPOTENCY_KEY_PATTERN


def _client_now_iso() -> str:
    """This machine's clock, at the moment a request is sent.

    Sent as `client_time` beside the event instant so the backend can place
    the event by *age* (`client_time - started_at`) on its own clock. The
    client's absolute time is never trusted -- it only ever appears in that
    difference, so a clock that is minutes out records exactly the same entry
    as a correct one. Read at send time, not at enqueue time: a queued action
    replayed later must report how old the event is *now*.
    """
    return datetime.now(timezone.utc).isoformat()


def _active_entry_from_conflict(response_body: Optional[str]) -> Optional[Dict[str, Any]]:
    """The running entry the backend attached to a 409, if it sent one.

    Older deployments answer with a bare string; that reads as None here and
    the caller falls back to asking `/time-entries/active`.
    """
    if not response_body:
        return None
    try:
        payload = json.loads(response_body)
    except (ValueError, TypeError):
        return None
    detail = payload.get("detail") if isinstance(payload, dict) else None
    if isinstance(detail, dict) and isinstance(detail.get("active_entry"), dict):
        return detail["active_entry"]
    return None


class ActiveTimerConflict(ApiError):
    """The backend refused a start because another entry is running.

    Carries that entry (`active_entry`, the backend's TimeEntryRead) when the
    backend sent it, so the caller can adopt the server's session instead of
    guessing which entry is live.
    """

    def __init__(self, active_entry: Optional[Dict[str, Any]] = None) -> None:
        super().__init__("User already has an active timer.", status_code=409)
        self.active_entry = active_entry


class TimeEntryService:
    """Service layer coordinating communication with backend time entry endpoints."""

    def __init__(self, api_client: ApiClient) -> None:
        """
        Initialize TimeEntryService.

        :param api_client: Shared ApiClient instance.
        """
        self.api_client = api_client

    def start_time_entry(
        self,
        project_id: int,
        task_id: int,
        started_at: Optional[str] = None,
        client_op: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Create a new time entry on the backend for the selected project and task.

        :param project_id: Project identifier.
        :param task_id: Task identifier.
        :param started_at: ISO-8601 UTC instant the user actually pressed
            Start, on this machine's clock. Sent with `client_time` (see
            `_client_now_iso`), so the backend records how long ago the press
            was rather than trusting this clock's absolute reading -- and so a
            queued or retried start still records when the timer really began.
        :param client_op: This tracking session's own key. The backend stores
            it and answers a retried start with the entry the first attempt
            created, so a lost response can never leave a second entry running
            or a queued stop without an id to stop.
        :raises ActiveTimerConflict: Another entry is running (409); carries it.
        :raises ApiError: On session expiry (401), validation errors, or network drop.
        :return: The created (or, on a retry, the existing) time entry as the
            backend serialises it -- `id`, `start_time`, `server_time`, ...
        """
        payload = {
            "project_id": project_id,
            "task_id": task_id,
            "description": None,
            "is_billable": None
        }
        if started_at:
            payload["started_at"] = started_at
            payload["client_time"] = _client_now_iso()
        # Only a key the backend's validation catalogue accepts is sent. A
        # key that would be rejected (a record persisted by an older build)
        # is simply omitted: the start still works, it is just not replayable.
        if client_op and IDEMPOTENCY_KEY_PATTERN.match(client_op):
            payload["client_op"] = client_op
        try:
            response = self.api_client.post("/time-entries/start", json_data=payload)
            data = response.json()
            if not isinstance(data, dict) or not data.get("id"):
                raise ApiError("Successfully communicated with backend, but response was missing time entry ID.")
            return data
        except ApiHttpError as e:
            if e.status_code == 401:
                raise ApiError("Session expired. Please log in again.", status_code=401)
            if e.status_code == 409:
                raise ActiveTimerConflict(_active_entry_from_conflict(e.response_body))
            if e.status_code == 422:
                raise ApiError("Validation error occurred during time entry start.", status_code=422)
            raise ApiError(f"Failed to start timer on backend: HTTP {e.status_code}.", status_code=e.status_code)
        except ApiConnectionError:
            raise ApiError("Failed to start timer: Network connection error.")
        except ApiError:
            raise
        except Exception as e:
            raise ApiError(f"Failed to start timer: {str(e)}")

    def stop_time_entry(
        self,
        entry_id: int,
        timeout: Optional[float] = None,
        stopped_at: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Stop/finalize the specified active time entry on the backend.

        Idempotent on the backend: stopping an entry that is already stopped
        returns it unchanged (200), so a retry after a lost response is safe
        and the caller always gets the canonical finalized entry.

        :param entry_id: Time entry database ID.
        :param timeout: Optional custom timeout in seconds.
        :param stopped_at: ISO-8601 UTC instant the user actually pressed
            Stop, on this machine's clock; sent with `client_time` so the
            backend places it by age on its own clock. This matters more than
            `started_at`: a stop that is retried for minutes used to leave the
            entry accruing until it landed, so the backend's duration exceeded
            the one the desktop had shown.
        :raises ApiError: On session expiry (401), timer not found (404), or network drop.
        :return: The finalized time entry as the backend serialises it.
        """
        payload = {
            "description": None
        }
        if stopped_at:
            payload["stopped_at"] = stopped_at
            payload["client_time"] = _client_now_iso()
        try:
            response = self.api_client.post(f"/time-entries/{entry_id}/stop", json_data=payload, timeout=timeout)
            return response.json()
        except ApiHttpError as e:
            if e.status_code == 401:
                raise ApiError("Session expired. Please log in again.", status_code=401)
            if e.status_code == 404:
                raise ApiError("Active timer not found on backend.", status_code=404)
            if e.status_code == 409:
                # Older deployments answer a repeated stop with 409; the
                # sync consumer treats that as "already applied".
                raise ApiError("Timer is already stopped.", status_code=409)
            raise ApiError(f"Failed to stop timer on backend: HTTP {e.status_code}.", status_code=e.status_code)
        except ApiConnectionError:
            raise ApiError("Failed to stop timer: Network connection error.")
        except ApiError:
            raise
        except Exception as e:
            raise ApiError(f"Failed to stop timer: {str(e)}")

    def get_active_time_entry(self) -> Dict[str, Any]:
        """
        The signed-in user's running entry, as the backend sees it.

        :return: ``{"entry": <TimeEntryRead> | None, "server_time": <iso>}``.
            Scoped to the caller by the backend itself; nothing to filter here.
        :raises ApiError: On session expiry (401) or network drop.
        """
        try:
            response = self.api_client.get("/time-entries/active")
            data = response.json()
            if not isinstance(data, dict) or "entry" not in data:
                raise ApiError("The backend's active-timer answer was not understood.")
            return data
        except ApiHttpError as e:
            if e.status_code == 401:
                raise ApiError("Session expired. Please log in again.", status_code=401)
            raise ApiError(f"Failed to read the active timer: HTTP {e.status_code}.", status_code=e.status_code)
        except ApiConnectionError:
            raise ApiError("Failed to read the active timer: Network connection error.")
        except ApiError:
            raise
        except Exception as e:
            raise ApiError(f"Failed to read the active timer: {str(e)}")

    def record_app_usage(self, time_entry_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Record a single application usage event on the backend.
        """
        try:
            response = self.api_client.post(f"/time-entries/{time_entry_id}/app-usage", json_data=payload)
            return response.json()
        except ApiHttpError as e:
            raise ApiError(f"Failed to record app usage: HTTP {e.status_code}", status_code=e.status_code)
        except ApiConnectionError:
            raise ApiError("Failed to record app usage: Network connection error")
        except Exception as e:
            raise ApiError(f"Failed to record app usage: {str(e)}")

    def batch_sync_app_usage(self, time_entry_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Batch upload application usage events.
        """
        try:
            response = self.api_client.post(f"/time-entries/{time_entry_id}/app-usage/batch", json_data=payload)
            return response.json()
        except ApiHttpError as e:
            raise ApiError(f"Failed to batch sync app usage: HTTP {e.status_code}", status_code=e.status_code)
        except ApiConnectionError:
            raise ApiError("Failed to batch sync app usage: Network connection error")
        except Exception as e:
            raise ApiError(f"Failed to batch sync app usage: {str(e)}")

    def get_app_usage(self, time_entry_id: int) -> Dict[str, Any]:
        """
        Get detailed app usage logs.
        """
        try:
            response = self.api_client.get(f"/time-entries/{time_entry_id}/app-usage")
            return response.json()
        except ApiHttpError as e:
            raise ApiError(f"Failed to retrieve app usage: HTTP {e.status_code}", status_code=e.status_code)
        except ApiConnectionError:
            raise ApiError("Failed to retrieve app usage: Network connection error")
        except Exception as e:
            raise ApiError(f"Failed to retrieve app usage: {str(e)}")

    def get_app_usage_summary(self, time_entry_id: int) -> Dict[str, Any]:
        """
        Get aggregated summary for a specific time entry.
        """
        try:
            response = self.api_client.get(f"/time-entries/{time_entry_id}/app-usage/summary")
            return response.json()
        except ApiHttpError as e:
            raise ApiError(f"Failed to retrieve app usage summary: HTTP {e.status_code}", status_code=e.status_code)
        except ApiConnectionError:
            raise ApiError("Failed to retrieve app usage summary: Network connection error")
        except Exception as e:
            raise ApiError(f"Failed to retrieve app usage summary: {str(e)}")

    def create_manual_time_entry(
        self,
        project_id: int,
        task_id: int,
        work_date: str,
        total_seconds: int,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
        description: Optional[str] = None,
        is_billable: bool = True,
    ) -> Dict[str, Any]:
        """
        Log a completed work session after the fact, distinct from the live
        Start/Stop timer above.

        Reuses the backend's existing manual-entry endpoint (the same bare
        path convention as /time-entries/start above) rather than a new one:
        it already validates the project/task, computes total_seconds from
        start_time/end_time when both are given, rejects overlap with an
        existing session, and defaults approval_status to 'pending' -- none
        of that is duplicated here.

        :param work_date: ISO date string (YYYY-MM-DD).
        :param start_time: ISO 8601 UTC datetime string, e.g. from
            datetime.isoformat(). Optional; if omitted (with end_time), the
            backend derives the slot from work_date + total_seconds instead.
        :param end_time: ISO 8601 UTC datetime string, paired with start_time.
        :raises ApiError: On session expiry (401), an overlapping time slot
            (409), validation errors (400/422), or network drop.
        :return: The created manual time entry (approval_status='pending').
        """
        payload = {
            "project_id": project_id,
            "task_id": task_id,
            "work_date": work_date,
            "total_seconds": total_seconds,
            "description": description,
            "is_billable": is_billable,
        }
        if start_time is not None and end_time is not None:
            payload["start_time"] = start_time
            payload["end_time"] = end_time
        try:
            response = self.api_client.post("/manual-time-entries", json_data=payload)
            return response.json()
        except ApiHttpError as e:
            if e.status_code == 401:
                raise ApiError("Session expired. Please log in again.", status_code=401)
            if e.status_code == 409:
                raise ApiError(
                    "This time slot overlaps an existing time entry.", status_code=409
                )
            if e.status_code in (400, 422):
                raise ApiError(f"Could not log this entry: {e.response_body}", status_code=e.status_code)
            raise ApiError(f"Failed to save manual time entry: HTTP {e.status_code}.", status_code=e.status_code)
        except ApiConnectionError:
            raise ApiError("Failed to save manual time entry: Network connection error.")
        except Exception as e:
            raise ApiError(f"Failed to save manual time entry: {str(e)}")

    def batch_sync_url_usage(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Batch upload browser URL usage events.
        """
        try:
            response = self.api_client.post("/url-usage/batch", json_data=payload)
            return response.json()
        except ApiHttpError as e:
            raise ApiError(f"Failed to batch sync URL usage: HTTP {e.status_code}", status_code=e.status_code)
        except ApiConnectionError:
            raise ApiError("Failed to batch sync URL usage: Network connection error")
        except Exception as e:
            raise ApiError(f"Failed to batch sync URL usage: {str(e)}")

    def batch_sync_activity(self, time_entry_id: Optional[int], payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Batch upload activity samples.

        The entry id comes first, as it does on every other per-entry upload
        here (record_unwanted_activity, record_adjustment) and as SyncService
        has always called it. With payload first, the caller's positional
        `upload(entry_id, batch)` put the samples dict into the URL path and
        the entry id into the body, so every batch was rejected with a 422
        and no activity ever reached the backend.
        """
        try:
            if time_entry_id:
                try:
                    response = self.api_client.post(f"/time-entries/{time_entry_id}/activity/batch", json_data=payload)
                    return response.json()
                except ApiHttpError as e:
                    if e.status_code != 404:
                        raise
            try:
                response = self.api_client.post("/api/v1/time-entry-activities/batch", json_data=payload)
            except ApiHttpError as e:
                if e.status_code == 404:
                    response = self.api_client.post("/time-entry-activities/batch", json_data=payload)
                else:
                    raise
            return response.json()
        except ApiHttpError as e:
            raise ApiError(f"Failed to batch sync activity: HTTP {e.status_code}", status_code=e.status_code)
        except ApiConnectionError:
            raise ApiError("Failed to batch sync activity: Network connection error")
        except Exception as e:
            raise ApiError(f"Failed to batch sync activity: {str(e)}")

    def upload_screenshot(
        self,
        time_entry_id: int,
        image_bytes: bytes,
        file_name: str,
        metadata: Dict[str, Any],
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        Upload one captured screenshot to the backend.

        The image travels as multipart rather than base64 in JSON: a WebP is
        binary, and base64 would inflate every upload by a third for no benefit.

        `metadata` must carry `client_screenshot_id` — the UUID the backend
        de-duplicates on. A retry after a lost response therefore returns the
        record that already exists instead of creating a second Google Drive
        file, which is the whole reason a client-generated id exists.

        Organization and user are deliberately **not** sent: the backend
        derives them from the authenticated session and the time entry. A
        client that could name its own organization_id could write into
        someone else's.

        :raises ApiError: on any failure, so the caller's queue can retry.
        :return: the stored screenshot record.
        """
        files = {"file": (file_name, image_bytes, "image/webp")}
        try:
            response = self.api_client.post_multipart(
                f"/time-entries/{time_entry_id}/screenshots",
                files=files,
                data={k: str(v) for k, v in metadata.items() if v is not None},
                timeout=timeout,
            )
            return response.json()
        except ApiHttpError as e:
            if e.status_code == 401:
                raise ApiError("Session expired. Please log in again.", status_code=401)
            raise ApiError(
                f"Failed to upload screenshot: HTTP {e.status_code}", status_code=e.status_code
            )
        except ApiConnectionError:
            raise ApiError("Failed to upload screenshot: Network connection error")
        except Exception as e:
            raise ApiError(f"Failed to upload screenshot: {str(e)}")

    def record_unwanted_activity(self, time_entry_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Record one unwanted-activity detection event.
        """
        try:
            response = self.api_client.post(
                f"/time-entries/{time_entry_id}/unwanted-activity", json_data=payload
            )
            return response.json()
        except ApiHttpError as e:
            raise ApiError(f"Failed to record unwanted activity: HTTP {e.status_code}", status_code=e.status_code)
        except ApiConnectionError:
            raise ApiError("Failed to record unwanted activity: Network connection error")
        except Exception as e:
            raise ApiError(f"Failed to record unwanted activity: {str(e)}")

    def record_adjustment(self, time_entry_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Record a time deduction (auditable adjustment; the entry's own
        total_seconds is never modified).
        """
        try:
            response = self.api_client.post(
                f"/time-entries/{time_entry_id}/adjustments", json_data=payload
            )
            return response.json()
        except ApiHttpError as e:
            raise ApiError(f"Failed to record adjustment: HTTP {e.status_code}", status_code=e.status_code)
        except ApiConnectionError:
            raise ApiError("Failed to record adjustment: Network connection error")
        except Exception as e:
            raise ApiError(f"Failed to record adjustment: {str(e)}")


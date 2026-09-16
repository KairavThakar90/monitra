"""
Backend call for the maintenance notice.

One method, one endpoint, in the same shape as `app/updates/service.py`: it
carries the request and hands the answer back. Whether Monitra is under
maintenance is the backend's answer -- an administrator's switch -- and this
client never decides it, infers it from an outage, or caches a guess.
"""
from typing import Any, Dict

from app.api.client import ApiClient, TIMEOUT_FAST
from app.api.exceptions import ApiConnectionError, ApiError, ApiHttpError
from core.logging_setup import get_logger

log = get_logger("maintenance.api")


class MaintenanceApiService:
    """Client for the backend's maintenance-status endpoint."""

    def __init__(self, api_client: ApiClient) -> None:
        self.api_client = api_client

    def get_maintenance_status(self) -> Dict[str, Any]:
        """Whether the maintenance notice is currently active.

        Returns the backend's payload unchanged::

            {"maintenance_mode": bool,
             "updated_at": str | None,
             "server_time": str}

        Every failure is raised as `ApiError` and the caller holds quietly:
        not knowing whether a notice should be shown is not something the
        person tracking time can act on, and it must never affect tracking.
        """
        try:
            response = self.api_client.get("/system/maintenance-status", timeout=TIMEOUT_FAST)
            return response.json()
        except ApiHttpError as exc:
            if exc.status_code == 401:
                raise ApiError("Session expired. Please log in again.", status_code=401)
            # 404 is an older deployment without the endpoint -- a normal state
            # during a rollout, and the same quiet hold as any other failure.
            log.debug("maintenance check failed: HTTP %s", exc.status_code)
            raise ApiError(
                f"Maintenance check failed (HTTP {exc.status_code}).",
                status_code=exc.status_code,
            )
        except ApiConnectionError:
            raise ApiError("Could not check maintenance status: network error.")
        except Exception as exc:  # noqa: BLE001
            raise ApiError(f"Could not check maintenance status: {exc}")

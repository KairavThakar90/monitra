"""
app_usage — Application-usage aggregation for display.

Extracted from `ui.workers.LoadAppUsageWorker` so the logic is a plain,
testable function rather than a QThread subclass. It merges what the backend
has already recorded with what is still queued locally, so usage captured
while offline is visible immediately instead of only after a successful sync.

The summary is for **one IST calendar day**, and both halves of the merge are
filtered by the same day window. It used to ask the backend for everything it
had and to add every pending local row regardless of when it was recorded, so
the Apps tab showed an all-time running total: five hours of editor use from
last Monday were still sitting in this morning's list.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional

from core.logging_setup import get_logger
from core.time_format import ist_day_bounds_utc
from tracking.app_identity import canonical_application_name

log = get_logger("activity.app_usage")

COLOR_PALETTE = [
    "#3B82F6", "#10B981", "#EC4899", "#8B5CF6", "#F97316",
    "#6366F1", "#1DB954", "#4B5563", "#0284C7", "#D97706",
]


def _format_duration(seconds: int) -> str:
    if seconds >= 3600:
        hours, minutes = seconds // 3600, (seconds % 3600) // 60
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    if seconds >= 60:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def _initials(name: str) -> str:
    words = name.split()
    if len(words) >= 2:
        return (words[0][0] + words[1][0]).upper()
    if len(name) >= 2:
        return name[:2].upper()
    return name.upper()


def build_app_usage_summary(
    api_client,
    cache=None,
    user_id: Optional[int] = None,
    day: Optional[date] = None,
) -> List[Dict[str, Any]]:
    """
    Build the ranked application-usage summary for one IST calendar day.

    Runs on a background thread via the shared task pool. It must not touch
    any widget.

    The remote read is issued **before** the local one, deliberately. A pending
    row is deleted locally only after its upload has been acknowledged, so a
    segment that finishes uploading between the two reads is counted once
    (locally) rather than twice. Reading local first would open the opposite
    window: the row could be absent from the local read and present in the
    remote one — or, worse, present in both — and the tab would flash a doubled
    total during every sync.

    :param api_client: ApiClient used to fetch the backend summary.
    :param cache: LocalCache; unsynced local records for the same day are
        merged in, so an offline day still shows its real totals.
    :param user_id: Optional user filter. The backend pins a caller without
        `time_entries:view_all` to their own records regardless.
    :param day: The IST calendar day to summarise. Required in practice; it
        defaults to None only so an older positional call cannot silently ask
        for an all-time total — that case now returns nothing to aggregate.
    :return: Rows ready for rendering, ordered by descending duration.
    """
    durations: Dict[str, int] = {}
    if day is None:
        return []

    start, end = ist_day_bounds_utc(day)
    params: Dict[str, Any] = {
        "start_date": start.isoformat(),
        # Exclusive: `end_date` on this endpoint is inclusive and is what the
        # web client sends. Asking for [start, next start) is what makes one
        # calendar day exact -- no lost final second, no record on the midnight
        # boundary counted in both days.
        "end_before": end.isoformat(),
    }
    if user_id:
        params["user_id"] = user_id

    try:
        response = api_client.get("/app-usage/summary", params=params)
        if response.status_code == 200:
            for entry in response.json().get("applications", []):
                # Historical rows still carry whatever spelling the desktop
                # of the day sent, so they are resolved through the same
                # catalogue before being merged -- otherwise "chrome" from
                # last week and "Google Chrome" from today would be two rows
                # for one browser. A name with no catalogue entry is kept
                # exactly as stored; nothing is dropped and nothing is
                # renamed into a catch-all.
                name = canonical_application_name(entry.get("application_name"))
                if not name:
                    continue
                durations[name] = durations.get(name, 0) + entry.get("duration_seconds", 0)
    except Exception as exc:  # noqa: BLE001
        # A backend failure must not hide locally captured usage.
        log.info("backend app-usage summary unavailable (%s); using local records only", exc)

    if cache is not None:
        try:
            for record in cache.get_unsynced_app_usage_between(
                start.isoformat(), end.isoformat()
            ):
                name = canonical_application_name(record.get("application_name"))
                if not name:
                    continue
                durations[name] = durations.get(name, 0) + record.get("duration_seconds", 0)
        except Exception:  # noqa: BLE001
            log.exception("could not merge pending local app usage")

    total = sum(durations.values())
    rows: List[Dict[str, Any]] = []
    ranked = sorted(durations.items(), key=lambda item: item[1], reverse=True)

    for index, (name, seconds) in enumerate(ranked):
        # The names are already canonical -- `canonical_application_name`
        # ran on both halves of the merge above. There used to be a rename
        # here ("python" -> "Monitra") that only this one view applied, so
        # the desktop's Apps tab and the web reports disagreed about what
        # the same rows were called. One catalogue, applied once.
        display_name = name
        rows.append({
            "name": display_name,
            "application_name": display_name,
            "seconds": seconds,
            "duration_seconds": seconds,
            "time_str": _format_duration(seconds),
            "percentage": round(seconds / total * 100) if total else 0,
            "color": COLOR_PALETTE[index % len(COLOR_PALETTE)],
            "letter": _initials(display_name),
        })
    return rows

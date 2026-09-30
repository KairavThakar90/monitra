"""One member's complete activity log for one IST calendar day.

This module is where the day's numbers are *defined*. The schema module
documents what each total includes; this one computes them, once, from the
same records every other report reads, so a frontend never has to add
entries up itself and can never disagree with the Reports page about the
same day.

The rules that matter
---------------------
* **The day is an IST calendar day** (``app.core.time_format``), expressed as
  a half-open UTC window. Every timestamp compared here is aware UTC.
* **Entries are clipped, never counted whole.** An entry that crosses
  midnight contributes ``23:50 -> 00:00`` to the first day and ``00:00 ->
  00:20`` to the second, and nothing twice. A running entry is closed at
  the server's ``now`` for the purpose of measuring it, and also clipped.
* **Reportable time is net of adjustments**, exactly as
  ``TimeTrackingRepository._net_duration_expression`` nets them: measured
  seconds plus the signed ``time_entry_adjustments``, floored at zero per
  entry. For an entry that lies inside one day this is the identical number
  the Reports and Time Tracking pages show. For an entry that crosses
  midnight each adjustment is attributed to the day containing the instant
  it was recorded (clamped into the entry's own span), so the two days add
  up to the entry's net total.
* **Idle time is what ``TimeEntryIdlePeriodService`` ruled.** ``counted`` on
  the row is the server's decision; this module reads it and never re-decides.
* **Nothing is invented.** Breaks are not recorded, so they are zero. A
  stop reason is derived from stored facts (an idle period answered with
  Stop, a reassignment target) and is otherwise the honest ``stop``.
"""
from collections import OrderedDict, defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Tuple

from sqlalchemy.orm import Session

from app.core.time_format import (
    IST, elapsed_seconds, format_hms, ist_day_end_utc, ist_day_start_utc, ist_today,
)
from app.models.time_entry_idle_period import IdlePeriodAction, IdlePeriodStatus
from app.models.user import User
from app.repositories.member_activity_log import MemberActivityLogRepository
from app.repositories.time_entry_adjustment import TimeEntryAdjustmentRepository
from app.services.member_service import MemberService
from app.services.time_entry_screenshot import TimeEntryScreenshotService, _build_windows

#: The one zone the product reports in. Users carry no timezone column.
REPORTING_TIMEZONE = str(IST.key)


# ── Pure helpers (no database) ───────────────────────────────────────────────


def _as_utc(value: Optional[datetime]) -> Optional[datetime]:
    """A stored timestamp as aware UTC. Naive values are UTC by contract."""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _whole_seconds(began: datetime, ended: datetime) -> int:
    """``round(ended - began)`` floored at zero -- the project's one rounding rule."""
    return max(0, round((ended - began).total_seconds()))


def clip_to_window(
    began: datetime, ended: datetime, window_start: datetime, window_end: datetime
) -> Optional[Tuple[datetime, datetime]]:
    """The part of ``[began, ended)`` inside ``[window_start, window_end)``,
    or ``None`` when the two do not overlap."""
    start = max(began, window_start)
    end = min(ended, window_end)
    if end <= start:
        return None
    return start, end


def day_span(
    began: datetime, ended: datetime, window_start: datetime, window_end: datetime
) -> Optional[Tuple[datetime, datetime, bool, bool]]:
    """Where a span lands on one day: ``(start, end, continues_from,
    continues_into)``, or ``None`` when no whole second of it is on this day.

    Midnight is applied in whole seconds, the unit every duration in this
    system is rounded to. A span that runs 338 ms past midnight does not
    continue into the next day: those milliseconds round to nothing there,
    and the span keeps its real end instant here. Without this, a stop that
    lands a few hundred milliseconds after midnight would produce a
    zero-second row on the next day and strand its adjustments on it.
    """
    if clip_to_window(began, ended, window_start, window_end) is None:
        return None
    continues_from = began < window_start and _whole_seconds(began, window_start) > 0
    continues_into = ended > window_end and _whole_seconds(window_end, ended) > 0
    start = window_start if continues_from else began
    end = window_end if continues_into else ended
    if _whole_seconds(start, end) == 0 and (began < window_start or ended > window_end):
        return None  # a sub-second sliver across midnight belongs to the other day
    return start, end, continues_from, continues_into


def attribute_adjustment(
    recorded_at: datetime,
    entry_start: datetime,
    entry_end: datetime,
    window_start: datetime,
    window_end: datetime,
    continues_from: bool = False,
    continues_into: bool = False,
) -> bool:
    """Whether an adjustment belongs to this day.

    The instant it was recorded is clamped into the entry's own span first,
    so a deduction written after the entry ended (or before it began, from a
    replayed queue) still lands on a day the entry actually covers. For an
    entry that lies inside one day every adjustment is therefore attributed
    to that day, whatever its timestamp. ``continues_*`` are the whole-second
    flags from ``day_span``: an instant past midnight still belongs here
    when the entry does not, in whole seconds, continue past it.
    """
    instant = min(max(recorded_at, entry_start), entry_end)
    if window_start <= instant < window_end:
        return True
    if instant >= window_end and not continues_into:
        return True
    if instant < window_start and not continues_from:
        return True
    return False


def weighted_activity(rows: Iterable[Tuple[int, int]]) -> Tuple[int, int]:
    """``(percentage, measured_seconds)`` from ``(percentage, window_seconds)``
    pairs: ``SUM(pct x seconds) / SUM(seconds)``, the product-wide definition.
    Percentage is 0 when nothing was measured; the second value says so."""
    weighted = 0.0
    measured = 0
    for percentage, seconds in rows:
        if seconds <= 0:
            continue
        weighted += percentage * seconds
        measured += seconds
    if not measured:
        return 0, 0
    return max(0, min(100, int(round(weighted / measured)))), measured


# ── The service ──────────────────────────────────────────────────────────────


class MemberActivityLogService:
    @staticmethod
    def build(db: Session, current_user: User, member_id: int, day: Optional[date]) -> dict:
        # Scope first: someone outside the caller's organisation or team is
        # reported as missing, the same way every other member read does it.
        # The *team* lookup, not the directory one: this is a person's
        # recorded day, and a leader's directory also lists clients.
        member = MemberService.get_team_member(db, current_user, member_id)
        organization_id = member.organization_id

        selected_day = day or ist_today()
        day_start = ist_day_start_utc(selected_day)
        day_end = ist_day_end_utc(selected_day)
        now = datetime.now(timezone.utc)

        # One query per concern, none of them per entry.
        entry_rows = MemberActivityLogRepository.entries_overlapping(
            db, organization_id, member.id, day_start, day_end
        )
        entry_ids = [row[0].id for row in entry_rows]
        adjustments = MemberActivityLogRepository.adjustments_for_entries(db, entry_ids)
        idle_periods = MemberActivityLogRepository.idle_periods_for_entries(db, entry_ids)
        legacy_manual = MemberActivityLogRepository.legacy_manual_entries(
            db, organization_id, member.id, selected_day
        )
        activity_rows = MemberActivityLogRepository.activity_rows(
            db, organization_id, member.id, day_start, day_end
        )
        app_rows = MemberActivityLogRepository.app_usage_rows(
            db, organization_id, member.id, day_start, day_end
        )
        url_rows = MemberActivityLogRepository.url_usage_rows(
            db, organization_id, member.id, day_start, day_end
        )
        screenshots = MemberActivityLogRepository.screenshots(
            db, organization_id, member.id, day_start, day_end
        )
        active = MemberActivityLogRepository.active_entry(db, organization_id, member.id)
        pending_idle = (
            MemberActivityLogRepository.pending_idle_for_entry(db, active[0].id) if active else None
        )

        adjustments_by_entry: Dict[int, list] = defaultdict(list)
        for adjustment in adjustments:
            adjustments_by_entry[int(adjustment.time_entry_id)].append(adjustment)
        idle_by_entry: Dict[int, list] = defaultdict(list)
        reassignment_targets: set[int] = set()
        for period in idle_periods:
            idle_by_entry[int(period.time_entry_id)].append(period)
            if period.reassigned_time_entry_id is not None:
                reassignment_targets.add(int(period.reassigned_time_entry_id))

        # ── Timeline: tracked / manual rows ──────────────────────────────
        events: List[dict] = []
        names_by_entry: Dict[int, dict] = {}
        for entry, project, task, task_status in entry_rows:
            names_by_entry[entry.id] = {
                "project_id": project.id if project else None,
                "project_name": project.project_name if project else None,
                "task_id": task.id if task else None,
                "task_name": task.task_name if task else None,
            }
            event = MemberActivityLogService._tracked_event(
                entry, project, task, task_status,
                adjustments_by_entry.get(entry.id, []),
                idle_by_entry.get(entry.id, []),
                entry.id in reassignment_targets,
                day_start, day_end, now,
            )
            if event is not None:
                events.append(event)

        for manual, project, task, task_status in legacy_manual:
            event = MemberActivityLogService._legacy_manual_event(
                manual, project, task, task_status, day_start, day_end
            )
            if event is not None:
                events.append(event)

        # ── Timeline: idle rows ──────────────────────────────────────────
        idle_events: List[dict] = []
        for entry, project, task, _status in entry_rows:
            for period in idle_by_entry.get(entry.id, []):
                event = MemberActivityLogService._idle_event(
                    period, entry, project, task, day_start, day_end, now
                )
                if event is not None:
                    idle_events.append(event)

        timeline = sorted(
            events + idle_events,
            key=lambda e: (e["start_time"], 0 if e["entry_type"] != "idle" else 1, e.get("entry_id") or 0),
        )

        # ── Projects and tasks ───────────────────────────────────────────
        projects = MemberActivityLogService._projects(events)

        # ── Activity ─────────────────────────────────────────────────────
        activity_percentage, activity_measured = weighted_activity(
            (int(r[1] or 0), int(r[2] or 0)) for r in activity_rows
        )
        keyboard = sum(int(r[3] or 0) for r in activity_rows)
        clicks = sum(int(r[4] or 0) for r in activity_rows)
        movements = sum(int(r[5] or 0) for r in activity_rows)
        top_urls = MemberActivityLogService._urls(url_rows)
        top_applications = MemberActivityLogService._applications(app_rows, top_urls)

        # ── Screenshots ──────────────────────────────────────────────────
        screenshot_items = MemberActivityLogService._screenshots(
            member, screenshots, activity_rows, names_by_entry
        )

        # ── Summary ──────────────────────────────────────────────────────
        summary = MemberActivityLogService._summary(
            events, idle_events, activity_percentage, activity_measured,
            len(screenshot_items), len(top_applications), projects,
        )

        # ── Current state ────────────────────────────────────────────────
        current_state = MemberActivityLogService._current_state(
            db, active, pending_idle, adjustments_by_entry, now
        )

        # ── Data quality ─────────────────────────────────────────────────
        last_sync = MemberActivityLogService._latest(
            [entry.updated_at for entry, *_ in entry_rows]
            + [entry.created_at for entry, *_ in entry_rows]
            + [manual.updated_at for manual, *_ in legacy_manual]
            + [r[6] for r in activity_rows]
            + [r[3] for r in app_rows]
            + [r[6] for r in url_rows]
            + [shot.created_at for shot in screenshots]
        )
        has_running = any(entry.end_time is None for entry, *_ in entry_rows)
        has_pending_idle = any(
            period.status == IdlePeriodStatus.PENDING for period in idle_periods
        )
        data_quality = {
            "last_sync_time": last_sync,
            "has_running_entry": has_running,
            "has_pending_idle": has_pending_idle,
            "data_complete": selected_day < ist_today() and not has_running and not has_pending_idle,
        }

        return {
            "user": {
                "id": member.id,
                "name": member.name,
                "email": member.email,
                "role": member.role_name,
                "designation": member.designation,
                "date": selected_day,
                "timezone": REPORTING_TIMEZONE,
            },
            "summary": summary,
            "projects": projects,
            "timeline": timeline,
            "screenshots": screenshot_items,
            "activity": {
                "activity_percentage": activity_percentage,
                "activity_measured_seconds": activity_measured,
                "keyboard_event_count": keyboard,
                "mouse_click_count": clicks,
                "mouse_movement_count": movements,
                "mouse_event_count": clicks + movements,
                "active_application_count": len(top_applications),
                "top_applications": top_applications,
                "top_urls": top_urls,
            },
            "current_state": current_state,
            "data_quality": data_quality,
        }

    # ── Events ───────────────────────────────────────────────────────────────

    @staticmethod
    def _tracked_event(
        entry, project, task, task_status, adjustments, idle_periods, is_reassignment_target,
        day_start, day_end, now,
    ) -> Optional[dict]:
        raw_start = _as_utc(entry.start_time)
        raw_end = _as_utc(entry.end_time) or now
        placed = day_span(raw_start, raw_end, day_start, day_end)
        if placed is None:
            return None
        start, end, continues_from, continues_into = placed
        running = entry.end_time is None

        # An entry wholly inside the day is reported with its stored
        # `total_seconds`, the figure every other report sums; only a clipped
        # or running entry is measured from its instants.
        if not running and not continues_from and not continues_into:
            measured = max(0, int(entry.total_seconds or 0))
        else:
            measured = _whole_seconds(start, end)

        adjustment_seconds = sum(
            int(a.adjustment_seconds)
            for a in adjustments
            if attribute_adjustment(
                _as_utc(a.recorded_at), raw_start, raw_end, day_start, day_end,
                continues_from, continues_into,
            )
        )
        net = max(0, measured + adjustment_seconds)

        # Idle seconds still inside the net figure: kept, plus anything not
        # yet answered. Discarded and reassigned idle time already left it
        # through the adjustments above.
        idle_inside = 0
        for period in idle_periods:
            split = MemberActivityLogService._idle_split(period, raw_start, raw_end, day_start, day_end, now)
            if split is None:
                continue
            idle_inside += split["kept"] + split["pending"]
        if is_reassignment_target:
            active = 0
        else:
            active = max(0, net - idle_inside)

        if running:
            stop_reason = None
        elif is_reassignment_target:
            stop_reason = "reassignment"
        elif any(p.action == IdlePeriodAction.STOP for p in idle_periods):
            stop_reason = "idle_stop"
        else:
            stop_reason = "stop"

        is_manual = bool(entry.is_manual)
        return {
            "entry_id": entry.id,
            "manual_entry_id": None,
            "project_id": project.id if project else None,
            "project_name": project.project_name if project else None,
            "task_id": task.id if task else None,
            "task_name": task.task_name if task else None,
            "task_status": MemberActivityLogService._task_status(task, task_status),
            "start_time": start,
            "end_time": None if (running and not continues_into) else end,
            "duration_seconds": net,
            "duration": format_hms(net),
            "measured_seconds": measured,
            "adjustment_seconds": adjustment_seconds,
            "entry_type": "manual" if is_manual else "tracked",
            "source": "manual_entry" if is_manual else ("idle_reassignment" if is_reassignment_target else "timer"),
            "description": entry.description,
            "is_manual": is_manual,
            "is_running": running,
            "stop_reason": None if is_manual else stop_reason,
            "continues_from_previous_day": continues_from,
            "continues_into_next_day": continues_into,
            "idle": None,
            # Internal, stripped by the response model: what the breakdown
            # and the summary need without re-deriving it.
            "_active_seconds": 0 if is_manual else active,
            "_manual_seconds": net if is_manual else 0,
            "_tracked_seconds": 0 if is_manual else net,
            "_raw_end": raw_end,
        }

    @staticmethod
    def _legacy_manual_event(manual, project, task, task_status, day_start, day_end) -> Optional[dict]:
        raw_start = _as_utc(manual.start_time)
        raw_end = _as_utc(manual.end_time)
        clipped = clip_to_window(raw_start, raw_end, day_start, day_end)
        if clipped is None:
            # A legacy row is placed by `work_date`, which is what selected it;
            # its instants may straddle the day. Report it whole rather than
            # lose it -- this matches the Reports page, which sums it whole.
            start, end = raw_start, raw_end
        else:
            start, end = clipped
        seconds = max(0, int(manual.total_seconds or 0))
        return {
            "entry_id": None,
            "manual_entry_id": manual.id,
            "project_id": project.id if project else None,
            "project_name": project.project_name if project else None,
            "task_id": task.id if task else None,
            "task_name": task.task_name if task else None,
            "task_status": MemberActivityLogService._task_status(task, task_status),
            "start_time": start,
            "end_time": end,
            "duration_seconds": seconds,
            "duration": format_hms(seconds),
            "measured_seconds": seconds,
            "adjustment_seconds": 0,
            "entry_type": "manual",
            "source": "manual_entry",
            "description": manual.description,
            "is_manual": True,
            "is_running": False,
            "stop_reason": None,
            "continues_from_previous_day": False,
            "continues_into_next_day": False,
            "idle": None,
            "_active_seconds": 0,
            "_manual_seconds": seconds,
            "_tracked_seconds": 0,
            "_raw_end": raw_end,
        }

    @staticmethod
    def _idle_split(period, entry_start, entry_end, day_start, day_end, now) -> Optional[dict]:
        """How many of this idle period's seconds fall in the day, and how
        they were ruled on: ``kept``, ``discarded``, ``reassigned``,
        ``pending`` -- the four always add up to ``total``.

        A period that crosses midnight is split pro rata: the row records one
        duration and one decision, so its parts scale with the clipped share.
        """
        resolved = period.status == IdlePeriodStatus.RESOLVED
        raw_began = _as_utc(period.idle_started_at)
        raw_ended = _as_utc(period.resolved_at) if resolved and period.resolved_at else now
        # Never outside the entry it belongs to.
        began = max(raw_began, entry_start)
        ended = min(raw_ended, entry_end)
        placed = day_span(began, ended, day_start, day_end)
        if placed is None:
            return None
        start, end, continues_from, continues_into = placed
        seconds = _whole_seconds(start, end)
        # Still open right now: unanswered, and the day being read reaches
        # the present. A past day's share of an unanswered period is closed
        # at midnight and flagged as continuing.
        open_now = (not resolved) and not continues_into
        base = {
            "start": start,
            "end": None if open_now else end,
            "continues_from": continues_from,
            "continues_into": continues_into,
        }

        if not resolved:
            return {**base, "total": seconds, "kept": 0, "discarded": 0, "reassigned": 0, "pending": seconds}

        stored = period.idle_duration_seconds
        full = max(0, int(stored)) if stored is not None else _whole_seconds(began, ended)
        share = min(1.0, seconds / full) if full else 1.0
        reassigned_full = int(period.reassigned_seconds or 0) if period.reassigned else 0
        reassigned = min(seconds, int(round(reassigned_full * share)))
        unreassigned = max(0, seconds - reassigned)
        kept = unreassigned if period.counted else 0
        discarded = 0 if period.counted else unreassigned
        return {**base, "total": seconds, "kept": kept, "discarded": discarded,
                "reassigned": reassigned, "pending": 0}

    @staticmethod
    def _idle_event(period, entry, project, task, day_start, day_end, now) -> Optional[dict]:
        raw_start = _as_utc(entry.start_time)
        raw_end = _as_utc(entry.end_time) or now
        split = MemberActivityLogService._idle_split(period, raw_start, raw_end, day_start, day_end, now)
        if split is None:
            return None
        seconds = split["total"]
        return {
            "entry_id": entry.id,
            "manual_entry_id": None,
            "project_id": project.id if project else None,
            "project_name": project.project_name if project else None,
            "task_id": task.id if task else None,
            "task_name": task.task_name if task else None,
            "task_status": None,
            "start_time": split["start"],
            "end_time": split["end"],
            "duration_seconds": seconds,
            "duration": format_hms(seconds),
            "measured_seconds": seconds,
            "adjustment_seconds": 0,
            "entry_type": "idle",
            "source": "timer",
            "description": None,
            "is_manual": False,
            "is_running": split["end"] is None,
            "stop_reason": None,
            "continues_from_previous_day": split["continues_from"],
            "continues_into_next_day": split["continues_into"],
            "idle": {
                "idle_period_id": period.id,
                "status": period.status,
                "keep_idle_time": period.keep_idle_time,
                "action": period.action,
                "counted": period.counted,
                "reassigned": bool(period.reassigned),
                "reassigned_seconds": period.reassigned_seconds,
                "reassigned_project_id": period.reassigned_project_id,
                "reassigned_task_id": period.reassigned_task_id,
                "reassigned_time_entry_id": period.reassigned_time_entry_id,
            },
            "_split": split,
        }

    @staticmethod
    def _task_status(task, task_status) -> Optional[str]:
        if task_status is not None and getattr(task_status, "name", None):
            return task_status.name
        return getattr(task, "status", None) if task is not None else None

    # ── Breakdown ────────────────────────────────────────────────────────────

    @staticmethod
    def _projects(events: List[dict]) -> List[dict]:
        projects: "OrderedDict[int, dict]" = OrderedDict()
        for event in events:
            if event["project_id"] is None or event["task_id"] is None:
                continue
            project = projects.setdefault(event["project_id"], {
                "project_id": event["project_id"],
                "project_name": event["project_name"] or "",
                "total_seconds": 0, "active_seconds": 0, "manual_seconds": 0,
                "tasks": OrderedDict(),
            })
            task = project["tasks"].setdefault(event["task_id"], {
                "task_id": event["task_id"],
                "task_name": event["task_name"] or "",
                "status": event.get("task_status"),
                "total_seconds": 0, "active_seconds": 0, "manual_seconds": 0,
                "first_start_time": None, "last_stop_time": None,
            })
            for bucket in (project, task):
                bucket["total_seconds"] += event["duration_seconds"]
                bucket["active_seconds"] += event["_active_seconds"]
                bucket["manual_seconds"] += event["_manual_seconds"]
            task["first_start_time"] = (
                event["start_time"] if task["first_start_time"] is None
                else min(task["first_start_time"], event["start_time"])
            )
            if event["end_time"] is not None and not event["continues_into_next_day"] and not event["is_manual"]:
                task["last_stop_time"] = (
                    event["end_time"] if task["last_stop_time"] is None
                    else max(task["last_stop_time"], event["end_time"])
                )

        result = []
        for project in projects.values():
            tasks = sorted(
                project["tasks"].values(),
                key=lambda t: (-t["total_seconds"], t["first_start_time"] or datetime.max.replace(tzinfo=timezone.utc)),
            )
            for task in tasks:
                MemberActivityLogService._format(task, ("total", "active", "manual"))
            project["tasks"] = tasks
            project["task_count"] = len(tasks)
            MemberActivityLogService._format(project, ("total", "active", "manual"))
            result.append(project)
        result.sort(key=lambda p: (-p["total_seconds"], p["project_name"]))
        return result

    @staticmethod
    def _format(bucket: dict, names: Tuple[str, ...]) -> None:
        for name in names:
            bucket[f"{name}_time"] = format_hms(bucket[f"{name}_seconds"])

    # ── Summary ──────────────────────────────────────────────────────────────

    @staticmethod
    def _summary(events, idle_events, activity_percentage, activity_measured,
                 screenshot_count, application_count, projects) -> dict:
        tracked = sum(e["_tracked_seconds"] for e in events)
        manual = sum(e["_manual_seconds"] for e in events)
        active = sum(e["_active_seconds"] for e in events)

        idle_total = sum(e["_split"]["total"] for e in idle_events)
        idle_kept = sum(e["_split"]["kept"] for e in idle_events)
        idle_discarded = sum(e["_split"]["discarded"] for e in idle_events)
        idle_reassigned = sum(e["_split"]["reassigned"] for e in idle_events)
        idle_pending = sum(e["_split"]["pending"] for e in idle_events)

        first_start = min((e["start_time"] for e in events), default=None)
        last_stop_event = None
        for event in events:
            if event["is_manual"] or event["end_time"] is None or event["continues_into_next_day"]:
                continue
            if last_stop_event is None or event["end_time"] > last_stop_event["end_time"]:
                last_stop_event = event

        summary = {
            "first_start_time": first_start,
            "last_stop_time": last_stop_event["end_time"] if last_stop_event else None,
            "last_stop_reason": last_stop_event["stop_reason"] if last_stop_event else None,
            "total_worked_seconds": tracked + manual,
            "total_tracked_seconds": tracked,
            "total_active_work_seconds": active,
            "total_manual_seconds": manual,
            "total_idle_seconds": idle_total,
            "idle_kept_seconds": idle_kept,
            "idle_discarded_seconds": idle_discarded,
            "idle_reassigned_seconds": idle_reassigned,
            "idle_pending_seconds": idle_pending,
            "total_break_seconds": 0,
            "activity_percentage": activity_percentage,
            "activity_measured_seconds": activity_measured,
            "screenshot_count": screenshot_count,
            "application_count": application_count,
            "project_count": len(projects),
            "task_count": sum(p["task_count"] for p in projects),
        }
        MemberActivityLogService._format(
            summary,
            ("total_worked", "total_tracked", "total_active_work", "total_manual", "total_idle", "total_break"),
        )
        return summary

    # ── Activity ─────────────────────────────────────────────────────────────

    @staticmethod
    def _urls(url_rows) -> List[dict]:
        grouped: "OrderedDict[tuple, dict]" = OrderedDict()
        for browser_name, domain, url, page_title, _recorded_at, duration, _created in url_rows:
            key = (browser_name, domain, url)
            item = grouped.setdefault(key, {
                "browser_name": browser_name, "domain": domain, "url": url,
                "page_title": None, "duration_seconds": 0,
            })
            item["duration_seconds"] += max(0, int(duration or 0))
            # The same page is recorded under several titles (unread-count
            # prefixes, renames); keep one rather than split the page.
            if page_title and (item["page_title"] is None or len(page_title) > len(item["page_title"])):
                item["page_title"] = page_title
        items = sorted(grouped.values(), key=lambda u: (-u["duration_seconds"], u["domain"], u["url"] or ""))
        for item in items:
            item["duration"] = format_hms(item["duration_seconds"])
        return items

    @staticmethod
    def _applications(app_rows, urls: List[dict]) -> List[dict]:
        grouped: "OrderedDict[str, dict]" = OrderedDict()
        for application_name, recorded_at, duration, _created in app_rows:
            began = _as_utc(recorded_at)
            seconds = max(0, int(duration or 0))
            ended = began + timedelta(seconds=seconds)
            item = grouped.setdefault(application_name, {
                "application_name": application_name, "duration_seconds": 0,
                "segment_count": 0, "start_time": began, "end_time": ended, "urls": [],
            })
            item["duration_seconds"] += seconds
            item["segment_count"] += 1
            item["start_time"] = min(item["start_time"], began)
            item["end_time"] = max(item["end_time"], ended)

        urls_by_browser: Dict[str, List[dict]] = defaultdict(list)
        for url in urls:
            urls_by_browser[url["browser_name"].strip().lower()].append(url)

        items = sorted(grouped.values(), key=lambda a: (-a["duration_seconds"], a["application_name"]))
        for item in items:
            item["duration"] = format_hms(item["duration_seconds"])
            item["urls"] = urls_by_browser.get(item["application_name"].strip().lower(), [])
        return items

    # ── Screenshots ──────────────────────────────────────────────────────────

    @staticmethod
    def _screenshots(member, screenshots, activity_rows, names_by_entry) -> List[dict]:
        if not screenshots:
            return []
        window_minutes = TimeEntryScreenshotService._window_minutes_for(
            getattr(member, "capture_frequency", None)
        )
        # The same bucketing the screenshot timeline uses, so a capture's
        # activity figure here is the one the Screenshots page shows for it.
        windows = _build_windows(
            window_minutes * 60,
            screenshots,
            [(r[0], int(r[1] or 0), int(r[2] or 0)) for r in activity_rows],
            [],
            names_by_entry,
        )
        entry_by_shot = {shot.id: shot.time_entry_id for shot in screenshots}
        items = []
        for window in windows:
            for shot in window["screenshots"]:
                items.append({
                    "screenshot_id": shot["id"],
                    "captured_at": shot["captured_at"],
                    "entry_id": entry_by_shot[shot["id"]],
                    "project_id": shot["project_id"],
                    "project_name": shot["project_name"],
                    "task_id": shot["task_id"],
                    "task_name": shot["task_name"],
                    "image_url": shot["view_url"],
                    "activity_percentage": window["activity_percentage"],
                    "activity_measured_seconds": window["activity_measured_seconds"],
                    "display_count": shot["display_count"],
                    "width": shot["width"],
                    "height": shot["height"],
                })
        items.sort(key=lambda s: (s["captured_at"], s["screenshot_id"]))
        return items

    # ── Current state ────────────────────────────────────────────────────────

    @staticmethod
    def _current_state(db, active, pending_idle, adjustments_by_entry, now) -> dict:
        state = {
            "currently_tracking": False,
            "currently_idle": False,
            "currently_on_break": False,
            "current_entry_id": None,
            "current_project_id": None,
            "current_project_name": None,
            "current_task_id": None,
            "current_task_name": None,
            "current_started_at": None,
            "current_elapsed_seconds": None,
            "current_idle_since": None,
            "server_time": now,
        }
        if not active:
            return state
        entry, project, task = active
        if entry.id in adjustments_by_entry:
            net_adjustment = sum(int(a.adjustment_seconds) for a in adjustments_by_entry[entry.id])
        else:
            # The running entry started on another day than the one being
            # read; its deductions were not part of the day's query.
            net_adjustment = TimeEntryAdjustmentRepository.sum_for_entry(db, entry.id)
        state.update({
            "currently_tracking": True,
            "currently_idle": pending_idle is not None,
            "current_entry_id": entry.id,
            "current_project_id": project.id if project else entry.project_id,
            "current_project_name": project.project_name if project else None,
            "current_task_id": task.id if task else entry.task_id,
            "current_task_name": task.task_name if task else None,
            "current_started_at": _as_utc(entry.start_time),
            "current_elapsed_seconds": max(0, elapsed_seconds(entry.start_time, now) + int(net_adjustment)),
            "current_idle_since": _as_utc(pending_idle.idle_started_at) if pending_idle else None,
        })
        return state

    @staticmethod
    def _latest(values: Iterable[Optional[datetime]]) -> Optional[datetime]:
        latest = None
        for value in values:
            value = _as_utc(value)
            if value is not None and (latest is None or value > latest):
                latest = value
        return latest

from sqlalchemy.orm import Session
from sqlalchemy import select, and_, func, extract, cast, case, Date, Float
from typing import List, Optional, Tuple, Dict, Any
from datetime import datetime, timezone, date
import math

from app.models.time_entry_activity import TimeEntryActivity
from app.models.time_entry import TimeEntry


class TimeEntryActivityRepository:
    @staticmethod
    def create(
        db: Session,
        organization_id: int,
        time_entry_id: int,
        recorded_at: Optional[datetime],
        keyboard_strokes: int,
        mouse_clicks: int,
        mouse_movements: int,
        activity_percentage: int
    ) -> TimeEntryActivity:
        now_utc = datetime.now(timezone.utc)
        record = TimeEntryActivity(
            organization_id=organization_id,
            time_entry_id=time_entry_id,
            recorded_at=recorded_at if recorded_at is not None else now_utc,
            keyboard_strokes=max(0, keyboard_strokes),
            mouse_clicks=max(0, mouse_clicks),
            mouse_movements=max(0, mouse_movements),
            activity_percentage=max(0, min(100, activity_percentage))
        )
        db.add(record)
        db.commit()
        db.refresh(record)
        return record

    @staticmethod
    def create_batch(
        db: Session,
        items: List[Dict[str, Any]]
    ) -> Tuple[int, int]:
        accepted = 0
        failed = 0
        now_utc = datetime.now(timezone.utc)
        
        for item in items:
            try:
                rec_at = item.get("recorded_at")
                record = TimeEntryActivity(
                    organization_id=item["organization_id"],
                    time_entry_id=item["time_entry_id"],
                    recorded_at=rec_at if rec_at is not None else now_utc,
                    keyboard_strokes=max(0, item.get("keyboard_strokes", 0)),
                    mouse_clicks=max(0, item.get("mouse_clicks", 0)),
                    mouse_movements=max(0, item.get("mouse_movements", 0)),
                    activity_percentage=max(0, min(100, item.get("activity_percentage", 0)))
                )
                db.add(record)
                accepted += 1
            except Exception:
                failed += 1

        if accepted > 0:
            db.commit()

        return accepted, failed

    @staticmethod
    def get_by_id(db: Session, activity_id: int, organization_id: Optional[int] = None) -> Optional[TimeEntryActivity]:
        query = select(TimeEntryActivity).where(TimeEntryActivity.id == activity_id)
        if organization_id is not None:
            query = query.where(TimeEntryActivity.organization_id == organization_id)
        return db.scalar(query)

    @staticmethod
    def update(
        db: Session,
        record: TimeEntryActivity,
        keyboard_strokes: Optional[int] = None,
        mouse_clicks: Optional[int] = None,
        mouse_movements: Optional[int] = None,
        activity_percentage: Optional[int] = None
    ) -> TimeEntryActivity:
        if keyboard_strokes is not None:
            record.keyboard_strokes = max(0, keyboard_strokes)
        if mouse_clicks is not None:
            record.mouse_clicks = max(0, mouse_clicks)
        if mouse_movements is not None:
            record.mouse_movements = max(0, mouse_movements)
        if activity_percentage is not None:
            record.activity_percentage = max(0, min(100, activity_percentage))

        db.add(record)
        db.commit()
        db.refresh(record)
        return record

    @staticmethod
    def delete(db: Session, record: TimeEntryActivity) -> None:
        db.delete(record)
        db.commit()

    @staticmethod
    def list_by_filters(
        db: Session,
        organization_id: int,
        user_id: Optional[int] = None,
        time_entry_id: Optional[int] = None,
        project_id: Optional[int] = None,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        skip: int = 0,
        limit: int = 100,
        sort_desc: bool = True
    ) -> Tuple[List[TimeEntryActivity], int]:
        conditions = [TimeEntryActivity.organization_id == organization_id]
        query = select(TimeEntryActivity)

        need_entry_join = user_id is not None or project_id is not None
        if need_entry_join:
            query = query.join(TimeEntry, TimeEntryActivity.time_entry_id == TimeEntry.id)
            if user_id is not None:
                conditions.append(TimeEntry.user_id == user_id)
            if project_id is not None:
                conditions.append(TimeEntry.project_id == project_id)

        if time_entry_id is not None:
            conditions.append(TimeEntryActivity.time_entry_id == time_entry_id)
        if start_date is not None:
            conditions.append(TimeEntryActivity.recorded_at >= start_date)
        if end_date is not None:
            conditions.append(TimeEntryActivity.recorded_at <= end_date)

        where_clause = and_(*conditions)
        total = db.scalar(select(func.count(TimeEntryActivity.id)).where(where_clause)) or 0

        order_col = TimeEntryActivity.recorded_at.desc() if sort_desc else TimeEntryActivity.recorded_at.asc()
        items = list(db.scalars(
            query.where(where_clause).order_by(order_col, TimeEntryActivity.id.desc()).offset(skip).limit(limit)
        ).all())

        return items, total

    @staticmethod
    def get_overview(
        db: Session,
        organization_id: int,
        user_id: Optional[int] = None,
        time_entry_id: Optional[int] = None,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None
    ) -> Dict[str, int]:
        conditions = [TimeEntryActivity.organization_id == organization_id]
        
        if user_id is not None:
            conditions.append(TimeEntry.user_id == user_id)
        if time_entry_id is not None:
            conditions.append(TimeEntryActivity.time_entry_id == time_entry_id)
        if start_date is not None:
            conditions.append(TimeEntryActivity.recorded_at >= start_date)
        if end_date is not None:
            conditions.append(TimeEntryActivity.recorded_at <= end_date)

        query = select(
            func.coalesce(func.sum(TimeEntryActivity.keyboard_strokes), 0),
            func.coalesce(func.sum(TimeEntryActivity.mouse_clicks), 0),
            func.coalesce(func.sum(TimeEntryActivity.mouse_movements), 0),
            # Duration-weighted: SUM(percentage x window_seconds) / SUM(window_seconds).
            func.coalesce(
                func.sum(cast(TimeEntryActivity.activity_percentage, Float)
                         * cast(TimeEntryActivity.window_seconds, Float))
                / func.nullif(cast(func.sum(TimeEntryActivity.window_seconds), Float), 0.0),
                0,
            ),
            func.coalesce(func.count(TimeEntryActivity.id), 0),
            func.coalesce(func.sum(case((TimeEntryActivity.activity_percentage > 0, 1), else_=0)), 0)
        )

        if user_id is not None:
            query = query.join(TimeEntry, TimeEntryActivity.time_entry_id == TimeEntry.id)

        res = db.execute(query.where(and_(*conditions))).first()
        if not res:
            return {
                "total_keyboard_strokes": 0,
                "total_mouse_clicks": 0,
                "total_mouse_movements": 0,
                "average_activity_percentage": 0,
                "active_intervals": 0,
                "total_intervals": 0
            }

        k_strokes, m_clicks, m_moves, avg_pct, total_int, active_int = res
        return {
            "total_keyboard_strokes": int(k_strokes),
            "total_mouse_clicks": int(m_clicks),
            "total_mouse_movements": int(m_moves),
            "average_activity_percentage": round(float(avg_pct)),
            "active_intervals": int(active_int),
            "total_intervals": int(total_int)
        }

    @staticmethod
    def get_timeline(
        db: Session,
        organization_id: int,
        user_id: Optional[int] = None,
        time_entry_id: Optional[int] = None,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None
    ) -> List[Dict[str, Any]]:
        conditions = [TimeEntryActivity.organization_id == organization_id]
        
        if user_id is not None:
            conditions.append(TimeEntry.user_id == user_id)
        if time_entry_id is not None:
            conditions.append(TimeEntryActivity.time_entry_id == time_entry_id)
        if start_date is not None:
            conditions.append(TimeEntryActivity.recorded_at >= start_date)
        if end_date is not None:
            conditions.append(TimeEntryActivity.recorded_at <= end_date)

        query = select(TimeEntryActivity)
        if user_id is not None:
            query = query.join(TimeEntry, TimeEntryActivity.time_entry_id == TimeEntry.id)

        records = list(db.scalars(
            query.where(and_(*conditions)).order_by(TimeEntryActivity.recorded_at.asc())
        ).all())

        timeline_points = []
        for r in records:
            ts_str = r.recorded_at.isoformat() if r.recorded_at else ""
            timeline_points.append({
                "timestamp": ts_str,
                "activity_percentage": r.activity_percentage,
                "keyboard_strokes": r.keyboard_strokes,
                "mouse_clicks": r.mouse_clicks,
                "mouse_movements": r.mouse_movements
            })

        return timeline_points

    @staticmethod
    def get_hourly(
        db: Session,
        organization_id: int,
        user_id: Optional[int] = None,
        time_entry_id: Optional[int] = None,
        target_date: Optional[date] = None
    ) -> Dict[str, Any]:
        conditions = [TimeEntryActivity.organization_id == organization_id]

        if user_id is not None:
            conditions.append(TimeEntry.user_id == user_id)
        if time_entry_id is not None:
            conditions.append(TimeEntryActivity.time_entry_id == time_entry_id)
        if target_date is not None:
            conditions.append(cast(TimeEntryActivity.recorded_at, Date) == target_date)

        query = select(
            extract('hour', TimeEntryActivity.recorded_at).label('hour_val'),
            func.coalesce(func.sum(TimeEntryActivity.keyboard_strokes), 0).label('sum_k'),
            func.coalesce(func.sum(TimeEntryActivity.mouse_clicks), 0).label('sum_c'),
            func.coalesce(func.sum(TimeEntryActivity.mouse_movements), 0).label('sum_m'),
            func.coalesce(func.avg(TimeEntryActivity.activity_percentage), 0).label('avg_overall'),
            func.count(TimeEntryActivity.id).label('cnt')
        )

        if user_id is not None:
            query = query.join(TimeEntry, TimeEntryActivity.time_entry_id == TimeEntry.id)

        rows = db.execute(query.where(and_(*conditions)).group_by('hour_val').order_by('hour_val')).all()

        date_str = target_date.isoformat() if target_date else datetime.now(timezone.utc).strftime("%Y-%m-%d")
        hours_list = []

        # Standard normalization thresholds for 60-second intervals in an hour
        # Max per interval: 120 keys, 30 clicks, 400 movements
        # In a full 60-minute hour: max = 60 * interval_max
        for r in rows:
            hour_num = int(r.hour_val)
            sum_k = int(r.sum_k)
            sum_c = int(r.sum_c)
            sum_m = int(r.sum_m)
            avg_overall = round(float(r.avg_overall))
            cnt = int(r.cnt)

            # Calculate normalized keyboard % and mouse % for the hour
            # cnt is the number of 60s intervals sampled in this hour
            max_k_possible = max(1, cnt * 120)
            max_c_possible = max(1, cnt * 30)
            max_m_possible = max(1, cnt * 400)

            k_score = min(1.0, sum_k / max_k_possible)
            c_score = min(1.0, sum_c / max_c_possible)
            m_score = min(1.0, sum_m / max_m_possible)

            k_pct = round(k_score * 100)
            m_pct = round(((c_score * 0.5) + (m_score * 0.5)) * 100)

            label_str = f"{hour_num:02d}:00 - {(hour_num + 1) % 24:02d}:00"
            hours_list.append({
                "hour": hour_num,
                "label": label_str,
                "keyboard_percentage": min(100, max(0, k_pct)),
                "mouse_percentage": min(100, max(0, m_pct)),
                "overall_activity_percentage": min(100, max(0, avg_overall)),
                "keyboard_strokes": sum_k,
                "mouse_clicks": sum_c,
                "mouse_movements": sum_m
            })

        return {
            "date": date_str,
            "hours": hours_list
        }

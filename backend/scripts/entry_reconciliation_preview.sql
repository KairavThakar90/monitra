-- READ-ONLY. What closing ONE running entry would do to every report. Writes nothing;
-- safe to run in pgAdmin against production. Used for the reviewable plan for entry 5958
-- (docs/TIMER_ENTRY_5958_RECONCILIATION_PLAN.md) and for any other legacy entry an
-- operator is asked to close.
--
-- Change the entry id (and `as_of`, to replay a moment such as Monday 10:55 IST) in `params`.
--
-- "before" is what the reports show for the entry as it stands; "after" is what they show once
-- an operator closes it (POST /internal/time-entries/reconcile?dry_run=false&include_unreported=true
-- &entry_id=...), which ends it at its newest evidence of life + 15 minutes (never before its
-- start, never in the future) and writes total_seconds = round(end - start).
--
-- Two ways the product buckets an entry, and both are shown:
--   reports_*        the Reports page, the weekly and monthly emails and the project / task
--                    reports key an entry on the IST DATE OF ITS start_time, whole. Week is
--                    Monday to Sunday (WEEKLY_REPORT_DAY, default monday), month is the IST month.
--   time_tracking_day  Admin > Time Tracking apportions an entry to every IST day it overlaps,
--                    clipped at midnight.
-- An entry that is still running with no heartbeat on record (`last_seen_at` NULL) is measured to
-- now by both, which is why it grows by the hour until somebody closes it.
WITH params AS (
    SELECT 5958::bigint        AS entry_id,
           now()               AS as_of,
           interval '15 minutes' AS grace,
           interval '10 minutes' AS stale_after
),
e AS (
    SELECT te.id, te.user_id, te.start_time, te.end_time, te.last_seen_at, p.as_of, p.grace, p.stale_after
    FROM time_entries te, params p
    WHERE te.id = p.entry_id
),
evidence AS (
    SELECT e.*,
           GREATEST(e.last_seen_at,
               (SELECT max(a.recorded_at + a.window_seconds * interval '1 second')
                  FROM time_entry_activity a WHERE a.time_entry_id = e.id),
               (SELECT max(s.captured_at) FROM time_entry_screenshots s WHERE s.time_entry_id = e.id),
               (SELECT max(u.recorded_at) FROM time_entry_app_usage u WHERE u.time_entry_id = e.id)
           ) AS last_evidence_at
    FROM e
),
spans AS (
    SELECT ev.id, ev.user_id, ev.start_time, ev.last_evidence_at, ev.as_of,
           COALESCE(ev.end_time,
                    CASE WHEN ev.last_seen_at IS NOT NULL AND ev.as_of - ev.last_seen_at > ev.stale_after
                         THEN GREATEST(ev.start_time, LEAST(ev.as_of, ev.last_seen_at + ev.grace))
                         ELSE ev.as_of END)                                         AS before_end,
           COALESCE(ev.end_time,
                    GREATEST(ev.start_time,
                             LEAST(ev.as_of, COALESCE(ev.last_evidence_at, ev.start_time) + ev.grace))) AS after_end
    FROM evidence ev
),
ist_days AS (
    SELECT s.id, s.start_time, s.before_end, s.after_end,
           (d.day AT TIME ZONE 'Asia/Kolkata')                           AS day_start,
           (d.day AT TIME ZONE 'Asia/Kolkata') + interval '1 day'        AS day_end,
           d.day::date                                                   AS ist_date
    FROM spans s
    CROSS JOIN LATERAL generate_series(
        (timezone('Asia/Kolkata', s.start_time))::date::timestamp,
        (timezone('Asia/Kolkata', GREATEST(s.before_end, s.after_end)))::date::timestamp,
        interval '1 day') AS d(day)
),
apportioned AS (
    SELECT ist_date,
           GREATEST(0, extract(epoch FROM LEAST(before_end, day_end) - GREATEST(start_time, day_start)))::bigint AS before_seconds,
           GREATEST(0, extract(epoch FROM LEAST(after_end,  day_end) - GREATEST(start_time, day_start)))::bigint AS after_seconds
    FROM ist_days
),
keyed AS (
    SELECT (timezone('Asia/Kolkata', s.start_time))::date AS start_date,
           round(extract(epoch FROM s.before_end - s.start_time))::bigint AS before_seconds,
           round(extract(epoch FROM s.after_end  - s.start_time))::bigint AS after_seconds
    FROM spans s
),
result AS (
    SELECT 'entry' AS report, 'facts' AS grain, NULL::date AS period, NULL::bigint AS before_seconds, NULL::bigint AS after_seconds,
           'entry ' || s.id || ' user ' || s.user_id
           || ' | started ' || to_char(s.start_time AT TIME ZONE 'Asia/Kolkata', 'YYYY-MM-DD HH24:MI') || ' IST'
           || ' | newest evidence ' || COALESCE(to_char(s.last_evidence_at AT TIME ZONE 'Asia/Kolkata', 'YYYY-MM-DD HH24:MI'), 'none') || ' IST'
           || ' | would end ' || to_char(s.after_end AT TIME ZONE 'Asia/Kolkata', 'YYYY-MM-DD HH24:MI') || ' IST' AS detail,
           0 AS sort_group
    FROM spans s
    UNION ALL
    SELECT 'time_tracking_day', 'day', ist_date, before_seconds, after_seconds, NULL, 1 FROM apportioned
    UNION ALL
    SELECT 'reports_day', 'day', start_date, before_seconds, after_seconds, 'keyed on the IST date of start_time', 2 FROM keyed
    UNION ALL
    SELECT 'reports_week', 'week', date_trunc('week', start_date)::date, before_seconds, after_seconds, 'Monday-start IST week of start_time', 3 FROM keyed
    UNION ALL
    SELECT 'reports_month', 'month', date_trunc('month', start_date)::date, before_seconds, after_seconds, 'IST month of start_time', 4 FROM keyed
)
SELECT report, grain, period, before_seconds, after_seconds,
       after_seconds - before_seconds                                                    AS change_seconds,
       CASE WHEN before_seconds IS NULL THEN NULL
            ELSE to_char(make_interval(secs => before_seconds), 'HH24:MI:SS') END         AS before_hms,
       CASE WHEN after_seconds IS NULL THEN NULL
            ELSE to_char(make_interval(secs => after_seconds),  'HH24:MI:SS') END         AS after_hms,
       detail
FROM result
ORDER BY sort_group, period;

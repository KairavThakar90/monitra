-- READ-ONLY. Every running time entry, how long its desktop has been silent, and
-- what the abandoned-timer policy would do to it. Writes nothing; safe to run in
-- pgAdmin against production (docs/TIMING_MODEL.md section 9).
--
--   * evidence_at   the newest instant anything says this entry's desktop was alive:
--                   last_seen_at (heartbeat / uploads, new), else the end of the newest
--                   activity window, else the newest screenshot or application-usage row.
--                   NULL when nothing names the entry at all.
--   * liveness      live    = reported within the stale window
--                   stale   = reported, then went quiet
--                   unknown = never reported (started before the heartbeat existed). The
--                             scheduled job never closes these; an operator names them.
--   * proposed_end  evidence_at + grace (never before the start, never in the future);
--                   for an entry with no evidence at all, start + grace.
--   * proposed_seconds   what total_seconds would be. Compare with measured_now_seconds,
--                   the figure Active Users showed before the policy.
--
-- The four numbers below are the policy defaults (app/core/config.py, TIMER_*_SECONDS);
-- change them here to see what another policy would do.
WITH policy AS (
    SELECT interval '10 minutes' AS stale_after,
           interval '15 minutes' AS grace,
           interval '2 hours'    AS no_signal_stop_after,
           interval '4 hours'    AS idle_unanswered_stop_after
),
running AS (
    SELECT te.id, te.user_id, te.organization_id, te.project_id, te.task_id,
           te.start_time, te.last_seen_at, te.client_op
    FROM time_entries te
    WHERE te.end_time IS NULL
),
evidence AS (
    SELECT r.*,
           GREATEST(
               r.last_seen_at,
               (SELECT max(a.recorded_at + a.window_seconds * interval '1 second')
                  FROM time_entry_activity a WHERE a.time_entry_id = r.id),
               (SELECT max(s.captured_at) FROM time_entry_screenshots s WHERE s.time_entry_id = r.id),
               (SELECT max(u.recorded_at) FROM time_entry_app_usage u WHERE u.time_entry_id = r.id)
           ) AS evidence_at,
           (SELECT p.idle_detected_at FROM time_entry_idle_periods p
             WHERE p.time_entry_id = r.id AND p.status = 'pending'
             ORDER BY p.id DESC LIMIT 1) AS pending_idle_detected_at
    FROM running r
)
SELECT e.id                                                   AS time_entry_id,
       e.user_id,
       u.name                                                 AS member,
       e.start_time,
       e.last_seen_at,
       e.evidence_at,
       CASE WHEN e.last_seen_at IS NULL THEN 'unknown'
            WHEN now() - e.last_seen_at <= p.stale_after THEN 'live'
            ELSE 'stale' END                                  AS liveness,
       now() - COALESCE(e.last_seen_at, e.evidence_at, e.start_time)    AS silent_for,
       round(extract(epoch FROM now() - e.start_time))::bigint          AS measured_now_seconds,
       CASE
           WHEN e.last_seen_at IS NOT NULL AND now() - e.last_seen_at > p.no_signal_stop_after
               THEN 'no_signal (scheduled job would close it)'
           WHEN e.last_seen_at IS NOT NULL AND e.pending_idle_detected_at IS NOT NULL
                AND now() - e.pending_idle_detected_at > p.idle_unanswered_stop_after
               THEN 'idle_unanswered (scheduled job would close it)'
           WHEN e.last_seen_at IS NULL AND now() - COALESCE(e.evidence_at, e.start_time) > p.no_signal_stop_after
               THEN 'operator only: never reported liveness (close by naming the entry)'
           ELSE 'none'
       END                                                    AS policy_action,
       GREATEST(e.start_time,
                LEAST(now(), COALESCE(e.last_seen_at, e.evidence_at, e.start_time) + p.grace))
                                                              AS proposed_end,
       round(extract(epoch FROM
             GREATEST(e.start_time,
                      LEAST(now(), COALESCE(e.last_seen_at, e.evidence_at, e.start_time) + p.grace))
             - e.start_time))::bigint                         AS proposed_seconds,
       e.pending_idle_detected_at
FROM evidence e
CROSS JOIN policy p
JOIN users u ON u.id = e.user_id
ORDER BY silent_for DESC;

-- READ-ONLY. One screen of numbers for the days after the abandoned-timer rollout. Writes nothing; safe to run
-- in pgAdmin against production at any time (docs/TIMER_ABANDONMENT_ROLLOUT.md section 9).
--
-- Read it as a trend, not a verdict: run it before the migration's backend is live, the day after each step,
-- and weekly. What each line means, and what to do when it moves:
--
--   running ...                  the live picture. `never reported` shrinks as desktops upload or are closed
--                                and never grows once every desktop is a release that sends a heartbeat.
--   running over 24 h, reporting in
--                                a person leaving a timer on an open app: the case this policy deliberately
--                                does not close. A handful is normal; a rising number is the business decision
--                                about a maximum session length.
--   server closures ...          what the reconciler did (zero while it is report-only, by design). A burst on
--                                the first day it is enabled is the backlog; steady state is a few a day.
--   closures that failed         should be zero. See the RECONCILE_FAILED lines in the backend log.
--   wrong-cut indicator          members who filed manual time for a slot beginning at or after the instant the
--                                server closed their entry, within three days of it. Each is a person saying "I
--                                was working then": if it rises after the threshold is set, the threshold is
--                                cutting real work (docs/TIMING_MODEL.md section 9.4).
--   last run per audit           when the reconciler last wrote an audit row (NULL while report-only).
WITH clock AS (SELECT now() AS t),
running AS (
    SELECT te.id, te.start_time, te.last_seen_at FROM time_entries te WHERE te.end_time IS NULL
)
SELECT metric, value FROM (
    SELECT 10 AS ord, 'running entries' AS metric, count(*)::text AS value FROM running
    UNION ALL SELECT 11, 'running, reported in the last 10 minutes',
        count(*) FILTER (WHERE last_seen_at > (SELECT t FROM clock) - interval '10 minutes')::text FROM running
    UNION ALL SELECT 12, 'running, reported before but not lately',
        count(*) FILTER (WHERE last_seen_at IS NOT NULL
                          AND last_seen_at <= (SELECT t FROM clock) - interval '10 minutes')::text FROM running
    UNION ALL SELECT 13, 'running, never reported (started before the heartbeat)',
        count(*) FILTER (WHERE last_seen_at IS NULL)::text FROM running
    UNION ALL SELECT 14, 'running over 24 h, reporting in',
        count(*) FILTER (WHERE start_time < (SELECT t FROM clock) - interval '24 hours'
                          AND last_seen_at > (SELECT t FROM clock) - interval '10 minutes')::text FROM running
    UNION ALL SELECT 15, 'oldest running entry, hours',
        coalesce(round(extract(epoch FROM (SELECT t FROM clock) - min(start_time)) / 3600.0, 1)::text, '-') FROM running
    UNION ALL SELECT 20, 'server closures, last 24 hours',
        count(*)::text FROM time_entry_reconciliations WHERE decided_at > (SELECT t FROM clock) - interval '24 hours'
    UNION ALL SELECT 21, 'server closures, last 7 days',
        count(*)::text FROM time_entry_reconciliations WHERE decided_at > (SELECT t FROM clock) - interval '7 days'
    UNION ALL SELECT 22 + o.n, '  of which ' || o.reason || ', last 7 days',
        (SELECT count(*) FROM time_entry_reconciliations r
          WHERE r.reason = o.reason AND r.decided_at > (SELECT t FROM clock) - interval '7 days')::text
        FROM (VALUES (0, 'no_signal'), (1, 'idle_unanswered'), (2, 'replaced_by_start'), (3, 'operator')) AS o(n, reason)
    UNION ALL SELECT 30, 'last audit row written',
        coalesce(to_char(max(decided_at) AT TIME ZONE 'Asia/Kolkata', 'YYYY-MM-DD HH24:MI') || ' IST', 'none yet') FROM time_entry_reconciliations
    UNION ALL SELECT 31, 'wrong-cut indicator: manual-time requests starting at or after a server closure, within 3 days',
        count(DISTINCT m.id)::text
        FROM time_entry_reconciliations r
        JOIN manual_time_entries m
          ON m.user_id = r.user_id AND m.deleted_at IS NULL
         AND m.start_time >= r.ended_at
         AND m.created_at BETWEEN r.decided_at AND r.decided_at + interval '3 days'
        WHERE r.reason IN ('no_signal', 'idle_unanswered')
          AND r.decided_at > (SELECT t FROM clock) - interval '30 days'
) metrics
ORDER BY ord;

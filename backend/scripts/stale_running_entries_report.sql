-- READ-ONLY. Every running time entry, how sure we are that it is really running,
-- and what the abandoned-timer policy would do to it. It writes nothing and takes no
-- lock beyond the statement's own snapshot, so it is safe to run in pgAdmin against
-- production (docs/TIMING_MODEL.md section 9). Reading it does not change an entry.
--
-- ONE ROW PER RUNNING ENTRY (end_time IS NULL), most urgent first. The columns:
--
--   category   confirmed_active   heard from within the stale window (10 min): leave it.
--              potentially_stale  it has been heard from, but not recently. NOT proof that
--                                 it is dead: an offline-but-working desktop looks the
--                                 same. `policy_action` says what the scheduled job does.
--              unverified         nothing (heartbeat, activity window, screenshot,
--                                 application usage) has ever named this entry, so there
--                                 is no evidence either way. Never closed automatically.
--              manual_review      a person should look: a timestamp is impossible (last
--                                 signal in the future, or before the start), or the entry
--                                 is reporting in but has run for more than a day.
--   why        the reason for the category, in words.
--   policy_action
--              close: no_signal         silent past the stop-after window; the job closes
--                                       it at last signal + grace
--              close: idle_unanswered   its idle prompt has been open past the limit
--              operator only ...        never reported liveness (started before the
--                                       heartbeat existed). The job never closes these;
--                                       an operator names the entry (see the 5958 plan).
--              leave open
--   last_seen_at          the heartbeat / upload clock (NULL on a legacy entry)
--   upload_evidence_at    the newest activity window end, screenshot or application-usage
--                         row that names the entry (works for desktops that predate the
--                         heartbeat: they upload an activity window every minute they run)
--   signal_source         where the last signal comes from
--   can_report_liveness   from DATA, not from a version guess: yes when the entry or the
--                         same person's other entries uploaded in the last 7 days
--   desktop_version / desktop_platform / version_checked_at
--                         the version the person's desktop last reported (its update
--                         check, every ~10 h; one row per user, so this is the version
--                         NOW, not necessarily the one that started this entry)
--   measured_before_policy_seconds   what Active Users showed before the policy existed
--   measured_with_policy_seconds     what it shows now
--   proposed_end / proposed_seconds  where the entry WOULD end if it were closed as of
--                         `as_of` (NULL for an entry that is reporting in and that the job leaves open).
--                         For a legacy entry this is what an explicit operator closure
--                         would write.
--
-- To replay a past moment ("as it stood on Monday 10:55 IST"), replace now() in the
-- `clock` CTE with a literal, e.g. TIMESTAMPTZ '2026-10-12 10:55:00+05:30'.
-- The thresholds in `policy` are the application defaults (app/core/config.py,
-- TIMER_*_SECONDS); edit them here to see what another policy would do.
WITH clock AS (
    SELECT now() AS as_of
),
policy AS (
    SELECT interval '10 minutes' AS stale_after,
           interval '15 minutes' AS grace,
           interval '2 hours'    AS no_signal_stop_after,
           interval '4 hours'    AS idle_unanswered_stop_after,
           interval '24 hours'   AS unusually_long
),
running AS (
    SELECT te.id, te.user_id, te.organization_id, te.project_id, te.task_id,
           te.start_time, te.end_time, te.status, te.last_seen_at
    FROM time_entries te
    WHERE te.end_time IS NULL
),
evidence AS (
    SELECT r.*,
           (SELECT max(a.recorded_at + a.window_seconds * interval '1 second')
              FROM time_entry_activity a WHERE a.time_entry_id = r.id)                   AS activity_end,
           (SELECT max(s.captured_at)
              FROM time_entry_screenshots s WHERE s.time_entry_id = r.id)                AS screenshot_at,
           (SELECT max(u.recorded_at)
              FROM time_entry_app_usage u WHERE u.time_entry_id = r.id)                  AS app_usage_at,
           (SELECT p.idle_detected_at FROM time_entry_idle_periods p
             WHERE p.time_entry_id = r.id AND p.status = 'pending'
             ORDER BY p.id DESC LIMIT 1)                                                 AS pending_idle_detected_at,
           (SELECT count(*) FROM time_entry_activity a2
              JOIN time_entries t2 ON t2.id = a2.time_entry_id
             WHERE t2.user_id = r.user_id AND t2.id <> r.id
               AND a2.recorded_at > (SELECT as_of FROM clock) - interval '7 days')       AS other_windows_7d
    FROM running r
),
signal AS (
    SELECT e.*,
           GREATEST(e.activity_end, e.screenshot_at, e.app_usage_at)                     AS upload_evidence_at,
           GREATEST(e.last_seen_at, e.activity_end, e.screenshot_at, e.app_usage_at)     AS last_signal_at
    FROM evidence e
),
base AS (
    SELECT s.*, c.as_of,
           p.stale_after, p.grace, p.no_signal_stop_after, p.idle_unanswered_stop_after, p.unusually_long,
           c.as_of - s.start_time                                                        AS running_for,
           c.as_of - s.last_signal_at                                                    AS silent_for,
           COALESCE(s.last_seen_at, GREATEST(s.activity_end, s.screenshot_at, s.app_usage_at), s.start_time)
                                                                                         AS basis_at
    FROM signal s CROSS JOIN clock c CROSS JOIN policy p
),
classified AS (
    SELECT b.*,
           (b.last_seen_at > b.as_of + interval '1 minute'
            OR b.last_seen_at < b.start_time - interval '5 minutes'
            OR b.start_time  > b.as_of + interval '1 minute')                            AS anomalous,
           (b.last_signal_at IS NOT NULL AND b.as_of - b.last_signal_at <= b.stale_after) AS fresh,
           CASE
               WHEN b.last_seen_at IS NOT NULL AND b.as_of - b.last_seen_at > b.no_signal_stop_after
                   THEN 'close: no_signal'
               WHEN b.last_seen_at IS NOT NULL AND b.pending_idle_detected_at IS NOT NULL
                    AND b.as_of - b.pending_idle_detected_at > b.idle_unanswered_stop_after
                   THEN 'close: idle_unanswered'
               WHEN b.last_seen_at IS NULL
                    AND b.as_of - COALESCE(GREATEST(b.activity_end, b.screenshot_at, b.app_usage_at), b.start_time)
                        > b.no_signal_stop_after
                   THEN 'operator only: never reported liveness (an operator names the entry)'
               ELSE 'leave open'
           END                                                                           AS policy_action
    FROM base b
),
judged AS (
    SELECT c.*,
           CASE
               WHEN c.anomalous                                  THEN 'manual_review'
               WHEN c.last_signal_at IS NULL                     THEN 'unverified'
               WHEN c.fresh AND c.running_for > c.unusually_long THEN 'manual_review'
               WHEN c.fresh                                      THEN 'confirmed_active'
               ELSE 'potentially_stale'
           END                                                                           AS category
    FROM classified c
)
SELECT j.id                                                       AS time_entry_id,
       j.user_id,
       u.name                                                     AS member,
       pr.project_name                                            AS project,
       t.task_name                                                AS task,
       j.start_time,
       j.end_time,
       j.status,
       j.category,
       CASE j.category
           WHEN 'manual_review' THEN
               CASE WHEN j.anomalous
                    THEN 'a timestamp is impossible (last signal in the future or before the start): look at it by hand'
                    ELSE 'reporting in, but running for more than 24 hours: confirm it is a real session'
               END
           WHEN 'unverified' THEN
               'nothing (heartbeat, activity window, screenshot, application usage) names this entry: no evidence either way'
           WHEN 'confirmed_active' THEN
               CASE WHEN j.policy_action = 'close: idle_unanswered'
                    THEN 'reporting in, but its idle prompt has gone unanswered past the limit; the job closes it at the prompt'
                    ELSE 'heard from within the stale window'
               END
           ELSE
               CASE WHEN j.policy_action LIKE 'close:%'
                    THEN 'silent past the policy window; the scheduled job closes it once enabled'
                    WHEN j.last_seen_at IS NULL
                    THEN 'last named by an old upload, never by a heartbeat; only an operator can close it'
                    ELSE 'went quiet, but not for long enough to close; could be offline-but-working'
               END
       END                                                        AS why,
       j.policy_action,
       j.last_seen_at,
       j.upload_evidence_at,
       j.last_signal_at,
       CASE WHEN j.last_seen_at IS NOT NULL      THEN 'last_seen_at (heartbeat or upload)'
            WHEN j.upload_evidence_at IS NOT NULL THEN 'legacy upload evidence only'
            ELSE 'none' END                                       AS signal_source,
       j.silent_for,
       j.running_for,
       round(extract(epoch FROM j.as_of - j.start_time))::bigint  AS measured_before_policy_seconds,
       round(extract(epoch FROM
             CASE WHEN j.last_seen_at IS NOT NULL AND j.as_of - j.last_seen_at > j.stale_after
                  THEN GREATEST(j.start_time, LEAST(j.as_of, j.last_seen_at + j.grace))
                  ELSE j.as_of END - j.start_time))::bigint       AS measured_with_policy_seconds,
       CASE
           WHEN j.last_seen_at IS NOT NULL
               THEN 'yes: this entry has reported liveness'
           WHEN j.other_windows_7d > 0
               THEN 'yes: this person uploaded ' || j.other_windows_7d || ' activity windows in the last 7 days'
           WHEN v.app_version IS NULL
               THEN 'unknown: no desktop version on record'
           ELSE 'unknown: desktop ' || v.app_version || ' (' || COALESCE(v.platform, '?')
                || ') uploaded nothing in the last 7 days'
       END                                                        AS can_report_liveness,
       v.app_version                                              AS desktop_version,
       v.platform                                                 AS desktop_platform,
       v.last_seen_at                                             AS version_checked_at,
       CASE WHEN j.fresh AND j.policy_action = 'leave open' THEN NULL ELSE
           GREATEST(j.start_time, LEAST(j.as_of,
               CASE WHEN j.policy_action = 'close: idle_unanswered'
                    THEN j.pending_idle_detected_at
                    ELSE GREATEST(j.basis_at + j.grace,
                                  CASE WHEN j.pending_idle_detected_at IS NOT NULL
                                       THEN LEAST(j.as_of, j.pending_idle_detected_at) END)
               END))
       END                                                        AS proposed_end,
       CASE WHEN j.fresh AND j.policy_action = 'leave open' THEN NULL ELSE
           round(extract(epoch FROM
               GREATEST(j.start_time, LEAST(j.as_of,
                   CASE WHEN j.policy_action = 'close: idle_unanswered'
                        THEN j.pending_idle_detected_at
                        ELSE GREATEST(j.basis_at + j.grace,
                                      CASE WHEN j.pending_idle_detected_at IS NOT NULL
                                           THEN LEAST(j.as_of, j.pending_idle_detected_at) END)
                   END)) - j.start_time))::bigint
       END                                                        AS proposed_seconds,
       j.pending_idle_detected_at
FROM judged j
JOIN users u            ON u.id = j.user_id
LEFT JOIN projects pr   ON pr.id = j.project_id
LEFT JOIN tasks t       ON t.id = j.task_id
LEFT JOIN desktop_client_versions v ON v.user_id = j.user_id
ORDER BY CASE j.category WHEN 'manual_review' THEN 0 WHEN 'potentially_stale' THEN 1
                         WHEN 'unverified' THEN 2 ELSE 3 END,
         j.silent_for DESC NULLS FIRST,
         j.id;

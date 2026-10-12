-- READ-ONLY. Finalized entries that look like a SLEPT-THROUGH DAY, booked by an older desktop.
-- It writes nothing and is safe to run in pgAdmin against production (docs/TIMING_MODEL.md section 9.6).
--
-- Why this exists. Desktops 1.0.0 to 1.3.2 split a running session at every IST midnight the machine
-- slept through, the moment it woke: a laptop closed Friday evening and opened Monday morning queued a
-- stop and a start at each midnight in between, and the backend accepts a backdated start for seven
-- days. The result is a run of CONTIGUOUS entries that begin and end exactly on IST midnights, run a
-- whole day, and carry no activity window, screenshot or application-usage row -- a machine that is
-- asleep reports nothing. A person who really worked through those hours has an activity window every
-- minute of them.
--
-- What it lists, per entry (one row each, oldest first):
--   full_day_no_activity   starts at an IST midnight, lasts 24 h, and nothing at all names it
--   edge_of_a_run          ends or starts at an IST midnight, lasts over 6 h, nothing names it, AND it is
--                          contiguous with a full_day_no_activity entry of the same member (the first
--                          and last entries of a weekend)
--
-- It is EVIDENCE for a person to review, not a verdict: nothing here is corrected automatically, and a
-- member's real overnight work (which does have activity rows) never appears. Correcting an entry is a
-- manual-time or administrator decision, entry by entry.
--
-- `since` limits the look-back; `tolerance` is how far from midnight an instant may sit (the server
-- places an instant by age, so a rolled-over start can be a few milliseconds off).
WITH params AS (
    SELECT now() - interval '120 days' AS since,
           interval '3 seconds'        AS tolerance
),
base AS (
    SELECT te.id, te.user_id, te.project_id, te.task_id, te.start_time, te.end_time, te.total_seconds,
           (SELECT count(*) FROM time_entry_activity a WHERE a.time_entry_id = te.id)
         + (SELECT count(*) FROM time_entry_screenshots s WHERE s.time_entry_id = te.id)
         + (SELECT count(*) FROM time_entry_app_usage u WHERE u.time_entry_id = te.id)   AS signal_rows,
           timezone('Asia/Kolkata', te.start_time)                                         AS start_ist,
           timezone('Asia/Kolkata', te.end_time)                                           AS end_ist
    FROM time_entries te, params p
    WHERE te.end_time IS NOT NULL
      AND te.is_manual = false
      AND te.start_time >= p.since
),
flagged AS (
    SELECT b.*,
           -- distance of an instant from the nearest IST midnight
           LEAST(extract(epoch FROM (b.start_ist - date_trunc('day', b.start_ist))),
                 extract(epoch FROM (date_trunc('day', b.start_ist) + interval '1 day' - b.start_ist))) AS start_off_midnight,
           LEAST(extract(epoch FROM (b.end_ist - date_trunc('day', b.end_ist))),
                 extract(epoch FROM (date_trunc('day', b.end_ist) + interval '1 day' - b.end_ist)))     AS end_off_midnight
    FROM base b
),
full_days AS (
    SELECT f.* FROM flagged f, params p
    WHERE f.signal_rows = 0
      AND f.start_off_midnight <= extract(epoch FROM p.tolerance)
      AND f.end_off_midnight   <= extract(epoch FROM p.tolerance)
      AND f.total_seconds BETWEEN 24 * 3600 - 10 AND 24 * 3600 + 10
),
edges AS (
    SELECT f.* FROM flagged f, params p
    WHERE f.signal_rows = 0
      AND f.total_seconds > 6 * 3600
      AND f.id NOT IN (SELECT id FROM full_days)
      AND (f.start_off_midnight <= extract(epoch FROM p.tolerance)
           OR f.end_off_midnight <= extract(epoch FROM p.tolerance))
      AND EXISTS (
            SELECT 1 FROM full_days d
            WHERE d.user_id = f.user_id
              AND (abs(extract(epoch FROM (d.start_time - f.end_time))) <= extract(epoch FROM p.tolerance)
                OR abs(extract(epoch FROM (f.start_time - d.end_time))) <= extract(epoch FROM p.tolerance)))
)
SELECT x.id                                              AS time_entry_id,
       x.user_id,
       u.name                                            AS member,
       pr.project_name                                   AS project,
       t.task_name                                       AS task,
       x.start_time,
       x.end_time,
       round(x.total_seconds / 3600.0, 2)                AS hours,
       x.signal_rows,
       x.reason
FROM (
    SELECT id, user_id, project_id, task_id, start_time, end_time, total_seconds, signal_rows,
           'full_day_no_activity' AS reason FROM full_days
    UNION ALL
    SELECT id, user_id, project_id, task_id, start_time, end_time, total_seconds, signal_rows,
           'edge_of_a_run' FROM edges
) x
JOIN users u            ON u.id = x.user_id
LEFT JOIN projects pr   ON pr.id = x.project_id
LEFT JOIN tasks t       ON t.id = x.task_id
ORDER BY x.user_id, x.start_time;

-- READ-ONLY. How long does a running timer go quiet to the SERVER in ordinary life?
-- This is the evidence for choosing TIMER_NO_SIGNAL_STOP_AFTER_SECONDS (docs/TIMING_MODEL.md
-- section 9.4). It writes nothing; run it in pgAdmin against production.
--
-- What it measures. Before the heartbeat existed, a desktop told the server it was alive
-- by uploading an activity window about every minute it ran. So the gaps between the times
-- the server RECEIVED an entry's consecutive windows (`created_at`, the server's clock) are
-- exactly the silences the new policy would have seen -- on entries that went on to
-- continue, because a later window is what ends a gap.
--
-- For each candidate "close after" threshold it answers:
--   silences_longer            how many silences in the last 60 days were longer than this
--   entries / members          in how many entries, for how many people
--   alive_but_offline          the first window after the silence had been recorded at least
--                              10 minutes before it arrived: the desktop was RUNNING and could
--                              not reach the server (an outage). Closing at this threshold
--                              would have cut work that really happened.
--   asleep_or_off              the first window after the silence was fresh: the machine was
--                              asleep or off. Closing at this threshold would have been right.
--   hours_past_the_grace       silent time beyond the 15 minutes the policy always keeps,
--                              i.e. what a closure at this threshold would not have counted
--
-- Caveat: `recorded_at` is the desktop's clock; a badly wrong clock misfiles a silence between
-- the two columns. The totals per threshold are unaffected.
--
-- Read it like this: pick the smallest threshold at which `alive_but_offline` is small enough to
-- accept. If it is large at 2 hours, the cost of 2 hours is real and a longer threshold (or the
-- manual-time request flow) is worth having; if `asleep_or_off` dominates, 2 hours is cutting
-- the right thing.
WITH params AS (
    SELECT now() - interval '60 days' AS since,
           interval '10 minutes'      AS backlog_after,
           interval '15 minutes'      AS grace
),
receipts AS (
    SELECT a.time_entry_id, te.user_id, a.created_at AS received_at, a.recorded_at,
           lag(a.created_at) OVER (PARTITION BY a.time_entry_id ORDER BY a.created_at, a.id) AS previous_received_at
    FROM time_entry_activity a
    JOIN time_entries te ON te.id = a.time_entry_id
    WHERE te.start_time >= (SELECT since FROM params)
),
silences AS (
    SELECT r.time_entry_id, r.user_id,
           r.received_at - r.previous_received_at                            AS silence,
           (r.received_at - r.recorded_at) > (SELECT backlog_after FROM params) AS arrived_as_backlog
    FROM receipts r
    WHERE r.previous_received_at IS NOT NULL
),
thresholds(threshold) AS (
    VALUES (interval '10 minutes'), (interval '30 minutes'), (interval '1 hour'),
           (interval '2 hours'), (interval '4 hours'), (interval '8 hours')
)
SELECT t.threshold,
       count(s.time_entry_id)                                              AS silences_longer,
       count(DISTINCT s.time_entry_id)                                     AS entries,
       count(DISTINCT s.user_id)                                           AS members,
       count(s.time_entry_id) FILTER (WHERE s.arrived_as_backlog)          AS alive_but_offline,
       count(s.time_entry_id) FILTER (WHERE NOT s.arrived_as_backlog)      AS asleep_or_off,
       round(coalesce(sum(extract(epoch FROM s.silence - (SELECT grace FROM params))), 0) / 3600.0, 1)
                                                                            AS hours_past_the_grace
FROM thresholds t
LEFT JOIN silences s ON s.silence > t.threshold
GROUP BY t.threshold
ORDER BY t.threshold;

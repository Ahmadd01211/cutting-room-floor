-- Drop-off detection.
--
-- Determinism lives HERE, not in the model. The LLM never decides what counts
-- as a drop; it only narrates what this returns. That is the difference between
-- an agent you can trust twice and a demo.
--
-- Method:
--   1. trailing baseline  = mean attention over the preceding `baseline_buckets`
--   2. z                  = (baseline - attention) / stddev of the cut's own
--                           bucket-to-bucket deltas, so the threshold is
--                           scale-free and travels across films
--   3. flag buckets where z exceeds {z_threshold}
--   4. collapse consecutive flagged buckets into segments (gaps-and-islands)
--   5. keep segments at least {min_duration_ms} long
--   6. ASOF each segment onto the shot it starts in
--
-- Params: {cut_id:String} {baseline_buckets:UInt32} {z_threshold:Float64}
--         {min_duration_ms:UInt32}

WITH
buckets AS
(
    SELECT cut_id, bucket_ms, attention, away_rate
    FROM crf.v_attention_250ms
    WHERE cut_id = {cut_id:String}
),
-- scale: how much does this cut normally move between adjacent buckets?
scale AS
(
    SELECT greatest(stddevPop(d), 0.05) AS sigma
    FROM (
        SELECT attention - lagInFrame(attention, 1) OVER (ORDER BY bucket_ms) AS d
        FROM buckets
    )
    WHERE d IS NOT NULL
),
scored AS
(
    SELECT
        cut_id,
        bucket_ms,
        attention,
        away_rate,
        avg(attention) OVER (
            ORDER BY bucket_ms
            ROWS BETWEEN {baseline_buckets:UInt32} PRECEDING AND 1 PRECEDING
        ) AS baseline
    FROM buckets
),
flagged AS
(
    SELECT
        cut_id,
        bucket_ms,
        attention,
        away_rate,
        baseline,
        (baseline - attention) / (SELECT sigma FROM scale) AS z
    FROM scored
    WHERE baseline > 0
      AND (baseline - attention) / (SELECT sigma FROM scale) >= {z_threshold:Float64}
),
-- gaps-and-islands: consecutive 250ms buckets share an island key
islands AS
(
    SELECT
        *,
        bucket_ms - 250 * (row_number() OVER (ORDER BY bucket_ms)) AS island
    FROM flagged
),
segments AS
(
    SELECT
        any(cut_id)                          AS cut_id,
        min(bucket_ms)                       AS start_ms,
        max(bucket_ms) + 250                 AS end_ms,
        max(bucket_ms) + 250 - min(bucket_ms) AS duration_ms,
        round(avg(z), 2)                     AS z_mean,
        round(max(z), 2)                     AS z_peak,
        round(avg(baseline) - avg(attention), 2) AS attention_lost,
        round(avg(away_rate) * 100, 2)       AS away_pct
    FROM islands
    GROUP BY island
    HAVING duration_ms >= {min_duration_ms:UInt32}
)
SELECT
    sg.start_ms      AS start_ms,
    sg.end_ms        AS end_ms,
    sg.duration_ms   AS duration_ms,
    sg.attention_lost AS attention_lost,
    sg.z_peak        AS z_peak,
    sg.away_pct      AS away_pct,
    sh.shot_idx      AS shot_idx,
    sh.scene_id      AS scene_id,
    sh.slug          AS slug,
    -- rank: how much attention, for how long, is what makes a note worth giving
    round(sg.attention_lost * (sg.duration_ms / 1000), 1) AS severity
FROM segments AS sg
ASOF LEFT JOIN crf.shot AS sh
    ON sg.cut_id = sh.cut_id AND sg.start_ms >= sh.tc_in_ms
ORDER BY severity DESC;

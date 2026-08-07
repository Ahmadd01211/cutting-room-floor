-- Per-shot attention, via ASOF JOIN.
--
-- This is the load-bearing query of the whole project. Reaction telemetry knows
-- only "where am I in the film"; the EDL knows only "where do the cuts fall".
-- ASOF is what marries them: for each 250ms bucket, attach the shot whose
-- tc_in_ms is the greatest value not after that bucket.
--
-- The inequality MUST be the last condition — every earlier condition is treated
-- as an equality key. Keeping cut_id as an equality key is what stops buckets
-- from one cut matching shots from another.
--
-- Params: {cut_id:String}

WITH buckets AS
(
    SELECT cut_id, bucket_ms, attention, attention_p10, dial, away_rate, viewers
    FROM crf.v_attention_250ms
    WHERE cut_id = {cut_id:String}
)
SELECT
    s.shot_idx                             AS shot_idx,
    s.scene_id                             AS scene_id,
    s.slug                                 AS slug,
    s.tc_in_ms                             AS tc_in_ms,
    s.tc_out_ms                            AS tc_out_ms,
    s.tc_out_ms - s.tc_in_ms               AS duration_ms,
    count()                                AS buckets,
    round(avg(b.attention), 2)             AS attention,
    round(min(b.attention), 2)             AS attention_min,
    round(avg(b.attention_p10), 2)         AS attention_p10,
    round(avg(b.dial), 2)                  AS dial,
    round(avg(b.away_rate) * 100, 2)       AS away_pct
FROM buckets AS b
ASOF LEFT JOIN crf.shot AS s
    ON b.cut_id = s.cut_id AND b.bucket_ms >= s.tc_in_ms
-- ASOF clamps anything past the tail onto the final shot; drop those rather
-- than letting run-out padding inflate the last shot's numbers.
WHERE b.bucket_ms < s.tc_out_ms
GROUP BY shot_idx, scene_id, slug, tc_in_ms, tc_out_ms
ORDER BY tc_in_ms;

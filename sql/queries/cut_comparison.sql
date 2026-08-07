-- Scene-by-scene comparison of two cuts.
--
-- Shot boundaries move between cuts — that is the whole point of re-cutting —
-- so the stable unit of comparison is the SCENE, not the shot or the timecode.
-- A scene that ran 94 seconds in the assembly and 61 in the next pass is the
-- same scene; comparing it at 00:04:12 in both would compare different material.
--
-- Both cuts resolve in a single ASOF pass because cut_id is an equality key.
--
-- Params: {cut_a:String} {cut_b:String}

WITH
buckets AS
(
    SELECT cut_id, bucket_ms, attention, away_rate
    FROM crf.v_attention_250ms
    WHERE cut_id IN ({cut_a:String}, {cut_b:String})
),
per_scene AS
(
    SELECT
        s.cut_id                  AS cut_id,
        s.scene_id                AS scene_id,
        avg(b.attention)          AS attention,
        avg(b.away_rate)          AS away_rate,
        count() * 250             AS screen_ms
    FROM buckets AS b
    ASOF LEFT JOIN crf.shot AS s
        ON b.cut_id = s.cut_id AND b.bucket_ms >= s.tc_in_ms
    WHERE b.bucket_ms < s.tc_out_ms
    GROUP BY cut_id, scene_id
)
SELECT
    scene_id,
    round(avgIf(attention, cut_id = {cut_a:String}), 2)          AS attention_a,
    round(avgIf(attention, cut_id = {cut_b:String}), 2)          AS attention_b,
    round(avgIf(attention, cut_id = {cut_b:String})
        - avgIf(attention, cut_id = {cut_a:String}), 2)          AS attention_delta,
    toUInt32(sumIf(screen_ms, cut_id = {cut_a:String}))          AS screen_ms_a,
    toUInt32(sumIf(screen_ms, cut_id = {cut_b:String}))          AS screen_ms_b,
    toInt32(sumIf(screen_ms, cut_id = {cut_b:String}))
        - toInt32(sumIf(screen_ms, cut_id = {cut_a:String}))     AS runtime_delta_ms,
    round(avgIf(away_rate, cut_id = {cut_b:String}) * 100
        - avgIf(away_rate, cut_id = {cut_a:String}) * 100, 2)    AS away_delta_pct
FROM per_scene
GROUP BY scene_id
HAVING screen_ms_a > 0 AND screen_ms_b > 0
ORDER BY attention_delta DESC;

-- Did the note actually work?
--
-- This is the query the whole project builds toward. Every analytics tool tells
-- you what is wrong. Almost none tell you whether their own advice helped.
--
-- For each note raised against cut A and marked applied in cut B, compare the
-- scene's attention across the two cuts and report whether the recommendation
-- moved the number in the direction it predicted.
--
-- The `verdict` is computed here, in SQL, not by a model. A recommendation
-- engine that gets to grade its own homework in prose is worth nothing.
--
-- Params: {run_id:String} {cut_a:String} {cut_b:String} {min_effect:Float64}

WITH
buckets AS
(
    SELECT cut_id, bucket_ms, attention
    FROM crf.v_attention_250ms
    WHERE cut_id IN ({cut_a:String}, {cut_b:String})
),
per_scene AS
(
    SELECT
        s.cut_id         AS cut_id,
        s.scene_id       AS scene_id,
        avg(b.attention) AS attention,
        count() * 250    AS screen_ms
    FROM buckets AS b
    ASOF LEFT JOIN crf.shot AS s
        ON b.cut_id = s.cut_id AND b.bucket_ms >= s.tc_in_ms
    WHERE b.bucket_ms < s.tc_out_ms
    GROUP BY cut_id, scene_id
),
scene_delta AS
(
    SELECT
        scene_id,
        avgIf(attention, cut_id = {cut_a:String}) AS attention_a,
        avgIf(attention, cut_id = {cut_b:String}) AS attention_b,
        toInt32(sumIf(screen_ms, cut_id = {cut_b:String}))
            - toInt32(sumIf(screen_ms, cut_id = {cut_a:String})) AS runtime_delta_ms
    FROM per_scene
    GROUP BY scene_id
),
-- the film moved as a whole between cuts; credit the note only with the
-- movement in EXCESS of that, or every note looks like a win in a better cut
baseline AS
(
    SELECT avg(attention_b - attention_a) AS drift FROM scene_delta
)
SELECT
    n.note_id                                   AS note_id,
    n.scene_id                                  AS scene_id,
    n.headline                                  AS headline,
    n.suggested_trim_ms                         AS suggested_trim_ms,
    d.runtime_delta_ms                          AS actual_trim_ms,
    round(d.attention_a, 2)                     AS attention_before,
    round(d.attention_b, 2)                     AS attention_after,
    round(d.attention_b - d.attention_a, 2)     AS raw_gain,
    round((SELECT drift FROM baseline), 2)      AS film_drift,
    round(d.attention_b - d.attention_a
        - (SELECT drift FROM baseline), 2)      AS adjusted_gain,
    multiIf(
        d.attention_b - d.attention_a - (SELECT drift FROM baseline) >=  {min_effect:Float64}, 'improved',
        d.attention_b - d.attention_a - (SELECT drift FROM baseline) <= -{min_effect:Float64}, 'regressed',
        'no effect'
    )                                           AS verdict
FROM crf.note AS n
INNER JOIN scene_delta AS d ON d.scene_id = n.scene_id
WHERE n.run_id = {run_id:String}
  AND n.cut_id = {cut_a:String}
  AND n.status = 'applied'
ORDER BY adjusted_gain DESC;

-- Who is the scene losing?
--
-- A scene that drops 6 points across everyone is a pacing problem. A scene that
-- drops 2 points overall but 14 among one cohort is a different note entirely,
-- and it is the note an editor cannot get from a screening-room PDF.
--
-- Each cohort is compared against ITS OWN film-wide mean, not against the pooled
-- mean — otherwise a cohort that simply runs cold overall looks like it hates
-- every scene.
--
-- Params: {cut_id:String} {start_ms:UInt32} {end_ms:UInt32}

WITH
cohort_baseline AS
(
    SELECT age_band, avg(attention) AS film_mean
    FROM crf.v_attention_250ms_cohort
    WHERE cut_id = {cut_id:String}
    GROUP BY age_band
),
window_mean AS
(
    SELECT
        age_band,
        avg(attention)   AS seg_mean,
        avg(away_rate)   AS seg_away,
        max(viewers)     AS viewers
    FROM crf.v_attention_250ms_cohort
    WHERE cut_id = {cut_id:String}
      AND bucket_ms >= {start_ms:UInt32}
      AND bucket_ms <  {end_ms:UInt32}
    GROUP BY age_band
)
SELECT
    w.age_band                              AS age_band,
    b.viewers                               AS viewers,
    round(c.film_mean, 2)                   AS film_mean,
    round(w.seg_mean, 2)                    AS segment_mean,
    round(w.seg_mean - c.film_mean, 2)      AS divergence,
    round(w.seg_away * 100, 2)              AS away_pct
FROM window_mean AS w
INNER JOIN cohort_baseline AS c USING (age_band)
INNER JOIN window_mean     AS b USING (age_band)
ORDER BY divergence ASC;

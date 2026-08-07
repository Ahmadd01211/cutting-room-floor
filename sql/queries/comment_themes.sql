-- What are people actually complaining about, and where?
--
-- Telemetry says attention dropped. Comments say why. Joining them is what
-- turns "scene 14 lost the room" into "scene 14 lost the room and 61% of the
-- comments about it mention pacing" — which is a note an editor can act on.
--
-- Comments carry a media_ms when they were made about a specific moment, so
-- they attach to a scene by the same ASOF mechanism as reaction telemetry.
-- Unscoped comments (about the film overall) are excluded from the join and
-- reported separately.
--
-- Params: {cut_id:String} {run_id:String}

WITH scoped AS
(
    SELECT
        c.comment_id AS comment_id,
        c.cut_id     AS cut_id,
        c.media_ms   AS media_ms,
        c.body       AS body
    FROM crf.comment AS c
    WHERE c.cut_id = {cut_id:String} AND c.scoped = 1
),
attributed AS
(
    SELECT
        sc.comment_id AS comment_id,
        s.scene_id    AS scene_id,
        sc.body       AS body
    FROM scoped AS sc
    ASOF LEFT JOIN crf.shot AS s
        ON sc.cut_id = s.cut_id AND sc.media_ms >= s.tc_in_ms
    WHERE sc.media_ms < s.tc_out_ms
)
SELECT
    at.scene_id                                   AS scene_id,
    a.theme                                       AS theme,
    count()                                       AS comments,
    round(avg(a.sentiment), 1)                    AS sentiment_mean,
    round(100 * countIf(a.sentiment < -20) / count(), 1) AS negative_pct,
    round(avg(a.confidence), 2)                   AS confidence,
    -- three verbatims, so a note can quote the audience rather than paraphrase
    arraySlice(groupArray(at.body), 1, 3)         AS examples
FROM attributed AS at
INNER JOIN crf.comment_analysis AS a ON a.comment_id = at.comment_id
WHERE a.run_id = {run_id:String}
GROUP BY scene_id, theme
HAVING comments >= 3
ORDER BY negative_pct DESC, comments DESC;

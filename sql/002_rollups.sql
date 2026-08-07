-- Insert-time rollups.
--
-- The agent must never scan the raw firehose. An incremental materialized view
-- collapses ~28M raw rows into ~100k pre-aggregated buckets as they land, so
-- every question the agent asks is answered from aggregate state.
--
-- Aggregate *state* is stored, not finished numbers — so buckets stay mergeable
-- and we can re-window at query time (per shot, per scene, per cohort) without
-- ever going back to the raw rows.

CREATE TABLE IF NOT EXISTS crf.reaction_250ms
(
    cut_id        LowCardinality(String),
    age_band      LowCardinality(String),
    bucket_ms     UInt32,
    viewers       AggregateFunction(uniq, UInt32),
    attention_avg AggregateFunction(avg, UInt8),
    attention_p10 AggregateFunction(quantileTDigest(0.10), UInt8),
    dial_avg      AggregateFunction(avg, Int8),
    away_rate     AggregateFunction(avg, UInt8)
)
ENGINE = AggregatingMergeTree
ORDER BY (cut_id, age_band, bucket_ms);

CREATE MATERIALIZED VIEW IF NOT EXISTS crf.mv_reaction_250ms
TO crf.reaction_250ms
AS
SELECT
    cut_id,
    age_band,
    intDiv(media_ms, 250) * 250                AS bucket_ms,
    uniqState(viewer_id)                       AS viewers,
    avgState(attention)                        AS attention_avg,
    quantileTDigestState(0.10)(attention)      AS attention_p10,
    avgState(dial)                             AS dial_avg,
    avgState(looked_away)                      AS away_rate
FROM crf.reaction_event
GROUP BY cut_id, age_band, bucket_ms;

-- Convenience view: merged buckets, all cohorts pooled. Everything the agent
-- asks starts here.
CREATE VIEW IF NOT EXISTS crf.v_attention_250ms AS
SELECT
    cut_id,
    bucket_ms,
    uniqMerge(viewers)                    AS viewers,
    avgMerge(attention_avg)               AS attention,
    quantileTDigestMerge(0.10)(attention_p10) AS attention_p10,
    avgMerge(dial_avg)                    AS dial,
    avgMerge(away_rate)                   AS away_rate
FROM crf.reaction_250ms
GROUP BY cut_id, bucket_ms;

-- Same, split by cohort — for the divergence question ("who is this losing?").
CREATE VIEW IF NOT EXISTS crf.v_attention_250ms_cohort AS
SELECT
    cut_id,
    age_band,
    bucket_ms,
    uniqMerge(viewers)      AS viewers,
    avgMerge(attention_avg) AS attention,
    avgMerge(away_rate)     AS away_rate
FROM crf.reaction_250ms
GROUP BY cut_id, age_band, bucket_ms;

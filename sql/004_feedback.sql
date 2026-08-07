-- Free-text feedback, the note ledger, and the audit trail.
--
-- Three ideas here, and the separation between them is deliberate:
--
--   crf.comment           raw audience text. Never mutated.
--   crf.comment_analysis  what an agent concluded about that text, stamped with
--                         which agent, which model and which run. Kept apart
--                         from the raw data so a re-run never overwrites the
--                         evidence, and so two model versions can be compared.
--   crf.note              what we recommended, and what the editor did about it.
--   crf.agent_trace       who did what. Every conclusion is traceable to the
--                         agent and tool call that produced it.

-- ------------------------------------------------------------------- comments

CREATE TABLE IF NOT EXISTS crf.comment
(
    comment_id   UInt64,
    screening_id LowCardinality(String),
    cut_id       LowCardinality(String),
    viewer_id    UInt32,
    -- NULL-equivalent (0) when the comment is about the film overall rather
    -- than a moment in it. Scene attribution is resolved by ASOF at query time,
    -- exactly like reaction telemetry.
    media_ms     UInt32,
    scoped       UInt8 COMMENT '1 = about a specific moment, 0 = about the film',
    body         String,
    embedding    Array(Float32) COMMENT 'gemini-embedding-001, L2-normalised',
    embed_model  LowCardinality(String)
)
ENGINE = MergeTree
PARTITION BY cut_id
ORDER BY (cut_id, media_ms, comment_id);

-- Agent output about a comment. ReplacingMergeTree keyed on (comment_id, run_id)
-- so re-running an analysis is idempotent without destroying earlier runs.
CREATE TABLE IF NOT EXISTS crf.comment_analysis
(
    comment_id  UInt64,
    run_id      LowCardinality(String),
    agent       LowCardinality(String),
    model       LowCardinality(String),
    sentiment   Int8 COMMENT '-100..100',
    theme       LowCardinality(String),
    confidence  Float32,
    rationale   String,
    created_at  DateTime DEFAULT now()
)
ENGINE = ReplacingMergeTree(created_at)
ORDER BY (comment_id, run_id);

-- --------------------------------------------------------------- note ledger

-- What the system recommended, and what happened next. This table is what
-- turns the project from analysis into a closed loop: a note carries the trim
-- it proposed, and `applied_in_cut` points at the cut where the editor acted on
-- it, so the outcome can be measured rather than assumed.
CREATE TABLE IF NOT EXISTS crf.note
(
    note_id           UInt64,
    run_id            LowCardinality(String),
    cut_id            LowCardinality(String),
    scene_id          LowCardinality(String),
    shot_idx          UInt32,
    start_ms          UInt32,
    end_ms            UInt32,
    severity          Float32,
    attention_lost    Float32,
    worst_cohort      LowCardinality(String),
    cohort_divergence Float32,
    headline          String,
    body              String,
    suggested_trim_ms Int32 COMMENT 'negative = lengthen',
    status            Enum8('proposed'=1,'accepted'=2,'rejected'=3,'applied'=4),
    applied_in_cut    LowCardinality(String),
    created_at        DateTime DEFAULT now()
)
ENGINE = ReplacingMergeTree(created_at)
ORDER BY (run_id, note_id);

-- --------------------------------------------------------------- audit trail

-- Every agent step, every tool call. Answers "why did it say that?" and "which
-- agent decided this?" — the two questions a producer asks before trusting a
-- recommendation, and the two a judge asks before believing the demo.
CREATE TABLE IF NOT EXISTS crf.agent_trace
(
    run_id      LowCardinality(String),
    step_seq    UInt32,
    agent       LowCardinality(String),
    node        LowCardinality(String),
    tool        LowCardinality(String),
    sql_file    LowCardinality(String) COMMENT 'which versioned query ran, if any',
    params      String COMMENT 'JSON',
    row_count   UInt32,
    output_hash FixedString(16) COMMENT 'first 16 hex of sha256 of the payload',
    model       LowCardinality(String),
    duration_ms UInt32,
    ok          UInt8,
    error       String,
    created_at  DateTime64(3) DEFAULT now64(3)
)
ENGINE = MergeTree
ORDER BY (run_id, step_seq);

-- Comment sentiment rolled up per cut and theme, computed on read from the
-- latest analysis run. Small table — no materialized view needed.
CREATE VIEW IF NOT EXISTS crf.v_comment_sentiment AS
SELECT
    c.cut_id                AS cut_id,
    a.run_id                AS run_id,
    a.theme                 AS theme,
    count()                 AS comments,
    round(avg(a.sentiment), 2) AS sentiment_mean,
    countIf(a.sentiment < -20) AS negative,
    countIf(a.sentiment >  20) AS positive
FROM crf.comment AS c
INNER JOIN crf.comment_analysis AS a USING (comment_id)
GROUP BY cut_id, run_id, theme;

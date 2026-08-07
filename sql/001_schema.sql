-- The Cutting Room Floor — core schema
--
-- Media time is always milliseconds from the head of a *cut*, never wall clock
-- and never frames. Frames are ambiguous across 23.976/24/25 and wall clock is
-- meaningless once you re-order the timeline.

CREATE DATABASE IF NOT EXISTS crf;

-- ---------------------------------------------------------------- film & cuts

CREATE TABLE IF NOT EXISTS crf.film
(
    film_id     LowCardinality(String),
    title       String,
    year        UInt16,
    fps         Float32,
    runtime_ms  UInt32,
    rights_note String  COMMENT 'provenance — how we are allowed to use this title'
)
ENGINE = MergeTree
ORDER BY film_id;

-- An edit version. Two cuts of the same film are compared against each other,
-- so cut_id is the primary partition of everything downstream.
CREATE TABLE IF NOT EXISTS crf.cut
(
    cut_id     LowCardinality(String),
    film_id    LowCardinality(String),
    label      String,
    runtime_ms UInt32,
    created_at DateTime DEFAULT now()
)
ENGINE = MergeTree
ORDER BY (film_id, cut_id);

-- ------------------------------------------------------------------- timeline

-- One row per shot, from the EDL. tc_in_ms is the ASOF probe key: for any point
-- in media time, the shot you are watching is the one with the greatest
-- tc_in_ms not after you.
CREATE TABLE IF NOT EXISTS crf.shot
(
    cut_id    LowCardinality(String),
    shot_idx  UInt32,
    tc_in_ms  UInt32,
    tc_out_ms UInt32,
    scene_id  LowCardinality(String),
    slug      String
)
ENGINE = MergeTree
ORDER BY (cut_id, tc_in_ms);

-- Scenes carry the text we reason about. `embedding` is populated for BOTH the
-- film under review and a comparison corpus of other films — "scenes like this
-- one that worked" is only a useful question if it can look outside this cut.
CREATE TABLE IF NOT EXISTS crf.scene
(
    film_id   LowCardinality(String),
    scene_id  LowCardinality(String),
    scene_num UInt32,
    heading   String,
    synopsis  String,
    dialogue  String,
    tags      Array(LowCardinality(String)),
    embedding Array(Float32) COMMENT 'gemini-embedding-001, L2-normalised'
)
ENGINE = MergeTree
ORDER BY (film_id, scene_num);

-- ------------------------------------------------------------------ telemetry

-- The raw firehose: one row per viewer per sample tick. At 4 Hz a 96-minute
-- feature is ~23k rows per viewer, so a 400-person screening is ~9.2M rows and
-- a three-cut study is ~28M. This is the table that makes ClickHouse the right
-- tool rather than a nice-to-have.
CREATE TABLE IF NOT EXISTS crf.reaction_event
(
    screening_id LowCardinality(String),
    cut_id       LowCardinality(String),
    viewer_id    UInt32,
    media_ms     UInt32 CODEC(DoubleDelta, ZSTD(1)),
    attention    UInt8  COMMENT '0..100 engagement index',
    dial         Int8   COMMENT '-100..100 dial-test valence',
    looked_away  UInt8  COMMENT '0/1 gaze off-screen this tick',
    age_band     LowCardinality(String),
    device       LowCardinality(String),
    first_time   UInt8  COMMENT '1 = has not seen any cut of this film before'
)
ENGINE = MergeTree
PARTITION BY cut_id
ORDER BY (cut_id, media_ms, viewer_id)
SETTINGS index_granularity = 8192;

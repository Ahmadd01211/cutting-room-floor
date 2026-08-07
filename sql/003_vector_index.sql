-- Vector similarity index for "scenes like this one that worked".
--
-- Applied separately from the core schema because it is optional: the index
-- accelerates the search, it does not define it. `cosineDistance` returns the
-- same answer without it, and some ClickHouse builds (chdb, for one) compile
-- the index type out entirely — so `migrate()` tolerates failure here.
--
-- The index earns its place because the corpus is not one film. Searching for
-- comparable scenes only inside the cut under review would be near-useless —
-- the whole value of the question is "where has this problem been solved
-- before", which means searching a corpus of other films' scenes. That is tens
-- of thousands of vectors, where HNSW stops being decoration.

SET allow_experimental_vector_similarity_index = 1;

ALTER TABLE crf.scene
    ADD INDEX IF NOT EXISTS idx_scene_embedding embedding
    TYPE vector_similarity('hnsw', 'cosineDistance', 768)
    GRANULARITY 1;

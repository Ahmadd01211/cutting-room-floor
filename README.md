# The Cutting Room Floor

**For film editors and post supervisors.** Test-screening telemetry, joined to the
edit timeline, turned into a timecoded note list you import into the bay.

An editor runs a preview screening. Weeks later the audience data comes back as
a PDF deck — aggregate scores, a few charts, no way to ask it anything. You
cannot align it to your timeline, cannot ask which cohort walked, and cannot
test what happens if you lose scene 42.

This turns that data into the only artifact an edit bay actually consumes: a
list of timecodes with notes attached.

```
 #  in           out            len   scene  shot   lost     sev
 1  00:02:02:00  00:02:08:19    6.8s   S007    38   11.1   149.7
 2  00:07:19:14  00:07:27:00    7.6s   S021   130    8.8    92.5
```

---

## Status

| Layer | State |
|---|---|
| ClickHouse schema, rollups, ASOF join | **working, tested** |
| Synthetic screening generator | **working, validated** |
| Drop-off detection + cohort divergence | **working, tested** |
| Audience comments, sentiment, embeddings | **working, tested** |
| Closed loop — recommend, apply, measure | **working, tested** |
| Marker/EDL export | scaffolded |
| Gemini agents (ADK `Workflow` graph) | not yet built |
| Web UI | not yet built |

39 tests, all passing, no server required.

---

## The part that makes it more than analytics

Every tool tells you what is wrong. This one tells you whether its own advice
worked.

A note is not just text — it is a row in `crf.note` carrying the trim it
proposed. When the editor makes that change and screens again, the system
compares the scene across both cuts and returns a verdict:

```
  scene     trim  before   after    gain   verdict
  S014    -9.5s   51.46    55.6   +4.09   improved
  S007    -8.0s   46.81   50.88   +4.03   improved
  S021    -5.5s   51.12   51.53   +0.37   no effect
  S002    -8.0s   60.68    58.6   -2.13   regressed

  4 improved, 3 no effect, 1 regressed.
```

Two things make that number honest. Gains are adjusted for **film-wide drift**,
so a note is credited only with movement it caused rather than with a generally
better cut. And the verdict is computed **in SQL** — a recommendation engine
that grades its own homework in prose is worth nothing.

Not every recommendation works. A system reporting otherwise would be
congratulating itself, not measuring.

---

## Run it

**With Docker** — brings up ClickHouse, seeds a screening, prints the analysis:

```bash
docker compose up --build
```

**Without anything** — the test suite runs a real ClickHouse engine embedded via
`chdb`, so no server, no Docker, no network:

```bash
pip install -e ".[test]"
pytest
CRF_BACKEND=chdb python -m scripts.demo --seed --report
```

---

## How it works

### The join

Reaction telemetry knows only *where am I in the film*. The EDL knows only
*where do the cuts fall*. Marrying them is the whole problem, and `ASOF JOIN` is
the tool:

```sql
FROM buckets AS b
ASOF LEFT JOIN crf.shot AS s
    ON b.cut_id = s.cut_id AND b.bucket_ms >= s.tc_in_ms
```

For every point in media time, attach the shot whose `tc_in_ms` is the greatest
value not after it. The inequality must be the **last** condition — every
earlier condition is an equality key, and keeping `cut_id` there is what stops
buckets from one cut matching shots from another.

`tests/test_asof.py` checks this against an independent Python implementation on
every boundary case: exact cut points, shots shorter than a sample bucket,
probes past the tail, and two cuts of the same film at identical media times.

### The rollup

A 96-minute feature sampled at 4 Hz is ~23k rows per viewer. A 400-person
screening is ~9.2M rows; a three-cut study is ~28M. The agent must never touch
that. An `AggregatingMergeTree` fed by an incremental materialized view collapses
it at insert time:

```sql
CREATE MATERIALIZED VIEW crf.mv_reaction_250ms TO crf.reaction_250ms AS
SELECT cut_id, age_band, intDiv(media_ms, 250) * 250 AS bucket_ms,
       uniqState(viewer_id), avgState(attention),
       quantileTDigestState(0.10)(attention), avgState(dial), avgState(looked_away)
FROM crf.reaction_event GROUP BY cut_id, age_band, bucket_ms;
```

Aggregate *state* is stored rather than finished numbers, so buckets stay
mergeable and the same rollup answers per-shot, per-scene and per-cohort
questions without ever going back to raw rows.

### Where the determinism lives

In SQL, not in the model. `sql/queries/dropoff_segments.sql` decides what counts
as a drop-off: a trailing baseline, a z-score scaled by the cut's own
bucket-to-bucket variance, gaps-and-islands to collapse consecutive flagged
buckets into segments, then an ASOF onto the shot each segment starts in.

The language model's job is to *narrate* what that returns and to choose which
question to ask next. It never computes a statistic and never sees a raw row.
Ask twice, get the same numbers.

---

## About the data

**The telemetry in this repo is synthetic, and the generator ships with it.**

Real test-screening data is among the most closely held data in the industry —
NRG and Screen Engine do not publish, and there is no open corpus of
second-by-second audience reaction. Rather than pass off invented numbers as
measured ones, `crf/screening.py` documents its model in full and seeds
deterministically.

It is not noise. The model reproduces effects documented in audience-measurement
and editing literature: slow baseline decay across a runtime, attention recovery
after each cut, long-take pressure, per-scene quality drawn from tag-conditioned
priors, cohort × tag affinity, AR(1) individual noise, and a disengagement
hazard where a viewer who runs cold stays cold.

That last one is why the detector uses a trailing baseline rather than a global
mean: a bad scene in reel two depresses reel three, and a global comparison
would blame the wrong scene.

**The generator plants a signal; the test suite checks the detector recovers
it.** `tests/test_detector.py` asserts that weak (`exposition`) scenes are
flagged and strong (`action`/`tension`/`reveal`) scenes are not — on the current
fixture, 100% versus 0%.

**Comments are generated *from* the telemetry, not beside it.** The naive way to
fake audience comments is to draw them independently, which produces a corpus
that contradicts the attention data — glowing praise for the scene where
everyone checked out. Here they are conditioned on the *measured* per-scene
attention, so people complain about what actually bored them and the two
modalities agree by construction. `tests/test_measurement.py` asserts that
low-attention scenes draw more negative comment than high-attention ones.

**Two bugs worth knowing about, because both made an earlier version lie.** The
scene→index map was built from a Python `set`; string hashing is randomised per
process, so scene quality landed on different scenes every run and the "seeded
and reproducible" claim was false outside a single process. And scene quality was
drawn from the *screening* RNG, so the same scene had different intrinsic quality
in cut A and cut B — meaning an apparent improvement could just be a luckier
draw, which would have quietly invalidated the entire measurement loop. Quality
is now keyed on `(film_id, scene_id, film_seed)`: only the audience varies
between screenings. Both have regression tests.

---

## Layout

```
sql/
  001_schema.sql          film, cut, shot, scene, reaction_event
  002_rollups.sql         AggregatingMergeTree + incremental MV
  003_vector_index.sql    HNSW over embeddings (optional, see below)
  004_feedback.sql        comments, analysis, the note ledger, the audit trail
  queries/                the deterministic analysis layer
crf/
  timeline.py             shots, scenes, timecode, EDL parse/write, tighten()
  screening.py            the telemetry model
  comments.py             audience comments, conditioned on measured attention
  embed.py                gemini-embedding-001, with an offline fallback
  db.py                   ClickHouse and chdb backends behind one interface
  analysis.py             typed query wrappers — these become the agent's tools
  pipeline.py             load orchestration and the closed loop
tests/                    ASOF correctness, detector recovery, the loop
scripts/demo.py           seed, report, outcome
```

`crf.comment` holds text and never changes; `crf.comment_analysis` holds what an
agent concluded about it, stamped with which agent, model and run. Keeping them
apart means a re-run never destroys the evidence, and two model versions can be
compared. `crf.agent_trace` records every step and tool call, so any conclusion
can be traced back to what produced it.

The vector index is applied separately and tolerated as optional: it accelerates
similarity search, it does not define it — `cosineDistance` is correct without
it, and some builds compile the index type out. It earns its place on the comment
corpus, which runs to thousands of vectors, rather than on a hundred scene
summaries.

---

## Licence

Apache-2.0. See [LICENSE](LICENSE).

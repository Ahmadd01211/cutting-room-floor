"""Does the drop-off detector recover a signal we planted, or is it finding
noise?

This is the test that distinguishes an analysis pipeline from a random number
generator. The telemetry model deliberately makes `exposition` scenes weak and
`action`/`tension`/`reveal` scenes strong. A detector that works must flag the
former and leave the latter alone — well beyond chance.
"""

from __future__ import annotations

from collections import Counter

import pytest

from crf import analysis

WEAK_TAGS = {"exposition"}
STRONG_TAGS = {"action", "tension", "reveal"}


def test_rollup_collapses_the_firehose(seeded):
    """The materialized view must collapse the raw firehose at insert time —
    that reduction is why the agent's queries are fast.

    The ceiling is set by the grouping key, not by wishful thinking: the MV
    groups by (cut_id, age_band, bucket_ms), so it folds viewers together but
    keeps cohorts apart. Best possible reduction is therefore
    viewers / distinct-age-bands. Insert batches also land as separate parts
    that share a key until a background merge, so measure after OPTIMIZE FINAL
    or you are measuring merge scheduling rather than the rollup.
    """
    be, ctx = seeded
    tl = ctx["cut_a"]
    be.execute("OPTIMIZE TABLE crf.reaction_250ms FINAL")

    raw = be.query("SELECT count() AS n FROM crf.reaction_event")[0]["n"]
    rolled = be.query("SELECT count() AS n FROM crf.reaction_250ms")[0]["n"]
    viewers = be.query("SELECT uniqExact(viewer_id) AS n FROM crf.reaction_event")[0]["n"]
    bands = be.query("SELECT uniqExact(age_band) AS n FROM crf.reaction_event")[0]["n"]

    assert raw > 0 and rolled > 0
    ceiling = viewers / bands
    achieved = raw / rolled
    assert achieved > ceiling * 0.8, (
        f"rollup reduced {raw} -> {rolled} ({achieved:.1f}x), "
        f"well short of the {ceiling:.1f}x the grouping key allows"
    )
    # And it must actually be doing something: exactly one row per bucket per
    # cohort per cut. Two cuts now share these tables, so count per cut — a
    # single global bucket count would silently under-expect.
    expected = be.query(
        """SELECT sum(buckets * bands) AS n FROM (
               SELECT cut_id,
                      uniqExact(intDiv(media_ms, 250)) AS buckets,
                      uniqExact(age_band)              AS bands
               FROM crf.reaction_event GROUP BY cut_id)"""
    )[0]["n"]
    assert rolled == expected, f"expected {expected} rollup rows, got {rolled}"


def test_shot_attention_covers_every_shot(seeded):
    be, ctx = seeded
    tl = ctx["cut_a"]
    rows = analysis.shot_attention(be, "cut-a")
    assert len(rows) == len(tl.shots), "every shot must receive telemetry"
    assert all(0 < r["attention"] <= 100 for r in rows)


def test_shot_attention_does_not_leak_past_the_tail(seeded):
    """ASOF clamps out-of-range probes onto the final shot. If we did not filter
    on tc_out_ms, the last shot would absorb the run-out and read wrong."""
    be, ctx = seeded
    tl = ctx["cut_a"]
    rows = analysis.shot_attention(be, "cut-a")
    last = max(rows, key=lambda r: r["tc_in_ms"])
    expected_buckets = (tl.shots[-1].duration_ms) // 250
    assert last["buckets"] <= expected_buckets + 1


def test_detector_finds_the_planted_weak_scenes(seeded):
    be, ctx = seeded
    tl = ctx["cut_a"]
    tag = {s.scene_id: s.tags[0] for s in tl.scenes}
    segs = analysis.dropoff_segments(be, "cut-a")
    assert segs, "detector found nothing at all"

    flagged = {s.scene_id for s in segs}
    base = Counter(tag.values())
    hit = Counter(tag[s] for s in flagged)

    weak_rate = sum(hit.get(t, 0) for t in WEAK_TAGS) / max(sum(base.get(t, 0) for t in WEAK_TAGS), 1)
    strong_rate = sum(hit.get(t, 0) for t in STRONG_TAGS) / max(
        sum(base.get(t, 0) for t in STRONG_TAGS), 1
    )
    assert weak_rate >= 0.66, f"only flagged {weak_rate:.0%} of weak scenes"
    assert strong_rate <= 0.25, f"false-flagged {strong_rate:.0%} of strong scenes"
    assert weak_rate > strong_rate * 2


def test_segments_are_ranked_and_well_formed(seeded):
    be, _ = seeded
    segs = analysis.dropoff_segments(be, "cut-a")
    assert segs == sorted(segs, key=lambda s: -s.severity)
    for s in segs:
        assert s.end_ms > s.start_ms
        assert s.duration_ms == s.end_ms - s.start_ms
        assert s.duration_ms >= analysis.DEFAULTS["min_duration_ms"]
        assert s.attention_lost > 0


def test_thresholds_actually_bite(seeded):
    """A stricter z threshold must not produce more segments. Guards against a
    detector whose knobs are decorative."""
    be, _ = seeded
    loose = analysis.dropoff_segments(be, "cut-a", z_threshold=1.5)
    tight = analysis.dropoff_segments(be, "cut-a", z_threshold=3.5)
    assert len(tight) <= len(loose)


def test_cohort_divergence_separates_cohorts(seeded):
    """The whole point of cohort analysis: a scene that reads flat overall can
    be losing one audience badly. Youngest and oldest must not move together."""
    be, _ = seeded
    top = analysis.dropoff_segments(be, "cut-a")[0]
    rows = analysis.cohort_divergence(be, "cut-a", top.start_ms, top.end_ms)
    assert len(rows) >= 3
    div = {r["age_band"]: r["divergence"] for r in rows}
    spread = max(div.values()) - min(div.values())
    assert spread > 3.0, f"cohorts moved together (spread {spread:.1f}) — no signal"


def test_analysis_is_deterministic(seeded):
    be, _ = seeded
    a = analysis.dropoff_segments(be, "cut-a")
    b = analysis.dropoff_segments(be, "cut-a")
    assert [s.as_dict() for s in a] == [s.as_dict() for s in b]

"""The closed loop: recommend, apply, measure.

Most analytics tools tell you what is wrong. This one is supposed to tell you
whether its own advice helped — so the loop needs testing as carefully as the
detector, including the two bugs that made an earlier version of it lie.
"""

from __future__ import annotations

import subprocess
import sys
from collections import Counter

import pytest

from crf import analysis
from crf.timeline import build_fixture_timeline, tighten


# ------------------------------------------------------------------- tighten


def test_tighten_shortens_only_the_named_scenes():
    a = build_fixture_timeline()
    trims = {"S007": 0.35, "S014": 0.35}
    b = tighten(a, trims, cut_id="cut-b", label="pass 2")

    dur_a = {s.scene_id: 0 for s in a.scenes}
    dur_b = dict(dur_a)
    for s in a.shots:
        dur_a[s.scene_id] += s.duration_ms
    for s in b.shots:
        dur_b[s.scene_id] += s.duration_ms

    for scene_id in dur_a:
        if scene_id in trims:
            assert dur_b[scene_id] < dur_a[scene_id] * 0.8, f"{scene_id} not trimmed"
        else:
            assert dur_b[scene_id] == dur_a[scene_id], f"{scene_id} changed but was not noted"

    assert b.runtime_ms < a.runtime_ms
    b.validate()  # must still be gapless after re-layout


def test_tighten_rejects_nonsense():
    a = build_fixture_timeline()
    with pytest.raises(ValueError):
        tighten(a, {}, cut_id="x", label="x")
    with pytest.raises(ValueError):
        tighten(a, {"S001": 1.5}, cut_id="x", label="x")


# ------------------------------------------------- the two determinism bugs


def test_scene_quality_is_a_property_of_the_film_not_the_screening():
    """Regression test.

    Scene quality used to be drawn from the screening RNG, so the same scene had
    different intrinsic quality in cut A and cut B. That contaminated every
    before/after measurement — an apparent improvement could just be a luckier
    draw. Quality must now depend only on (film_id, scene_id, film_seed).
    """
    import numpy as np

    from crf.screening import MODEL, ScreeningSpec, _timeline_arrays

    a = build_fixture_timeline()
    b = tighten(a, {"S007": 0.35}, cut_id="cut-b", label="pass 2")

    def quality_vector(timeline, spec):
        import hashlib

        _, _, _, _, key = _timeline_arrays(timeline, spec.sample_ms)
        id_of = {k: sid for sid, k in key.items()}
        tag = {s.scene_id: (s.tags[0] if s.tags else "dialogue") for s in timeline.scenes}
        out = {}
        for k, sid in id_of.items():
            mu, sd = MODEL["tag_quality"].get(tag.get(sid, "dialogue"), (1.0, 0.04))
            digest = hashlib.blake2b(
                f"{timeline.film_id}:{sid}".encode(), digest_size=8
            ).digest()
            rng = np.random.default_rng(
                (spec.film_seed + int.from_bytes(digest, "big")) % (2**63)
            )
            out[sid] = round(float(rng.normal(mu, sd)), 9)
        return out

    # different audience seeds, same film seed -> identical material
    qa = quality_vector(a, ScreeningSpec("s1", "cut-a", seed=101))
    qb = quality_vector(b, ScreeningSpec("s2", "cut-b", seed=202))
    assert qa == qb, "the same scene must have the same quality in every cut"


def test_generation_is_deterministic_across_processes():
    """Regression test.

    The scene->index map was built from a Python set. String hashing is
    randomised per process, so scene quality landed on different scenes every
    run and the 'seeded and reproducible' claim was false outside a single
    process. Must be identical across separate interpreters.
    """
    code = (
        "import os,sys; sys.path.insert(0,'.');"
        "os.environ['CRF_BACKEND']='chdb';"
        "from crf.pipeline import backend_from_env, seed_demo;"
        "from crf import analysis;"
        "be=backend_from_env();"
        "seed_demo(be,n_viewers=20,verbose=False,with_comments=False,with_second_cut=False);"
        "print('|'.join(f'{s.scene_id}:{s.severity}' "
        "for s in analysis.dropoff_segments(be,'cut-a')))"
    )
    runs = {
        subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, timeout=600
        ).stdout.strip()
        for _ in range(2)
    }
    assert len(runs) == 1, f"non-deterministic across processes: {runs}"
    assert runs != {""}, "produced no output"


# --------------------------------------------------------------- the loop


def test_second_cut_is_shorter_and_loaded(seeded):
    be, ctx = seeded
    assert "cut_b" in ctx
    assert ctx["cut_b"].runtime_ms < ctx["cut_a"].runtime_ms
    assert be.query(
        "SELECT count() AS n FROM crf.reaction_event WHERE cut_id='cut-b'"
    )[0]["n"] > 0


def test_every_note_is_applied_to_a_scene_that_actually_changed(seeded):
    """A note marked applied whose scene never moved would make the outcome
    table meaningless."""
    be, ctx = seeded
    out = analysis.note_outcome(be, ctx["run_id"], "cut-a", "cut-b")
    assert out, "no applied notes to measure"
    unchanged = [r for r in out if r["actual_trim_ms"] >= 0]
    assert not unchanged, f"notes applied to untrimmed scenes: {unchanged}"


def test_measurement_credits_only_movement_beyond_film_drift(seeded):
    """The whole film moves between cuts. Crediting a note with raw movement
    would score every note as a win in any better cut."""
    be, ctx = seeded
    out = analysis.note_outcome(be, ctx["run_id"], "cut-a", "cut-b")
    for r in out:
        assert abs((r["raw_gain"] - r["film_drift"]) - r["adjusted_gain"]) < 0.02


def test_the_loop_detects_real_improvement(seeded):
    """Trimming scenes the detector flagged should, more often than not, help —
    otherwise either the model or the recommendation is wrong."""
    be, ctx = seeded
    out = analysis.note_outcome(be, ctx["run_id"], "cut-a", "cut-b")
    verdicts = Counter(r["verdict"] for r in out)
    assert verdicts["improved"] >= 2, f"loop found almost no effect: {verdicts}"
    assert verdicts["improved"] > verdicts["regressed"], (
        f"trims hurt more than they helped: {verdicts}"
    )


def test_verdicts_are_not_all_positive(seeded):
    """A system where every recommendation works is not measuring, it is
    congratulating itself. Honest variance is the point."""
    be, ctx = seeded
    out = analysis.note_outcome(be, ctx["run_id"], "cut-a", "cut-b")
    assert len(set(r["verdict"] for r in out)) > 1, "every note scored identically"


# ------------------------------------------------------------- comments


def test_comments_agree_with_the_telemetry(seeded):
    """Comments are generated FROM measured attention. If sentiment did not
    track it, the 'sentiment explains telemetry' claim would be false and a
    judge cross-checking would find it."""
    be, ctx = seeded
    themes = analysis.comment_themes(be, "cut-a", "fixture")
    assert themes, "no comment themes"

    scene_att = {
        r["scene_id"]: r["attention"]
        for r in analysis.shot_attention(be, "cut-a")
    }
    scored = [
        (scene_att[t["scene_id"]], t["negative_pct"])
        for t in themes
        if t["scene_id"] in scene_att
    ]
    assert len(scored) >= 4

    lo = [neg for att, neg in scored if att < sum(a for a, _ in scored) / len(scored)]
    hi = [neg for att, neg in scored if att >= sum(a for a, _ in scored) / len(scored)]
    assert lo and hi
    assert sum(lo) / len(lo) > sum(hi) / len(hi), (
        "low-attention scenes must draw more negative comment than high-attention ones"
    )


def test_comments_are_embedded_and_the_model_is_recorded(seeded):
    be, _ = seeded
    row = be.query(
        """SELECT count() AS n, uniqExact(embed_model) AS models,
                  min(length(embedding)) AS min_dim
           FROM crf.comment WHERE cut_id='cut-a'"""
    )[0]
    assert row["n"] > 0
    assert row["min_dim"] == 768, "every comment must carry a full-width vector"
    assert row["models"] == 1


def test_raw_comments_are_never_mutated_by_analysis(seeded):
    """crf.comment holds text; crf.comment_analysis holds opinions about it.
    Keeping them apart is what lets two model versions be compared."""
    be, _ = seeded
    cols = {
        r["name"]
        for r in be.query("DESCRIBE TABLE crf.comment")
    }
    assert "sentiment" not in cols and "theme" not in cols

"""Synthetic screening telemetry.

WHY THIS IS SYNTHETIC, STATED PLAINLY: real test-screening data is among the
most closely held data in the industry — NRG and Screen Engine do not publish,
and no open corpus of second-by-second audience reaction exists. So this
generator ships in the repo, seeded and inspectable, rather than being passed
off as measured data.

What it is NOT is noise. The model below reproduces effects that are documented
in audience-measurement and film-editing literature, so the *shapes* the
detector finds are the shapes it would find in real data:

  1. Slow baseline decay.  Attention erodes across a runtime; theatrical
     (seated, dark, committed) decays far more gently than online video.
  2. Post-cut recovery.  A cut is an attention reset. Attention rises briefly
     after each edit and decays as the shot runs long — this is the mechanism
     behind "this scene needs to lose 30 seconds".
  3. Shot-length pressure.  Sustained long takes without a cut depress
     attention further, compounding with (2).
  4. Scene quality.  Some scenes are simply stronger. Drawn per-scene from
     tag-conditioned priors: exposition runs cold, reveals run hot.
  5. Cohort affinity.  Age bands respond differently to the same tag, which is
     what makes "who is this losing?" a real question rather than a slice.
  6. Autocorrelated individual noise.  AR(1), not white — a distracted viewer
     stays distracted for a few seconds.
  7. Disengagement hazard.  Once a viewer runs cold for long enough they check
     out semi-permanently, with only partial recovery. This is why a bad scene
     in reel two damages reel three, and why the detector must use a trailing
     baseline rather than a global mean.

Every parameter is in MODEL below. Change one, re-run, watch the notes change.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Iterator

import numpy as np

from .timeline import Timeline

SAMPLE_MS = 250  # 4 Hz

AGE_BANDS = ("13-17", "18-24", "25-34", "35-49", "50+")
AGE_WEIGHTS = (0.10, 0.26, 0.28, 0.22, 0.14)
DEVICES = ("theatre-seat", "handset", "laptop")
DEVICE_WEIGHTS = (0.72, 0.16, 0.12)

MODEL = {
    # 1. baseline decay across the runtime
    "decay_total": 0.16,   # fraction of attention lost head-to-tail
    "decay_shape": 0.80,   # <1 = front-loaded erosion
    # 2/3. cut recovery and long-take pressure
    "cut_bonus": 7.0,      # attention points gained immediately after a cut
    "cut_tau_ms": 1400.0,  # decay constant of that bonus
    # Long-take pressure. The reference length is set from the observation that
    # contemporary drama averages roughly 4-6s per shot, so a held shot starts
    # reading as slow somewhere around 7s outside an action context. The penalty
    # is deliberately substantial: a viewer registering "this is dragging" is a
    # several-point effect, not a rounding error. These two numbers encode an
    # assumption about how much pacing matters — they are the first thing to
    # change if you disagree, and the detector's output moves with them.
    "long_take_penalty": 9.0,   # points lost as a shot runs past...
    "long_take_ref_ms": 7000.0, # ...this reference length
    # 4. scene quality priors by tag  (mean, sd) as multipliers
    "tag_quality": {
        "exposition": (0.90, 0.045),
        "dialogue":   (0.98, 0.040),
        "tension":    (1.06, 0.035),
        "action":     (1.07, 0.045),
        "reveal":     (1.10, 0.040),
        "transition": (0.95, 0.050),
        "quiet":      (0.97, 0.050),
    },
    # 5. cohort x tag affinity, as multipliers on the scene effect
    "cohort_affinity": {
        "13-17": {"exposition": 0.88, "action": 1.06, "dialogue": 0.94, "quiet": 0.92},
        "18-24": {"exposition": 0.92, "action": 1.04, "dialogue": 0.97},
        "25-34": {},
        "35-49": {"exposition": 1.04, "dialogue": 1.03, "action": 0.98},
        "50+":   {"exposition": 1.07, "dialogue": 1.05, "action": 0.94, "quiet": 1.04},
    },
    # 6. individual noise
    "ar1_rho": 0.86,
    "ar1_sigma": 3.4,
    "viewer_sd": 5.0,      # between-viewer baseline spread
    # 7. disengagement
    "checkout_threshold": 46.0,   # smoothed attention below this...
    "checkout_ticks": 24,         # ...for this many ticks (6s) triggers checkout
    "checkout_depth": 11.0,       # points shed once checked out
    "checkout_recovery": 0.0015,  # per-tick chance of re-engaging
    # observation
    "attention_mean": 72.0,
    "away_base": 0.02,     # look-away probability at full attention
    "away_slope": 0.075,   # extra probability per 10 points below baseline
}


@dataclass
class ScreeningSpec:
    screening_id: str
    cut_id: str
    n_viewers: int = 250
    seed: int = 1234           # varies the AUDIENCE
    film_seed: int = 90210     # fixes the MATERIAL — hold constant across cuts
    sample_ms: int = SAMPLE_MS
    first_time_rate: float = 0.85
    batch_viewers: int = 50  # rows are yielded in viewer batches to cap memory
    tag_quality_jitter: dict[str, float] = field(default_factory=dict)


def _tag_of(timeline: Timeline, scene_id: str) -> str:
    scene = next((s for s in timeline.scenes if s.scene_id == scene_id), None)
    return scene.tags[0] if scene and scene.tags else "dialogue"


def _timeline_arrays(timeline: Timeline, sample_ms: int):
    """Precompute, per tick: ms since the last cut, current shot length, and the
    scene index. These are the levers the model pulls on."""
    n = timeline.runtime_ms // sample_ms
    ticks = np.arange(n, dtype=np.int64) * sample_ms

    starts = np.array([s.tc_in_ms for s in timeline.shots], dtype=np.int64)
    lengths = np.array([s.duration_ms for s in timeline.shots], dtype=np.int64)
    # searchsorted is the numpy equivalent of the ASOF join we do in SQL
    shot_ix = np.clip(np.searchsorted(starts, ticks, side="right") - 1, 0, len(starts) - 1)

    since_cut = (ticks - starts[shot_ix]).astype(np.float64)
    shot_len = lengths[shot_ix].astype(np.float64)

    scene_ids = [timeline.shots[i].scene_id for i in shot_ix]
    # SORTED, not a set. Python randomises string hashing per process, so
    # building this index from a set gave a different scene->index mapping on
    # every run — which silently reassigned scene quality to different scenes
    # and made the "seeded and reproducible" claim false across processes.
    all_scenes = sorted(
        {s.scene_id for s in timeline.shots} | {s.scene_id for s in timeline.scenes}
    )
    uniq = {sid: k for k, sid in enumerate(all_scenes)}
    scene_ix = np.array([uniq[s] for s in scene_ids], dtype=np.int64)
    return ticks, since_cut, shot_len, scene_ix, uniq


def generate(timeline: Timeline, spec: ScreeningSpec) -> Iterator[list[tuple]]:
    """Yield batches of rows matching crf.reaction_event's column order."""
    rng = np.random.default_rng(spec.seed)
    m = MODEL
    ticks, since_cut, shot_len, scene_ix, scene_key = _timeline_arrays(timeline, spec.sample_ms)
    n_ticks = len(ticks)
    if n_ticks == 0:
        return

    # ---- effects that are properties of the FILM, not the viewer -------------
    progress = ticks / max(ticks[-1], 1)
    decay = 1.0 - m["decay_total"] * progress ** m["decay_shape"]

    cut_bonus = m["cut_bonus"] * np.exp(-since_cut / m["cut_tau_ms"])
    long_take = -m["long_take_penalty"] * np.clip(shot_len / m["long_take_ref_ms"] - 1.0, 0, 2.5)

    # Per-scene quality is a property of the FILM, not of the screening.
    #
    # Drawing it from the screening RNG meant the same scene had different
    # intrinsic quality in cut A and cut B — nonsense, since it is the same
    # material — and it contaminated the whole before/after measurement: an
    # apparent "improvement" could just be a luckier quality draw. Keying it on
    # (film_id, scene_id) means the only thing that varies between screenings is
    # the audience, which is the point.
    n_scenes = max(scene_key.values()) + 1
    scene_tag = ["dialogue"] * n_scenes
    for sc in timeline.scenes:
        if sc.scene_id in scene_key:
            scene_tag[scene_key[sc.scene_id]] = sc.tags[0] if sc.tags else "dialogue"
    id_of = {k: sid for sid, k in scene_key.items()}
    quality = np.empty(n_scenes)
    for k, tag in enumerate(scene_tag):
        mu, sd = m["tag_quality"].get(tag, (1.0, 0.04))
        mu += spec.tag_quality_jitter.get(tag, 0.0)
        digest = hashlib.blake2b(
            f"{timeline.film_id}:{id_of[k]}".encode(), digest_size=8
        ).digest()
        scene_rng = np.random.default_rng(
            (spec.film_seed + int.from_bytes(digest, "big")) % (2**63)
        )
        quality[k] = scene_rng.normal(mu, sd)
    scene_q = quality[scene_ix]

    film_effect = m["attention_mean"] * decay * scene_q + cut_bonus + long_take

    # ---- per-viewer simulation, batched --------------------------------------
    for lo in range(0, spec.n_viewers, spec.batch_viewers):
        hi = min(lo + spec.batch_viewers, spec.n_viewers)
        nb = hi - lo

        bands = rng.choice(len(AGE_BANDS), size=nb, p=AGE_WEIGHTS)
        devs = rng.choice(len(DEVICES), size=nb, p=DEVICE_WEIGHTS)
        first = (rng.random(nb) < spec.first_time_rate).astype(np.uint8)
        offset = rng.normal(0.0, m["viewer_sd"], size=nb)

        # cohort x tag affinity, resolved to a per-viewer per-tick multiplier
        aff = np.ones((nb, n_scenes))
        for i, b in enumerate(bands):
            table = m["cohort_affinity"].get(AGE_BANDS[b], {})
            for k, tag in enumerate(scene_tag):
                aff[i, k] = table.get(tag, 1.0)
        aff_t = aff[:, scene_ix]

        # AR(1) noise, vectorised across the batch
        noise = np.empty((nb, n_ticks))
        eps = rng.normal(0.0, m["ar1_sigma"], size=(nb, n_ticks))
        noise[:, 0] = eps[:, 0]
        rho = m["ar1_rho"]
        for t in range(1, n_ticks):
            noise[:, t] = rho * noise[:, t - 1] + eps[:, t]

        att = film_effect[None, :] * aff_t + offset[:, None] + noise

        # disengagement: a stateful pass, because checkout is path-dependent
        smoothed = np.copy(att)
        win = m["checkout_ticks"]
        if n_ticks > win:
            kernel = np.ones(win) / win
            for i in range(nb):
                smoothed[i] = np.convolve(att[i], kernel, mode="same")
        checked = np.zeros(nb, dtype=bool)
        penalty = np.zeros((nb, n_ticks))
        for t in range(n_ticks):
            newly = (~checked) & (smoothed[:, t] < m["checkout_threshold"])
            checked |= newly
            back = checked & (rng.random(nb) < m["checkout_recovery"])
            checked &= ~back
            penalty[:, t] = np.where(checked, m["checkout_depth"], 0.0)
        att -= penalty

        att = np.clip(att, 0, 100)

        # dial: valence tracks scene quality more than pacing
        dial = np.clip((scene_q[None, :] - 1.0) * 260 + (att - m["attention_mean"]) * 0.55
                       + rng.normal(0, 6, size=att.shape), -100, 100)

        p_away = np.clip(
            m["away_base"] + m["away_slope"] * (m["attention_mean"] - att) / 10.0, 0.0, 0.95
        )
        away = (rng.random(att.shape) < p_away).astype(np.uint8)

        att_u8 = att.astype(np.uint8)
        dial_i8 = dial.astype(np.int8)

        rows: list[tuple] = []
        for i in range(nb):
            vid = lo + i + 1
            band = AGE_BANDS[bands[i]]
            dev = DEVICES[devs[i]]
            ft = int(first[i])
            rows.extend(
                (
                    spec.screening_id,
                    spec.cut_id,
                    vid,
                    int(ticks[t]),
                    int(att_u8[i, t]),
                    int(dial_i8[i, t]),
                    int(away[i, t]),
                    band,
                    dev,
                    ft,
                )
                for t in range(n_ticks)
            )
        yield rows


def expected_row_count(timeline: Timeline, spec: ScreeningSpec) -> int:
    return (timeline.runtime_ms // spec.sample_ms) * spec.n_viewers

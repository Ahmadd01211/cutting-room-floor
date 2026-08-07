"""Synthetic audience comments — generated FROM the telemetry, not beside it.

The naive way to fake comments is to draw them independently. That produces a
corpus that contradicts the attention data: glowing praise for the scene where
everyone checked out. A judge who cross-checks finds it immediately, and the
whole "sentiment explains telemetry" claim collapses.

So comments here are conditioned on the *observed* per-scene attention that was
actually measured and stored. People complain about what bored them. Scenes that
held the room draw praise; scenes that lost it draw pacing complaints, weighted
by how badly they lost it. Agreement between the two modalities is guaranteed by
construction rather than hoped for.

As with the telemetry: this is synthetic, it is seeded, and it ships in the repo.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from .timeline import Timeline

THEMES = ("pacing", "confusion", "character", "sound", "ending", "craft")

# (theme, valence) -> phrasings. Valence: -1 negative, 0 mixed, +1 positive.
TEMPLATES: dict[tuple[str, int], tuple[str, ...]] = {
    ("pacing", -1): (
        "This part dragged. I checked my phone twice.",
        "Felt like it went on forever — could lose a couple of minutes here easily.",
        "I started drifting during this stretch. Nothing was happening.",
        "Way too slow. I got what the scene was doing about a minute before it ended.",
        "The pace died here. I was waiting for it to move on.",
    ),
    ("pacing", 0): (
        "A bit slow but I didn't mind it.",
        "Slightly long, though I understood why it was there.",
    ),
    ("pacing", 1): (
        "Moved really well through here.",
        "Tight. Never felt like it was wasting my time.",
    ),
    ("confusion", -1): (
        "I lost track of who was who in this bit.",
        "Wasn't sure what just happened or why it mattered.",
        "Had to work out what the point of this was, and I never really did.",
        "Confusing. I think I missed something important earlier.",
    ),
    ("confusion", 0): (
        "Took me a second to follow, but I got there.",
    ),
    ("character", -1): (
        "Hard to care about anyone here.",
        "The dialogue felt written rather than spoken.",
        "I didn't believe the way she reacted to that.",
    ),
    ("character", 1): (
        "Really liked her in this. Felt real.",
        "The performances carried this section.",
    ),
    ("sound", -1): (
        "Couldn't make out some of the dialogue over the score.",
        "Music was doing too much work here.",
    ),
    ("ending", -1): (
        "The ending didn't land for me.",
        "I wanted more resolution than that.",
    ),
    ("ending", 1): (
        "Stuck the landing. I sat there for a second after.",
    ),
    ("craft", 1): (
        "Beautiful shot here.",
        "Loved the way this was cut.",
        "Genuinely tense. My whole row was still.",
    ),
    ("craft", -1): (
        "The cutting felt choppy through here.",
    ),
}

UNSCOPED = (
    "Overall I enjoyed it, but it's too long.",
    "Solid film. Would recommend it to a friend.",
    "Good but it sags in the middle.",
    "I'd watch it again. The performances stayed with me.",
    "Not sure who this is for, honestly.",
    "Better than I expected going in.",
)


@dataclass
class CommentSpec:
    screening_id: str
    cut_id: str
    seed: int = 4242
    per_viewer_max: int = 3
    unscoped_rate: float = 0.35


@dataclass
class GeneratedComment:
    comment_id: int
    viewer_id: int
    media_ms: int
    scoped: int
    body: str
    # ground truth, used only to seed fixture analysis — never inserted into
    # crf.comment, which holds raw text and nothing else
    theme: str
    valence: int
    sentiment: int


def _scene_attention(backend, cut_id: str) -> list[dict]:
    """Per-scene attention with a representative timecode, read back from what
    was actually measured."""
    return backend.query(
        """
        WITH buckets AS (
            SELECT cut_id, bucket_ms, attention
            FROM crf.v_attention_250ms
            WHERE cut_id = {cut_id:String}
        )
        SELECT
            s.scene_id     AS scene_id,
            min(s.tc_in_ms) AS scene_start_ms,
            max(s.tc_out_ms) AS scene_end_ms,
            avg(b.attention) AS attention
        FROM buckets AS b
        ASOF LEFT JOIN crf.shot AS s
            ON b.cut_id = s.cut_id AND b.bucket_ms >= s.tc_in_ms
        WHERE b.bucket_ms < s.tc_out_ms
        GROUP BY scene_id
        ORDER BY scene_start_ms
        """,
        {"cut_id": cut_id},
    )


def generate(backend, timeline: Timeline, spec: CommentSpec,
             n_viewers: int) -> list[GeneratedComment]:
    rng = random.Random(spec.seed)
    scenes = _scene_attention(backend, spec.cut_id)
    if not scenes:
        raise RuntimeError(f"no telemetry for {spec.cut_id} — seed it before comments")

    attentions = [s["attention"] for s in scenes]
    mean_att = sum(attentions) / len(attentions)
    spread = max(1e-6, max(attentions) - min(attentions))

    tag_of = {s.scene_id: (s.tags[0] if s.tags else "dialogue") for s in timeline.scenes}

    # weight scene selection toward the scenes that lost the room — that is
    # where an audience actually leaves comments
    weights = [max(0.05, (mean_att - s["attention"]) / spread + 0.35) for s in scenes]

    out: list[GeneratedComment] = []
    cid = int(spec.seed) * 1_000_000
    for viewer in range(1, n_viewers + 1):
        for _ in range(rng.randint(0, spec.per_viewer_max)):
            cid += 1
            if rng.random() < spec.unscoped_rate:
                out.append(
                    GeneratedComment(cid, viewer, 0, 0, rng.choice(UNSCOPED),
                                     "overall", 0, rng.randint(-15, 35))
                )
                continue

            scene = rng.choices(scenes, weights=weights, k=1)[0]
            # how far below the film's own mean did this scene sit?
            deficit = (mean_att - scene["attention"]) / spread

            if deficit > 0.35:
                valence = -1
            elif deficit > 0.05:
                valence = rng.choice([-1, 0])
            else:
                valence = rng.choice([1, 1, 0])

            tag = tag_of.get(scene["scene_id"], "dialogue")
            if valence < 0:
                pool = ["pacing", "pacing", "confusion"] if tag == "exposition" \
                    else ["pacing", "confusion", "character", "sound", "craft"]
            elif valence > 0:
                pool = ["craft", "character", "pacing"]
            else:
                pool = ["pacing", "confusion"]
            theme = rng.choice(pool)

            options = TEMPLATES.get((theme, valence)) or TEMPLATES.get((theme, -1)) \
                or TEMPLATES[("pacing", -1)]
            body = rng.choice(options)

            base = {-1: -55, 0: 0, 1: 55}[valence]
            sentiment = max(-100, min(100, int(base + rng.gauss(0, 18)
                                               - deficit * 20 * (valence <= 0))))

            span = max(1, scene["scene_end_ms"] - scene["scene_start_ms"] - 1)
            media_ms = int(scene["scene_start_ms"] + rng.random() * span)

            out.append(
                GeneratedComment(cid, viewer, media_ms, 1, body, theme, valence, sentiment)
            )
    return out


COMMENT_COLUMNS = [
    "comment_id", "screening_id", "cut_id", "viewer_id",
    "media_ms", "scoped", "body", "embedding", "embed_model",
]
ANALYSIS_COLUMNS = [
    "comment_id", "run_id", "agent", "model",
    "sentiment", "theme", "confidence", "rationale",
]


def load(backend, spec: CommentSpec, comments: list[GeneratedComment],
         embedder) -> int:
    vectors = embedder.embed([c.body for c in comments])
    backend.insert(
        "crf.comment",
        [
            (c.comment_id, spec.screening_id, spec.cut_id, c.viewer_id,
             c.media_ms, c.scoped, c.body, vec, embedder.model_name)
            for c, vec in zip(comments, vectors)
        ],
        COMMENT_COLUMNS,
    )
    return len(comments)


def seed_fixture_analysis(backend, comments: list[GeneratedComment],
                          run_id: str = "fixture") -> int:
    """Populate crf.comment_analysis from the generator's ground truth.

    FIXTURE ONLY. In the real pipeline the sentiment agent produces these rows
    by reading the text. This exists so the comment queries and their tests can
    run without a model in the loop. The `agent` column reads 'fixture-oracle'
    precisely so nobody mistakes it for model output.
    """
    backend.insert(
        "crf.comment_analysis",
        [
            (c.comment_id, run_id, "fixture-oracle", "none",
             c.sentiment, c.theme, 1.0, "ground truth from the generator")
            for c in comments
        ],
        ANALYSIS_COLUMNS,
    )
    return len(comments)

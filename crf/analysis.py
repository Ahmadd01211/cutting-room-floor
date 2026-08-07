"""The deterministic analysis layer.

Every function here is a pure query against ClickHouse with a typed signature.
These become the agent's tools verbatim — which is the point: the agent chooses
*which* question to ask and how to phrase the resulting note, but it never
decides what counts as a drop-off, never computes a statistic, and never sees a
raw row. Ask the same question twice and you get the same numbers.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any

from .db import Backend, load_query


@dataclass(frozen=True)
class DropoffSegment:
    start_ms: int
    end_ms: int
    duration_ms: int
    attention_lost: float
    z_peak: float
    away_pct: float
    shot_idx: int
    scene_id: str
    slug: str
    severity: float

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# Defaults are deliberately conservative: a drop must be ~2 sigma below its own
# trailing baseline and last at least 3 seconds. Loosening these produces more
# notes, not better ones — an editor who gets 60 notes reads none of them.
DEFAULTS = {
    "baseline_buckets": 40,   # 10s trailing window at 4 Hz
    "z_threshold": 2.0,
    "min_duration_ms": 3000,
}


def shot_attention(backend: Backend, cut_id: str) -> list[dict]:
    """Per-shot attention for a cut, via the ASOF join."""
    return backend.query(load_query("shot_attention.sql"), {"cut_id": cut_id})


def dropoff_segments(
    backend: Backend,
    cut_id: str,
    baseline_buckets: int | None = None,
    z_threshold: float | None = None,
    min_duration_ms: int | None = None,
    limit: int | None = None,
) -> list[DropoffSegment]:
    """Ranked attention drop-offs. This is the spine of the whole product."""
    params = {
        "cut_id": cut_id,
        "baseline_buckets": baseline_buckets or DEFAULTS["baseline_buckets"],
        "z_threshold": z_threshold or DEFAULTS["z_threshold"],
        "min_duration_ms": min_duration_ms or DEFAULTS["min_duration_ms"],
    }
    rows = backend.query(load_query("dropoff_segments.sql"), params)
    segs = [
        DropoffSegment(
            start_ms=int(r["start_ms"]),
            end_ms=int(r["end_ms"]),
            duration_ms=int(r["duration_ms"]),
            attention_lost=float(r["attention_lost"]),
            z_peak=float(r["z_peak"]),
            away_pct=float(r["away_pct"]),
            shot_idx=int(r["shot_idx"]),
            scene_id=str(r["scene_id"]),
            slug=str(r["slug"]),
            severity=float(r["severity"]),
        )
        for r in rows
    ]
    return segs[:limit] if limit else segs


def cohort_divergence(backend: Backend, cut_id: str, start_ms: int, end_ms: int) -> list[dict]:
    """Which cohort is this window losing, relative to that cohort's own norm."""
    return backend.query(
        load_query("cohort_divergence.sql"),
        {"cut_id": cut_id, "start_ms": start_ms, "end_ms": end_ms},
    )


def scene_context(backend: Backend, film_id: str, scene_id: str) -> dict | None:
    rows = backend.query(
        """SELECT scene_id, scene_num, heading, synopsis, dialogue, tags
           FROM crf.scene
           WHERE film_id = {film_id:String} AND scene_id = {scene_id:String}
           LIMIT 1""",
        {"film_id": film_id, "scene_id": scene_id},
    )
    return rows[0] if rows else None


def comparable_scenes(
    backend: Backend,
    embedding: list[float],
    exclude_film_id: str,
    limit: int = 5,
) -> list[dict]:
    """Semantically similar scenes from OTHER films.

    Deliberately excludes the film under review: "here is a scene like yours
    from your own cut" is not advice. The useful comparison is how a similar
    beat was handled somewhere it worked.
    """
    return backend.query(
        """SELECT film_id, scene_id, heading, synopsis, tags,
                  round(cosineDistance(embedding, {probe:Array(Float32)}), 4) AS distance
           FROM crf.scene
           WHERE film_id != {exclude:String} AND length(embedding) > 0
           ORDER BY distance ASC
           LIMIT {limit:UInt32}""",
        {"probe": embedding, "exclude": exclude_film_id, "limit": limit},
    )


def cut_comparison(backend: Backend, cut_a: str, cut_b: str) -> list[dict]:
    """Scene-by-scene comparison of two cuts.

    Scenes, not timecodes — shot boundaries move between cuts, which is the
    whole point of re-cutting.
    """
    return backend.query(
        load_query("cut_comparison.sql"), {"cut_a": cut_a, "cut_b": cut_b}
    )


def note_outcome(
    backend: Backend,
    run_id: str,
    cut_a: str,
    cut_b: str,
    min_effect: float = 1.0,
) -> list[dict]:
    """Did the notes work?

    Compares each applied note's scene across the two cuts, credits the note
    only with movement in excess of the film-wide drift, and returns a verdict
    computed in SQL. A recommendation engine that grades its own homework in
    prose is worth nothing.
    """
    return backend.query(
        load_query("note_outcome.sql"),
        {"run_id": run_id, "cut_a": cut_a, "cut_b": cut_b, "min_effect": min_effect},
    )


def comment_themes(backend: Backend, cut_id: str, run_id: str) -> list[dict]:
    """What the audience complained about, attributed to scenes.

    Telemetry says attention dropped; comments say why.
    """
    return backend.query(
        load_query("comment_themes.sql"), {"cut_id": cut_id, "run_id": run_id}
    )


def similar_comments(
    backend: Backend,
    embedding: list[float],
    cut_id: str,
    limit: int = 8,
) -> list[dict]:
    """Nearest comments by embedding — the clustering primitive the sentiment
    agent works from. This is the query the HNSW index exists for: thousands of
    comments, not a hundred scene summaries."""
    return backend.query(
        """SELECT comment_id, body, media_ms,
                  round(cosineDistance(embedding, {probe:Array(Float32)}), 4) AS distance
           FROM crf.comment
           WHERE cut_id = {cut_id:String} AND length(embedding) > 0
           ORDER BY distance ASC
           LIMIT {limit:UInt32}""",
        {"probe": embedding, "cut_id": cut_id, "limit": limit},
    )


def cut_summary(backend: Backend, cut_id: str) -> dict:
    """Headline numbers for a cut — what the agent opens its report with."""
    rows = backend.query(
        """SELECT
               count()                                   AS buckets,
               round(avg(attention), 2)                  AS attention_mean,
               round(min(attention), 2)                  AS attention_min,
               round(avg(away_rate) * 100, 2)            AS away_pct,
               max(viewers)                              AS viewers,
               max(bucket_ms) + 250                      AS runtime_ms
           FROM crf.v_attention_250ms
           WHERE cut_id = {cut_id:String}""",
        {"cut_id": cut_id},
    )
    return rows[0] if rows else {}

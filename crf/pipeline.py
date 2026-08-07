"""Load orchestration: timeline in, telemetry in, analysis out."""

from __future__ import annotations

import hashlib
import os
import time

from . import analysis, comments, embed
from .db import Backend, ChdbBackend, ClickHouseBackend, migrate
from .screening import ScreeningSpec, expected_row_count, generate
from .timeline import Timeline, build_fixture_timeline, tighten

EVENT_COLUMNS = [
    "screening_id", "cut_id", "viewer_id", "media_ms", "attention",
    "dial", "looked_away", "age_band", "device", "first_time",
]


def backend_from_env() -> Backend:
    """CRF_BACKEND=chdb runs embedded with no server; anything else connects to
    ClickHouse using the CLICKHOUSE_* variables."""
    if os.environ.get("CRF_BACKEND", "clickhouse").lower() == "chdb":
        return ChdbBackend(os.environ.get("CRF_CHDB_PATH") or None)
    return ClickHouseBackend(
        host=os.environ.get("CLICKHOUSE_HOST", "localhost"),
        port=int(os.environ.get("CLICKHOUSE_PORT", "8123")),
        user=os.environ.get("CLICKHOUSE_USER", "default"),
        password=os.environ.get("CLICKHOUSE_PASSWORD", ""),
        secure=os.environ.get("CLICKHOUSE_SECURE", "false").lower() == "true",
    )


def load_timeline(backend: Backend, timeline: Timeline, *, title: str,
                  year: int = 2026, fps: float = 24.0, rights_note: str = "") -> None:
    timeline.validate()

    backend.execute(
        "ALTER TABLE crf.shot DELETE WHERE cut_id = {cut_id:String}",
        {"cut_id": timeline.cut_id},
    )
    backend.insert(
        "crf.film",
        [(timeline.film_id, title, year, fps, timeline.runtime_ms, rights_note)],
        ["film_id", "title", "year", "fps", "runtime_ms", "rights_note"],
    )
    backend.insert(
        "crf.cut",
        [(timeline.cut_id, timeline.film_id, timeline.label, timeline.runtime_ms)],
        ["cut_id", "film_id", "label", "runtime_ms"],
    )
    backend.insert(
        "crf.shot",
        [(s.cut_id, s.shot_idx, s.tc_in_ms, s.tc_out_ms, s.scene_id, s.slug)
         for s in timeline.shots],
        ["cut_id", "shot_idx", "tc_in_ms", "tc_out_ms", "scene_id", "slug"],
    )
    backend.insert(
        "crf.scene",
        [(sc.film_id, sc.scene_id, sc.scene_num, sc.heading, sc.synopsis,
          sc.dialogue, list(sc.tags), list(sc.embedding))
         for sc in timeline.scenes],
        ["film_id", "scene_id", "scene_num", "heading", "synopsis",
         "dialogue", "tags", "embedding"],
    )


def run_screening(backend: Backend, timeline: Timeline, spec: ScreeningSpec,
                  verbose: bool = True) -> int:
    """Generate and insert a screening. Rows stream in viewer batches so peak
    memory stays flat regardless of audience size."""
    total = expected_row_count(timeline, spec)
    if verbose:
        print(f"  {spec.screening_id}: {spec.n_viewers} viewers x "
              f"{timeline.runtime_ms // spec.sample_ms} ticks = {total:,} rows")
    t0, written = time.time(), 0
    for batch in generate(timeline, spec):
        backend.insert("crf.reaction_event", batch, EVENT_COLUMNS)
        written += len(batch)
        if verbose and written % 500_000 < len(batch):
            print(f"    {written:,} / {total:,}")
    if verbose:
        dt = time.time() - t0
        print(f"    done: {written:,} rows in {dt:.1f}s ({written/max(dt,.01):,.0f} rows/s)")
    return written


NOTE_COLUMNS = [
    "note_id", "run_id", "cut_id", "scene_id", "shot_idx", "start_ms", "end_ms",
    "severity", "attention_lost", "worst_cohort", "cohort_divergence",
    "headline", "body", "suggested_trim_ms", "status", "applied_in_cut",
]


def _note_id(run_id: str, scene_id: str, start_ms: int) -> int:
    return int(hashlib.blake2b(
        f"{run_id}:{scene_id}:{start_ms}".encode(), digest_size=7
    ).hexdigest(), 16)


def write_notes(backend: Backend, run_id: str, cut_id: str, segments,
                trim_fraction: float = 0.35) -> list[dict]:
    """Persist detected drop-offs as proposed notes.

    In the real pipeline the recommender agent writes `headline`, `body` and
    `suggested_trim_ms`. Here they are derived arithmetically so the loop can be
    exercised end to end without a model — the agent replaces this function, it
    does not wrap it.
    """
    rows, records = [], []
    for seg in segments:
        cohort = analysis.cohort_divergence(backend, cut_id, seg.start_ms, seg.end_ms)
        worst = min(cohort, key=lambda r: r["divergence"]) if cohort else None
        trim = int(seg.duration_ms * trim_fraction)
        nid = _note_id(run_id, seg.scene_id, seg.start_ms)
        rec = {
            "note_id": nid,
            "scene_id": seg.scene_id,
            "start_ms": seg.start_ms,
            "end_ms": seg.end_ms,
            "severity": seg.severity,
            "suggested_trim_ms": trim,
            "worst_cohort": worst["age_band"] if worst else "",
            "cohort_divergence": float(worst["divergence"]) if worst else 0.0,
        }
        records.append(rec)
        rows.append((
            nid, run_id, cut_id, seg.scene_id, seg.shot_idx, seg.start_ms, seg.end_ms,
            float(seg.severity), float(seg.attention_lost),
            rec["worst_cohort"], rec["cohort_divergence"],
            f"{seg.scene_id} loses the room for {seg.duration_ms/1000:.1f}s",
            f"Attention falls {seg.attention_lost:.1f} points below its trailing "
            f"baseline, worst among {rec['worst_cohort'] or 'all cohorts'}.",
            trim, "proposed", "",
        ))
    backend.insert("crf.note", rows, NOTE_COLUMNS)
    return records


def mark_applied(backend: Backend, run_id: str, note_ids: list[int],
                 applied_in_cut: str) -> None:
    """ReplacingMergeTree keyed on (run_id, note_id) — re-inserting the row with
    a later timestamp is the update."""
    if not note_ids:
        return
    existing = backend.query(
        """SELECT * FROM crf.note
           WHERE run_id = {run_id:String} AND note_id IN {ids:Array(UInt64)}""",
        {"run_id": run_id, "ids": note_ids},
    )
    rows = [
        tuple(
            applied_in_cut if col == "applied_in_cut"
            else "applied" if col == "status"
            else r[col]
            for col in NOTE_COLUMNS
        )
        for r in existing
    ]
    backend.insert("crf.note", rows, NOTE_COLUMNS)
    backend.execute("OPTIMIZE TABLE crf.note FINAL")


def seed_demo(backend: Backend, n_viewers: int = 120, verbose: bool = True,
              run_id: str = "demo-run", with_comments: bool = True,
              with_second_cut: bool = True) -> dict:
    """The full closed loop, end to end.

    1. Screen the assembly (cut A).
    2. Detect drop-offs and write them as notes.
    3. Tighten exactly the scenes that were noted -> cut B.
    4. Screen cut B.
    5. Generate audience comments for both, conditioned on measured attention.

    Step 3 is what makes step 5 of the real product possible: the system can now
    be asked whether its own recommendation worked.
    """
    migrate(backend, verbose=verbose)

    cut_a = build_fixture_timeline(cut_id="cut-a", label="Fixture - assembly")
    load_timeline(backend, cut_a, title="Fixture Film",
                  rights_note="synthetic, original to this repo")
    run_screening(
        backend, cut_a,
        ScreeningSpec(screening_id="scr-a1", cut_id="cut-a",
                      n_viewers=n_viewers, seed=101),
        verbose=verbose,
    )

    segments = analysis.dropoff_segments(backend, "cut-a")
    notes = write_notes(backend, run_id, "cut-a", segments)
    if verbose:
        print(f"  {len(notes)} notes proposed on cut-a")

    out: dict = {"cut_a": cut_a, "notes": notes, "run_id": run_id}

    if with_second_cut and notes:
        # trim exactly what was noted, proportional to how bad it was
        trims: dict[str, float] = {}
        for n in notes:
            trims[n["scene_id"]] = max(trims.get(n["scene_id"], 0.0), 0.35)
        cut_b = tighten(cut_a, trims, cut_id="cut-b", label="Fixture - pass 2")
        load_timeline(backend, cut_b, title="Fixture Film",
                      rights_note="synthetic, original to this repo")
        run_screening(
            backend, cut_b,
            ScreeningSpec(screening_id="scr-b1", cut_id="cut-b",
                          n_viewers=n_viewers, seed=202),
            verbose=verbose,
        )
        mark_applied(backend, run_id, [n["note_id"] for n in notes], "cut-b")
        out["cut_b"] = cut_b
        if verbose:
            saved = cut_a.runtime_ms - cut_b.runtime_ms
            print(f"  cut-b: {len(trims)} scenes tightened, "
                  f"runtime {saved/1000:.0f}s shorter")

    if with_comments:
        embedder = embed.for_environment()
        total = 0
        for tl, screening in [(cut_a, "scr-a1")] + (
            [(out["cut_b"], "scr-b1")] if "cut_b" in out else []
        ):
            spec = comments.CommentSpec(
                screening_id=screening, cut_id=tl.cut_id,
                seed=4242 + (0 if tl.cut_id == "cut-a" else 1),
            )
            generated = comments.generate(backend, tl, spec, n_viewers)
            comments.load(backend, spec, generated, embedder)
            comments.seed_fixture_analysis(backend, generated, run_id="fixture")
            total += len(generated)
        out["comments"] = total
        if verbose:
            print(f"  {total} comments generated (embedder: {embedder.model_name})")

    return out

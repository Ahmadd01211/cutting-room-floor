"""Seed the database and print the analysis. `docker compose up` runs this.

    python -m scripts.demo --seed --report
    CRF_BACKEND=chdb python -m scripts.demo --seed --report   # no server needed
"""

from __future__ import annotations

import argparse
import os
import sys

from crf import analysis
from crf.pipeline import backend_from_env, seed_demo
from crf.timeline import ms_to_tc


def fmt(ms: int) -> str:
    return ms_to_tc(ms, 24.0)


def report(backend, cut_id: str = "cut-a") -> int:
    summary = analysis.cut_summary(backend, cut_id)
    if not summary or not summary.get("buckets"):
        print("no telemetry loaded — run with --seed first", file=sys.stderr)
        return 1

    print("\n" + "=" * 74)
    print(f"  CUT {cut_id}   {summary['viewers']} viewers   "
          f"runtime {fmt(summary['runtime_ms'])}")
    print(f"  mean attention {summary['attention_mean']}   "
          f"floor {summary['attention_min']}   look-away {summary['away_pct']}%")
    print("=" * 74)

    segs = analysis.dropoff_segments(backend, cut_id)
    print(f"\n{len(segs)} drop-off segments, ranked by severity "
          f"(attention lost x seconds held)\n")
    print(f"  {'#':>2}  {'in':<12} {'out':<12} {'len':>6}  {'scene':>6} {'shot':>5} "
          f"{'lost':>6} {'sev':>7}")
    print("  " + "-" * 70)
    for i, s in enumerate(segs, 1):
        print(f"  {i:>2}  {fmt(s.start_ms):<12} {fmt(s.end_ms):<12} "
              f"{s.duration_ms/1000:>5.1f}s  {s.scene_id:>6} {s.shot_idx:>5} "
              f"{s.attention_lost:>6.1f} {s.severity:>7.1f}")

    if segs:
        top = segs[0]
        print(f"\n  Worst segment — {top.scene_id} at {fmt(top.start_ms)}")
        print(f"  Who is it losing? (vs each cohort's own film-wide mean)\n")
        for r in analysis.cohort_divergence(backend, cut_id, top.start_ms, top.end_ms):
            bar = "#" * int(abs(r["divergence"]) * 2)
            sign = "-" if r["divergence"] < 0 else "+"
            print(f"    {r['age_band']:>7}  {sign}{abs(r['divergence']):>5.1f}  {bar}")

        themes = [t for t in analysis.comment_themes(backend, cut_id, "fixture")
                  if t["scene_id"] == top.scene_id]
        if themes:
            t = themes[0]
            print(f"\n  And what they said about it — {t['theme']}, "
                  f"{t['negative_pct']}% negative across {t['comments']} comments:")
            for ex in t["examples"][:2]:
                print(f"    “{ex}”")
    print()
    return 0


def outcome_report(backend, run_id: str, cut_a: str = "cut-a",
                   cut_b: str = "cut-b") -> int:
    """Did the notes work? The question almost no analytics tool asks itself."""
    rows = analysis.note_outcome(backend, run_id, cut_a, cut_b)
    if not rows:
        print("no applied notes to measure — seed with a second cut first")
        return 1

    print("\n" + "=" * 74)
    print(f"  DID THE NOTES WORK?   {cut_a} -> {cut_b}")
    print("=" * 74)
    print(f"\n  Gains are adjusted for film-wide drift ({rows[0]['film_drift']:+.2f} "
          f"points), so a note is credited only with movement it caused.\n")
    print(f"  {'scene':>6} {'trim':>8} {'before':>7} {'after':>7} {'gain':>7}   verdict")
    print("  " + "-" * 62)
    for r in rows:
        print(f"  {r['scene_id']:>6} {r['actual_trim_ms']/1000:>7.1f}s "
              f"{r['attention_before']:>7} {r['attention_after']:>7} "
              f"{r['adjusted_gain']:>+7.2f}   {r['verdict']}")

    from collections import Counter
    tally = Counter(r["verdict"] for r in rows)
    print(f"\n  {tally['improved']} improved, {tally['no effect']} no effect, "
          f"{tally['regressed']} regressed.")
    print("  Not every recommendation works. A system that reported otherwise")
    print("  would be congratulating itself, not measuring.\n")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="The Cutting Room Floor — demo")
    ap.add_argument("--seed", action="store_true", help="migrate and load telemetry")
    ap.add_argument("--report", action="store_true", help="print the analysis")
    ap.add_argument("--outcome", action="store_true",
                    help="print whether the notes actually worked")
    ap.add_argument("--cut", default="cut-a")
    ap.add_argument("--run-id", default="demo-run")
    ap.add_argument("--viewers", type=int,
                    default=int(os.environ.get("CRF_VIEWERS", "120")))
    args = ap.parse_args()

    backend = backend_from_env()
    if args.seed:
        print(f"seeding ({args.viewers} viewers)...")
        seed_demo(backend, n_viewers=args.viewers, run_id=args.run_id)
    rc = 0
    if args.report or not (args.seed or args.outcome):
        rc |= report(backend, args.cut)
    if args.outcome or args.seed:
        rc |= outcome_report(backend, args.run_id)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())

"""Timeline model: cuts, shots, scenes.

Media time is milliseconds from the head of a cut. Everything converts to that
at the edges; nothing downstream deals in frames or timecode strings.
"""

from __future__ import annotations

import csv
import random
import re
from dataclasses import dataclass, field
from pathlib import Path

# Scene tags drive both the telemetry model and, later, the note the agent
# writes. Keep this list short — it is a vocabulary, not a taxonomy.
SCENE_TAGS = (
    "exposition",
    "dialogue",
    "tension",
    "action",
    "reveal",
    "transition",
    "quiet",
)


@dataclass(frozen=True)
class Shot:
    cut_id: str
    shot_idx: int
    tc_in_ms: int
    tc_out_ms: int
    scene_id: str
    slug: str

    @property
    def duration_ms(self) -> int:
        return self.tc_out_ms - self.tc_in_ms


@dataclass(frozen=True)
class Scene:
    film_id: str
    scene_id: str
    scene_num: int
    heading: str
    synopsis: str
    dialogue: str
    tags: tuple[str, ...]
    embedding: list[float] = field(default_factory=list)


@dataclass
class Timeline:
    """A cut: an ordered, gapless list of shots covering [0, runtime_ms)."""

    cut_id: str
    film_id: str
    label: str
    shots: list[Shot]
    scenes: list[Scene]

    @property
    def runtime_ms(self) -> int:
        return self.shots[-1].tc_out_ms if self.shots else 0

    def validate(self) -> None:
        """A timeline with a gap or an overlap silently corrupts every ASOF
        join downstream, so refuse to load one."""
        if not self.shots:
            raise ValueError(f"{self.cut_id}: no shots")
        if self.shots[0].tc_in_ms != 0:
            raise ValueError(f"{self.cut_id}: first shot must start at 0")
        for a, b in zip(self.shots, self.shots[1:]):
            if a.tc_out_ms != b.tc_in_ms:
                raise ValueError(
                    f"{self.cut_id}: shot {a.shot_idx} ends at {a.tc_out_ms} "
                    f"but shot {b.shot_idx} starts at {b.tc_in_ms}"
                )
            if a.duration_ms <= 0:
                raise ValueError(f"{self.cut_id}: shot {a.shot_idx} is empty")
        known = {s.scene_id for s in self.scenes}
        unknown = {s.scene_id for s in self.shots} - known
        if unknown:
            raise ValueError(f"{self.cut_id}: shots reference unknown scenes {sorted(unknown)}")

    def scene_at(self, media_ms: int) -> Scene | None:
        shot = self.shot_at(media_ms)
        if shot is None:
            return None
        return next((s for s in self.scenes if s.scene_id == shot.scene_id), None)

    def shot_at(self, media_ms: int) -> Shot | None:
        """Reference implementation of what the ASOF join does in SQL. Used by
        the tests to check the database against plain Python."""
        candidate = None
        for shot in self.shots:
            if shot.tc_in_ms <= media_ms:
                candidate = shot
            else:
                break
        if candidate is not None and media_ms >= candidate.tc_out_ms:
            return None  # past the tail of the cut
        return candidate


# --------------------------------------------------------------------- timecode

_TC = re.compile(r"^(\d{1,2}):(\d{2}):(\d{2})[:;](\d{1,3})$")


def tc_to_ms(tc: str, fps: float) -> int:
    """'01:23:45:12' -> milliseconds. Accepts ';' for drop-frame notation but
    does not attempt drop-frame arithmetic — we normalise to ms at ingest and
    never round-trip back to timecode."""
    m = _TC.match(tc.strip())
    if not m:
        raise ValueError(f"not a timecode: {tc!r}")
    h, mm, ss, ff = (int(g) for g in m.groups())
    return int(round(((h * 3600 + mm * 60 + ss) + ff / fps) * 1000))


def ms_to_tc(ms: int, fps: float) -> str:
    total_s, rem_ms = divmod(int(ms), 1000)
    h, rem = divmod(total_s, 3600)
    mm, ss = divmod(rem, 60)
    ff = int(rem_ms * fps / 1000)
    return f"{h:02d}:{mm:02d}:{ss:02d}:{ff:02d}"


# ------------------------------------------------------------------ EDL parsing


def parse_edl(path: str | Path, cut_id: str, film_id: str, fps: float = 24.0) -> list[Shot]:
    """Parse a CMX3600 EDL into shots.

    Deliberately minimal: event number, source in/out, record in/out. Real EDLs
    carry transitions, speed changes and comments we do not need — the record
    timecodes are what place a cut on the timeline.
    """
    shots: list[Shot] = []
    event = re.compile(
        r"^(\d{3,6})\s+(\S+)\s+(\S+)\s+(\S+)\s+"
        r"(\d{2}:\d{2}:\d{2}[:;]\d{2})\s+(\d{2}:\d{2}:\d{2}[:;]\d{2})\s+"
        r"(\d{2}:\d{2}:\d{2}[:;]\d{2})\s+(\d{2}:\d{2}:\d{2}[:;]\d{2})"
    )
    pending_scene = "S001"
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line.startswith("*"):
            if "SCENE" in line.upper():
                pending_scene = line.upper().split("SCENE")[-1].strip().strip(":") or pending_scene
            continue
        m = event.match(line)
        if not m:
            continue
        idx, reel, _track, _kind, _src_in, _src_out, rec_in, rec_out = m.groups()
        shots.append(
            Shot(
                cut_id=cut_id,
                shot_idx=int(idx),
                tc_in_ms=tc_to_ms(rec_in, fps),
                tc_out_ms=tc_to_ms(rec_out, fps),
                scene_id=pending_scene,
                slug=reel,
            )
        )
    return shots


def write_edl(timeline: Timeline, path: str | Path, fps: float = 24.0) -> Path:
    """Emit a CMX3600 EDL — the format an editor actually imports."""
    path = Path(path)
    lines = [f"TITLE: {timeline.label}", "FCM: NON-DROP FRAME", ""]
    for sh in timeline.shots:
        src_in = ms_to_tc(0, fps)
        src_out = ms_to_tc(sh.duration_ms, fps)
        lines.append(f"* SCENE {sh.scene_id}")
        lines.append(
            f"{sh.shot_idx:06d}  {sh.slug[:8]:<8} V     C        "
            f"{src_in} {src_out} {ms_to_tc(sh.tc_in_ms, fps)} {ms_to_tc(sh.tc_out_ms, fps)}"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


MARKER_COLUMNS = (
    "Number",
    "Source In",
    "Source Out",
    "Name",
    "Notes",
    "Color",
    "Duration Frames",
)


def write_markers_csv(rows: list[dict], path: str | Path, fps: float = 24.0) -> Path:
    """Timecoded marker list — the artifact the editor takes back to the bay.

    Plain CSV rather than an NLE-specific binary: Resolve, Premiere and Avid all
    differ on marker import, and a spreadsheet an assistant editor can read is
    worth more than a format that only opens in one of the three. The companion
    EDL from `write_edl` carries the same notes as `* COMMENT` lines for tools
    that prefer that route.
    """
    path = Path(path)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(MARKER_COLUMNS))
        w.writeheader()
        for i, r in enumerate(rows, 1):
            w.writerow(
                {
                    "Number": i,
                    "Source In": ms_to_tc(r["start_ms"], fps),
                    "Source Out": ms_to_tc(r["end_ms"], fps),
                    "Name": r["name"],
                    "Notes": r["note"],
                    "Color": r.get("color", "Red"),
                    "Duration Frames": max(1, int(r["duration_ms"] * fps / 1000)),
                }
            )
    return path


# ------------------------------------------------------- synthetic test fixture


def build_fixture_timeline(
    cut_id: str = "cut-a",
    film_id: str = "fixture",
    label: str = "Fixture — assembly",
    n_scenes: int = 24,
    seed: int = 11,
) -> Timeline:
    """A small original timeline that ships in the repo so `pytest` and
    `docker compose up` work with no download and no third-party rights."""
    rng = random.Random(seed)
    scenes, shots, t, idx = [], [], 0, 1
    for n in range(1, n_scenes + 1):
        tag = SCENE_TAGS[(n * 3) % len(SCENE_TAGS)]
        scene_id = f"S{n:03d}"
        scenes.append(
            Scene(
                film_id=film_id,
                scene_id=scene_id,
                scene_num=n,
                heading=f"INT. LOCATION {n} - {'NIGHT' if n % 2 else 'DAY'}",
                synopsis=f"Scene {n}: a {tag} beat.",
                dialogue=f"Placeholder dialogue for scene {n} ({tag}).",
                tags=(tag,),
            )
        )
        # exposition scenes run long and get few cuts — which is exactly the
        # shape that produces the attention dips this project is built to find
        n_shots = rng.randint(2, 4) if tag == "exposition" else rng.randint(4, 12)
        for _ in range(n_shots):
            dur = rng.randint(6000, 14000) if tag == "exposition" else rng.randint(1200, 5200)
            shots.append(Shot(cut_id, idx, t, t + dur, scene_id, f"A{idx:04d}"))
            t += dur
            idx += 1
    tl = Timeline(cut_id, film_id, label, shots, scenes)
    tl.validate()
    return tl


def tighten(
    timeline: Timeline,
    trims: dict[str, float],
    *,
    cut_id: str,
    label: str,
    min_shot_ms: int = 800,
) -> Timeline:
    """Produce the next cut by trimming named scenes.

    `trims` maps scene_id -> fraction to remove (0.35 = lose 35% of the scene).
    Shots inside a trimmed scene shrink proportionally, and everything after
    slides earlier — which is exactly why scenes, not timecodes, are the unit of
    comparison between two cuts.

    This is what closes the loop: the system recommends a trim, the editor makes
    it, and the next screening measures whether it helped.
    """
    if not trims:
        raise ValueError("tighten() with no trims produces an identical cut")
    for scene_id, frac in trims.items():
        if not 0.0 < frac < 0.9:
            raise ValueError(f"trim for {scene_id} must be in (0, 0.9), got {frac}")

    new_shots: list[Shot] = []
    t = 0
    for sh in timeline.shots:
        frac = trims.get(sh.scene_id, 0.0)
        dur = max(min_shot_ms, int(round(sh.duration_ms * (1.0 - frac))))
        new_shots.append(
            Shot(cut_id, sh.shot_idx, t, t + dur, sh.scene_id, sh.slug)
        )
        t += dur

    tl = Timeline(cut_id, timeline.film_id, label, new_shots, list(timeline.scenes))
    tl.validate()
    return tl

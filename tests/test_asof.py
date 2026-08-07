"""The ASOF join is the load-bearing piece of this project. If it is wrong,
every note the agent writes is attached to the wrong scene and nothing else
matters — so it gets tested against an independent Python implementation,
including every boundary case we could think of.

Runs on chdb: a real ClickHouse engine, no server, no network, no Docker.
"""

from __future__ import annotations

import pytest

from crf.timeline import Shot, Timeline, build_fixture_timeline, ms_to_tc, tc_to_ms

SHOTS = [
    Shot("cutA", 1, 0, 5000, "S01", "A0001"),
    Shot("cutA", 2, 5000, 12000, "S01", "A0002"),
    Shot("cutA", 3, 12000, 12500, "S02", "A0003"),   # 500ms — shorter than a bucket
    Shot("cutA", 4, 12500, 30000, "S02", "A0004"),
    Shot("cutB", 1, 0, 8000, "S01", "B0001"),
    Shot("cutB", 2, 8000, 30000, "S02", "B0002"),
]

PROBES = [
    ("cutA", 0),      # exact head of the timeline
    ("cutA", 4999),   # final ms of shot 1
    ("cutA", 5000),   # boundary: belongs to shot 2, not shot 1
    ("cutA", 12499),  # inside the sub-bucket shot
    ("cutA", 12500),  # boundary into shot 4
    ("cutA", 29999),  # final ms of the cut
    ("cutB", 7999),   # same media time as a cutA probe...
    ("cutB", 8000),   # ...must resolve against cutB's own shots
]


@pytest.fixture(scope="module")
def shots(backend):
    """Uses cut ids "cutA"/"cutB" so these rows never collide with the seeded
    fixture's "cut-a"/"cut-b" in the shared engine."""
    backend.insert(
        "crf.shot",
        [(s.cut_id, s.shot_idx, s.tc_in_ms, s.tc_out_ms, s.scene_id, s.slug) for s in SHOTS],
        ["cut_id", "shot_idx", "tc_in_ms", "tc_out_ms", "scene_id", "slug"],
    )
    return backend


def _asof(be, cut_id: str, media_ms: int):
    rows = be.query(
        """SELECT s.shot_idx AS shot_idx, s.scene_id AS scene_id
           FROM (SELECT {cut:String} AS cut_id, {ms:UInt32} AS media_ms) AS e
           ASOF LEFT JOIN crf.shot AS s
             ON e.cut_id = s.cut_id AND e.media_ms >= s.tc_in_ms""",
        {"cut": cut_id, "ms": media_ms},
    )
    return rows[0] if rows else None


def _expected(cut_id: str, media_ms: int):
    candidates = [s for s in SHOTS if s.cut_id == cut_id and s.tc_in_ms <= media_ms]
    return max(candidates, key=lambda s: s.tc_in_ms) if candidates else None


@pytest.mark.parametrize("cut_id,media_ms", PROBES)
def test_asof_matches_python(shots, cut_id, media_ms):
    got = _asof(shots, cut_id, media_ms)
    exp = _expected(cut_id, media_ms)
    assert got is not None, "ASOF LEFT JOIN must return a row even when unmatched"
    assert got["shot_idx"] == exp.shot_idx
    assert got["scene_id"] == exp.scene_id


def test_boundary_belongs_to_the_new_shot(shots):
    """A sample landing exactly on a cut belongs to the shot starting there.
    Off-by-one here would misattribute every note that starts on a cut."""
    assert _asof(shots, "cutA", 5000)["shot_idx"] == 2
    assert _asof(shots, "cutA", 4999)["shot_idx"] == 1


def test_cuts_do_not_bleed(shots):
    """cut_id is an equality key, so cutB probes never match cutA shots."""
    assert _asof(shots, "cutB", 7999)["shot_idx"] == 1
    assert _asof(shots, "cutA", 7999)["shot_idx"] == 2


def test_shot_shorter_than_a_bucket_is_still_matched(shots):
    assert _asof(shots, "cutA", 12250)["shot_idx"] == 3


# ------------------------------------------------------------------- timecode


@pytest.mark.parametrize("tc,fps,ms", [
    ("00:00:00:00", 24.0, 0),
    ("00:00:01:00", 24.0, 1000),
    ("01:00:00:00", 24.0, 3_600_000),
    ("00:01:30:12", 24.0, 90_500),
])
def test_tc_roundtrip(tc, fps, ms):
    assert tc_to_ms(tc, fps) == ms
    assert ms_to_tc(ms, fps) == tc


def test_bad_timecode_rejected():
    with pytest.raises(ValueError):
        tc_to_ms("banana", 24.0)


# ------------------------------------------------------------------- timeline


def test_fixture_timeline_is_gapless():
    build_fixture_timeline().validate()


def test_gap_is_rejected():
    tl = build_fixture_timeline()
    broken = Timeline(
        tl.cut_id, tl.film_id, tl.label,
        [tl.shots[0], Shot("cut-a", 2, tl.shots[0].tc_out_ms + 1, 99_999, "S001", "X")],
        tl.scenes,
    )
    with pytest.raises(ValueError, match="ends at"):
        broken.validate()


def test_shot_at_matches_sql_semantics():
    """The Python reference used by these tests must agree with the SQL."""
    tl = build_fixture_timeline()
    for shot in tl.shots[:20]:
        assert tl.shot_at(shot.tc_in_ms).shot_idx == shot.shot_idx
        assert tl.shot_at(shot.tc_out_ms - 1).shot_idx == shot.shot_idx
    assert tl.shot_at(tl.runtime_ms + 5000) is None  # past the tail

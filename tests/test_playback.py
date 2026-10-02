"""Placing a recording on one clock, without writing anything out."""

from __future__ import annotations

import pytest
from rrr.playback import (
    Direction,
    TimeRange,
    crop_clock_points,
    in_colour_camera,
    sample_range,
)
from rrr.timeline import AudioClockPoint, AudioTimeline, Rig
from rrr.video import Extrinsics

RATE = 16_000
TIMES = [(10, 100.0), (11, 100.5), (12, 101.0), (13, 101.5)]


def _timeline() -> AudioTimeline:
    """One second per 16000 samples, starting at 100 s."""
    return AudioTimeline(
        [AudioClockPoint(0, 100.0), AudioClockPoint(RATE, 101.0)], RATE
    )


def test_a_range_of_frames_leaves_the_recordings_own_edges_open() -> None:
    assert TimeRange.of_frames(TIMES, 0, None) == TimeRange()
    assert TimeRange.of_frames(TIMES, 1, 3) == TimeRange(100.5, 101.5)
    assert TimeRange.of_frames(TIMES, 2, 10) == TimeRange(101.0, None)
    with pytest.raises(ValueError, match="outside an archive of 4 frames"):
        TimeRange.of_frames(TIMES, 4, None)


def test_a_range_is_half_open() -> None:
    selected = TimeRange(100.5, 101.5)
    assert selected.selected and not TimeRange().selected
    assert selected.contains(100.5)
    assert not selected.contains(101.5)


def test_the_sample_range_follows_the_clock_and_stays_inside_the_file() -> None:
    timeline = _timeline()
    # A quarter sample early, so rounding up lands on a whole sample either way.
    early = 0.25 / RATE
    selected = TimeRange(100.5 - early, 100.75 - early)
    assert sample_range(timeline, selected, RATE) == (8_000, 12_000)
    assert sample_range(timeline, TimeRange(99.0, 200.0), RATE) == (0, RATE)
    # Nothing to place the interval with: the whole file, never a guess.
    assert sample_range(None, TimeRange(100.5, 100.75), RATE) == (0, RATE)


def test_cropped_clock_points_start_at_zero_and_cover_the_crop() -> None:
    points = crop_clock_points(_timeline(), 4_000, 12_000)
    assert points[0] == AudioClockPoint(0, pytest.approx(100.25), 0)
    assert points[-1] == AudioClockPoint(8_000, pytest.approx(100.75), 0)


def test_directions_stay_in_the_array_frame_while_the_rig_is_unset() -> None:
    readings = [
        Direction(time=1.0, angle=90.0, voice=True),
        Direction(time=1.1, angle=0.0, voice=False),
    ]
    assert in_colour_camera(readings, Rig(), Extrinsics.identity()) is None
    measured = Rig(
        source="measured",
        rotation=(1.0, 0.0, 0.0, 0.0, 0.0, -1.0, 0.0, 1.0, 0.0),
        translation=(0.0, 0.0, 0.0),
    )
    assert in_colour_camera(readings, measured, None) is None
    # The array's +Y onto the camera's +Z: 0 deg is straight ahead, +X stays 90.
    turned = in_colour_camera(readings, measured, Extrinsics.identity())
    assert turned is not None
    assert [r.angle for r in turned] == [pytest.approx(90.0), pytest.approx(0.0)]

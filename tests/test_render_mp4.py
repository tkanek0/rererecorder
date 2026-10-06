from __future__ import annotations

import json
from pathlib import Path

import av
import pytest
from realsense_adapter import (
    Extrinsics,
)
from render_mp4 import _directions, main, render
from rrr.playback import Direction
from rrr.timeline import (
    Rig,
    SyncCalibration,
    read_manifest,
    write_manifest,
)

from .conftest import SMALL_FRAMES as FRAMES


def test_renders_playable_video_and_audio(small_session, tmp_path: Path) -> None:
    output = tmp_path / "review.mp4"
    report = render(small_session.directory, output)

    assert output.exists()
    assert report.frames == FRAMES
    assert report.audio
    assert report.audio_channel == "processed channel 0"
    assert report.offset_s is None
    with av.open(str(output)) as container:
        assert len(container.streams.video) == 1
        assert len(container.streams.audio) == 1
        video = list(container.decode(video=0))
        assert len(video) == FRAMES
        assert video[0].width == 32
        assert video[0].height == 24


def test_measured_offset_and_rig_are_reported(small_session, tmp_path: Path) -> None:
    manifest = read_manifest(small_session)
    rig = Rig(
        source="measured",
        rotation=(1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0),
        translation=(0.0, 0.0, 0.0),
        microphones=((0.0, 0.0, 0.0),),
        channels=(1,),
    )
    manifest.rig = rig
    manifest.calibration = SyncCalibration(offset_s=0.08)
    write_manifest(small_session, manifest)
    report = render(small_session.directory, tmp_path / "calibrated.mp4", audio_channel="mix")
    assert report.offset_s == pytest.approx(0.08)
    assert report.audio_channel == "physical microphone mix from rig"
    assert report.doa == "colour-camera coordinates (measured rig)"


def test_direction_falls_back_to_array_coordinates(tmp_path: Path) -> None:
    path = tmp_path / "doa.jsonl"
    path.write_text(json.dumps({"t": 10.0, "angle": 90, "voice": True}) + "\n")
    readings, mode = _directions(
        str(path), 0.25, Rig(), Extrinsics.identity(), enabled=True
    )
    assert readings == [Direction(time=10.25, angle=90.0, voice=True)]
    assert mode == "array coordinates (rig unset)"


def test_existing_movie_is_not_replaced(small_session, tmp_path: Path) -> None:
    output = tmp_path / "review.mp4"
    output.write_bytes(b"keep")
    with pytest.raises(FileExistsError):
        render(small_session.directory, output)
    assert output.read_bytes() == b"keep"


def test_the_command_line_writes_inside_the_session_by_default(small_session) -> None:
    assert main([small_session.directory]) == 0

    output = Path(small_session.review)
    assert output == Path(small_session.directory) / "review.mp4"
    with av.open(str(output)) as container:
        assert container.streams.video

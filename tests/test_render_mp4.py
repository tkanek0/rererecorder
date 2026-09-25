from __future__ import annotations

import json
import wave
from pathlib import Path

import av
import numpy as np
import pytest

from rrr.timeline import (
    AudioClockPoint,
    AudioClockWriter,
    AudioTrack,
    Rig,
    SessionManifest,
    SessionPaths,
    SyncCalibration,
    VideoTrack,
    read_manifest,
    write_manifest,
)
from rrr.tools.render_mp4 import _Direction, _directions, main, render
from rrr.video import (
    ArchiveWriter,
    Calibration,
    Extrinsics,
    FrameSet,
    Intrinsics,
    StreamConfig,
)

RATE = 8_000
FPS = 10.0
FRAMES = 4
START = 1_000.0


@pytest.fixture
def session(tmp_path: Path) -> SessionPaths:
    paths = SessionPaths.create(str(tmp_path / "sessions"), "whole")
    intrinsics = Intrinsics(
        width=32,
        height=24,
        fx=16.0,
        fy=16.0,
        ppx=16.0,
        ppy=12.0,
        model="brown_conrady",
        coeffs=(0.0,) * 5,
    )
    calibration = Calibration(
        color=intrinsics,
        depth=intrinsics,
        depth_scale=0.001,
        depth_to_color=Extrinsics.identity(),
        aligned=False,
    )
    with ArchiveWriter(
        paths.video,
        calibration=calibration,
        config=StreamConfig(depth=False, infrared=False),
    ) as writer:
        for n in range(FRAMES):
            image = np.zeros((24, 32, 3), dtype=np.uint8)
            image[:, :, n % 3] = 64 + n * 32
            assert writer.append(
                FrameSet(
                    index=n,
                    color_timestamp_ms=(START + n / FPS) * 1_000,
                    depth_timestamp_ms=None,
                    received_monotonic=START + n / FPS,
                    color=image,
                    depth=None,
                    color_format="rgb8",
                    calibration=calibration,
                    motion=None,
                    timestamp_domain="global_time",
                )
            )
        assert writer.drain()

    count = round(FRAMES / FPS * RATE)
    samples = np.zeros((count, 6), dtype="<i2")
    samples[:, 0] = np.arange(count) % 1_000
    with wave.open(paths.audio, "wb") as handle:
        handle.setnchannels(6)
        handle.setsampwidth(2)
        handle.setframerate(RATE)
        handle.writeframes(samples.tobytes())
    with AudioClockWriter(paths.audio_clock) as clock:
        clock.append(AudioClockPoint(0, START))
        clock.append(AudioClockPoint(count, START + count / RATE))
    Path(paths.doa).write_text(
        json.dumps({"t": START, "angle": 90, "voice": True}) + "\n"
    )
    write_manifest(
        paths,
        SessionManifest(
            session_id="whole",
            video=VideoTrack(frames=FRAMES, fps=FPS),
            audio=AudioTrack(
                rate=RATE,
                channels=6,
                samples=count,
                first_monotonic=START,
            ),
            doa_file="doa.jsonl",
        ),
    )
    return paths


def test_renders_playable_video_and_audio(session, tmp_path: Path) -> None:
    output = tmp_path / "review.mp4"
    report = render(session.directory, output)

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


def test_measured_offset_and_rig_are_reported(session, tmp_path: Path) -> None:
    manifest = read_manifest(session)
    rig = Rig(
        source="measured",
        rotation=(1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0),
        translation=(0.0, 0.0, 0.0),
        microphones=((0.0, 0.0, 0.0),),
        channels=(1,),
    )
    write_manifest(
        session,
        manifest.with_rig(rig).with_calibration(SyncCalibration(offset_s=0.08)),
    )
    report = render(session.directory, tmp_path / "calibrated.mp4", audio_channel="mix")
    assert report.offset_s == pytest.approx(0.08)
    assert report.audio_channel == "physical microphone mix from rig"
    assert report.doa == "colour-camera coordinates (measured rig)"


def test_direction_falls_back_to_array_coordinates(tmp_path: Path) -> None:
    path = tmp_path / "doa.jsonl"
    path.write_text(json.dumps({"t": 10.0, "angle": 90, "voice": True}) + "\n")
    readings, mode = _directions(
        str(path), 0.25, Rig(), Extrinsics.identity(), enabled=True
    )
    assert readings == [_Direction(time=10.25, angle=90.0, voice=True)]
    assert mode == "array coordinates (rig unset)"


def test_existing_movie_is_not_replaced(session, tmp_path: Path) -> None:
    output = tmp_path / "review.mp4"
    output.write_bytes(b"keep")
    with pytest.raises(FileExistsError):
        render(session.directory, output)
    assert output.read_bytes() == b"keep"


def test_the_command_line_writes_inside_the_session_by_default(session) -> None:
    assert main([session.directory]) == 0

    output = Path(session.review)
    assert output == Path(session.directory) / "review.mp4"
    with av.open(str(output)) as container:
        assert container.streams.video

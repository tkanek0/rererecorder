"""Whether a known offset can be measured back out of a synthetic session.

A real handclap is the point of the tool, but it cannot be tested: the answer
would be whatever the hardware happens to do. So a session is built here with an
offset put into it deliberately - an audio impulse at one instant, a burst of
movement in the video at another - and the tool is asked to name the difference.

The accuracy that can be expected is one frame interval, because that is all the
video says. The assertions allow that and no more.
"""

from __future__ import annotations

import json
import wave
from pathlib import Path

import numpy as np
import pytest

from rrr.timeline import (
    AudioClockPoint,
    AudioClockWriter,
    AudioTrack,
    ClockPair,
    SessionManifest,
    SessionPaths,
    VideoTrack,
    read_manifest,
    write_manifest,
)
from rrr.tools import calibrate
from rrr.video import ArchiveWriter, Calibration, Extrinsics, Intrinsics, StreamConfig

RATE = 16_000
CHANNELS = 6
WIDTH, HEIGHT = 64, 48
FPS = 30.0
#: Where the session sits on the monotonic axis, and its epoch counterpart.
MONO = 1_402_562.0
REAL = 1_788_330_516.0
OFFSET = REAL - MONO

#: The video frame, counting from the first, whose image differs from the one
#: before it - the synthetic "hands meeting".
MOVEMENT_FRAME = 60

#: What is put in: the audio impulse lands this much later than the movement.
#: Chosen larger than a frame interval so a wrong answer cannot look right.
PLANTED_OFFSET_S = 0.080


@pytest.fixture
def calibration() -> Calibration:
    """Calibration for the tiny synthetic frames."""
    intrinsics = Intrinsics(
        width=WIDTH, height=HEIGHT, fx=32.0, fy=32.0,
        ppx=32.0, ppy=24.0, model="brown_conrady", coeffs=(0.0,) * 5,
    )
    return Calibration(
        color=intrinsics,
        depth=intrinsics,
        depth_scale=0.001,
        depth_to_color=Extrinsics.identity(),
        aligned=False,
    )


@pytest.fixture
def session(tmp_path: Path, calibration: Calibration) -> SessionPaths:
    """A session with a clap planted in it, video and audio.

    The video is 150 frames of a static scene with one frame that differs; the
    audio is silence with one impulse, placed PLANTED_OFFSET_S later.
    """
    from rrr.video import FrameSet

    paths = SessionPaths.create(str(tmp_path), "planted")
    frames_total = 150
    rng = np.random.default_rng(20260902)
    still = rng.integers(0, 200, (HEIGHT, WIDTH), dtype=np.uint8)
    moved = rng.integers(0, 200, (HEIGHT, WIDTH), dtype=np.uint8)

    with ArchiveWriter(
        paths.video,
        calibration=calibration,
        config=StreamConfig(infrared=True),
    ) as writer:
        for n in range(frames_total):
            capture = MONO + n / FPS
            image = moved if n == MOVEMENT_FRAME else still
            assert writer.append(
                FrameSet(
                    index=n + 1,
                    color_timestamp_ms=None,
                    depth_timestamp_ms=(capture + OFFSET) * 1000.0,
                    received_monotonic=capture,
                    color=None,
                    depth=np.zeros((HEIGHT, WIDTH), np.uint16),
                    calibration=calibration,
                    motion=None,
                    timestamp_domain="global_time",
                    infrared=(image, image),
                ),
                timeout=30.0,
            )
        assert writer.drain()

    # Audio: silence, with an impulse at the movement's time plus the offset.
    seconds = frames_total / FPS
    samples = np.zeros((int(seconds * RATE), CHANNELS), dtype="<i2")
    noise = np.random.default_rng(7).integers(-40, 40, samples.shape)
    samples += noise.astype("<i2")
    impulse_at = MOVEMENT_FRAME / FPS + PLANTED_OFFSET_S
    start = int(impulse_at * RATE)
    samples[start : start + 160, :] = 12_000  # 10 ms of loud

    with wave.open(paths.audio, "wb") as out:
        out.setnchannels(CHANNELS)
        out.setsampwidth(2)
        out.setframerate(RATE)
        out.writeframes(samples.tobytes())

    with AudioClockWriter(paths.audio_clock) as clock_writer:
        for block in range(0, len(samples), RATE):
            clock_writer.append(
                AudioClockPoint(sample=block, monotonic=MONO + block / RATE)
            )
        clock_writer.append(
            AudioClockPoint(sample=len(samples), monotonic=MONO + len(samples) / RATE)
        )

    write_manifest(
        paths,
        SessionManifest(
            session_id="planted",
            started_at=ClockPair(MONO, REAL),
            stopped_at=ClockPair(MONO + seconds, REAL + seconds),
            clock_samples=[ClockPair(MONO, REAL), ClockPair(MONO + seconds, REAL + seconds)],
            video=VideoTrack(
                frames=frames_total,
                first_monotonic=MONO,
                last_monotonic=MONO + (frames_total - 1) / FPS,
                timestamp_domain="global_time",
                fps=FPS,
            ),
            audio=AudioTrack(
                rate=RATE,
                channels=CHANNELS,
                samples=len(samples),
                first_monotonic=MONO,
            ),
        ),
    )
    return paths


# -- the two halves -----------------------------------------------------------


def test_the_audio_impulse_is_located(session: SessionPaths) -> None:
    """To well under a video frame: this half is not the limiting one."""
    claps = calibrate._find_claps(session)

    assert len(claps) == 1
    expected = MONO + MOVEMENT_FRAME / FPS + PLANTED_OFFSET_S
    assert claps[0] == pytest.approx(expected, abs=0.005)


def test_the_video_movement_is_located(session: SessionPaths) -> None:
    """To within one frame, which is all the video can say."""
    from rrr.video import ArchiveSource

    claps = calibrate._find_claps(session)
    with ArchiveSource(session.video) as archive:
        found = calibrate._find_movement(
            archive, archive.frame_times(), claps[0], "ir1"
        )

    assert found is not None
    index, at, sharpness = found
    assert index == MOVEMENT_FRAME + 1, "the archive's own index of that frame"
    assert at == pytest.approx(MONO + MOVEMENT_FRAME / FPS, abs=1e-6)
    assert sharpness > 2.0, "a distinct movement, not the noise floor"


def test_a_still_recording_yields_no_movement(session: SessionPaths) -> None:
    """Looking somewhere with nothing happening must not invent a peak."""
    from rrr.video import ArchiveSource

    with ArchiveSource(session.video) as archive:
        found = calibrate._find_movement(
            archive, archive.frame_times(), MONO + 1.0, "ir1"
        )

    # A peak is always found - some frame differs most - so the caller is told
    # how distinct it was rather than being handed a bare answer.
    assert found is not None
    assert found[2] < 2.0, "and it is indistinguishable from the noise"


# -- end to end ---------------------------------------------------------------


def test_the_planted_offset_is_measured_back(session: SessionPaths, capsys) -> None:
    root = str(Path(session.directory).parent)
    assert calibrate.main([f"{root}/planted"]) == 0

    printed = capsys.readouterr().out
    assert "impulses        1 found" in printed
    # One frame interval is the tolerance the tool itself claims.
    assert f"{PLANTED_OFFSET_S * 1000:+.1f} ms" in printed or "+80" in printed


def test_nothing_is_written_without_apply(session: SessionPaths) -> None:
    """A measurement nobody looked at is not better than admitting ignorance."""
    root = str(Path(session.directory).parent)
    calibrate.main([f"{root}/planted"])

    assert read_manifest(session).calibration.measured is False


def test_apply_writes_the_offset_and_its_uncertainty(session: SessionPaths) -> None:
    root = str(Path(session.directory).parent)
    assert calibrate.main([f"{root}/planted", "--apply"]) == 0

    result = read_manifest(session).calibration
    assert result.measured is True
    assert result.offset_s == pytest.approx(PLANTED_OFFSET_S, abs=1.0 / FPS)
    assert result.method == "handclap"
    # Half a frame for a single clap - the tool's own claim about itself.
    assert result.uncertainty_s == pytest.approx(1 / FPS / 2, rel=0.01)
    assert result.measured_at is not None


def test_a_session_with_one_device_is_refused(tmp_path) -> None:
    paths = SessionPaths.create(str(tmp_path), "videoonly")
    write_manifest(
        paths,
        SessionManifest(
            session_id="videoonly",
            started_at=ClockPair(MONO, REAL),
            video=VideoTrack(frames=10),
        ),
    )
    assert calibrate.main([str(tmp_path / "videoonly")]) == 1


def test_a_session_without_a_clap_is_refused(session: SessionPaths) -> None:
    """Silence must not produce an offset."""
    quiet = np.zeros((int(5 * RATE), CHANNELS), dtype="<i2")
    with wave.open(session.audio, "wb") as out:
        out.setnchannels(CHANNELS)
        out.setsampwidth(2)
        out.setframerate(RATE)
        out.writeframes(quiet.tobytes())

    root = str(Path(session.directory).parent)
    assert calibrate.main([f"{root}/planted"]) == 1
    assert read_manifest(session).calibration.measured is False


def test_the_measured_value_is_reported_for_the_record(session, capsys) -> None:
    """Prints what it measured, so the number appears in the test log.

    Not an assertion about formatting - the assertions above cover the value.
    This exists because "within one frame" is a wide tolerance and it is worth
    being able to see how wide the error actually was.
    """
    root = str(Path(session.directory).parent)
    calibrate.main([f"{root}/planted"])
    out = capsys.readouterr().out
    print("\n" + "\n".join(line for line in out.splitlines() if line.strip()))

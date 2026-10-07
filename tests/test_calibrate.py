"""Whether a known offset can be measured back out of a synthetic session.

An audio impulse and a video movement are planted a known interval apart; the
assertions allow one frame interval, which is all the video can resolve.
"""

from __future__ import annotations

import wave
from pathlib import Path

import calibrate
import numpy as np
import pytest
from realsense_adapter import (
    Calibration,
    Extrinsics,
    FrameSet,
    Intrinsics,
    StreamConfig,
)
from rrr.offset import handclap
from rrr.timeline import (
    ClockPair,
    SessionManifest,
    SessionPaths,
    VideoTrack,
    read_manifest,
    write_manifest,
)

from .conftest import write_session

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
        width=WIDTH,
        height=HEIGHT,
        fx=32.0,
        fy=32.0,
        ppx=32.0,
        ppy=24.0,
        model="brown_conrady",
        coeffs=(0.0,) * 5,
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
    """150 still frames but one, and an impulse PLANTED_OFFSET_S later."""
    frames_total = 150
    rng = np.random.default_rng(20260902)
    still = rng.integers(0, 200, (HEIGHT, WIDTH), dtype=np.uint8)
    moved = rng.integers(0, 200, (HEIGHT, WIDTH), dtype=np.uint8)
    frames = []
    for n in range(frames_total):
        capture = MONO + n / FPS
        image = moved if n == MOVEMENT_FRAME else still
        frames.append(
            FrameSet(
                index=n + 1,
                depth_timestamp_ms=(capture + OFFSET) * 1000.0,
                received_monotonic=capture,
                color=None,
                depth=np.zeros((HEIGHT, WIDTH), np.uint16),
                calibration=calibration,
                timestamp_domain="global_time",
                infrared=(image, image),
            )
        )

    # Audio: noise, with an impulse at the movement's time plus the offset.
    seconds = frames_total / FPS
    samples = (
        np.random.default_rng(7)
        .integers(-40, 40, (int(seconds * RATE), CHANNELS))
        .astype("<i2")
    )
    start = int((MOVEMENT_FRAME / FPS + PLANTED_OFFSET_S) * RATE)
    samples[start : start + 160, :] = 12_000  # 10 ms of loud

    return write_session(
        SessionPaths.create(str(tmp_path), "planted"),
        calibration=calibration,
        config=StreamConfig(infrared=True),
        frames=frames,
        audio=samples,
        rate=RATE,
        clock_every=RATE,
        started_at=ClockPair(MONO, REAL),
        stopped_at=ClockPair(MONO + seconds, REAL + seconds),
        clock_samples=[
            ClockPair(MONO, REAL),
            ClockPair(MONO + seconds, REAL + seconds),
        ],
    )


# -- what is measured --------------------------------------------------------


def test_a_still_recording_yields_no_movement(session: SessionPaths) -> None:
    """Looking somewhere with nothing happening must not invent a peak."""
    from rrr.video import ArchiveSource

    with ArchiveSource(session.video) as archive:
        found = handclap._find_movement(
            archive, archive.frame_times(), MONO + 1.0, "ir1"
        )

    # Some frame always differs most; its sharpness says it is noise.
    assert found is not None
    assert found[2] < 2.0, "and it is indistinguishable from the noise"


# -- end to end ---------------------------------------------------------------


def test_the_planted_offset_is_measured_back_and_written_only_on_apply(
    session: SessionPaths, capsys
) -> None:
    directory = str(Path(session.directory))
    assert calibrate.main([directory]) == 0
    printed = capsys.readouterr().out
    assert "impulses        1 found" in printed
    # Audio planted late, so adding the offset to an audio time must go back.
    assert f"{-PLANTED_OFFSET_S * 1000:+.1f} ms" in printed
    # A measurement nobody looked at is not better than admitting ignorance.
    assert read_manifest(session).calibration.measured is False

    assert calibrate.main([directory, "--apply"]) == 0
    result = read_manifest(session).calibration
    assert result.offset_s == pytest.approx(-PLANTED_OFFSET_S, abs=1.0 / FPS)
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

"""What a recording says about the rig it was made with.

Depth and colour were always recorded. The infrared pair and the inertial
sensor were not, and without them a session cannot be used for anything that
fuses the two: a stereo pair with no baseline has no scale, and inertial
samples with no transform to the camera are numbers in an unnamed frame.

Nothing here opens a device. What is tested is that the values survive the
archive, and that an archive written before they existed still opens.
"""

from __future__ import annotations

import numpy as np
import pytest

from rrr.video import (
    ArchiveSource,
    ArchiveWriter,
    Calibration,
    Extrinsics,
    Intrinsics,
    StreamConfig,
)
from rrr.video.types import MotionCalibration, MotionIntrinsics

from .conftest import DEPTH_SCALE, HEIGHT, WIDTH

#: The measured baseline of a D455's stereo pair, in metres.
BASELINE_M = 0.095


def _intrinsics(ppx: float) -> Intrinsics:
    return Intrinsics(
        width=WIDTH,
        height=HEIGHT,
        fx=650.0,
        fy=650.0,
        ppx=ppx,
        ppy=HEIGHT / 2,
        model="brown_conrady",
        coeffs=(0.0,) * 5,
    )


def _motion() -> MotionCalibration:
    return MotionCalibration(
        accel=MotionIntrinsics(
            data=tuple(float(v) for v in range(12)),
            noise_variances=(1e-3, 1e-3, 1e-3),
            bias_variances=(1e-5, 1e-5, 1e-5),
        ),
        gyro=MotionIntrinsics(
            data=tuple(float(v) for v in range(12, 24)),
            noise_variances=(2e-3, 2e-3, 2e-3),
            bias_variances=(2e-5, 2e-5, 2e-5),
        ),
        depth_to_accel=Extrinsics(
            rotation=(1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0),
            translation=(-0.0302, 0.0074, 0.0166),
        ),
        depth_to_gyro=Extrinsics.identity(),
    )


def _full() -> Calibration:
    """Everything a moving rig needs, as a D455 reports it."""
    return Calibration(
        color=_intrinsics(WIDTH / 2),
        depth=_intrinsics(WIDTH / 2),
        depth_scale=DEPTH_SCALE,
        depth_to_color=Extrinsics.identity(),
        aligned=False,
        infrared=(_intrinsics(WIDTH / 2), _intrinsics(WIDTH / 2 + 1)),
        # Depth is computed in the left imager's frame, so the left transform
        # is the identity and the right one carries the baseline.
        depth_to_infrared=(
            Extrinsics.identity(),
            Extrinsics(
                rotation=(1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0),
                translation=(-BASELINE_M, 0.0, 0.0),
            ),
        ),
        motion=_motion(),
    )


def _write(path: str, calibration: Calibration, make_frames) -> None:
    with ArchiveWriter(
        path, calibration=calibration, config=StreamConfig(color_format="rgb8")
    ) as writer:
        assert writer.append(
            make_frames(index=0, depth=np.zeros((HEIGHT, WIDTH), np.uint16)),
            timeout=10.0,
        )


def test_the_baseline_comes_out_of_the_two_transforms() -> None:
    assert _full().infrared_baseline_m == pytest.approx(BASELINE_M)


def test_a_recording_without_infrared_has_no_baseline(calibration) -> None:
    """Not zero: an unknown baseline and a zero one are different claims."""
    assert calibration.infrared_baseline_m is None


def test_the_whole_rig_survives_the_archive(tmp_path, make_frames) -> None:
    path = str(tmp_path / "video.rrdb")
    original = _full()
    _write(path, original, make_frames)

    with ArchiveSource(path) as archive:
        assert archive.calibration == original


def test_an_archive_written_before_the_rig_still_opens(
    tmp_path, calibration, make_frames
) -> None:
    """The new fields are absent from every recording made so far."""
    path = str(tmp_path / "video.rrdb")
    _write(path, calibration, make_frames)

    with ArchiveSource(path) as archive:
        assert archive.calibration.depth == calibration.depth
        assert archive.calibration.infrared == (None, None)
        assert archive.calibration.depth_to_infrared == (None, None)
        assert archive.calibration.motion is None


def test_a_recording_can_have_infrared_without_an_inertial_sensor(
    tmp_path, make_frames
) -> None:
    """The two are separate options, and a device may fail to start one."""
    path = str(tmp_path / "video.rrdb")
    partial = Calibration(
        color=None,
        depth=_intrinsics(WIDTH / 2),
        depth_scale=DEPTH_SCALE,
        depth_to_color=None,
        aligned=False,
        infrared=_full().infrared,
        depth_to_infrared=_full().depth_to_infrared,
        motion=None,
    )
    _write(path, partial, make_frames)

    with ArchiveSource(path) as archive:
        assert archive.calibration.motion is None
        assert archive.calibration.infrared_baseline_m == pytest.approx(BASELINE_M)

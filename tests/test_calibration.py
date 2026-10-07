"""What a recording says about the rig it was made with.

Infrared and inertial calibration must survive the archive, and an archive
written before they existed must still open. No device is opened.
"""

from __future__ import annotations

import numpy as np
import pytest
from realsense_adapter import Calibration, Extrinsics, Intrinsics, StreamConfig
from realsense_adapter.types import MotionCalibration, MotionIntrinsics
from rrr.video import ArchiveSource, ArchiveWriter

from .conftest import DEPTH_SCALE, HEIGHT, WIDTH

#: The measured baseline of a D455's stereo pair, in metres.
BASELINE_M = 0.09513


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


def test_a_calibration_without_infrared_or_an_inertial_sensor_survives(
    tmp_path, calibration, make_frames
) -> None:
    """The two are separate options, and a device may fail to start one."""
    partial = Calibration(
        color=None,
        depth=_intrinsics(WIDTH / 2),
        depth_scale=DEPTH_SCALE,
        depth_to_color=None,
        aligned=False,
        infrared=_full().infrared,
        depth_to_infrared=_full().depth_to_infrared,
    )
    for n, original in enumerate((calibration, partial)):
        path = str(tmp_path / f"video{n}.rrdb")
        _write(path, original, make_frames)
        with ArchiveSource(path) as archive:
            assert archive.calibration == original
            assert archive.calibration.motion is None
    assert partial.infrared_baseline_m == pytest.approx(BASELINE_M)
    assert calibration.infrared_baseline_m is None


# -- the emitter --------------------------------------------------------------


def test_every_emitter_mode_is_accepted_and_no_other() -> None:
    """Refused rather than ignored: silently recording with the projector in
    the wrong state costs a session that looks fine."""
    for mode in ("on", "off", "alternating"):
        assert StreamConfig(emitter=mode).as_dict()["emitter"] == mode
    with pytest.raises(ValueError, match="emitter mode"):
        StreamConfig(emitter="sometimes")


def test_the_emitter_mode_is_recorded_in_the_archive(tmp_path, make_frames) -> None:
    path = str(tmp_path / "video.rrdb")
    with ArchiveWriter(
        path,
        calibration=_full(),
        config=StreamConfig(color_format="rgb8", emitter="alternating"),
    ) as writer:
        assert writer.append(
            make_frames(index=0, depth=np.zeros((HEIGHT, WIDTH), np.uint16)),
            timeout=10.0,
        )

    with ArchiveSource(path) as archive:
        assert archive.meta["config"]["emitter"] == "alternating"

"""Synthetic frames and audio, so that nothing here needs a device attached.

The numbers are the ones measured on the real hardware rather than round
figures: a D455 at its native depth resolution, frame timestamps in epoch
milliseconds because that is what ``global_time`` reports, and a frame arriving
8 ms after the instant it claims to describe because that is what
``wait_for_frames`` was observed doing.
"""

from __future__ import annotations

from collections.abc import Callable

import numpy as np
import pytest

from timeline import ClockPair
from video import Calibration, Extrinsics, FrameSet, Intrinsics, Motion

#: The D455 at its native depth resolution, measured on the device.
WIDTH, HEIGHT = 848, 480
FX, FY = 426.6, 426.2
DEPTH_SCALE = 0.001

#: A plausible pair of host clocks, from a real reading on this machine. The
#: offset between them is large, which is what makes a forgotten conversion
#: obvious instead of subtle.
MONO = 1_322_228.023434
REAL = 1_788_250_182.059000
OFFSET = REAL - MONO

#: How long after its own timestamp a frame reached the process. Measured 2-11
#: ms on a D455; 8 ms is in the middle of that.
ARRIVAL_LAG_S = 0.008

FPS = 30.0


@pytest.fixture
def intrinsics() -> Intrinsics:
    """Intrinsics of a D455 depth stream at 848x480."""
    return Intrinsics(
        width=WIDTH,
        height=HEIGHT,
        fx=FX,
        fy=FY,
        ppx=WIDTH / 2,
        ppy=HEIGHT / 2,
        model="brown_conrady",
        coeffs=(0.0,) * 5,
    )


@pytest.fixture
def calibration(intrinsics: Intrinsics) -> Calibration:
    """Calibration with depth aligned to colour, as a recording would hold it."""
    return Calibration(
        color=intrinsics,
        depth=intrinsics,
        depth_scale=DEPTH_SCALE,
        depth_to_color=Extrinsics.identity(),
        aligned=True,
    )


@pytest.fixture
def make_frames(calibration: Calibration) -> Callable[..., FrameSet]:
    """Return a factory that wraps arrays in a FrameSet with realistic times."""

    def build(
        depth: np.ndarray | None = None,
        color: np.ndarray | None = None,
        motion: Motion | None = None,
        index: int = 1,
        timestamp_domain: str = "global_time",
        color_format: str = "rgb8",
        infrared: tuple[np.ndarray, np.ndarray] | None = None,
    ) -> FrameSet:
        """Build a frame set carrying whatever was passed.

        Args:
            depth: Raw uint16 depth, or None.
            color: Colour image in ``color_format``, or None.
            motion: Inertial sample, or None.
            index: Frame counter. Also sets the frame's place in time, at 30 fps.
            timestamp_domain: What the timestamp is supposed to mean.
            color_format: ``"rgb8"`` or ``"yuyv"``.
            infrared: The left and right raw images, or None.

        Returns:
            The frame set. Its ``capture_monotonic`` works out to
            ``MONO + index / FPS`` exactly, which is what assertions can lean on.
        """
        capture = index / FPS
        clock = ClockPair(
            monotonic=MONO + capture + ARRIVAL_LAG_S,
            realtime=REAL + capture + ARRIVAL_LAG_S,
        )
        return FrameSet(
            index=index,
            timestamp_ms=(REAL + capture) * 1000.0,
            received_at=clock.monotonic,
            color=color,
            depth=depth,
            calibration=calibration,
            motion=motion,
            clock=clock,
            timestamp_domain=timestamp_domain,
            color_format=color_format,
            infrared=infrared,
        )

    return build

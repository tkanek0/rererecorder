"""Getting frames off a RealSense camera.

Opens the device, applies a :class:`StreamConfig` - including the emitter
sequence the firmware insists on - and delivers :class:`FrameSet` values with
the device's own calibration. Knows nothing about HTTP, the audio device or
``rrr``, and reads no environment variable: every setting is an argument,
defaulting to :mod:`realsense_adapter.config`.
"""

from .config import (
    DEFAULT_COLOR,
    DEFAULT_COLOR_FORMAT,
    DEFAULT_DEPTH,
    DEFAULT_EMITTER,
    StreamConfig,
    StreamSpec,
)
from .source import FrameSource, LiveSource, StreamError, list_devices
from .types import (
    Calibration,
    DeviceInfo,
    Extrinsics,
    FrameSet,
    Intrinsics,
    Motion,
    MotionCalibration,
    MotionIntrinsics,
    MotionSample,
    color_to_bgr,
    color_to_rgb,
)

__all__ = [
    "DEFAULT_COLOR",
    "DEFAULT_COLOR_FORMAT",
    "DEFAULT_DEPTH",
    "DEFAULT_EMITTER",
    "Calibration",
    "DeviceInfo",
    "Extrinsics",
    "FrameSet",
    "FrameSource",
    "Intrinsics",
    "LiveSource",
    "Motion",
    "MotionCalibration",
    "MotionIntrinsics",
    "MotionSample",
    "StreamConfig",
    "StreamError",
    "StreamSpec",
    "color_to_bgr",
    "color_to_rgb",
    "list_devices",
]

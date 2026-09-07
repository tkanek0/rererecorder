"""Device layer: getting frames off a RealSense camera.

Copied from realsense-playground's ``sensor/`` and changed where recording two
devices at once demands it:

* a frame carries the clock pair that was read when it arrived, so its epoch
  timestamp can be converted onto the monotonic axis the audio is on,
* global time is enabled explicitly rather than relied on,
* sets the SDK re-delivers, and sets whose streams disagree about the moment,
  are discarded rather than written into a recording that claims to be
  synchronised. Both were measured happening on a real D455.

Depends on pyrealsense2, numpy and :mod:`timeline`. It knows nothing about HTTP,
JPEG or the audio device.
"""

from .archive import LEGACY_SUFFIX as ARCHIVE_LEGACY_SUFFIX
from .archive import SUFFIX as ARCHIVE_SUFFIX
from .archive import ArchiveSource, ArchiveWriter, WriterStats
from .config import (
    DEFAULT_COLOR,
    DEFAULT_COLOR_FORMAT,
    DEFAULT_DEPTH,
    DEFAULT_EMITTER,
    EMITTER_MODES,
    StreamConfig,
    StreamSpec,
)
from .hub import FrameHub
from .source import MAX_PAIR_SKEW_MS, FrameSource, LiveSource, StreamError, list_devices
from .types import (
    Calibration,
    DeviceInfo,
    Extrinsics,
    FrameSet,
    Intrinsics,
    Motion,
    join_yuyv,
    split_yuyv,
)

__all__ = [
    "ARCHIVE_LEGACY_SUFFIX",
    "ARCHIVE_SUFFIX",
    "DEFAULT_COLOR",
    "DEFAULT_COLOR_FORMAT",
    "DEFAULT_DEPTH",
    "DEFAULT_EMITTER",
    "EMITTER_MODES",
    "MAX_PAIR_SKEW_MS",
    "ArchiveSource",
    "ArchiveWriter",
    "Calibration",
    "DeviceInfo",
    "Extrinsics",
    "FrameHub",
    "FrameSet",
    "FrameSource",
    "Intrinsics",
    "LiveSource",
    "Motion",
    "StreamConfig",
    "StreamError",
    "StreamSpec",
    "WriterStats",
    "join_yuyv",
    "list_devices",
    "split_yuyv",
]

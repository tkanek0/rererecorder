"""Device layer: getting frames off a RealSense camera.

Copied from realsense-playground's ``sensor/`` and changed where recording two
devices at once demands it:

* a frame carries ``received_monotonic``, read when it arrived, which is the
  axis the audio is also on,
* global time is enabled explicitly rather than relied on,
* a set the SDK re-delivers is discarded rather than written into the
  recording twice - measured happening on a real D455. A set whose streams
  disagree about the moment is not: each stream's own timestamp is kept, so a
  consumer judges that for itself rather than have it decided here.

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
from .source import FrameSource, LiveSource, StreamError, list_devices
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

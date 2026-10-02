"""Device layer: getting frames off a RealSense camera.

Knows nothing about HTTP, JPEG or the audio device. See docs/design.md
"Module boundaries".
"""

from .archive import LEGACY_SUFFIX as ARCHIVE_LEGACY_SUFFIX
from .archive import SUFFIX as ARCHIVE_SUFFIX
from .archive import ArchiveSource, ArchiveWriter, WriterStats, join_yuyv, split_yuyv
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
    color_to_bgr,
    color_to_rgb,
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
    "color_to_bgr",
    "color_to_rgb",
    "join_yuyv",
    "list_devices",
    "split_yuyv",
]

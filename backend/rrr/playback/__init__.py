"""Reading a recording back on one clock, for anything that converts it.

The archive, the WAV and the sidecars stay the measurements. This package only
places them on the recording's monotonic axis - frames by their measured times,
audio through its clock sidecar and the offset, directions into the camera when
the rig allows - and says when it had to fall back to a nominal rate. What the
result is written out as belongs to the caller: see ``scripts/``.
"""

from .audio import (
    AudioSelection,
    audio_timeline,
    crop_clock_points,
    read_mono,
    read_wav,
    resample_onto_video,
    sample_range,
    select_audio,
)
from .direction import Direction, direction_at, in_colour_camera, read_directions
from .frames import TimeRange
from .output import replacing

__all__ = [
    "AudioSelection",
    "Direction",
    "TimeRange",
    "audio_timeline",
    "crop_clock_points",
    "direction_at",
    "in_colour_camera",
    "read_directions",
    "read_mono",
    "read_wav",
    "replacing",
    "resample_onto_video",
    "sample_range",
    "select_audio",
]

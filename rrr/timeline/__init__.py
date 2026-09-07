"""The clock layer: what time it was, on which clock, and how well it is known.

Imports nothing but numpy - no device, no web framework - so every claim it
makes can be tested without a camera or an array attached. That matters more
here than anywhere else in this repository: the whole point of recording two
devices at once is that the result can be lined up afterwards, and if the
arithmetic in this package is wrong, nothing downstream can detect it.
"""

from .audio_clock import (
    SUFFIX as AUDIO_CLOCK_SUFFIX,
)
from .audio_clock import (
    AudioClockPoint,
    AudioClockWriter,
    AudioTimeline,
    TimelineReport,
)
from .clock import ClockPair, ClockTrack, read_clocks
from .session import (
    AUDIO_CLOCK_NAME,
    AUDIO_NAME,
    DOA_NAME,
    FORMAT_VERSION,
    MANIFEST_NAME,
    VIDEO_NAME,
    AudioTrack,
    SessionError,
    SessionManifest,
    SessionPaths,
    SyncCalibration,
    VideoTrack,
    listing,
    read_manifest,
    write_manifest,
)

__all__ = [
    "AUDIO_CLOCK_NAME",
    "AUDIO_CLOCK_SUFFIX",
    "AUDIO_NAME",
    "DOA_NAME",
    "FORMAT_VERSION",
    "MANIFEST_NAME",
    "VIDEO_NAME",
    "AudioClockPoint",
    "AudioClockWriter",
    "AudioTimeline",
    "AudioTrack",
    "ClockPair",
    "ClockTrack",
    "SessionError",
    "SessionManifest",
    "SessionPaths",
    "SyncCalibration",
    "TimelineReport",
    "VideoTrack",
    "listing",
    "read_clocks",
    "read_manifest",
    "write_manifest",
]

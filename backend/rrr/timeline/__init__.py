"""The clock layer: what time it was, on which clock, and how well it is known.

Imports nothing but numpy - no device, no web framework - so all of it can be
tested without hardware.
"""

from .audio_clock import (
    AudioClockPoint,
    AudioClockWriter,
    AudioTimeline,
)
from .clock import ClockPair, ClockTrack, drift_ppm, read_clocks
from .events import Event, EventWriter, read_events
from .session import (
    REVIEW_NAME,
    AudioTrack,
    Rig,
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
    "REVIEW_NAME",
    "AudioClockPoint",
    "AudioClockWriter",
    "AudioTimeline",
    "AudioTrack",
    "ClockPair",
    "ClockTrack",
    "drift_ppm",
    "Event",
    "EventWriter",
    "Rig",
    "SessionError",
    "SessionManifest",
    "SessionPaths",
    "SyncCalibration",
    "VideoTrack",
    "listing",
    "read_clocks",
    "read_events",
    "read_manifest",
    "write_manifest",
]

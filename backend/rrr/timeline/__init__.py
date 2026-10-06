"""The clock layer: what time it was, on which clock, and how well it is known.

Imports nothing but numpy - no device, no web framework - so all of it can be
tested without hardware.
"""

from .audio_clock import (
    AudioClockPoint,
    AudioTimeline,
)
from .clock import ClockPair, ClockTrack, drift_ppm, read_clocks
from .events import Event, read_events
from .jsonl import JsonlWriter, read_jsonl
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
    "AudioTimeline",
    "AudioTrack",
    "ClockPair",
    "ClockTrack",
    "drift_ppm",
    "Event",
    "JsonlWriter",
    "Rig",
    "SessionError",
    "SessionManifest",
    "SessionPaths",
    "SyncCalibration",
    "VideoTrack",
    "listing",
    "read_clocks",
    "read_events",
    "read_jsonl",
    "read_manifest",
    "write_manifest",
]

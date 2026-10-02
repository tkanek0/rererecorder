"""Device layer: getting audio and a direction off a ReSpeaker USB Mic Array.

Derived from respeaker-playground, but each capture block is stamped with
PortAudio's ``inputBufferAdcTime`` (see :mod:`rrr.audio.capture`). Knows nothing
about HTTP or the camera.
"""

from .capture import (
    AudioTap,
    DeviceNotFound,
    DeviceStatus,
    devices,
    probe,
    rescan,
)
from .config import (
    BLOCK_SIZE,
    CHANNEL_MICS,
    CHANNEL_PLAYBACK,
    CHANNEL_PROCESSED,
    CHANNELS,
    DEVICE_NAME,
    SAMPLE_RATE,
)
from .doa import DoaTap, Reading
from .types import BlockStamp, Chunk, Window, dbfs, rms

__all__ = [
    "BLOCK_SIZE",
    "CHANNELS",
    "CHANNEL_MICS",
    "CHANNEL_PLAYBACK",
    "CHANNEL_PROCESSED",
    "DEVICE_NAME",
    "SAMPLE_RATE",
    "AudioTap",
    "BlockStamp",
    "Chunk",
    "DeviceNotFound",
    "DeviceStatus",
    "Reading",
    "DoaTap",
    "Window",
    "dbfs",
    "devices",
    "probe",
    "rescan",
    "rms",
]

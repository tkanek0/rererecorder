"""Getting audio and a direction off a ReSpeaker USB Mic Array.

Derived from respeaker-playground, but each capture block is stamped with
PortAudio's ``inputBufferAdcTime`` (see :mod:`respeaker_adapter.capture`).
Knows nothing about HTTP, the camera or ``rrr``, and reads no environment
variable: every setting is an argument, defaulting to
:mod:`respeaker_adapter.config`.
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

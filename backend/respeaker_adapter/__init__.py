"""Getting audio and a direction off a ReSpeaker USB Mic Array.

Derived from respeaker-playground, but each capture block is stamped with
PortAudio's ``inputBufferAdcTime`` (see :mod:`respeaker_adapter.capture`).
Knows nothing about HTTP, the camera or ``rrr``, and reads no environment
variable: every setting is an argument, defaulting to
:mod:`respeaker_adapter.config`.
"""

from .capture import (
    AudioTap,
    probe,
    rescan,
)
from .config import (
    BLOCK_SIZE,
    CHANNELS,
    DEVICE_NAME,
)
from .doa import DoaTap, Reading
from .types import BlockStamp, Chunk, DeviceNotFound, dbfs, rms

__all__ = [
    "BLOCK_SIZE",
    "CHANNELS",
    "DEVICE_NAME",
    "AudioTap",
    "BlockStamp",
    "Chunk",
    "DeviceNotFound",
    "DoaTap",
    "Reading",
    "dbfs",
    "probe",
    "rescan",
    "rms",
]

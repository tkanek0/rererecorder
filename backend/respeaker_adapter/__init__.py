"""Getting audio and a direction off a ReSpeaker USB Mic Array.

Primitives only: open, read, close. Sharing a device between consumers, and
what happens when it fails, belong to ``rrr.devices``. Knows nothing about HTTP,
the camera or ``rrr``, and reads no environment variable: every setting is an
argument, defaulting to :mod:`respeaker_adapter.config`.
"""

from .capture import Capture, probe, rescan
from .config import BLOCK_SIZE, CHANNELS, DEVICE_NAME, DOA_POLL_HZ, SAMPLE_RATE
from .tuning import find_tuning
from .types import BlockStamp, Chunk, DeviceNotFound, Window, dbfs, rms, to_int16

__all__ = [
    "BLOCK_SIZE",
    "CHANNELS",
    "DEVICE_NAME",
    "DOA_POLL_HZ",
    "SAMPLE_RATE",
    "BlockStamp",
    "Capture",
    "Chunk",
    "DeviceNotFound",
    "Window",
    "dbfs",
    "find_tuning",
    "probe",
    "rescan",
    "rms",
    "to_int16",
]

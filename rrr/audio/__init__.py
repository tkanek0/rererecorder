"""Device layer: getting audio and a direction off a ReSpeaker USB Mic Array.

Copied from respeaker-playground and changed in one place that matters: times
come from PortAudio's ``inputBufferAdcTime`` rather than from ``time.monotonic()``
in the callback, and each capture block carries its own stamp. That is what lets
a sample position in a recording be placed against a camera frame; the upstream
version is 64 ms late and has no per-block resolution.

Imports sounddevice, pyusb and numpy, and nothing else in this repository except
:mod:`timeline` conventions. It knows nothing about HTTP or about the camera.
"""

from .capture import AudioTap, DeviceNotFound, devices
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
    "Reading",
    "DoaTap",
    "Window",
    "dbfs",
    "devices",
    "rms",
]

"""What to record, and where to put it.

Every value can be overridden through the environment, so the same code runs on
this laptop and in a container on a Raspberry Pi without editing anything. The
prefix is ``RRR_`` throughout; the audio layer keeps its own ``RRR_AUDIO_``
namespace because it was inherited with one.
"""

from __future__ import annotations

import os

from video import (
    DEFAULT_COLOR,
    DEFAULT_COLOR_FORMAT,
    DEFAULT_DEPTH,
    StreamConfig,
    StreamSpec,
)


def _flag(name: str, default: bool) -> bool:
    """Read a boolean from the environment.

    Args:
        name: Variable to read.
        default: Value to use when it is unset.

    Returns:
        The flag. Anything but ``0``, ``false``, ``no``, ``off`` or empty is true.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


def _spec(name: str, default: StreamSpec | None) -> StreamSpec | None:
    """Read a ``WIDTHxHEIGHT@FPS`` stream description from the environment.

    Args:
        name: Variable to read.
        default: Value to use when it is unset.

    Returns:
        The stream spec, or None if the variable asks for the stream to be off.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    if raw.strip().lower() in ("off", "none", ""):
        return None
    size, _, fps = raw.partition("@")
    width, _, height = size.partition("x")
    return (int(width), int(height), int(fps))


#: Where session directories are created.
#:
#: A relative default, so a checkout works anywhere and a container needs only a
#: mount. At the sizes recorded here - 54 MB/s, 195 GB an hour - this wants to
#: point at a disk with room: set ``RRR_SESSIONS_DIR``, or change it from the
#: recording page, which writes the same variable.
SESSIONS_ROOT = os.environ.get("RRR_SESSIONS_DIR", "var/sessions")

#: Serial of the camera to open; empty means whichever the SDK finds first.
SERIAL = os.environ.get("RRR_SERIAL", "")

#: What to ask the camera for.
#:
#: The defaults are what the sensors themselves produce: depth at the depth
#: processor's maximum, colour at the colour sensor's own size, both at 30 fps,
#: unaligned, with the raw infrared pair. Measured through the RSUSB backend
#: this loses no frames at all - 172 MB/s raw, 54 MB/s after lossless
#: compression.
#:
#: Turn it down on a machine that cannot keep up, rather than editing this:
#: ``RRR_COLOR=640x360@30 RRR_DEPTH=640x360@30 RRR_INFRARED=0``.
DEFAULT_STREAMS = StreamConfig(
    color=_spec("RRR_COLOR", DEFAULT_COLOR),
    depth=_spec("RRR_DEPTH", DEFAULT_DEPTH),
    color_format=os.environ.get("RRR_COLOR_FORMAT", DEFAULT_COLOR_FORMAT),
    infrared=_flag("RRR_INFRARED", True),
    # Off, and not merely defaulted off: alignment resamples the depth onto the
    # colour grid, which destroys its correspondence with the infrared pair and
    # cannot be undone. Every consumer can align on the way out from
    # ``calibration.depth_to_color``; none of them can un-align.
    align_to_color=_flag("RRR_ALIGN", False),
    motion=_flag("RRR_MOTION", True),
)

#: How the archive encodes each stream. See video.archive.DEFAULT_CODECS.
CODECS = {"depth": os.environ.get("RRR_DEPTH_CODEC", "zlib")}

#: Whether each device is recorded at all. Both, unless told otherwise - the
#: point of this repository is the pair.
RECORD_VIDEO = _flag("RRR_VIDEO", True)
RECORD_AUDIO = _flag("RRR_AUDIO", True)
RECORD_DOA = _flag("RRR_DOA", True)

"""What to record, and where to put it.

Every value can be overridden through the environment, so the same code runs on
this laptop and in a container on a Raspberry Pi without editing anything. The
prefix is ``RRR_`` throughout; the audio layer keeps its own ``RRR_AUDIO_``
namespace because it was inherited with one.
"""

from __future__ import annotations

import os

from video import DEFAULT_COLOR, DEFAULT_DEPTH, StreamConfig, StreamSpec


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
SESSIONS_ROOT = os.environ.get("RRR_SESSIONS_DIR", "var/sessions")

#: Serial of the camera to open; empty means whichever the SDK finds first.
SERIAL = os.environ.get("RRR_SERIAL", "")

#: What to ask the camera for.
#:
#: The defaults are the D455's native depth resolution with colour matched to it,
#: so that alignment neither up- nor downsamples. Lossless at 848x480/30 costs
#: about 25 MB/s - measured 24 MB/s over a real 15 second recording - which this
#: machine sustains and a Raspberry Pi will not. Turn it down there rather than
#: here: ``RRR_COLOR=424x240@15 RRR_DEPTH=424x240@15``.
DEFAULT_STREAMS = StreamConfig(
    color=_spec("RRR_COLOR", DEFAULT_COLOR),
    depth=_spec("RRR_DEPTH", DEFAULT_DEPTH),
    align_to_color=_flag("RRR_ALIGN", True),
    motion=_flag("RRR_MOTION", True),
)

#: Whether each device is recorded at all. Both, unless told otherwise - the
#: point of this repository is the pair.
RECORD_VIDEO = _flag("RRR_VIDEO", True)
RECORD_AUDIO = _flag("RRR_AUDIO", True)
RECORD_DOA = _flag("RRR_DOA", True)

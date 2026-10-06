"""What to record, and where to put it, each value overridable from the environment."""

from __future__ import annotations

import os

from realsense_adapter import (
    DEFAULT_COLOR,
    DEFAULT_COLOR_FORMAT,
    DEFAULT_DEPTH,
    DEFAULT_EMITTER,
    StreamConfig,
    StreamSpec,
)
from respeaker_adapter import config as respeaker


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


#: Where session directories are created. See docs/decisions.md 28.
SESSIONS_ROOT = os.environ.get("RRR_SESSIONS_DIR", "data/sessions")

#: Serial of the camera to open; empty means whichever the SDK finds first.
SERIAL = os.environ.get("RRR_SERIAL", "")

#: What to ask the camera for: every stream at the sensors' own sizes.
DEFAULT_STREAMS = StreamConfig(
    color=_spec("RRR_COLOR", DEFAULT_COLOR),
    depth=_spec("RRR_DEPTH", DEFAULT_DEPTH),
    color_format=os.environ.get("RRR_COLOR_FORMAT", DEFAULT_COLOR_FORMAT),
    infrared=_flag("RRR_INFRARED", True),
    emitter=os.environ.get("RRR_EMITTER", DEFAULT_EMITTER),
    # Off: docs/decisions.md 2.
    align_to_color=_flag("RRR_ALIGN", False),
    motion=_flag("RRR_MOTION", True),
)

#: How the archive encodes each stream. See docs/decisions.md 5 and 22.
CODECS = {
    "depth": os.environ.get("RRR_DEPTH_CODEC", "zlib"),
    "color": os.environ.get("RRR_COLOR_CODEC", "png"),
    "infrared": os.environ.get("RRR_INFRARED_CODEC", "png"),
}

#: What "compressed" means per stream, for the CLI and the page. See
#: docs/decisions.md 23.
COMPRESSED_CODECS = {"depth": "zlib", "color": "png", "infrared": "png"}


def codec_for(stream: str, choice: str) -> str:
    """Translate a "compressed"/"raw" choice into an archive codec name.

    Args:
        stream: Which stream this choice is for - ``"color"``, ``"depth"`` or
            ``"infrared"``.
        choice: ``"compressed"`` (the codec in COMPRESSED_CODECS) or
            ``"raw"``.

    Returns:
        The codec name :class:`~rrr.video.ArchiveWriter` understands.

    Raises:
        ValueError: If ``stream`` or ``choice`` is not one of the above.
    """
    if choice == "raw":
        return "raw"
    if choice != "compressed":
        raise ValueError(
            f"unknown codec choice {choice!r}, expected 'compressed' or 'raw'"
        )
    try:
        return COMPRESSED_CODECS[stream]
    except KeyError:
        raise ValueError(f"no such stream {stream!r}") from None

#: Whether each device is recorded at all.
RECORD_VIDEO = _flag("RRR_VIDEO", True)
RECORD_AUDIO = _flag("RRR_AUDIO", True)
RECORD_DOA = _flag("RRR_DOA", True)

#: How the array is opened. respeaker_adapter reads no environment, so its
#: settings are chosen here and passed in.
AUDIO_DEVICE = os.environ.get("RRR_AUDIO_DEVICE", respeaker.DEVICE_NAME)
AUDIO_BLOCK_SIZE = int(os.environ.get("RRR_AUDIO_BLOCK_SIZE", respeaker.BLOCK_SIZE))
AUDIO_WINDOW_S = float(os.environ.get("RRR_AUDIO_WINDOW_S", respeaker.WINDOW_S))
DOA_POLL_HZ = float(os.environ.get("RRR_AUDIO_DOA_POLL_HZ", respeaker.DOA_POLL_HZ))

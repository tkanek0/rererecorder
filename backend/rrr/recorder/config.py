"""What to record, and where to put it, each value overridable from the environment."""

from __future__ import annotations

import os
from collections.abc import Mapping

from realsense_adapter import (
    DEFAULT_COLOR,
    DEFAULT_COLOR_FORMAT,
    DEFAULT_DEPTH,
    DEFAULT_EMITTER,
    StreamConfig,
    StreamSpec,
)
from respeaker_adapter import config as respeaker

from rrr.video.archive import COMPRESSED_CODECS

#: The streams a recording can turn on and off.
STREAM_NAMES = ("color", "depth", "infrared", "motion")


def env_flag(name: str, default: bool) -> bool:
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
    infrared=env_flag("RRR_INFRARED", True),
    emitter=os.environ.get("RRR_EMITTER", DEFAULT_EMITTER),
    # Off: docs/decisions.md 2.
    align_to_color=env_flag("RRR_ALIGN", False),
    motion=env_flag("RRR_MOTION", True),
)

def with_streams(current: StreamConfig, enabled: Mapping[str, bool]) -> StreamConfig:
    """Turn streams on or off; a stream turned on comes back at its configured size.

    Args:
        current: The configuration to change.
        enabled: Any of :data:`STREAM_NAMES` mapped to on or off; others keep
            their value.

    Returns:
        The changed configuration.

    Raises:
        ValueError: For an unknown stream, or a combination ``StreamConfig``
            refuses, e.g. infrared without depth.
    """
    unknown = set(enabled) - set(STREAM_NAMES)
    if unknown:
        raise ValueError(f"unknown stream: {', '.join(sorted(unknown))}")
    changes: dict[str, object] = {}
    for name in ("color", "depth"):
        if name in enabled:
            changes[name] = getattr(DEFAULT_STREAMS, name) if enabled[name] else None
    for name in ("infrared", "motion"):
        if name in enabled:
            changes[name] = bool(enabled[name])
    return current.with_changes(**changes)


def with_codecs(current: Mapping[str, str], choices: Mapping[str, str]) -> dict[str, str]:
    """Choose ``"compressed"`` or ``"raw"`` for streams; others keep their codec.

    Args:
        current: Codec per stream, as :class:`~rrr.video.ArchiveWriter` takes it.
        choices: Stream names mapped to a choice.

    Returns:
        The codec per stream.

    Raises:
        ValueError: For an unknown stream or choice.
    """
    codecs = dict(current)
    for stream, choice in choices.items():
        if stream not in COMPRESSED_CODECS:
            raise ValueError(f"no such stream {stream!r}")
        if choice not in ("compressed", "raw"):
            raise ValueError(
                f"unknown codec choice {choice!r}, expected 'compressed' or 'raw'"
            )
        codecs[stream] = "raw" if choice == "raw" else COMPRESSED_CODECS[stream]
    return codecs


#: How the archive encodes each stream: ``compressed`` unless
#: ``RRR_<STREAM>_CODEC=raw``. See docs/decisions.md 5 and 22.
CODECS = with_codecs(
    COMPRESSED_CODECS,
    {
        stream: os.environ[f"RRR_{stream.upper()}_CODEC"]
        for stream in COMPRESSED_CODECS
        if f"RRR_{stream.upper()}_CODEC" in os.environ
    },
)

#: Whether each device is recorded at all.
RECORD_VIDEO = env_flag("RRR_VIDEO", True)
RECORD_AUDIO = env_flag("RRR_AUDIO", True)
RECORD_DOA = env_flag("RRR_DOA", True)

#: How the array is opened. respeaker_adapter reads no environment, so its
#: settings are chosen here and passed in.
AUDIO_DEVICE = os.environ.get("RRR_AUDIO_DEVICE", respeaker.DEVICE_NAME)
AUDIO_BLOCK_SIZE = int(os.environ.get("RRR_AUDIO_BLOCK_SIZE", respeaker.BLOCK_SIZE))
#: Seconds of audio kept in memory, for the level meter and a slow writer.
AUDIO_WINDOW_S = float(os.environ.get("RRR_AUDIO_WINDOW_S", "10"))
DOA_POLL_HZ = float(os.environ.get("RRR_AUDIO_DOA_POLL_HZ", respeaker.DOA_POLL_HZ))

#: How long a device stays open after its last consumer leaves. Outlasts a page
#: reload, since reopening the camera costs a second plus auto-exposure.
IDLE_SHUTDOWN_S = float(os.environ.get("RRR_IDLE_SHUTDOWN_S", "20"))

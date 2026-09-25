"""What to record, and where to put it.

Every value can be overridden through the environment, so the same code runs on
this laptop and in a container on a Raspberry Pi without editing anything. The
prefix is ``RRR_`` throughout; the audio layer keeps its own ``RRR_AUDIO_``
namespace because it was inherited with one.
"""

from __future__ import annotations

import os

from rrr.video import (
    DEFAULT_COLOR,
    DEFAULT_COLOR_FORMAT,
    DEFAULT_DEPTH,
    DEFAULT_EMITTER,
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
    # See video.config.EMITTER_MODES. "on" is right when depth is the point;
    # "off" or "alternating" is what makes the infrared pair usable for
    # tracking, which is what it is recorded for here.
    emitter=os.environ.get("RRR_EMITTER", DEFAULT_EMITTER),
    # Off, and not merely defaulted off: alignment resamples the depth onto the
    # colour grid, which destroys its correspondence with the infrared pair and
    # cannot be undone. Every consumer can align on the way out from
    # ``calibration.depth_to_color``; none of them can un-align.
    align_to_color=_flag("RRR_ALIGN", False),
    motion=_flag("RRR_MOTION", True),
)

#: How the archive encodes each stream. See video.archive.DEFAULT_CODECS.
#:
#: ``"raw"`` (any of the three) skips compression entirely - for a CPU that
#: cannot compress real camera content fast enough to hold 30 fps even with
#: more encoder threads, trading disk space for reliably clearing budget. See
#: DEFAULT_CODECS's own docstring for the measurement that motivated it.
CODECS = {
    "depth": os.environ.get("RRR_DEPTH_CODEC", "zlib"),
    "color": os.environ.get("RRR_COLOR_CODEC", "png"),
    "infrared": os.environ.get("RRR_INFRARED_CODEC", "png"),
}

#: What each stream's codec is when a caller - the CLI or the page - asks
#: merely for "compressed" rather than naming a specific algorithm.
#:
#: Depth prefers zlib over PNG16 (faster and smaller on this data - see
#: ``video.archive.encode_depth_zlib``); colour and infrared have only PNG as
#: a compressed choice. Kept separate from CODECS itself so that an operator
#: who does want ``png16`` can still reach it through ``RRR_DEPTH_CODEC``
#: without this mapping getting in the way.
COMPRESSED_CODECS = {"depth": "zlib", "color": "png", "infrared": "png"}


def codec_for(stream: str, choice: str) -> str:
    """Translate a "compressed"/"raw" choice into an archive codec name.

    Args:
        stream: Which stream this choice is for - ``"color"``, ``"depth"`` or
            ``"infrared"``.
        choice: ``"compressed"`` (the codec in COMPRESSED_CODECS) or
            ``"raw"`` (no compression at all - see
            ``video.archive.DEFAULT_CODECS`` for why that exists). On this
            Windows setup, only color+raw has been measured to hold 30 fps
            with nothing dropped - see ``docs/windows-native.md``.

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

#: Whether each device is recorded at all. Both, unless told otherwise - the
#: point of this repository is the pair.
RECORD_VIDEO = _flag("RRR_VIDEO", True)
RECORD_AUDIO = _flag("RRR_AUDIO", True)
RECORD_DOA = _flag("RRR_DOA", True)
